import dataclasses
import errno
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

from fileconverter.engine import QueueManager, choose_output, publish
from fileconverter.models import Job, Options, State
from fileconverter.planner import target_bitrate


@pytest.mark.parametrize(
    "source,output",
    [
        ("mp4", "mov"),
        ("mov", "mp4"),
        ("m4v", "mp4"),
        ("mkv", "mp4"),
        ("mp4", "webm"),
        ("mp4", "gif"),
        ("wav", "mp3"),
        ("flac", "mp3"),
        ("mp3", "wav"),
        ("png", "jpg"),
        ("jpg", "webp"),
        ("png", "avif"),
        ("png", "pdf"),
        ("mp4", "m4a"),
    ],
)
def test_conversions(media, convert, source, output):
    job = convert(media[source], format=output, encoder_preset="ultrafast", audio_bitrate=320000)
    assert job.result["size"] > 0


@pytest.mark.parametrize("source_height,target_height", [(1080, 720), (2160, 1080)])
def test_resize(tmp_path, cap, runner, convert, source_height, target_height):
    source = tmp_path / "resolution.mp4"
    runner.run(
        [
            cap.ffmpeg,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=blue:size={source_height * 16 // 9}x{source_height}:rate=24",
            "-t",
            "0.4",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            str(source),
        ]
    )
    job = convert(source, format="mp4", height=target_height, encoder_preset="ultrafast")
    assert job.result["height"] == target_height


def test_remux(media, convert):
    job = convert(media["mp4"], format="mov", remux=True)
    assert job.plan["codec"] == "copy"


def test_av1_software_conversion_and_preview(cap, engine, convert, tmp_path):
    source = Path(__file__).parent / "assets/av1.mkv"
    info = engine.detector.inspect(source)
    decoder = cap.decoding_arguments(info)
    assert decoder[1] in ("libdav1d", "libaom-av1")
    job = convert(source, format="mp4", encoder_preset="ultrafast")
    assert job.result["valid"]
    assert engine.detector.inspect(job.output).video_codec == "h264"
    assert engine.preview(info, threading.Event())
    remux = convert(source, format="mkv", remux=True)
    assert remux.result["valid"]
    assert engine.detector.inspect(remux.output).video_codec == "av1"


def test_av1_without_software_decoder_has_actionable_error(cap, engine, monkeypatch):
    source = Path(__file__).parent / "assets/av1.mkv"
    info = engine.detector.inspect(source)
    monkeypatch.setattr(cap, "decoders", {"av1", "av1_qsv"})
    with pytest.raises(ValueError, match="lacks a software AV1 decoder"):
        engine.planner.plan(info, Options(format="mp4"))


@pytest.mark.parametrize("maximum", [10_000_000, 25_000_000])
def test_video_size_limits(tmp_path, cap, runner, convert, maximum):
    source = tmp_path / "large.mkv"
    runner.run(
        [
            cap.ffmpeg,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1280x720:rate=30,noise=alls=70:allf=t+u",
            "-t",
            "3",
            "-c:v",
            "ffv1",
            "-threads",
            "2",
            str(source),
        ]
    )
    assert source.stat().st_size > maximum
    job = convert(source, target_bytes=maximum, encoder_preset="ultrafast")
    assert job.result["size"] <= maximum
    assert job.plan["passes"] == 2


def test_audio_size_limit(media, convert):
    job = convert(media["wav"], format="mp3", target_bytes=45000, audio_bitrate=320000)
    assert job.result["size"] <= 45000


def test_image_size_limit(media, convert):
    job = convert(media["png"], format="jpg", target_bytes=5000)
    assert job.result["size"] <= 5000


def test_frames(media, convert):
    one = convert(media["mp4"], format="png", operation="frame", frame_time=0.5)
    with Image.open(one.output) as image:
        assert image.width == 320
    many = convert(media["mp4"], format="png", operation="frames", frame_interval=0.5)
    assert many.result["files"] == 4


def test_pdf_roundtrip(media, convert):
    pdf = convert(media["png"], format="pdf")
    pages = convert(pdf.output, format="png")
    assert pages.result["files"] == 1


def test_text_pdf(tmp_path, convert):
    source = tmp_path / "notes.txt"
    source.write_text("Local-first conversion\nUnicode: café 東京", encoding="utf-8")
    pdf = convert(source, format="pdf")
    text = convert(pdf.output, format="txt")
    assert "Local-first" in Path(text.output).read_text()


def test_office(tmp_path, cap, runner, convert):
    if not cap.office:
        pytest.skip("LibreOffice is not installed")
    text = tmp_path / "document.txt"
    text.write_text("Office backend integration", encoding="utf-8")
    docx = convert(text, format="docx")
    pdf = convert(docx.output, format="pdf")
    assert pdf.result["valid"]


@pytest.mark.parametrize("filename", ["東京 café.mp4", "spaces & $; test.mp4", "x" * 150 + ".mp4"])
def test_paths(tmp_path, media, convert, filename):
    import shutil

    source = tmp_path / filename
    shutil.copyfile(media["mp4"], source)
    assert convert(source, format="mov").state == State.COMPLETED


@pytest.mark.parametrize("content", [b"", b"corrupt media input"])
def test_bad_input(engine, tmp_path, content):
    source = tmp_path / "broken.mp4"
    source.write_bytes(content)
    job = Job(str(source), str(tmp_path), Options())
    engine.execute(job, threading.Event())
    assert job.state == State.FAILED
    assert not job.output


def test_cancel(engine, media, tmp_path):
    event = threading.Event()
    job = Job(str(media["mp4"]), str(tmp_path / "out"), Options())

    def cancel_at_processing(job):
        if job.state == State.PROCESSING:
            event.set()

    engine.execute(job, event, cancel_at_processing)
    assert job.state == State.CANCELLED
    assert not list((tmp_path / "out").glob("*.mp4"))
    assert not list((tmp_path / "out").glob(".fileconverter-*"))


def test_hardware_fallback(media, convert, cap):
    if "h264_nvenc" not in cap.encoders:
        pytest.skip("No discoverable hardware encoder to test fallback")
    job = convert(media["mp4"], acceleration="hardware", encoder_preset="ultrafast")
    if not job.plan["hardware"]:
        assert any("fallback" in w for w in job.plan["warnings"])


def test_queue_continues(engine, media, tmp_path):
    manager = QueueManager(engine, 2)
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"bad")
    bad = manager.submit(Job(str(broken), str(tmp_path / "out"), Options()))
    good = manager.submit(Job(str(media["wav"]), str(tmp_path / "out"), Options(format="mp3")))
    deadline = time.monotonic() + 20
    while good.state not in (State.COMPLETED, State.FAILED) and time.monotonic() < deadline:
        time.sleep(0.1)
    manager.close()
    assert bad.state == State.FAILED
    assert good.state == State.COMPLETED


def test_target_calculation():
    assert target_bitrate(10_000_000, 100, 128000) == 624000
    with pytest.raises(ValueError):
        target_bitrate(100, 100, 128000)
    with pytest.raises(ValueError):
        target_bitrate(100, 0)


def test_transitions():
    job = Job("a", "b", Options())
    with pytest.raises(ValueError):
        job.transition(State.PROCESSING)
    job.transition(State.CANCELLED)
    with pytest.raises(ValueError):
        job.transition(State.INSPECTING)


def test_collision_and_no_clobber(tmp_path):
    source = tmp_path / "original.txt"
    source.write_text("source")
    options = Options(format="txt")
    output = choose_output(source, tmp_path, options)
    output.write_text("keep")
    assert choose_output(source, tmp_path, options) != output
    temporary = tmp_path / "temp.txt"
    temporary.write_text("new")
    with pytest.raises(FileExistsError):
        publish(temporary, output)
    assert output.read_text() == "keep"
    with pytest.raises(ValueError):
        choose_output(source, tmp_path, dataclasses.replace(options, suffix=""))


def test_m4v_conversion_on_filesystem_without_hardlinks(media, convert, monkeypatch):
    def unsupported_link(*args, **kwargs):
        error = OSError(errno.EINVAL, "Incorrect function")
        error.winerror = 1
        raise error

    monkeypatch.setattr("fileconverter.engine.os.link", unsupported_link)
    original = media["m4v"].read_bytes()
    job = convert(media["m4v"], format="mp4", encoder_preset="ultrafast")
    assert job.result["valid"]
    assert media["m4v"].read_bytes() == original


def test_unsupported_hardlink_preserves_existing_output(tmp_path, monkeypatch):
    def unsupported_link(*args, **kwargs):
        error = OSError(errno.EINVAL, "Incorrect function")
        error.winerror = 1
        raise error

    monkeypatch.setattr("fileconverter.engine.os.link", unsupported_link)
    source = tmp_path / "validated.mp4"
    source.write_bytes(b"new")
    destination = tmp_path / "existing.mp4"
    destination.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        publish(source, destination)
    assert source.read_bytes() == b"new"
    assert destination.read_bytes() == b"keep"


def test_persistence(engine, media, tmp_path):
    from fileconverter.store import Store

    job = Job(str(media["mp4"]), str(tmp_path), Options())
    job.transition(State.INSPECTING)
    engine.store.save_job(job)
    reloaded = Store(engine.store.root)
    restored = reloaded.restore_queue()
    assert restored[0].state == State.INTERRUPTED
    reloaded.close()


def test_preset_serialization(engine, tmp_path):
    path = tmp_path / "presets.json"
    engine.store.export_configuration(path)
    engine.store.import_configuration(path)
    assert engine.store.get("preset", "Discord 10 MB")["options"]["target_bytes"] == 10_000_000


def test_invalid_plan(engine, media):
    info = engine.detector.inspect(media["mp4"])
    with pytest.raises(ValueError):
        engine.planner.plan(info, Options(format="webm", codec="libx264"))
    with pytest.raises(ValueError):
        engine.planner.plan(info, Options(remux=True, height=720))


def test_capabilities(cap):
    assert "libx264" in cap.codecs("mp4", "video")
    assert "libx264" not in cap.codecs("webm", "video")
    assert "png" in cap.outputs("image")


def test_archive_streaming(tmp_path):
    import zipfile

    from fileconverter.archive import compress_archive

    source = tmp_path / "folder"
    source.mkdir()
    (source / "東京.txt").write_text("local" * 10000)
    (source / "nested").mkdir()
    (source / "nested/data.csv").write_text("a,b\n1,2")
    output = tmp_path / "output.zip"
    result = compress_archive([source], output, threading.Event())
    assert result["valid"] and result["files"] == 2
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        assert "folder/東京.txt" in archive.namelist()
    with pytest.raises(FileExistsError):
        compress_archive([source], output, threading.Event())


def test_disk_full_diagnosis(engine, media, tmp_path, monkeypatch):
    import collections
    import shutil

    monkeypatch.setattr(
        shutil,
        "disk_usage",
        lambda path: collections.namedtuple("Usage", "total used free")(100, 100, 0),
    )
    job = Job(str(media["mp4"]), str(tmp_path / "out"), Options())
    engine.execute(job, threading.Event())
    assert job.state == State.FAILED
    assert "space" in job.error


def test_cancel_running_process(engine, media, tmp_path):
    event = threading.Event()
    job = Job(str(media["mp4"]), str(tmp_path / "out"), Options(encoder_preset="veryslow"))
    timer = None

    def cancel_later(changed):
        nonlocal timer
        if changed.state == State.PROCESSING and timer is None:
            timer = threading.Timer(0.05, event.set)
            timer.start()

    engine.execute(job, event, cancel_later)
    if timer:
        timer.join()
    assert job.state == State.CANCELLED
    assert not engine.runner.processes
    assert not list((tmp_path / "out").glob(".fileconverter-*"))


def test_source_removed_during_job(engine, tmp_path, media):
    import shutil

    source = tmp_path / "source.mp4"
    shutil.copyfile(media["mp4"], source)
    job = Job(str(source), str(tmp_path / "out"), Options())

    def remove_at_processing(changed):
        if changed.state == State.PROCESSING:
            source.unlink(missing_ok=True)

    engine.execute(job, threading.Event(), remove_at_processing)
    assert job.state == State.FAILED
    assert not job.output


def test_oversize_never_published(engine, media, tmp_path):
    job = Job(str(media["wav"]), str(tmp_path / "out"), Options(format="mp3", target_bytes=1000))
    engine.execute(job, threading.Event())
    assert job.state == State.FAILED
    assert not job.output


def test_metadata_strip(engine, tmp_path, convert):
    source = tmp_path / "photo.jpg"
    image = Image.new("RGB", (50, 50), "red")
    exif = Image.Exif()
    exif[315] = "Private author"
    exif[36867] = "2026:10:07 10:00:00"
    image.save(source, exif=exif)
    job = convert(source, format="jpg", metadata="strip")
    with Image.open(job.output) as output:
        assert len(output.getexif()) == 0


def test_subtitles_extract_burn_preserve(tmp_path, cap, runner, convert):
    subtitles = tmp_path / "captions.srt"
    subtitles.write_text("1\n00:00:00,000 --> 00:00:01,500\nCaption text\n", encoding="utf-8")
    source = tmp_path / "captioned.mkv"
    runner.run(
        [
            cap.ffmpeg,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=24",
            "-i",
            str(subtitles),
            "-t",
            "2",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:s",
            "srt",
            str(source),
        ]
    )
    extracted = convert(source, operation="subtitles", subtitle_stream=1)
    assert "Caption text" in Path(extracted.output).read_text()
    preserved = convert(
        source, format="mp4", subtitles="preserve", subtitle_stream=1, encoder_preset="ultrafast"
    )
    streams = runner.json(
        [cap.ffprobe, "-v", "error", "-show_streams", "-of", "json", preserved.output]
    )["streams"]
    assert any(s["codec_type"] == "subtitle" for s in streams)
    burned = convert(
        source, format="mp4", subtitles="burn", subtitle_stream=1, encoder_preset="ultrafast"
    )
    assert burned.result["valid"]


def test_abandoned_owned_temporary_cleanup(engine, tmp_path):
    destination = tmp_path / "outputs"
    destination.mkdir()
    temporary = destination / ".fileconverter-interrupted"
    temporary.mkdir()
    job = Job(str(tmp_path / "source.mp4"), str(destination), Options())
    job.state = State.PROCESSING
    job.plan["temporary_dir"] = str(temporary)
    (temporary / ".owner").write_text(job.id)
    (temporary / "partial.mp4").write_bytes(b"invalid")
    engine.store.save_job(job)
    manager = QueueManager(engine)
    assert not temporary.exists()
    assert manager.jobs[0].state == State.INTERRUPTED
    manager.close()


def test_queue_reorder_persists(engine, tmp_path, media):
    manager = QueueManager(engine)
    first = manager.submit(Job(str(media["mp4"]), str(tmp_path), Options()), start=False)
    second = manager.submit(Job(str(media["mov"]), str(tmp_path), Options()), start=False)
    manager.reorder(second.id, -1)
    assert manager.jobs[0].id == second.id
    assert engine.store.restore_queue()[0].id == second.id
    assert first.state == State.QUEUED
    manager.close()


def test_office_metadata_strip(tmp_path, cap, convert):
    if not cap.office:
        pytest.skip("LibreOffice is unavailable")
    import zipfile

    source = tmp_path / "source.txt"
    source.write_text("Private document text")
    output = convert(source, format="docx", metadata="strip")
    with zipfile.ZipFile(output.output) as archive:
        assert b"creator>" not in archive.read("docProps/core.xml")


def test_maintenance_ipc(tmp_path):
    import os
    import subprocess
    import sys

    process = subprocess.Popen(
        [sys.executable, "-m", "fileconverter.app", "--data-dir", str(tmp_path / "app-data")],
        env=dict(os.environ, QT_QPA_PLATFORM="offscreen"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 30
    reply = None
    while time.monotonic() < deadline and process.poll() is None:
        if not (tmp_path / "app-data/app.lock").exists():
            time.sleep(0.1)
            continue
        reply = subprocess.run(
            [
                sys.executable,
                "-m",
                "fileconverter.app",
                "--maintenance-close",
                "--data-dir",
                str(tmp_path / "app-data"),
            ],
            env=dict(os.environ, QT_QPA_PLATFORM="offscreen"),
            capture_output=True,
            timeout=35,
        )
        if reply.returncode == 0 and (tmp_path / "app-data/state.sqlite3").exists():
            break
        time.sleep(0.5)
    try:
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stderr.decode()
        assert reply is not None and reply.returncode == 0
        assert b"Traceback" not in stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


@pytest.mark.parametrize("output", ["png", "tiff", "bmp", "ico", "gif", "webp", "avif"])
def test_image_outputs(media, convert, output):
    assert convert(media["png"], format=output).result["valid"]


def test_minimal_metadata(tmp_path, convert):
    source = tmp_path / "photo.jpg"
    image = Image.new("RGB", (50, 50), "red")
    exif = Image.Exif()
    exif[315] = "Author to preserve"
    exif[271] = "Private device"
    exif[36867] = "2026:10:07 10:00:00"
    image.save(source, exif=exif)
    output = convert(source, format="jpg", metadata="minimal")
    with Image.open(output.output) as image:
        assert image.getexif()[315] == "Author to preserve"
        assert 271 not in image.getexif()
        assert 36867 not in image.getexif()


def test_stdout_not_dropped(runner):
    import sys

    result = runner.run([sys.executable, "-c", "for i in range(3000): print(i)"])
    assert len(result.splitlines()) == 3000


def test_remote_playlist_rejected(engine, tmp_path):
    source = tmp_path / "playlist.m3u8"
    source.write_text(
        "#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\nhttps://example.com/private.ts\n#EXT-X-ENDLIST\n"
    )
    from fileconverter.process import ProcessFailure

    with pytest.raises(ProcessFailure) as error:
        engine.detector.inspect(source)
    assert "whitelist" in error.value.diagnostic


@pytest.mark.parametrize("output", ["jpg", "png", "webp", "tiff", "avif"])
def test_exif_removed_across_formats(tmp_path, convert, output):
    source = tmp_path / "private.jpg"
    image = Image.new("RGB", (50, 50), "blue")
    exif = Image.Exif()
    exif[315] = "Private author"
    exif[36867] = "2026:10:07 10:00:00"
    image.save(source, exif=exif)
    result = convert(source, format=output, metadata="strip")
    with Image.open(result.output) as image:
        assert 315 not in image.getexif()
        assert 36867 not in image.getexif()


def test_normalized_merge_job(media, engine, tmp_path):
    sources = [str(media["png"]), str(media["jpg"])]
    job = Job(
        sources[0],
        str(tmp_path / "out"),
        Options(format="pdf", operation="merge", filename_template="merged", suffix=""),
        sources=sources,
    )
    engine.execute(job, threading.Event())
    assert job.state == State.COMPLETED, job.error
    assert Path(job.output).name == "merged.pdf"
    assert job.plan["pages"] == 2
    assert engine.detector.inspect(job.output).metadata["pages"] == 2


def test_normalized_archive_job(engine, tmp_path):
    folder = tmp_path / "sources"
    folder.mkdir()
    (folder / "arbitrary.bin").write_bytes(b"any binary file" * 1000)
    job = Job(
        str(folder),
        str(tmp_path / "out"),
        Options(format="zip", operation="archive", filename_template="archive", suffix=""),
        sources=[str(folder)],
    )
    engine.execute(job, threading.Event())
    assert job.state == State.COMPLETED, job.error
    assert job.result["files"] == 1
    assert Path(job.output).name == "archive.zip"


def test_filename_template_safety(tmp_path):
    with pytest.raises(ValueError):
        Options(filename_template="../{stem}").validate()
    with pytest.raises(ValueError):
        Options(filename_template="{stem.__class__}").validate()
    source = tmp_path / "input.mp4"
    source.write_bytes(b"source")
    with pytest.raises(ValueError):
        choose_output(source, tmp_path, Options(filename_template="CON"))


@pytest.mark.parametrize("source", ["mp4", "png", "jpg"])
def test_real_preview(engine, media, source):
    import io

    info = engine.detector.inspect(media[source])
    payload = engine.preview(info, threading.Event())
    with Image.open(io.BytesIO(payload)) as image:
        assert image.width <= 400 and image.height <= 260


def test_history_search_all_rows_and_unicode(engine):
    for index in range(250):
        job = Job(f"normal-{index}.mp4", "out", Options(), state=State.FAILED)
        engine.store.save_job(job)
    target = Job("東京 CAFÉ %_video.mp4", "out", Options(), state=State.FAILED)
    engine.store.save_job(target)
    for index in range(250, 500):
        engine.store.save_job(Job(f"normal-{index}.mp4", "out", Options(), state=State.FAILED))
    assert engine.store.jobs(search="café %_video")[0].id == target.id
    engine.store.clear_history()
    assert engine.store.jobs() == []
