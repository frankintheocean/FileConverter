"""Seed/verify actual user preferences, presets and a queued job across maintenance."""

import argparse
import os

from fileconverter.models import Job, Options
from fileconverter.store import Store

parser = argparse.ArgumentParser()
group = parser.add_mutually_exclusive_group(required=True)
group.add_argument("--seed", action="store_true")
group.add_argument("--verify", action="store_true")
args = parser.parse_args()
if os.name != "nt":
    parser.error("Run this preservation check on a disposable Windows runner")
store = Store()
try:
    if args.seed:
        source = store.root / "preservation-input.txt"
        source.write_text("User input retained across repair and uninstall", encoding="utf-8")
        store.save_settings(store.settings | {"theme": "dark"})
        store.put(
            "preset",
            "Preservation check",
            {"name": "Preservation check", "options": {"format": "txt"}, "builtin": False},
        )
        store.save_job(Job(str(source), str(store.root / "Outputs"), Options(format="txt")))
    else:
        if store.settings["theme"] != "dark":
            raise RuntimeError("User preferences changed during maintenance")
        if not store.get("preset", "Preservation check"):
            raise RuntimeError("User preset was removed during maintenance")
        if not store.jobs(search="preservation-input.txt"):
            raise RuntimeError("User queued job was removed during maintenance")
        if not (store.root / "preservation-input.txt").is_file():
            raise RuntimeError("User source file was removed during maintenance")
        print("User settings, preset, queued job and source preserved.")
finally:
    store.close()
