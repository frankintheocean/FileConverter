"""Real native Windows conversions and installed desktop lifecycle verification."""

import json
import os
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
with tempfile.TemporaryDirectory(prefix="FileConverter café QA ") as directory:
    folder = Path(directory)
    store = Store(folder / "engine-state")
    runner = Runner()
    cap = Capabilities(runner, store)
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
        store.close()
