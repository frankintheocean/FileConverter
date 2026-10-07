import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from fileconverter.capabilities import Capabilities
from fileconverter.engine import Engine, QueueManager
from fileconverter.models import Options, State
from fileconverter.process import Runner
from fileconverter.store import Store
from fileconverter.ui import Bridge, Window, asset
from fileconverter.watchers import WatchManager


def test_window_navigation_import_conversion(tmp_path, media, monkeypatch):
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", lambda: False)
    app = QApplication.instance() or QApplication([])
    store = Store(tmp_path / "data")
    runner = Runner()
    cap = Capabilities(runner, store)
    bridge = Bridge()
    engine = Engine(store, cap, runner)
    queue = QueueManager(engine, 1, bridge.changed.emit, bridge.terminal.emit)
    watchers = WatchManager(store, engine, queue)
    window = Window(store, engine, queue, watchers, bridge)
    window.show()
    app.processEvents()
    assert not window.windowIcon().isNull()
    for index in range(8):
        window.navigation.setCurrentRow(index)
        app.processEvents()
        assert window.title.text() == window.navigation.item(index).text()
    window.navigation.setCurrentRow(0)
    window.import_paths([str(media["mp4"])])
    deadline = time.monotonic() + 20
    while not window.imports and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.05)
    assert len(window.imports) == 1
    window.editor.load(Options(format="mov", encoder_preset="ultrafast"))
    window.destination.setText(str(tmp_path / "outputs"))
    assert window.destination_mode.currentIndex() == 1
    assert window.output_destination(media["mp4"]) == str((tmp_path / "outputs").resolve())
    window.destination_mode.setCurrentIndex(0)
    assert window.output_destination(media["mp4"]) == str(media["mp4"].resolve().parent)
    second_source = tmp_path / "another source folder" / "clip.mp4"
    assert window.output_destination(second_source) == str(second_source.resolve().parent)
    window.destination.clear()
    window.destination_mode.setCurrentIndex(1)
    with pytest.raises(ValueError, match="Choose an output folder"):
        window.output_destination(media["mp4"])
    window.destination.setText(str(tmp_path / "outputs"))
    window.enqueue_all()
    window.start()
    while (
        queue.jobs[0].state not in (State.COMPLETED, State.FAILED) and time.monotonic() < deadline
    ):
        app.processEvents()
        time.sleep(0.05)
    assert queue.jobs[0].state == State.COMPLETED
    store.save_settings(store.settings | {"theme": "dark"})
    window.apply_theme()
    app.processEvents()
    window.refresh_history()
    while window.history_busy and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.05)
    assert window.history_model.rowCount() == 1
    window.force_exit = True
    window.close()
    runner.close()


def test_icon_sizes():
    with Image.open(asset("app.ico")) as icon:
        assert icon.ico.sizes() == {(n, n) for n in (16, 20, 24, 32, 40, 48, 64, 128, 256)}
        for dimensions in icon.ico.sizes():
            image = icon.ico.getimage(dimensions)
            assert image.mode == "RGBA"
            assert image.getextrema()[3][0] == 0
            assert image.getextrema()[3][1] == 255


def test_tool_options_are_executable(cap, media, runner):
    from fileconverter.detect import Detector
    from fileconverter.ui import OptionsEditor

    app = QApplication.instance() or QApplication([])
    editor = OptionsEditor(cap)
    editor.set_info(Detector(cap, runner).inspect(media["mp4"]))
    editor.operation.setCurrentText("frame")
    assert editor.options().format == "png"
    assert editor.options().operation == "frame"
    editor.operation.setCurrentText("subtitles")
    assert editor.options().format == "srt"
    editor.operation.setCurrentText("convert")
    container = "webm" if editor.format.findText("webm") >= 0 else "avi"
    assert editor.format.findText(container) >= 0
    editor.format.setCurrentText(container)
    assert "alac" not in [editor.audio_codec.itemText(i) for i in range(editor.audio_codec.count())]
    editor.format.setCurrentText("gif")
    assert not editor.audio_codec.isVisibleTo(editor)
    editor.close()
    app.processEvents()
