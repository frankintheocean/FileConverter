import os
import threading
from pathlib import Path

import pytest
from PIL import Image

from fileconverter.capabilities import Capabilities
from fileconverter.engine import Engine
from fileconverter.models import Job, Options, State
from fileconverter.process import Runner
from fileconverter.store import Store

os.environ.setdefault("FILECONVERTER_DEVELOPMENT", "1")


@pytest.fixture(scope="session")
def runner():
    value = Runner()
    yield value
    value.close()


@pytest.fixture(scope="session")
def cap(runner):
    return Capabilities(runner)


@pytest.fixture
def engine(tmp_path, cap, runner):
    store = Store(tmp_path / "state")
    value = Engine(store, cap, runner)
    yield value
    value.close()
    store.close()


@pytest.fixture(scope="session")
def media(tmp_path_factory, cap, runner):
    folder = tmp_path_factory.mktemp("media")
    paths = {}
    for ext in ("mp4", "mov", "mkv"):
        path = folder / f"source.{ext}"
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
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=44100",
                "-t",
                "2",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-c:a",
                "aac",
                str(path),
            ]
        )
        paths[ext] = path
    for ext, codec in (("wav", "pcm_s16le"), ("flac", "flac"), ("mp3", "libmp3lame")):
        path = folder / f"audio.{ext}"
        runner.run(
            [
                cap.ffmpeg,
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=44100",
                "-t",
                "3",
                "-c:a",
                codec,
                str(path),
            ]
        )
        paths[ext] = path
    for rate in (128, 320):
        path = folder / f"track{rate}.mp3"
        runner.run(
            [
                cap.ffmpeg,
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=880",
                "-t",
                "2",
                "-c:a",
                "libmp3lame",
                "-b:a",
                f"{rate}k",
                str(path),
            ]
        )
        paths[f"mp3{rate}"] = path
    image = Image.new("RGBA", (640, 480), (100, 160, 220, 150))
    image.save(folder / "image.png")
    image.convert("RGB").save(folder / "image.jpg")
    paths["png"] = folder / "image.png"
    paths["jpg"] = folder / "image.jpg"
    return paths


@pytest.fixture
def convert(engine, tmp_path):
    def run(source, **settings):
        job = Job(str(source), str(tmp_path / "outputs"), Options(**settings))
        engine.execute(job, threading.Event())
        assert job.state == State.COMPLETED, (job.error, job.diagnostic)
        assert Path(job.output).exists()
        assert job.result["valid"]
        return job

    return run
