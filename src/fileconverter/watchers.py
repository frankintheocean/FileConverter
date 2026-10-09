from __future__ import annotations

import dataclasses
import fnmatch
import hashlib
import json
import os
import queue
import shutil
import threading
import time
import uuid
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .engine import identity
from .models import Job, Options, State

INCOMPLETE = {".part", ".tmp", ".crdownload", ".download", ".partial"}


def preset_token(preset):
    return hashlib.sha256(json.dumps([preset["id"], preset["version"]]).encode()).hexdigest()


@dataclasses.dataclass
class Watcher:
    name: str
    path: str
    preset_id: str
    id: str = dataclasses.field(default_factory=lambda: uuid.uuid4().hex)
    enabled: bool = False
    recursive: bool = True
    priority: int = 0
    rules: dict = dataclasses.field(default_factory=lambda: {"all": []})
    output_mode: str = "folder"
    destination: str = ""
    source_action: str = "keep"
    archive: str = ""
    collision: str = "rename"
    startup: str = "scan"
    stability_seconds: float = 10
    reconcile_seconds: float = 300
    notifications: bool = True
    warning_ack: str = ""

    def validate(self):
        if not isinstance(self.enabled, bool) or not isinstance(self.recursive, bool):
            raise ValueError("Enabled and recursive must be booleans")
        if (
            not isinstance(self.name, str)
            or not self.name.strip()
            or not isinstance(self.preset_id, str)
        ):
            raise ValueError("A watcher needs a name and preset identifier")
        if not Path(self.path).is_absolute():
            raise ValueError("Watch location must be an absolute path")
        if self.stability_seconds < 1 or self.reconcile_seconds < 10:
            raise ValueError("Stability must be ≥1 second and reconciliation ≥10 seconds")
        if self.output_mode not in ("same", "folder", "mirror", "preset"):
            raise ValueError("Unknown output rule")
        if self.output_mode in ("folder", "mirror") and not self.destination:
            raise ValueError("Choose an output folder")
        if self.source_action not in ("keep", "archive", "recycle", "delete"):
            raise ValueError("Unknown source action")
        if self.source_action == "archive" and not self.archive:
            raise ValueError("Choose an archive folder")
        if self.startup not in ("scan", "new", "ask"):
            raise ValueError("Unknown startup behavior")
        if self.collision not in ("rename", "skip", "replace", "newer"):
            raise ValueError("Unknown collision rule")
        validate_rules(self.rules)


FIELDS = {
    "category",
    "format",
    "extension",
    "name",
    "size",
    "age",
    "bitrate",
    "audio_bitrate",
    "sample_rate",
    "channels",
    "lossless",
    "duration",
    "width",
    "height",
    "fps",
    "video_codec",
    "audio_codec",
    "metadata",
    "hdr",
}
OPERATORS = {"eq", "ne", "lt", "le", "gt", "ge", "between", "in", "glob"}


def validate_rules(rule, depth=0):
    if depth > 8 or not isinstance(rule, dict):
        raise ValueError("Invalid or excessively nested rules")
    if "all" in rule or "any" in rule:
        key = "all" if "all" in rule else "any"
        if not isinstance(rule[key], list) or len(rule[key]) > 50:
            raise ValueError("Rules must be a list of at most 50 conditions")
        for child in rule[key]:
            validate_rules(child, depth + 1)
        return
    if rule.get("field") not in FIELDS or rule.get("op") not in OPERATORS or "value" not in rule:
        raise ValueError("Invalid rule field, operator, or missing value")
    if rule["op"] in ("between", "in") and not isinstance(rule["value"], list):
        raise ValueError("Between/In rules require a list")
    if rule["op"] == "between" and len(rule["value"]) != 2:
        raise ValueError("Between requires two bounds")


def match_rules(rule, info):
    if "all" in rule:
        return all(match_rules(child, info) for child in rule["all"])
    if "any" in rule:
        return any(match_rules(child, info) for child in rule["any"])
    field, op, expected = rule["field"], rule["op"], rule["value"]
    path = Path(info.path)
    values = dataclasses.asdict(info)
    values.update(
        extension=path.suffix.lower().lstrip("."),
        name=path.name,
        age=max(0, time.time() - path.stat().st_mtime),
        metadata=bool(info.metadata),
        hdr=any(s.get("color_transfer") in ("smpte2084", "arib-std-b67") for s in info.streams),
    )
    actual = values.get(field)
    if field == "format":
        actual = info.format.split(",")
        expected_values = expected if isinstance(expected, list) else [expected]
        if op in ("eq", "in"):
            return bool(set(actual) & set(expected_values))
        if op == "ne":
            return not bool(set(actual) & set(expected_values))
    # Missing bitrate is unknown, never zero masquerading as low quality.
    if field in ("bitrate", "audio_bitrate", "duration", "fps", "sample_rate") and not actual:
        return False
    try:
        if op == "eq":
            return actual == expected
        if op == "ne":
            return actual != expected
        if op == "lt":
            return actual < expected
        if op == "le":
            return actual <= expected
        if op == "gt":
            return actual > expected
        if op == "ge":
            return actual >= expected
        if op == "between":
            return expected[0] <= actual <= expected[1]
        if op == "in":
            return actual in expected
        if op == "glob":
            return fnmatch.fnmatchcase(str(actual).lower(), str(expected).lower())
    except (TypeError, ValueError):
        return False
    return False


def scan_files(root, recursive, stop=None):
    """Stream discovery; do not follow symlinks/junctions or load a tree into memory."""
    root = Path(root).resolve()
    pending = [root]
    while pending:
        if stop and stop.is_set():
            return
        folder = pending.pop()
        with os.scandir(folder) as entries:
            for entry in entries:
                if stop and stop.is_set():
                    return
                path = Path(entry.path)
                if entry.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if recursive and not entry.name.startswith(".fileconverter-"):
                        pending.append(path)
                elif entry.is_file(follow_symlinks=False):
                    yield path


class StabilityDetector:
    def __init__(self):
        self.observed = {}

    def ready(self, path, delay, now=None):
        now = time.monotonic() if now is None else now
        path = Path(path)
        if path.suffix.lower() in INCOMPLETE or path.is_symlink():
            return False
        try:
            stat = path.stat()
            if not path.is_file() or not stat.st_size:
                return False
            key = str(path.resolve())
            current = (stat.st_size, stat.st_mtime_ns)
            old = self.observed.get(key)
            if not old or old[0] != current:
                self.observed[key] = (current, now)
                return False
            if now - old[1] < delay:
                return False
            if os.name == "nt":
                import ctypes
                from ctypes import wintypes

                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.CreateFileW.argtypes = [
                    wintypes.LPCWSTR,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.LPVOID,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.HANDLE,
                ]
                kernel.CreateFileW.restype = wintypes.HANDLE
                handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x80, None)
                if handle == wintypes.HANDLE(-1).value:
                    return False
                kernel.CloseHandle.argtypes = [wintypes.HANDLE]
                kernel.CloseHandle(handle)
            with path.open("rb") as readable:
                readable.read(1)
            return True
        except OSError:
            return False

    def forget(self, path):
        self.observed.pop(str(Path(path).resolve()), None)


class EventAdapter(FileSystemEventHandler):
    def __init__(self, manager, watcher):
        self.manager = manager
        self.watcher = watcher

    def on_created(self, event):
        self.forward(event.src_path, event.is_directory)

    def on_modified(self, event):
        self.forward(event.src_path, event.is_directory)

    def on_moved(self, event):
        self.forward(event.dest_path, event.is_directory)

    def forward(self, path, directory):
        if not directory:
            self.manager.enqueue(self.watcher, path)
        elif (
            self.manager.watchers.get(self.watcher)
            and self.manager.watchers[self.watcher].recursive
        ):
            self.manager.request_scan(self.watcher, explicit=False)


class WatchManager:
    def __init__(self, store, engine, jobs):
        self.store, self.engine, self.jobs = store, engine, jobs
        self.lock = threading.RLock()
        self.watchers = {}
        self.states = {}
        self.observers = {}
        self.stop = threading.Event()
        self.paused = store.settings["watching_paused"]
        self.events: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=4096)
        self.scan_requests = set()
        self.candidates = {}
        self.stability = StabilityDetector()
        self.last_scan = {}
        self.scan_active = set()
        self.next_connect = {}
        self.retry_counts = {}
        self.seen = set()
        for value in store.records("watcher"):
            try:
                watcher = Watcher(**value)
                watcher.validate()
                self.watchers[watcher.id] = watcher
            except Exception as error:
                store.activity(
                    value.get("id", "invalid"),
                    "FAILED",
                    error=f"Invalid watcher configuration: {error}",
                )
        old_terminal = jobs.on_terminal

        def terminal(job):
            if job.watcher_id:
                self.complete(job)
            old_terminal(job)

        jobs.on_terminal = terminal
        if not self.paused:
            active_ids = {key for key, watcher in self.watchers.items() if watcher.enabled}
            jobs.start(lambda job: job.watcher_id in active_ids)
        self.thread = threading.Thread(target=self._run, daemon=True, name="watch-scheduler")
        self.thread.start()

    def save(self, watcher):
        watcher.validate()
        preset = self.store.get("preset", watcher.preset_id)
        if not preset:
            raise ValueError("Selected preset no longer exists")
        with self.lock:
            self._disconnect(watcher.id)
            self.watchers[watcher.id] = watcher
            self.next_connect.pop(watcher.id, None)
            self.last_scan.pop(watcher.id, None)
            self.store.put("watcher", watcher.id, dataclasses.asdict(watcher))

    def delete(self, key):
        with self.lock:
            self._disconnect(key)
            self.watchers.pop(key, None)
            self.store.delete("watcher", key)

    def _disconnect(self, key):
        observer = self.observers.pop(key, None)
        if observer:
            observer.stop()
            observer.join(timeout=3)

    def request_scan(self, key, explicit=True):
        with self.lock:
            if explicit:
                with self.store.lock, self.store.db:
                    self.store.db.execute(
                        "DELETE FROM processed WHERE watcher=? AND status='BASELINE'", (key,)
                    )
            self.scan_requests.add(key)

    def enqueue(self, key, path):
        try:
            self.events.put_nowait((key, str(path)))
        except queue.Full:
            self.request_scan(key, explicit=False)

    def global_pause(self, paused):
        self.paused = paused
        self.store.save_settings(self.store.settings | {"watching_paused": paused})
        if not paused:
            for key in list(self.watchers):
                self.request_scan(key, explicit=False)

    def excluded(self, watcher, path):
        path = Path(path)
        if (
            path.is_symlink()
            or path.suffix.lower() in INCOMPLETE
            or any(part.startswith(".fileconverter-") for part in path.parts)
        ):
            return True
        resolved = path.resolve()
        root = Path(watcher.path).resolve()
        if not resolved.is_relative_to(root):
            return True
        if not watcher.recursive and resolved.parent != root:
            return True
        for folder in (
            watcher.destination if watcher.output_mode in ("folder", "mirror") else "",
            watcher.archive,
        ):
            if folder and resolved.is_relative_to(Path(folder).resolve()):
                return True
        return self.store.is_output(resolved)

    def destination(self, watcher, source):
        if watcher.output_mode == "same":
            return str(Path(source).parent)
        if watcher.output_mode == "preset":
            return self.store.settings["output"] or str(Path(source).parent)
        root = Path(watcher.destination).resolve()
        if watcher.output_mode == "mirror":
            relative = Path(source).resolve().parent.relative_to(Path(watcher.path).resolve())
            root /= relative
        return str(root)

    def preview(self, watcher, cancel=None, limit=1000):
        watcher.validate()
        preset = self.store.get("preset", watcher.preset_id)
        if not preset:
            raise ValueError("Selected preset has been deleted")
        options = Options(**preset["options"])
        scanned = matches = skipped = estimated = 0
        rows: list[dict] = []
        for path in scan_files(watcher.path, watcher.recursive, cancel):
            scanned += 1
            if self.excluded(watcher, path):
                skipped += 1
                continue
            try:
                info = self.engine.detector.inspect(path, cancel)
                if not match_rules(watcher.rules, info):
                    skipped += 1
                    continue
                plan = self.engine.planner.plan(info, options)
                matches += 1
                estimated += plan.get("expected_bytes", 0)
                if len(rows) < limit:
                    rows.append(
                        dict(
                            source=str(path),
                            destination=self.destination(watcher, path),
                            plan=plan,
                            properties=dataclasses.asdict(info),
                        )
                    )
            except Exception as error:
                skipped += 1
                if len(rows) < limit:
                    rows.append(dict(source=str(path), error=str(error)))
        return dict(
            scanned=scanned,
            matches=matches,
            skipped=skipped,
            estimated_bytes=estimated,
            rows=rows,
            details_limited_to=limit,
            dry_run=True,
        )

    def _scan(self, watcher, baseline_before=None):
        try:
            for path in scan_files(watcher.path, watcher.recursive, self.stop):
                if self.stop.is_set():
                    break
                if baseline_before is not None:
                    try:
                        preset = self.store.get("preset", watcher.preset_id)
                        if (
                            preset
                            and path.stat().st_ctime <= baseline_before
                            and not self.excluded(watcher, path)
                        ):
                            token = identity(path) + ":" + preset_token(preset)
                            if not self.store.processed(watcher.id, token):
                                self.store.record_processed(watcher.id, token, "", "", "BASELINE")
                            continue
                    except OSError:
                        continue
                # Backpressure bounds event and candidate memory for huge trees.
                while not self.stop.is_set():
                    try:
                        self.events.put((watcher.id, str(path)), timeout=0.5)
                        break
                    except queue.Full:
                        continue
        except OSError as error:
            self.store.activity(watcher.id, "FAILED", error=str(error))
        finally:
            with self.lock:
                self.scan_active.discard(watcher.id)

    def _run(self):
        while not self.stop.is_set():
            try:
                self._run_loop()
            except Exception as error:
                self.store.activity("scheduler", "FAILED", error=str(error))
                self.stop.wait(5)

    def _run_loop(self):
        watcher: Watcher | None
        while not self.stop.wait(0.25):
            if self.paused:
                continue
            now = time.monotonic()
            with self.lock:
                watchers = sorted(self.watchers.values(), key=lambda w: (-w.priority, w.id))
            for watcher in watchers:
                if not watcher.enabled:
                    self.states[watcher.id] = "Paused"
                    continue
                if not Path(watcher.path).is_dir():
                    self.states[watcher.id] = "Waiting for folder"
                    self._disconnect(watcher.id)
                    continue
                if watcher.id not in self.observers and now >= self.next_connect.get(watcher.id, 0):
                    try:
                        observer = Observer()
                        observer.schedule(
                            EventAdapter(self, watcher.id),
                            watcher.path,
                            recursive=watcher.recursive,
                        )
                        observer.start()
                        self.observers[watcher.id] = observer
                        self.states[watcher.id] = "Watching"
                        self.retry_counts[watcher.id] = 0
                        if watcher.startup == "scan" or watcher.id in self.seen:
                            self.request_scan(watcher.id, explicit=False)
                        elif watcher.startup == "ask":
                            self.states[watcher.id] = "Choose Scan now to process existing files"
                        if watcher.startup in ("new", "ask") and watcher.id not in self.seen:
                            with self.lock:
                                self.scan_active.add(watcher.id)
                            threading.Thread(
                                target=self._scan, args=(watcher, time.time()), daemon=True
                            ).start()
                        self.last_scan[watcher.id] = now
                        self.seen.add(watcher.id)
                    except OSError as error:
                        count = self.retry_counts.get(watcher.id, 0) + 1
                        self.retry_counts[watcher.id] = count
                        self.next_connect[watcher.id] = now + min(300, 2 ** min(count, 8))
                        self.states[watcher.id] = "Waiting for folder"
                        self.store.activity(watcher.id, "FAILED", error=str(error))
                with self.lock:
                    scan = (
                        watcher.id in self.scan_requests
                        or now - self.last_scan.get(watcher.id, now) >= watcher.reconcile_seconds
                    )
                    if scan and watcher.id not in self.scan_active:
                        self.scan_requests.discard(watcher.id)
                        self.last_scan[watcher.id] = now
                        self.scan_active.add(watcher.id)
                        threading.Thread(target=self._scan, args=(watcher,), daemon=True).start()
            for _ in range(128):
                try:
                    key, path = self.events.get_nowait()
                except queue.Empty:
                    break
                watcher = self.watchers.get(key)
                if not watcher or not watcher.enabled or self.excluded(watcher, path):
                    continue
                if len(self.candidates) < 8192:
                    candidate_key = (key, path)
                    if candidate_key not in self.candidates:
                        self.candidates[candidate_key] = (now, 0)
                        self.store.activity(key, "WAITING_FOR_FILE", path)
            for (key, path), (added, _attempt) in list(self.candidates.items())[:128]:
                watcher = self.watchers.get(key)
                if (
                    not watcher
                    or not watcher.enabled
                    or not Path(path).exists()
                    or self.excluded(watcher, path)
                ):
                    self.candidates.pop((key, path), None)
                    self.stability.forget(path)
                    continue
                if now - added > 86400:
                    self.store.activity(
                        key, "FAILED", path, error="File remained locked or unstable for 24 hours"
                    )
                    self.candidates.pop((key, path), None)
                    self.stability.forget(path)
                    continue
                if not self.stability.ready(path, watcher.stability_seconds):
                    continue
                try:
                    self._process(path, watchers)
                except Exception as error:
                    self.store.activity(key, "FAILED", path, error=str(error))
                self.candidates.pop((key, path), None)
                self.stability.forget(path)

    def _process(self, path, watchers):
        fingerprint = identity(path)
        info = None
        for watcher in watchers:
            if not watcher.enabled or self.excluded(watcher, path):
                continue
            preset = self.store.get("preset", watcher.preset_id)
            if not preset:
                self.states[watcher.id] = "Preset deleted — choose a preset"
                self.store.activity(watcher.id, "FAILED", path, error="Preset deleted")
                continue
            # Processed state includes preset version: edits deliberately permit reevaluation.
            token = fingerprint + ":" + preset_token(preset)
            previous = self.store.processed(watcher.id, token)
            if previous:
                status, job_id, _ = previous
                if status == "COMPLETED":
                    return
                if status == "SKIPPED":
                    continue
                if status == "BASELINE":
                    continue
                if status == "QUEUED":
                    old = next((job for job in self.jobs.jobs if job.id == job_id), None)
                    if old and old.state not in (State.INTERRUPTED, State.FAILED, State.CANCELLED):
                        return
                if status == "FAILED":
                    # Failed inputs require explicit retry, avoiding perpetual reconciliation retries.
                    return
            if info is None:
                info = self.engine.detector.inspect(path, self.stop)
            if not match_rules(watcher.rules, info):
                self.store.record_processed(watcher.id, token, "", "", "SKIPPED")
                self.store.activity(
                    watcher.id, "SKIPPED", path, properties=dataclasses.asdict(info)
                )
                continue
            options = Options(**preset["options"])
            options.collision = watcher.collision
            plan = self.engine.planner.plan(info, options)
            warning_key = json.dumps([watcher.rules, preset["options"]], sort_keys=True)
            if (
                any("cannot restore" in warning for warning in plan["warnings"])
                and watcher.warning_ack != warning_key
            ):
                self.states[watcher.id] = "Quality warning — edit watcher to acknowledge"
                self.store.activity(watcher.id, "IGNORED", path, warning=plan["warnings"])
                return
            self.store.activity(
                watcher.id, "MATCHED", path, properties=dataclasses.asdict(info), plan=plan
            )
            job = Job(
                str(path),
                self.destination(watcher, path),
                options,
                watcher_id=watcher.id,
                preset_id=preset["id"],
                source_identity=token,
                watcher_snapshot=dataclasses.asdict(watcher),
            )
            submitted = self.jobs.submit(job)
            if submitted:
                self.store.record_processed(watcher.id, token, job.id, "", "QUEUED")
                self.store.activity(watcher.id, "QUEUED", path, job=job.id, preset=preset)
            return  # Deterministic highest-priority matching watcher wins.

    def complete(self, job):
        self.store.record_processed(
            job.watcher_id, job.source_identity, job.id, job.output, job.state.value
        )
        self.store.activity(
            job.watcher_id, job.state.value, job.source, output=job.output, error=job.error
        )
        watcher = self.watchers.get(job.watcher_id)
        if watcher and job.watcher_snapshot:
            try:
                watcher = Watcher(**job.watcher_snapshot)
                watcher.validate()
            except (TypeError, ValueError):
                self.store.activity(
                    job.watcher_id,
                    "FAILED",
                    job.source,
                    error="Invalid saved source handling; original kept",
                )
                return
        elif watcher:
            watcher = dataclasses.replace(watcher, source_action="keep")
        if job.state != State.COMPLETED or not watcher or watcher.source_action == "keep":
            return
        try:
            # Avoid deleting a replacement source written during conversion.
            original = json.loads(job.source_identity.rsplit(":", 1)[0])
            if json.loads(identity(job.source)) != original:
                raise ValueError("Source identity changed; original was kept")
            if watcher.source_action == "recycle":
                from send2trash import send2trash

                send2trash(job.source)
            elif watcher.source_action == "delete":
                Path(job.source).unlink()
            elif watcher.source_action == "archive":
                folder = Path(watcher.archive)
                folder.mkdir(parents=True, exist_ok=True)
                target = folder / Path(job.source).name
                for index in range(100000):
                    candidate = (
                        target
                        if index == 0
                        else target.with_name(f"{target.stem} ({index}){target.suffix}")
                    )
                    try:
                        with Path(job.source).open("rb") as source, candidate.open("xb") as output:
                            shutil.copyfileobj(source, output, 1024 * 1024)
                        shutil.copystat(job.source, candidate)
                        Path(job.source).unlink()
                        break
                    except FileExistsError:
                        continue
                else:
                    raise ValueError("Archive collision limit reached")
        except Exception as error:
            self.store.activity(
                watcher.id,
                "FAILED",
                job.source,
                error=f"Output valid; source action failed: {error}",
            )

    def close(self):
        self.stop.set()
        for key in list(self.observers):
            self._disconnect(key)
        self.thread.join(timeout=15)
        deadline = time.monotonic() + 10
        while self.scan_active and time.monotonic() < deadline:
            time.sleep(0.05)
