from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from .models import TERMINAL, Job, Options, State


def data_directory() -> Path:
    root = os.environ.get("LOCALAPPDATA") if os.name == "nt" else os.environ.get("XDG_DATA_HOME")
    return Path(root or Path.home() / ".local/share") / "FileConverter"


DEFAULT_SETTINGS = dict(
    default_quality=23,
    default_image_quality=85,
    theme="system",
    workers=2,
    output="",
    notifications=True,
    failure_notifications=True,
    close_behavior="ask",
    acceleration="software",
    metadata="strip",
    collision="rename",
    suffix="-converted",
    completion="nothing",
    watching_paused=False,
)


class Store:
    def __init__(self, root: Path | None = None):
        self.root = Path(root or data_directory())
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.generation = 0
        self.db = sqlite3.connect(self.root / "state.sqlite3", check_same_thread=False, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated REAL);
        CREATE INDEX IF NOT EXISTS jobs_updated ON jobs(updated DESC);
        CREATE TABLE IF NOT EXISTS records(kind TEXT, id TEXT, payload TEXT NOT NULL,
                                          PRIMARY KEY(kind,id));
        CREATE TABLE IF NOT EXISTS processed(watcher TEXT, identity TEXT, job TEXT,
                         output TEXT, status TEXT, PRIMARY KEY(watcher,identity));
        CREATE TABLE IF NOT EXISTS activity(id INTEGER PRIMARY KEY AUTOINCREMENT,
                         watcher TEXT, status TEXT, source TEXT, payload TEXT, timestamp REAL);
        CREATE INDEX IF NOT EXISTS activity_watcher ON activity(watcher,id);
        CREATE TABLE IF NOT EXISTS outputs(path TEXT PRIMARY KEY);
        """)
        self.db.commit()
        self.seed_presets()

    def close(self):
        with self.lock:
            self.db.close()

    def put(self, kind, key, value):
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO records VALUES(?,?,?)", (kind, key, json.dumps(value))
            )

    def get(self, kind, key, default=None):
        with self.lock:
            row = self.db.execute(
                "SELECT payload FROM records WHERE kind=? AND id=?", (kind, key)
            ).fetchone()
        return json.loads(row[0]) if row else default

    def records(self, kind):
        with self.lock:
            rows = self.db.execute(
                "SELECT id,payload FROM records WHERE kind=? ORDER BY id", (kind,)
            ).fetchall()
        values = []
        for key, payload in rows:
            try:
                value = json.loads(payload)
                if not isinstance(value, dict):
                    raise ValueError("Saved record is not an object")
                values.append(value)
            except (ValueError, TypeError):
                if kind == "watcher":
                    self.activity(
                        key,
                        "FAILED",
                        error="Corrupt saved watcher record; preserved in SQLite for recovery",
                    )
        return values

    def delete(self, kind, key):
        with self.lock, self.db:
            self.db.execute("DELETE FROM records WHERE kind=? AND id=?", (kind, key))

    @property
    def settings(self):
        return DEFAULT_SETTINGS | self.get("settings", "app", {})

    def save_settings(self, settings):
        self.put("settings", "app", settings)

    def save_job(self, job: Job):
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO jobs VALUES(?,?,?)",
                (job.id, json.dumps(job.to_dict()), time.time()),
            )
            self.generation += 1

    def jobs(self, limit=1000, offset=0, search="", cancel=None, terminal_only=False):
        connection = sqlite3.connect(
            (self.root / "state.sqlite3").resolve().as_uri() + "?mode=ro", uri=True, timeout=30
        )
        try:
            connection.create_function(
                "casefold", 1, lambda value: str(value or "").casefold(), deterministic=True
            )
            if cancel:
                connection.set_progress_handler(lambda: int(cancel.is_set()), 1000)
            conditions = []
            args: list = []
            if terminal_only:
                conditions.append(
                    "json_extract(payload,'$.state') IN ('COMPLETED','FAILED','CANCELLED','INTERRUPTED')"
                )
            if search:
                term = (
                    "%"
                    + search.casefold()
                    .replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
                    + "%"
                )
                conditions.append(
                    "(casefold(json_extract(payload,'$.source')) LIKE ? ESCAPE char(92) OR casefold(json_extract(payload,'$.output')) LIKE ? ESCAPE char(92) OR casefold(json_extract(payload,'$.state')) LIKE ? ESCAPE char(92))"
                )
                args += [term, term, term]
            query = "SELECT payload FROM jobs"
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " ORDER BY updated DESC LIMIT ? OFFSET ?"
            rows = connection.execute(query, args + [limit, offset]).fetchall()
        finally:
            connection.close()
        return [Job.from_dict(json.loads(r[0])) for r in rows]

    def clear_history(self):
        with self.lock, self.db:
            self.db.execute(
                "DELETE FROM jobs WHERE json_extract(payload,'$.state') IN ('COMPLETED','FAILED','CANCELLED','INTERRUPTED')"
            )
            self.generation += 1

    def restore_queue(self):
        with self.lock:
            rows = self.db.execute("SELECT payload FROM jobs ORDER BY updated").fetchall()
        jobs = []
        for row in rows:
            job = Job.from_dict(json.loads(row[0]))
            if job.state not in TERMINAL and job.state != State.QUEUED:
                job.transition(State.INTERRUPTED)
                job.error = (
                    "Application closed during processing. Retry creates a new validated output."
                )
                self.save_job(job)
            if job.state == State.QUEUED or job.state == State.INTERRUPTED:
                jobs.append(job)
        ranks = {key: index for index, key in enumerate(self.get("queue", "order", []))}
        return sorted(jobs, key=lambda job: (ranks.get(job.id, len(ranks)), job.created))

    def delete_job(self, key):
        with self.lock, self.db:
            self.db.execute("DELETE FROM jobs WHERE id=?", (key,))
            self.generation += 1

    def processed(self, watcher, identity):
        with self.lock:
            return self.db.execute(
                "SELECT status,job,output FROM processed WHERE watcher=? AND identity=?",
                (watcher, identity),
            ).fetchone()

    def record_processed(self, watcher, identity, job, output, status):
        with self.lock, self.db:
            old = self.db.execute(
                "SELECT job,status FROM processed WHERE watcher=? AND identity=?",
                (watcher, identity),
            ).fetchone()
            if (
                status == "QUEUED"
                and old
                and old[0] == job
                and old[1] in ("COMPLETED", "FAILED", "CANCELLED")
            ):
                return
            self.db.execute(
                "INSERT OR REPLACE INTO processed VALUES(?,?,?,?,?)",
                (watcher, identity, job, output, status),
            )
            if output and status == "COMPLETED":
                self.db.execute(
                    "INSERT OR IGNORE INTO outputs VALUES(?)", (str(Path(output).resolve()),)
                )

    def is_output(self, path):
        with self.lock:
            return bool(
                self.db.execute(
                    "SELECT 1 FROM outputs WHERE path=?", (str(Path(path).resolve()),)
                ).fetchone()
            )

    def register_output(self, path):
        with self.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO outputs VALUES(?)", (str(Path(path).resolve()),))

    def activity(self, watcher, status, source="", **details):
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO activity(watcher,status,source,payload,timestamp) VALUES(?,?,?,?,?)",
                (watcher, status, source, json.dumps(details), time.time()),
            )
            self.db.execute("DELETE FROM activity WHERE id < (SELECT MAX(id)-10000 FROM activity)")

    def activities(self, watcher="", limit=1000):
        with self.lock:
            query = "SELECT watcher,status,source,payload,timestamp FROM activity"
            args = []
            if watcher:
                query += " WHERE watcher=?"
                args.append(watcher)
            query += " ORDER BY id DESC LIMIT ?"
            args.append(limit)
            rows = self.db.execute(query, args).fetchall()
        return [
            dict(watcher=r[0], status=r[1], source=r[2], details=json.loads(r[3]), time=r[4])
            for r in rows
        ]

    def seed_presets(self):
        presets: dict[str, dict] = {
            "Discord 10 MB": dict(target_bytes=10_000_000),
            "Discord 25 MB": dict(target_bytes=25_000_000),
            "Email attachment": dict(target_bytes=20_000_000),
            "Web optimized": dict(codec="libx264", quality=24, height=1080),
            "Smallest practical": dict(codec="libx265", quality=32, height=720),
            "Balanced": dict(quality=23),
            "High quality": dict(quality=18),
            "Original-quality/remux": dict(remux=True, metadata="preserve"),
            "H.265 Archive": dict(codec="libx265", quality=20),
            "Opus 192 kbps": dict(format="opus", audio_bitrate=192000),
            "AAC 256 kbps": dict(format="m4a", audio_bitrate=256000),
            "Lossless audio": dict(format="flac", lossless=True),
        }
        for height, name in ((720, "720p"), (1080, "1080p"), (1440, "1440p"), (2160, "4K")):
            presets[name] = dict(height=height)
        for rate in (128, 192, 320):
            presets[f"MP3 {rate} kbps"] = dict(format="mp3", audio_bitrate=rate * 1000)
        for name, settings in presets.items():
            if self.get("preset", name) is None:
                self.put(
                    "preset",
                    name,
                    dict(
                        id=name,
                        name=name,
                        version=1,
                        builtin=True,
                        options=dataclasses.asdict(Options(**settings)),
                    ),
                )

    def export_configuration(self, path):
        payload = dict(schema=1, settings=self.settings, presets=self.records("preset"))
        target = Path(path)
        temp = target.with_suffix(target.suffix + ".tmp")
        temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(temp, target)

    def import_configuration(self, path):
        path = Path(path)
        if path.stat().st_size > 2_000_000:
            raise ValueError("Configuration file is too large")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema") != 1 or not isinstance(payload.get("presets"), list):
            raise ValueError("Unsupported configuration schema")
        validated = []
        for preset in payload["presets"]:
            options = Options(**preset["options"])
            options.validate()
            name = str(preset["name"]).strip()
            if not name or len(name) > 100:
                raise ValueError("Invalid preset name")
            validated.append(
                dict(
                    id=name,
                    name=name,
                    version=1,
                    builtin=bool(self.get("preset", name, {}).get("builtin", False)),
                    options=dataclasses.asdict(options),
                )
            )
        with self.lock, self.db:
            for preset in validated:
                self.db.execute(
                    "INSERT OR REPLACE INTO records VALUES(?,?,?)",
                    ("preset", preset["id"], json.dumps(preset)),
                )
        # Paths and destructive completion settings are deliberately not imported.
        appearance = payload.get("settings", {}).get("theme")
        if appearance in ("system", "light", "dark"):
            self.save_settings(self.settings | {"theme": appearance})


@contextlib.contextmanager
def temporary_store(path):
    store = Store(path)
    try:
        yield store
    finally:
        store.close()
