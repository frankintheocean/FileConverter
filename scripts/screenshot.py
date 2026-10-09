"""Capture the real Qt application with a real completed conversion, using the offscreen platform."""

import os
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication

from fileconverter.capabilities import Capabilities
from fileconverter.engine import Engine, QueueManager
from fileconverter.models import Job, Options, State
from fileconverter.process import Runner
from fileconverter.store import Store
from fileconverter.ui import Bridge, Window
from fileconverter.watchers import WatchManager

root = Path(__file__).resolve().parents[1]
app = QApplication([])
with tempfile.TemporaryDirectory() as directory:
    folder = Path(directory)
    store = Store(folder / "data")
    runner = Runner()
    cap = Capabilities(runner, store)
    source = folder / "Ocean-study.mp4"
    runner.run(
        [
            cap.ffmpeg,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1280x720:rate=24",
            "-t",
            "2",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            str(source),
        ]
    )
    engine = Engine(store, cap, runner)
    bridge = Bridge()
    jobs = QueueManager(engine, 1, bridge.changed.emit, bridge.terminal.emit)
    watching = WatchManager(store, engine, jobs)
    window = Window(store, engine, jobs, watching, bridge)
    window.show()
    info = engine.detector.inspect(source)
    window.background_done("import-file", info)
    window.destination.setText(str(folder / "Converted"))
    job = jobs.submit(
        Job(str(source), str(folder / "Converted"), Options(format="mov", remux=True))
    )
    deadline = time.monotonic() + 20
    while job.state not in (State.COMPLETED, State.FAILED) and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.05)
    if job.state != State.COMPLETED:
        raise RuntimeError(job.error)
    while jobs.active and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.05)
    app.processEvents()
    window.refresh()
    (root / "docs/screenshots").mkdir(exist_ok=True)
    for theme in ("light", "dark"):
        store.save_settings(store.settings | {"theme": theme})
        window.apply_theme()
        app.processEvents()
        window.grab().save(str(root / f"docs/screenshots/{theme}.png"))
    window.force_exit = True
    window.close()
    runner.close()
