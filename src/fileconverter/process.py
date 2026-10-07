from __future__ import annotations

import collections
import json
import os
import queue
import signal
import subprocess
import threading
import time


class Cancelled(Exception):
    pass


class ProcessFailure(RuntimeError):
    def __init__(self, message, diagnostic=""):
        super().__init__(message)
        self.diagnostic = diagnostic


class Runner:
    """Own each subprocess; bounded diagnostics, cancellable reads, no shell."""

    def __init__(self):
        from .windows import own_process_tree

        own_process_tree()
        self.lock = threading.Lock()
        self.processes = set()

    @staticmethod
    def terminate(process):
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(process.pid, signal.SIGTERM)  # type: ignore[attr-defined]  # POSIX branch
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            if os.name == "nt":
                # taskkill targets only the exact process tree that this runner created.
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)  # type: ignore[attr-defined]  # POSIX branch
                except ProcessLookupError:
                    pass
            process.wait(timeout=10)

    def close(self):
        with self.lock:
            processes = list(self.processes)
        for process in processes:
            self.terminate(process)

    def run(self, args, cancel=None, progress=None, duration=0, timeout=3600, cwd=None):
        if cancel and cancel.is_set():
            raise Cancelled("Cancelled")
        flags = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW | 0x4
            if os.name == "nt"
            else 0
        )
        process = subprocess.Popen(
            [str(a) for a in args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=flags,
            start_new_session=os.name != "nt",
            cwd=cwd,
        )
        if os.name == "nt":
            from .windows import assign_and_resume

            try:
                assign_and_resume(process.pid)
            except Exception:
                process.kill()
                process.wait(timeout=10)
                assert process.stdout is not None and process.stderr is not None
                process.stdout.close()
                process.stderr.close()
                raise
        with self.lock:
            self.processes.add(process)
        lines: queue.Queue[bytes] = queue.Queue(maxsize=256)
        errors: collections.deque[str] = collections.deque(maxlen=150)
        output: collections.deque[str] = collections.deque()
        output_size = 0
        reader_stop = threading.Event()

        stdout, stderr = process.stdout, process.stderr
        assert stdout is not None and stderr is not None

        def stdout_reader():
            for line in iter(lambda: stdout.readline(65536), b""):
                while not reader_stop.is_set():
                    try:
                        lines.put(line, timeout=0.1)
                        break
                    except queue.Full:
                        continue
            stdout.close()

        def stderr_reader():
            for line in iter(lambda: stderr.readline(8192), b""):
                errors.append(line[-4096:].decode("utf-8", "replace"))
            stderr.close()

        threads = [
            threading.Thread(target=stdout_reader, daemon=True),
            threading.Thread(target=stderr_reader, daemon=True),
        ]
        for thread in threads:
            thread.start()
        started = time.monotonic()
        stats = {}
        try:
            while process.poll() is None or threads[0].is_alive() or not lines.empty():
                if cancel and cancel.is_set():
                    raise Cancelled("Cancelled")
                if time.monotonic() - started > timeout:
                    raise ProcessFailure("The backend exceeded its time limit")
                try:
                    line = lines.get(timeout=0.1).decode("utf-8", "replace")
                except queue.Empty:
                    continue
                if progress:
                    key, _, value = line.strip().partition("=")
                    stats[key] = value
                    if key == "progress":
                        try:
                            seconds = float(stats.get("out_time_us", 0)) / 1_000_000
                        except (ValueError, TypeError):
                            seconds = 0
                        progress(min(0.99, seconds / duration) if duration else 0, dict(stats))
                else:
                    output_size += len(line)
                    if output_size > 16_000_000:
                        raise ProcessFailure("Backend metadata exceeded the safe limit")
                    output.append(line)
            code = process.wait()
            threads[1].join(timeout=3)
            if code:
                raise ProcessFailure(
                    f"Backend failed (exit {code}). Inspect diagnostics for details.",
                    "".join(errors),
                )
            return "".join(output)
        finally:
            reader_stop.set()
            self.terminate(process)
            for thread in threads:
                thread.join(timeout=3)
            with self.lock:
                self.processes.discard(process)

    def json(self, args, cancel=None):
        try:
            return json.loads(self.run(args, cancel=cancel, timeout=60))
        except json.JSONDecodeError as error:
            raise ProcessFailure("Backend returned malformed metadata") from error
