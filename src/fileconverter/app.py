from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from .capabilities import Capabilities
from .engine import Engine, QueueManager
from .process import Runner
from .store import Store, data_directory


def ipc_name(root):
    return "FileConverter-" + hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:24]


def request_running(root, command):
    from PySide6.QtNetwork import QLocalSocket

    socket = QLocalSocket()
    socket.connectToServer(ipc_name(root))
    if not socket.waitForConnected(2000):
        return False
    socket.write(command.encode())
    socket.waitForBytesWritten(2000)
    socket.waitForReadyRead(30000)
    reply = socket.readAll().data()
    socket.disconnectFromServer()
    return reply == b"ok"


def main():
    parser = argparse.ArgumentParser(description="FileConverter local desktop application")
    parser.add_argument("--diagnostics", action="store_true")
    parser.add_argument("--maintenance-close", action="store_true")
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    if (
        os.name != "nt"
        and not args.diagnostics
        and os.environ.get("FILECONVERTER_DEVELOPMENT") != "1"
    ):
        parser.error(
            "FileConverter is a Windows-only desktop app. Non-Windows UI testing requires FILECONVERTER_DEVELOPMENT=1."
        )
    root = args.data_dir or data_directory()
    if args.maintenance_close:
        from PySide6.QtCore import QCoreApplication, QLockFile

        _core = QCoreApplication(sys.argv[:1])
        lock = QLockFile(str(root / "app.lock"))
        lock.setStaleLockTime(0)
        if lock.tryLock(100):
            lock.unlock()
            return 0
        return 0 if request_running(root, "quit") else 1
    store = Store(root)
    runner = Runner()
    if args.diagnostics:
        cap = Capabilities(runner, store)
        payload = json.dumps(cap.diagnostics(), indent=2)
        if sys.stdout:
            print(payload)
        (store.root / "dependency-report.json").write_text(payload, encoding="utf-8")
        store.close()
        return 0
    if os.name == "nt":
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("FileConverter.Desktop.1")
    from PySide6.QtCore import QLockFile
    from PySide6.QtNetwork import QLocalServer
    from PySide6.QtWidgets import QApplication, QMessageBox

    from .ui import Bridge, Window
    from .watchers import WatchManager

    app = QApplication(sys.argv[:1])
    app.setApplicationName("FileConverter")
    app.setApplicationVersion("1.0.2")
    app.setOrganizationName("FileConverter")
    app.setStyle("Fusion")
    lock = QLockFile(str(store.root / "app.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        focused = request_running(store.root, "focus")
        if not focused:
            QMessageBox.information(
                None,
                "FileConverter is running",
                "Open FileConverter from its system tray icon or existing window.",
            )
        store.close()
        return 0 if focused else 1
    maintenance_mutex = None
    if os.name == "nt":
        import ctypes

        create_mutex = ctypes.windll.kernel32.CreateMutexW
        create_mutex.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        create_mutex.restype = ctypes.c_void_p
        maintenance_mutex = create_mutex(None, False, "Local\\FileConverter.Desktop.1")
        if not maintenance_mutex:
            raise OSError("Could not establish application maintenance identity")
    cap = Capabilities(runner, store)
    bridge = Bridge()
    engine = Engine(store, cap, runner)
    jobs = QueueManager(
        engine, store.settings["workers"], bridge.changed.emit, bridge.terminal.emit
    )
    watchers = WatchManager(store, engine, jobs)
    window = Window(store, engine, jobs, watchers, bridge)
    server = QLocalServer()
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    QLocalServer.removeServer(ipc_name(store.root))
    if not server.listen(ipc_name(store.root)):
        window.error("Local maintenance channel could not start: " + server.errorString())

    def connected():
        socket = server.nextPendingConnection()

        def received():
            command = socket.readAll().data()
            if command == b"focus":
                window.show_from_tray()
                socket.write(b"ok")
            elif command == b"quit":
                window.force_exit = True
                # Close workers and durable state synchronously before acknowledging maintenance.
                window.close()
                socket.write(b"ok" if window.closed else b"busy")
            else:
                socket.write(b"invalid")
            socket.flush()
            socket.waitForBytesWritten(1000)
            socket.disconnectFromServer()

        socket.readyRead.connect(received)

    server.newConnection.connect(connected)
    app.styleHints().colorSchemeChanged.connect(
        lambda scheme: window.apply_theme() if store.settings["theme"] == "system" else None
    )
    window.show()
    try:
        return app.exec()
    finally:
        server.close()
        runner.close()
        lock.unlock()
        if maintenance_mutex:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(maintenance_mutex))


if __name__ == "__main__":
    raise SystemExit(main())
