"""Real native Windows conversions and installed desktop lifecycle verification."""

import hashlib
import json
import os
import struct
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from PySide6.QtCore import QCoreApplication

from fileconverter.app import request_running
from fileconverter.capabilities import Capabilities
from fileconverter.engine import Engine
from fileconverter.models import Job, Options, State
from fileconverter.process import Runner
from fileconverter.store import Store

if os.name != "nt":
    raise SystemExit("This verification must run on Windows")

installed = Path(os.environ["LOCALAPPDATA"]) / "Programs/FileConverter"
os.environ["PATH"] = str(installed / "vendor") + os.pathsep + os.environ["PATH"]
os.environ["QT_QPA_PLATFORM"] = "offscreen"
app = QCoreApplication([])
results = []


def verify_executable_icon():
    # Compare the executable's first icon group with every image in the bundled custom ICO.
    import pefile

    icon = (installed / "_internal/assets/app.ico").read_bytes()
    count = struct.unpack_from("<HHH", icon)[2]
    expected = set()
    for index in range(count):
        size, offset = struct.unpack_from("<II", icon, 6 + index * 16 + 8)
        expected.add(hashlib.sha256(icon[offset : offset + size]).digest())
    with pefile.PE(str(installed / "FileConverter.exe")) as executable:
        image_resources = {}
        group = None
        for entry in executable.DIRECTORY_ENTRY_RESOURCE.entries:
            if entry.id not in (3, 14):
                continue
            for item in entry.directory.entries:
                data = item.directory.entries[0].data.struct
                raw = executable.get_data(data.OffsetToData, data.Size)
                if entry.id == 3:
                    image_resources[item.id] = raw
                elif group is None:
                    group = raw
        if group is None:
            raise RuntimeError("Executable has no application icon group")
        group_count = struct.unpack_from("<HHH", group)[2]
        actual = set()
        for index in range(group_count):
            identifier = struct.unpack_from("<H", group, 6 + index * 14 + 12)[0]
            actual.add(hashlib.sha256(image_resources[identifier]).digest())
        if expected != actual or count != 9:
            raise RuntimeError("Executable icon resources differ from the original custom app icon")
    return {"custom_executable_icon_all_nine_sizes": True}


results.append(verify_executable_icon())
with tempfile.TemporaryDirectory(prefix="FileConverter café QA ") as directory:
    folder = Path(directory)
    store = Store(folder / "engine-state")
    runner = Runner()
    cap = Capabilities(runner, store)
    if "libdav1d" not in cap.decoders:
        raise RuntimeError("Bundled software AV1 decoder is missing")
    engine = Engine(store, cap, runner)
    try:
        video = folder / "Video & Unicode café.mp4"
        runner.run(
            [
                cap.ffmpeg,
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=640x360:rate=24",
                "-t",
                "2",
                "-c:v",
                "libx264",
                "-threads",
                "2",
                "-pix_fmt",
                "yuv420p",
                str(video),
            ]
        )
        audio = folder / "audio.wav"
        m4v = folder / "Video café.m4v"
        runner.run(
            [cap.ffmpeg, "-v", "error", "-i", str(video), "-c", "copy", "-f", "ipod", str(m4v)]
        )
        runner.run(
            [
                cap.ffmpeg,
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=2",
                str(audio),
            ]
        )
        cases = [
            (
                Path(__file__).resolve().parent.parent / "tests/assets/av1.mkv",
                Options(format="mp4"),
            ),
            (m4v, Options(format="mp4")),
            (video, Options(format="mov")),
            (video, Options(format="gif", width=320)),
            (video, Options(format="png", operation="frame")),
            (audio, Options(format="mp3", audio_bitrate=320000)),
        ]
        for source, options in cases:
            job = Job(str(source), str(folder / "outputs"), options)
            engine.execute(job, threading.Event())
            if job.state != State.COMPLETED:
                raise RuntimeError(f"Native conversion failed: {job.error}; {job.diagnostic}")
            results.append(
                {"format": options.format, "size": job.result["size"], "validated": True}
            )
        source = folder / "large.mkv"
        runner.run(
            [
                cap.ffmpeg,
                "-v",
                "error",
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
        for maximum in (10_000_000, 25_000_000):
            if source.stat().st_size <= maximum:
                raise RuntimeError("Compression test source is insufficiently large")
            job = Job(
                str(source),
                str(folder / str(maximum)),
                Options(target_bytes=maximum, encoder_preset="ultrafast"),
            )
            engine.execute(job, threading.Event())
            if job.state != State.COMPLETED or job.result["size"] > maximum:
                raise RuntimeError(f"Target-size validation failed: {job.error}")
            results.append({"maximum": maximum, "size": job.result["size"], "validated": True})
        state = folder / "desktop-state"
        executable = installed / "FileConverter.exe"
        process = subprocess.Popen([str(executable), "--data-dir", str(state)])
        try:
            deadline = time.monotonic() + 60
            while not request_running(state, "focus"):
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("Installed desktop startup or focus IPC failed")
                time.sleep(0.25)
            subprocess.run(
                [str(executable), "--maintenance-close", "--data-dir", str(state)],
                check=True,
                timeout=45,
            )
            if process.wait(timeout=30) != 0:
                raise RuntimeError("Installed desktop did not close cleanly")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        results.append({"installed_desktop_start_focus_close": True})
        artifact = Path("artifacts/windows-verification.json")
        artifact.parent.mkdir(exist_ok=True)
        artifact.write_text(
            json.dumps({"dependencies": cap.diagnostics(), "results": results}, indent=2),
            encoding="utf-8",
        )
        print("Native Windows conversions, maximum sizes and installed desktop lifecycle: PASS")
    finally:
        runner.close()
        engine.close()
        store.close()
