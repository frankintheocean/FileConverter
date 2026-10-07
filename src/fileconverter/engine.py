from __future__ import annotations

import dataclasses
import errno
import hashlib
import io
import json
import logging
import logging.handlers
import os
import platform
import shutil
import tempfile
import threading
import time
from pathlib import Path

from PIL import Image, ImageOps

from . import __version__
from .adapters import DocumentAdapter, ImageAdapter
from .capabilities import AUDIO
from .detect import Detector
from .models import TERMINAL, Job, MediaInfo, State
from .planner import CODEC_NAMES, Planner
from .process import Cancelled, ProcessFailure

OUTPUT_LOCK = threading.Lock()


def choose_output(source, destination, options, directory=False):
    folder = Path(destination).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    stem = Path(source).stem.rstrip(" .")[:140] or "converted"
    name = options.filename_template.format(
        stem=stem, suffix=options.suffix, format=options.format
    ).rstrip(" .")[:180]
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if not name or name.upper().split(".")[0] in reserved:
        raise ValueError("Output filename is empty or reserved by Windows")
    extension = "srt" if options.operation == "subtitles" else options.format
    candidate = folder / f"{name}.{extension}"
    if directory:
        candidate = folder / f"{name}-{extension}"
    if candidate.resolve() == Path(source).resolve():
        raise ValueError("Output cannot replace the source file")
    if candidate.exists():
        if options.collision == "skip":
            raise FileExistsError("Output already exists; collision policy is Skip")
        if (
            options.collision == "newer"
            and candidate.stat().st_mtime >= Path(source).stat().st_mtime
        ):
            raise FileExistsError("Existing output is newer than the source")
        if options.collision in ("replace", "newer"):
            return candidate
        for index in range(1, 100000):
            unique = candidate.with_name(f"{candidate.stem} ({index}){candidate.suffix}")
            if not unique.exists():
                return unique
        raise FileExistsError("No available output filename")
    return candidate


def publish(temp, final, replace=False):
    """No-clobber finalization; temp and final are always on the same filesystem."""
    if replace:
        if Path(temp).is_dir():
            raise ValueError("Replacing a folder output is unsafe; use automatic rename")
        os.replace(temp, final)
    elif Path(temp).is_dir():
        # Windows rename rejects existing destinations. POSIX rename replaces empty dirs,
        # so reserve with mkdir and move individual validated files into it.
        Path(final).mkdir()
        try:
            for child in Path(temp).iterdir():
                child.replace(Path(final) / child.name)
            Path(temp).rmdir()
        except Exception:
            shutil.rmtree(final)
            raise
    else:
        try:
            os.link(temp, final)
            Path(temp).unlink()
        except OSError as error:
            if error.errno not in (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP):
                raise
            # Filesystems without hardlinks: create exclusively, preserving no-overwrite.
            try:
                with Path(temp).open("rb") as src, Path(final).open("xb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
                    dst.flush()
                    os.fsync(dst.fileno())
            except Exception:
                # Never remove an existing destination we did not create.
                if "dst" in locals():
                    Path(final).unlink(missing_ok=True)
                raise
            Path(temp).unlink()


def identity(path):
    path = Path(path).resolve(strict=True)
    stat = path.stat()
    return json.dumps([str(path), stat.st_size, stat.st_mtime_ns, stat.st_ino], ensure_ascii=False)


class Validator:
    def __init__(self, detector, runner, cap):
        self.detector = detector
        self.runner = runner
        self.cap = cap

    def validate(self, path, source, options, plan, cancel):
        path = Path(path)
        if path.is_dir():
            count = total_size = 0
            for file in path.iterdir():
                if cancel.is_set():
                    raise Cancelled("Validation cancelled")
                with Image.open(file) as image:
                    image.load()
                count += 1
                total_size += file.stat().st_size
            if not count:
                raise ValueError("Backend produced no output files")
            if source.format == "pdf" and count != source.metadata["pages"]:
                raise ValueError("Not all PDF pages were exported")
            return dict(valid=True, size=total_size, files=count)
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError("Backend produced an empty output")
        if options.operation == "archive":
            import zipfile

            count = 0
            with zipfile.ZipFile(path) as archive:
                for entry in archive.infolist():
                    with archive.open(entry) as reader:
                        while reader.read(1024 * 1024):
                            if cancel.is_set():
                                raise Cancelled("Archive validation cancelled")
                    count += 1
            if not count:
                raise ValueError("Archive is empty")
            return dict(valid=True, size=path.stat().st_size, files=count, format="zip")
        if plan["target_bytes"] and path.stat().st_size > plan["target_bytes"]:
            raise ValueError("Output exceeds the requested maximum size")
        if options.operation == "subtitles":
            text = path.read_text(encoding="utf-8-sig")
            if "-->" not in text:
                raise ValueError("No readable subtitle cues were produced")
            return dict(valid=True, size=path.stat().st_size, format="srt")
        info = self.detector.inspect(path, cancel)
        if plan.get("pages") and info.metadata.get("pages") != plan["pages"]:
            raise ValueError("Merged PDF page count is incorrect")
        fmt = options.format
        if options.operation not in ("frame", "frames"):
            aliases = {
                "jpg": "jpeg",
                "m4a": "mov",
                "mp4": "mov",
                "m4v": "mov",
                "3gp": "mov",
                "mkv": "matroska",
                "ts": "mpegts",
                "mts": "mpegts",
                "m2ts": "mpegts",
                "wma": "asf",
                "oga": "ogg",
                "opus": "ogg",
                "ogv": "ogg",
                "aac": "aac",
                "aif": "aiff",
                "mpg": "mpeg",
                "vob": "mpeg",
            }
            if aliases.get(fmt, fmt) not in info.format.split(","):
                raise ValueError(
                    "Output container or image type does not match the requested format"
                )
        if fmt in ("txt", "html", "csv"):
            return dict(
                valid=True, size=info.size, format=fmt, metadata_policy="Text content retained"
            )
        if info.category in ("video", "audio"):
            if options.operation not in ("frame", "frames"):
                if info.duration <= 0 or abs(info.duration - plan["duration"]) > max(
                    0.25, plan["duration"] * 0.03
                ):
                    raise ValueError(
                        "Output duration differs unexpectedly from the planned duration"
                    )
                expected = plan["codec"]
                if expected != "copy":
                    codec = info.audio_codec if fmt in AUDIO else info.video_codec
                    expected = CODEC_NAMES.get(
                        expected, expected.split("_")[0] if plan["hardware"] else expected
                    )
                    if codec != expected:
                        raise ValueError(f"Unexpected output codec: {codec}, expected {expected}")
                if fmt not in AUDIO and fmt != "gif":
                    if (info.width, info.height) != (plan["width"], plan["height"]):
                        raise ValueError("Output resolution does not match the execution plan")
                if fmt not in AUDIO and plan["audio_codec"] and not info.audio_codec:
                    raise ValueError("Required output audio stream is missing")
                if options.remove_audio and info.audio_codec and fmt not in AUDIO:
                    raise ValueError("Output still contains audio")
                if options.channels and info.channels != options.channels:
                    raise ValueError("Requested channel configuration was not applied")
                if options.sample_rate and info.sample_rate != options.sample_rate:
                    raise ValueError("Requested sample rate was not applied")
                if options.fps and fmt not in AUDIO and abs(info.fps - options.fps) > 0.1:
                    raise ValueError("Requested frame rate was not applied")
            # Fully decode output, catching truncation that a header probe can miss.
            self.runner.run(
                [
                    self.cap.ffmpeg,
                    "-v",
                    "error",
                    "-xerror",
                    "-nostdin",
                    "-protocol_whitelist",
                    "file,pipe,crypto,data",
                    "-i",
                    str(path),
                    "-map",
                    "0:v?",
                    "-map",
                    "0:a?",
                    "-f",
                    "null",
                    os.devnull,
                ],
                cancel=cancel,
            )
        elif info.category == "image":
            with Image.open(path) as image:
                image.load()
                structural = {
                    256,
                    257,
                    258,
                    259,
                    262,
                    273,
                    277,
                    278,
                    279,
                    282,
                    283,
                    284,
                    296,
                    338,
                    339,
                }
                exif_fields = set(image.getexif()) - (
                    structural if info.format == "tiff" else set()
                )
                if options.metadata == "strip" and exif_fields:
                    raise ValueError("Output still contains EXIF metadata")
                if options.metadata == "minimal" and exif_fields - {270, 315, 33432}:
                    raise ValueError("Unrequested device/location/timestamp EXIF remains")
            if (
                options.operation != "frame"
                and fmt != "ico"
                and (info.width, info.height) != (plan["width"], plan["height"])
            ):
                raise ValueError("Image dimensions do not match the plan")
        if options.metadata != "preserve":
            sensitive = {
                "creation_time",
                "author",
                "comment",
                "artist",
                "title",
                "location",
                "copyright",
                "gps",
                "created",
                "modified",
                "creation-date",
                "date",
                "description",
            }
            if options.metadata == "minimal":
                sensitive -= {"author", "comment", "artist", "title", "copyright", "description"}
            if sensitive.intersection(k.lower() for k in info.metadata if info.metadata[k]):
                # User-requested tags are exempt; metadata semantics are validated, not promised blindly.
                remaining = sensitive.intersection(
                    k.lower() for k in info.metadata if info.metadata[k]
                ) - set(options.tags)
                if remaining:
                    raise ValueError(
                        "Sensitive metadata remains in output: " + ", ".join(sorted(remaining))
                    )
        return dict(
            valid=True,
            size=info.size,
            format=info.format,
            duration=info.duration,
            width=info.width,
            height=info.height,
            codec=info.video_codec or info.audio_codec,
            metadata_policy="Validated removable metadata policy",
        )


class Engine:
    def __init__(self, store, cap, runner):
        self.store = store
        self.cap = cap
        self.runner = runner
        self.detector = Detector(cap, runner)
        self.planner = Planner(cap)
        self.validator = Validator(self.detector, runner, cap)
        self.image = ImageAdapter()
        self.documents = DocumentAdapter(cap, runner)
        self.log = logging.getLogger(
            "fileconverter." + hashlib.sha256(str(store.root).encode()).hexdigest()[:16]
        )
        if not self.log.handlers:
            handler = logging.handlers.RotatingFileHandler(
                store.root / "diagnostics.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
            )
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            self.log.addHandler(handler)
            self.log.setLevel(logging.INFO)
        self.log.info(
            json.dumps(
                dict(
                    event="startup",
                    version=__version__,
                    os=platform.platform(),
                    dependencies=cap.versions,
                )
            )
        )

    def close(self):
        for handler in list(self.log.handlers):
            handler.close()
            self.log.removeHandler(handler)

    def preview(self, info, cancel):
        if cancel.is_set():
            raise Cancelled("Preview cancelled")
        if info.category == "image":
            with Image.open(info.path) as original:
                original.draft("RGB", (400, 260))
                image = ImageOps.exif_transpose(original)
                image.thumbnail((400, 260))
                image.info.clear()
                buffer = io.BytesIO()
                image.save(buffer, "PNG")
                data = buffer.getvalue()
        elif info.format == "pdf":
            import pymupdf

            from .adapters import PDF_LOCK

            with PDF_LOCK, pymupdf.open(info.path) as document:
                page = document[0]
                scale = min(400 / page.rect.width, 260 / page.rect.height)
                data = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).tobytes(
                    "png"
                )
        elif info.category == "video" or any(
            s.get("disposition", {}).get("attached_pic") for s in info.streams
        ):
            with tempfile.TemporaryDirectory(prefix="fileconverter-preview-") as directory:
                output = Path(directory) / "thumbnail.png"
                self.runner.run(
                    [
                        self.cap.ffmpeg,
                        "-v",
                        "error",
                        "-nostdin",
                        "-y",
                        "-protocol_whitelist",
                        "file,pipe,crypto,data",
                        "-ss",
                        str(min(info.duration * 0.1, 10) if info.category == "video" else 0),
                        "-i",
                        info.path,
                        "-map",
                        "0:v:0",
                        "-frames:v",
                        "1",
                        "-vf",
                        "scale=400:260:force_original_aspect_ratio=decrease",
                        "-an",
                        "-threads",
                        "2",
                        str(output),
                    ],
                    cancel=cancel,
                    timeout=30,
                )
                data = output.read_bytes()
        else:
            return b""
        if cancel.is_set():
            raise Cancelled("Preview cancelled")
        return data

    def cleanup_interrupted(self, jobs):
        for job in jobs:
            temporary = job.plan.get("temporary_dir")
            if not temporary:
                continue
            path = Path(temporary)
            try:
                if (
                    not path.exists()
                    or path.is_symlink()
                    or not path.name.startswith(".fileconverter-")
                ):
                    continue
                if path.resolve().parent != Path(job.destination).resolve():
                    continue
                marker = path / ".owner"
                if marker.is_file() and not marker.is_symlink() and marker.read_text() == job.id:
                    shutil.rmtree(path)
                elif not any(path.iterdir()):
                    path.rmdir()
            except OSError as error:
                job.error += " Temporary-file cleanup needs attention: " + str(error)
                self.store.save_job(job)

    def execute(self, job, cancel, changed=lambda job: None):
        started = time.monotonic()
        job.started = time.time()

        def update(state=None):
            if state:
                job.transition(state)
            self.store.save_job(job)
            changed(job)
            if state:
                self.log.info(
                    json.dumps(
                        dict(
                            job=job.id,
                            stage=state.value,
                            backend=job.plan.get("backend"),
                            validation=job.result.get("valid"),
                        )
                    )
                )

        last_save = [0.0]

        def progress(fraction, stats):
            job.progress = min(99, max(job.progress, fraction * 100))
            job.result["speed"] = stats.get("speed", "")
            elapsed = time.monotonic() - started
            job.result["elapsed"] = elapsed
            job.result["remaining"] = (
                elapsed * (1 - fraction) / fraction if fraction > 0.01 else None
            )
            if time.monotonic() - last_save[0] > 0.3:
                last_save[0] = time.monotonic()
                changed(job)

        try:
            update(State.INSPECTING)
            source_id = identity(job.source)
            inputs = job.sources or [job.source]
            if job.options.operation == "archive":
                from .watchers import scan_files

                total_size = file_count = 0
                for source in inputs:
                    files = (
                        scan_files(source, True, cancel)
                        if Path(source).is_dir()
                        else [Path(source)]
                    )
                    for file in files:
                        if cancel.is_set():
                            raise Cancelled("Archive inspection cancelled")
                        total_size += file.stat().st_size
                        file_count += 1
                info = MediaInfo(
                    job.source,
                    "archive",
                    "directory" if Path(job.source).is_dir() else "file",
                    total_size,
                    metadata={"files": file_count},
                )
            else:
                info = self.detector.inspect(job.source, cancel)
            source_ids = {source: identity(source) for source in inputs if Path(source).is_file()}
            job.info = dataclasses.asdict(info)
            update(State.PLANNING)
            plan = self.planner.plan(info, job.options)
            if job.options.operation == "merge":
                plan["pages"] = 0
                info.size = 0
                for source in inputs:
                    part = self.detector.inspect(source, cancel)
                    if part.category != "image" and part.format != "pdf":
                        raise ValueError("Merge supports images and PDFs only")
                    plan["pages"] += part.metadata.get("pages", 1)
                    info.size += part.size
            job.plan = plan
            update(State.READY)
            directory = job.options.operation == "frames" or (
                info.format == "pdf" and job.options.format in ("png", "jpg")
            )
            with OUTPUT_LOCK:
                final = choose_output(job.source, job.destination, job.options, directory)
            minimum_space = max(plan.get("expected_bytes", 0) * 2, min(info.size * 2, 100_000_000))
            if shutil.disk_usage(final.parent).free < minimum_space:
                raise OSError(
                    errno.ENOSPC, "Not enough disk space for temporary output and validation"
                )
            update(State.PROCESSING)
            with tempfile.TemporaryDirectory(
                prefix=".fileconverter-", dir=final.parent
            ) as temporary:
                work = Path(temporary)
                job.plan["temporary_dir"] = str(work)
                self.store.save_job(job)
                (work / ".owner").write_text(job.id, encoding="ascii")
                output = work / (
                    "frames"
                    if directory
                    else "output."
                    + ("srt" if job.options.operation == "subtitles" else job.options.format)
                )
                if job.options.operation == "merge":
                    self.documents.merge(inputs, output, cancel)
                elif job.options.operation == "archive":
                    from .archive import compress_archive

                    compress_archive(
                        inputs,
                        output,
                        cancel,
                        lambda count: progress(
                            min(0.94, count / max(1, info.metadata["files"]) * 0.94), {}
                        ),
                    )
                elif info.category in ("video", "audio"):
                    if job.options.operation == "frames":
                        output.mkdir()
                        pattern = output / "frame-%08d.png"
                    else:
                        pattern = output
                    if job.options.subtitles == "burn":
                        self.runner.run(
                            [
                                self.cap.ffmpeg,
                                "-v",
                                "error",
                                "-nostdin",
                                "-y",
                                "-i",
                                info.path,
                                "-map",
                                f"0:{job.options.subtitle_stream}",
                                "-c:s",
                                "srt",
                                str(work / "subtitles.srt"),
                            ],
                            cancel=cancel,
                        )
                    for attempt in range(4):
                        job.stage = (
                            f"Encoding · attempt {attempt + 1}"
                            if plan["target_bytes"]
                            else "Encoding"
                        )
                        changed(job)
                        try:
                            for pass_number in [1, 2] if plan["passes"] == 2 else [0]:
                                args = self.planner.arguments(
                                    info, job.options, plan, pattern, work, pass_number
                                )
                                job.plan["arguments"] = args
                                job.stage = f"Encoding · pass {pass_number or 1}/{plan['passes']}"
                                changed(job)
                                offset = (pass_number - 1) / 2 if pass_number else 0
                                weight = 0.5 if pass_number else 1
                                self.runner.run(
                                    args,
                                    cancel=cancel,
                                    progress=lambda f, s, o=offset, w=weight: progress(
                                        (o + f * w) * 0.94, s
                                    ),
                                    duration=plan["duration"],
                                    cwd=work,
                                )
                        except ProcessFailure as error:
                            if not plan["hardware"]:
                                raise
                            plan.update(codec=plan["software_codec"], hardware=False)
                            plan["warnings"].append(
                                "Hardware encoding failed; software fallback used. "
                                + error.diagnostic[-1500:]
                            )
                            output.unlink(missing_ok=True)
                            args = self.planner.arguments(info, job.options, plan, pattern, work)
                            job.plan["arguments"] = args
                            self.runner.run(
                                args,
                                cancel=cancel,
                                progress=lambda f, s: progress(f * 0.94, s),
                                duration=plan["duration"],
                                cwd=work,
                            )
                        if (
                            not plan["target_bytes"]
                            or output.is_dir()
                            or output.stat().st_size <= plan["target_bytes"]
                        ):
                            break
                        ratio = plan["target_bytes"] / output.stat().st_size * 0.92
                        next_rate = int(plan["bitrate"] * ratio)
                        if plan["codec"] == "gif":
                            factor = max(0.1, ratio**0.5)
                            plan["width"] = max(1, int(plan["width"] * factor))
                            plan["height"] = max(1, int(plan["height"] * factor))
                        if next_rate < 16000 or attempt == 3:
                            raise ValueError(
                                "Target-size maximum is infeasible at these settings. No oversized output was published."
                            )
                        plan["bitrate"] = next_rate
                        output.unlink()
                elif info.category == "image" and job.options.format != "pdf":
                    self.image.convert(info, job.options, plan, output, cancel, progress)
                else:
                    self.documents.convert(info, job.options, plan, output, work, cancel, progress)
                if cancel.is_set():
                    raise Cancelled("Cancelled")
                if job.options.operation != "archive" and identity(job.source) != source_id:
                    raise ValueError("The source changed during conversion; output was discarded")
                if any(
                    identity(source) != fingerprint for source, fingerprint in source_ids.items()
                ):
                    raise ValueError("A source changed during conversion; output was discarded")
                update(State.VALIDATING)
                validation = self.validator.validate(output, info, job.options, plan, cancel)
                if cancel.is_set():
                    raise Cancelled("Cancelled")
                with OUTPUT_LOCK:
                    final = choose_output(job.source, job.destination, job.options, directory)
                    publish(output, final, replace=job.options.collision in ("replace", "newer"))
                job.output = str(final)
                self.store.register_output(final)
                job.result.update(
                    validation,
                    original_size=info.size,
                    seconds=time.monotonic() - started,
                    saved_percent=(1 - validation["size"] / info.size) * 100,
                    output=str(final),
                )
            job.progress = 100
            job.plan.pop("temporary_dir", None)
            update(State.COMPLETED)
        except Cancelled:
            job.error = "Conversion cancelled. No partial output was published."
            update(State.CANCELLED)
        except Exception as error:
            job.error = str(error) or type(error).__name__
            job.diagnostic = getattr(error, "diagnostic", "")[-16000:]
            self.log.error(json.dumps(dict(job=job.id, error=job.error, diagnostic=job.diagnostic)))
            update(State.FAILED)
        return job


class QueueManager:
    def __init__(self, engine, workers=2, on_change=lambda job: None, on_terminal=lambda job: None):
        self.engine = engine
        self.store = engine.store
        self.on_change = on_change
        self.on_terminal = on_terminal
        self.lock = threading.RLock()
        self.jobs = self.store.restore_queue()
        engine.cleanup_interrupted(self.jobs)
        self.pending = []
        self.active = {}
        self.max_workers = max(1, min(4, workers))
        self.paused = False
        self.stopping = False
        self.wake = threading.Event()
        self.thread = threading.Thread(target=self._dispatch, daemon=True, name="queue-dispatch")
        self.thread.start()

    def submit(self, job, start=True):
        with self.lock:
            if self.stopping:
                raise ValueError("Application is shutting down")
            if len(self.jobs) >= 10000:
                self.jobs = [j for j in self.jobs if j.state not in TERMINAL]
            if len(self.jobs) >= 10000:
                raise ValueError("Queue limit reached; clear completed jobs before importing more")
            if job.watcher_id and any(
                j.source == job.source and j.state not in TERMINAL for j in self.jobs
            ):
                return None
            self.jobs.append(job)
            self.store.save_job(job)
            self.store.put("queue", "order", [j.id for j in self.jobs])
            if start:
                self.pending.append(job)
        self.on_change(job)
        self.wake.set()
        return job

    def start(self, predicate=None):
        with self.lock:
            for job in self.jobs:
                if (
                    job.state == State.QUEUED
                    and job not in self.pending
                    and (predicate is None or predicate(job))
                ):
                    self.pending.append(job)
            self.paused = False
        self.wake.set()

    def pause(self, paused=True):
        # Pause dispatch, never pretend an in-flight codec can safely checkpoint.
        self.paused = paused
        self.wake.set()

    def cancel(self, job_id):
        with self.lock:
            if job_id in self.active:
                self.active[job_id][0].set()
                return
            for job in self.jobs:
                if job.id == job_id and job.state == State.QUEUED:
                    if job in self.pending:
                        self.pending.remove(job)
                    job.transition(State.CANCELLED)
                    self.store.save_job(job)
                    self.on_change(job)
                    self.on_terminal(job)

    def retry(self, job):
        return self.submit(
            Job(
                job.source,
                job.destination,
                dataclasses.replace(job.options),
                watcher_id=job.watcher_id,
                source_identity=job.source_identity,
                sources=list(job.sources),
                watcher_snapshot=dict(job.watcher_snapshot),
                preset_id=job.preset_id,
            )
        )

    def reorder(self, job_id, delta):
        with self.lock:
            items = self.jobs
            index = next(i for i, j in enumerate(items) if j.id == job_id)
            target = max(0, min(len(items) - 1, index + delta))
            items.insert(target, items.pop(index))
            rank = {job.id: i for i, job in enumerate(items)}
            self.pending.sort(key=lambda job: rank[job.id])
            self.store.put("queue", "order", [j.id for j in self.jobs])

    def remove(self, job_id):
        with self.lock:
            if job_id in self.active:
                raise ValueError("Cancel the running job before removing it")
            self.jobs = [j for j in self.jobs if j.id != job_id]
            self.pending = [j for j in self.pending if j.id != job_id]
            self.store.delete_job(job_id)

    def _dispatch(self):
        while not self.stopping:
            self.wake.wait(0.5)
            self.wake.clear()
            with self.lock:
                while (
                    not self.paused
                    and self.pending
                    and len(self.active) < self.max_workers
                    and not self.stopping
                ):
                    job = self.pending.pop(0)
                    cancel = threading.Event()
                    thread = threading.Thread(target=self._execute, args=(job, cancel), daemon=True)
                    self.active[job.id] = (cancel, thread)
                    thread.start()

    def _execute(self, job, cancel):
        try:
            self.engine.execute(job, cancel, self.on_change)
            self.on_terminal(job)
        finally:
            with self.lock:
                self.active.pop(job.id, None)
            self.on_change(job)
            self.wake.set()

    def close(self):
        self.stopping = True
        self.wake.set()
        self.thread.join(timeout=3)
        with self.lock:
            active = list(self.active.values())
        for cancel, _ in active:
            cancel.set()
        self.engine.runner.close()
        for _, thread in active:
            thread.join(timeout=15)
        if any(thread.is_alive() for _, thread in active):
            raise RuntimeError("A worker is still stopping; wait before closing persistence")
        self.engine.close()
