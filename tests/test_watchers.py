import dataclasses
import json
import shutil
import time

import pytest

from fileconverter.engine import QueueManager
from fileconverter.models import State
from fileconverter.watchers import (
    StabilityDetector,
    Watcher,
    WatchManager,
    match_rules,
    scan_files,
    validate_rules,
)


@pytest.fixture
def watching(engine):
    queue = QueueManager(engine, 1)
    manager = WatchManager(engine.store, engine, queue)
    manager.global_pause(True)
    yield manager, queue
    manager.close()
    queue.close()


def rules():
    return {
        "all": [
            {"field": "category", "op": "eq", "value": "audio"},
            {"field": "format", "op": "eq", "value": "mp3"},
            {"field": "audio_bitrate", "op": "le", "value": 128000},
        ]
    }


@pytest.mark.parametrize("key,expected", [("mp3128", True), ("mp3320", False), ("flac", False)])
def test_real_bitrate_matching(engine, media, key, expected):
    info = engine.detector.inspect(media[key])
    assert match_rules(rules(), info) == expected


def test_grouped_rules(engine, media):
    info = engine.detector.inspect(media["mp3128"])
    assert match_rules(
        {
            "any": [
                {"field": "extension", "op": "in", "value": ["mp3", "flac"]},
                {"field": "size", "op": "lt", "value": 1},
            ]
        },
        info,
    )
    assert not match_rules({"all": [{"field": "width", "op": "ge", "value": 100}, rules()]}, info)


def test_recursive_scan(tmp_path):
    (tmp_path / "root.txt").write_text("root")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub/child.txt").write_text("child")
    assert len(list(scan_files(tmp_path, False))) == 1
    assert len(list(scan_files(tmp_path, True))) == 2
    (tmp_path / "loop").symlink_to(tmp_path, target_is_directory=True)
    assert len(list(scan_files(tmp_path, True))) == 2


def test_stability_growing_deleted_renamed(tmp_path):
    source = tmp_path / "new.mp3"
    source.write_bytes(b"a")
    detector = StabilityDetector()
    assert not detector.ready(source, 5, now=0)
    assert not detector.ready(source, 5, now=4)
    assert detector.ready(source, 5, now=5)
    source.write_bytes(b"growing")
    assert not detector.ready(source, 5, now=6)
    assert not detector.ready(source, 5, now=10)
    assert detector.ready(source, 5, now=11)
    renamed = source.with_name("renamed.mp3")
    source.rename(renamed)
    assert not detector.ready(source, 5, now=12)
    assert not detector.ready(renamed, 5, now=12)
    renamed.unlink()
    assert not detector.ready(renamed, 5, now=20)


@pytest.mark.parametrize("suffix", [".part", ".tmp", ".crdownload", ".download"])
def test_incomplete_files(tmp_path, suffix):
    source = tmp_path / ("test" + suffix)
    source.write_bytes(b"data")
    detector = StabilityDetector()
    assert not detector.ready(source, 1, now=0)
    assert not detector.ready(source, 1, now=10)


def make_watcher(manager, folder, **changes):
    watcher = Watcher(
        "Music",
        str(folder),
        "MP3 320 kbps",
        rules=rules(),
        destination=str(folder / "outputs"),
        **changes,
    )
    preset = manager.store.get("preset", watcher.preset_id)
    watcher.warning_ack = json.dumps([watcher.rules, preset["options"]], sort_keys=True)
    manager.save(watcher)
    return watcher


def test_dry_run_no_conversion(watching, tmp_path, media):
    manager, queue = watching
    shutil.copyfile(media["mp3128"], tmp_path / "track.mp3")
    watcher = make_watcher(manager, tmp_path)
    result = manager.preview(watcher)
    assert result["matches"] == 1
    assert not queue.jobs
    assert not (tmp_path / "outputs").exists()
    assert "cannot restore" in result["rows"][0]["plan"]["warnings"][0]


def test_convert_exactly_once_and_restart(watching, tmp_path, media):
    manager, queue = watching
    source = tmp_path / "track.mp3"
    shutil.copyfile(media["mp3128"], source)
    watcher = make_watcher(manager, tmp_path, enabled=True)
    manager._process(str(source), [watcher])
    manager._process(str(source), [watcher])
    deadline = time.monotonic() + 20
    while (
        queue.jobs[0].state not in (State.COMPLETED, State.FAILED) and time.monotonic() < deadline
    ):
        time.sleep(0.1)
    assert queue.jobs[0].state == State.COMPLETED
    manager._process(str(source), [watcher])
    assert len(queue.jobs) == 1
    assert manager.store.is_output(queue.jobs[0].output)
    assert manager.excluded(watcher, queue.jobs[0].output)
    from fileconverter.store import Store

    restored = Store(manager.store.root)
    assert restored.processed(watcher.id, queue.jobs[0].source_identity)[0] == "COMPLETED"
    restored.close()


def test_warning_blocks_until_ack(watching, tmp_path, media):
    manager, queue = watching
    source = tmp_path / "track.mp3"
    shutil.copyfile(media["mp3128"], source)
    watcher = make_watcher(manager, tmp_path, enabled=True)
    watcher.warning_ack = ""
    manager._process(str(source), [watcher])
    assert not queue.jobs
    assert "warning" in manager.states[watcher.id].lower()


def test_preset_changed_deleted(watching, tmp_path, media):
    manager, queue = watching
    source = tmp_path / "track.mp3"
    shutil.copyfile(media["mp3128"], source)
    watcher = make_watcher(manager, tmp_path, enabled=True)
    preset = manager.store.get("preset", watcher.preset_id)
    preset["options"]["audio_bitrate"] = 192000
    preset["version"] += 1
    manager.store.put("preset", preset["id"], preset)
    manager._process(str(source), [watcher])
    assert not queue.jobs  # Changed bitrate invalidates quality acknowledgement.
    manager.store.delete("preset", preset["id"])
    manager._process(str(source), [watcher])
    assert "deleted" in manager.states[watcher.id]


def test_mirror_destination(watching, tmp_path):
    manager, _ = watching
    source = tmp_path / "source/artist/album"
    source.mkdir(parents=True)
    watcher = Watcher(
        "mirror",
        str(tmp_path / "source"),
        "Balanced",
        output_mode="mirror",
        destination=str(tmp_path / "out"),
    )
    assert manager.destination(watcher, source / "track.mp3") == str(tmp_path / "out/artist/album")


def test_pause_and_persistence(watching, tmp_path):
    manager, _ = watching
    watcher = make_watcher(manager, tmp_path)
    manager.global_pause(True)
    assert manager.store.settings["watching_paused"]
    assert manager.store.get("watcher", watcher.id)["enabled"] is False
    manager.global_pause(False)
    assert not manager.store.settings["watching_paused"]


def test_priority(watching, tmp_path, media):
    manager, queue = watching
    source = tmp_path / "track.mp3"
    shutil.copyfile(media["mp3128"], source)
    low = make_watcher(manager, tmp_path, enabled=True, priority=1)
    high = dataclasses.replace(low, id="high", priority=10)
    manager.save(high)
    manager._process(str(source), [high, low])
    assert len(queue.jobs) == 1
    assert queue.jobs[0].watcher_id == high.id


def test_invalid_rules():
    with pytest.raises(ValueError):
        validate_rules({"field": "unknown", "op": "eq", "value": 1})
    with pytest.raises(ValueError):
        validate_rules({"field": "size", "op": "between", "value": [1]})


def test_unavailable_restored_folder(watching, tmp_path):
    manager, _ = watching
    root = tmp_path / "external"
    watcher = make_watcher(manager, root, enabled=True)
    manager.global_pause(False)
    time.sleep(0.7)
    assert manager.states[watcher.id] == "Waiting for folder"
    root.mkdir()
    time.sleep(0.7)
    assert watcher.id in manager.observers
    root.rename(tmp_path / "disconnected")
    time.sleep(0.7)
    assert manager.states[watcher.id] == "Waiting for folder"


def test_native_event_and_startup_scan(watching, tmp_path, media):
    manager, queue = watching
    source = tmp_path / "existing.mp3"
    shutil.copyfile(media["mp3128"], source)
    make_watcher(manager, tmp_path, enabled=True, stability_seconds=1)
    manager.global_pause(False)
    deadline = time.monotonic() + 15
    while not any(j.state == State.COMPLETED for j in queue.jobs) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert len(queue.jobs) == 1
    shutil.copyfile(media["mp3128"], tmp_path / "new.mp3")
    while (
        len([j for j in queue.jobs if j.state == State.COMPLETED]) < 2
        and time.monotonic() < deadline
    ):
        time.sleep(0.1)
    assert len(queue.jobs) == 2
    assert all(j.state == State.COMPLETED for j in queue.jobs)


def test_corrupt_watcher_data(engine):
    engine.store.put("watcher", "broken", dict(id="broken", invalid=True))
    queue = QueueManager(engine)
    manager = WatchManager(engine.store, engine, queue)
    assert "broken" not in manager.watchers
    assert manager.store.activities("broken")[0]["status"] == "FAILED"
    manager.close()
    queue.close()


def test_priority_remains_once_after_completion(watching, tmp_path, media):
    manager, queue = watching
    source = tmp_path / "track.mp3"
    shutil.copyfile(media["mp3128"], source)
    low = make_watcher(manager, tmp_path, enabled=True, priority=1)
    high = dataclasses.replace(low, id="high", priority=10)
    manager.save(high)
    manager._process(str(source), [high, low])
    deadline = time.monotonic() + 15
    while queue.active and time.monotonic() < deadline:
        time.sleep(0.05)
    while (
        queue.jobs[0].state not in (State.COMPLETED, State.FAILED) and time.monotonic() < deadline
    ):
        time.sleep(0.05)
    manager._process(str(source), [high, low])
    assert len(queue.jobs) == 1


def test_terminal_registry_not_overwritten_by_queued(watching):
    manager, _ = watching
    manager.store.record_processed("watch", "id", "job", "", "COMPLETED")
    manager.store.record_processed("watch", "id", "job", "", "QUEUED")
    assert manager.store.processed("watch", "id")[0] == "COMPLETED"


def test_new_only_baseline(watching, tmp_path, media):
    manager, queue = watching
    source = tmp_path / "existing.mp3"
    shutil.copyfile(media["mp3128"], source)
    watcher = make_watcher(manager, tmp_path, enabled=True, startup="new")
    manager._scan(watcher, baseline_before=time.time())
    manager._process(str(source), [watcher])
    assert not queue.jobs
    manager.request_scan(watcher.id)  # Explicit scan includes the existing baseline.
    manager._process(str(source), [watcher])
    assert len(queue.jobs) == 1


def test_same_folder_loop_protection(watching, tmp_path, media):
    manager, queue = watching
    source = tmp_path / "track.mp3"
    shutil.copyfile(media["mp3128"], source)
    watcher = make_watcher(manager, tmp_path, enabled=True)
    watcher.output_mode = "same"
    manager._process(str(source), [watcher])
    deadline = time.monotonic() + 15
    while (
        queue.jobs[0].state not in (State.COMPLETED, State.FAILED) and time.monotonic() < deadline
    ):
        time.sleep(0.05)
    assert queue.jobs[0].state == State.COMPLETED
    deadline = time.monotonic() + 3
    while queue.active and time.monotonic() < deadline:
        time.sleep(0.05)
    assert manager.excluded(watcher, queue.jobs[0].output)
    manager._process(queue.jobs[0].output, [watcher])
    assert len(queue.jobs) == 1


def test_malformed_watcher_json_does_not_crash(engine):
    with engine.store.lock, engine.store.db:
        engine.store.db.execute(
            "INSERT INTO records VALUES(?,?,?)", ("watcher", "broken-json", "{invalid")
        )
    queue = QueueManager(engine)
    manager = WatchManager(engine.store, engine, queue)
    assert "broken-json" not in manager.watchers
    assert engine.store.activities("broken-json")[0]["status"] == "FAILED"
    manager.close()
    queue.close()


@pytest.mark.parametrize("snapshot", ["keep", "legacy", "corrupt"])
def test_queued_source_action_does_not_follow_later_edits(watching, tmp_path, snapshot):
    from fileconverter.models import Job, Options
    from fileconverter.watchers import identity

    manager, queue = watching
    source = tmp_path / "original.txt"
    source.write_text("Original user data")
    watcher = Watcher(
        name="Safe source", path=str(tmp_path), preset_id="Balanced", output_mode="same"
    )
    manager.save(watcher)
    saved = dataclasses.asdict(watcher) if snapshot == "keep" else {}
    if snapshot == "corrupt":
        saved = {"unexpected": True}
    watcher.source_action = "delete"
    manager.save(watcher)
    job = Job(
        str(source),
        str(tmp_path),
        Options(),
        state=State.COMPLETED,
        watcher_id=watcher.id,
        source_identity=identity(source) + ":version",
        watcher_snapshot=saved,
    )
    manager.complete(job)
    assert source.read_text() == "Original user data"
