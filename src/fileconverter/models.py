from __future__ import annotations

import dataclasses
import enum
import string
import time
import uuid
from typing import Any


class State(str, enum.Enum):
    QUEUED = "QUEUED"
    INSPECTING = "INSPECTING"
    PLANNING = "PLANNING"
    READY = "READY"
    PROCESSING = "PROCESSING"
    VALIDATING = "VALIDATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"


TERMINAL = {State.COMPLETED, State.FAILED, State.CANCELLED, State.INTERRUPTED}
ORDER = [
    State.QUEUED,
    State.INSPECTING,
    State.PLANNING,
    State.READY,
    State.PROCESSING,
    State.VALIDATING,
    State.COMPLETED,
]


@dataclasses.dataclass
class MediaInfo:
    path: str
    category: str
    format: str
    size: int
    duration: float = 0
    width: int = 0
    height: int = 0
    fps: float = 0
    bitrate: int = 0
    audio_bitrate: int = 0
    channels: int = 0
    sample_rate: int = 0
    video_codec: str = ""
    audio_codec: str = ""
    lossless: bool = False
    metadata: dict = dataclasses.field(default_factory=dict)
    streams: list = dataclasses.field(default_factory=list)
    warnings: list = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class Options:
    format: str = "mp4"
    codec: str = ""
    audio_codec: str = ""
    quality: int = 23
    image_quality: int = 85
    lossless: bool = False
    width: int = 0
    height: int = 0
    scale_percent: float = 100
    aspect: bool = True
    fps: float = 0
    bitrate: int = 0
    max_bitrate: int = 0
    audio_bitrate: int = 192000
    sample_rate: int = 0
    channels: int = 0
    remove_audio: bool = False
    metadata: str = "strip"
    artwork: bool = False
    encoder_preset: str = "medium"
    pixel_format: str = "yuv420p"
    acceleration: str = "software"
    target_bytes: int = 0
    reduction: float = 0
    trim_start: float = 0
    trim_end: float = 0
    crop: str = ""
    rotation: int = 0
    volume: float = 1
    normalize: bool = False
    video_stream: int = -1
    audio_stream: int = -1
    subtitle_stream: int = -1
    subtitles: str = "remove"
    remux: bool = False
    operation: str = "convert"
    frame_time: float = 0
    frame_interval: float = 1
    background: str = "#ffffff"
    tags: dict = dataclasses.field(default_factory=dict)
    collision: str = "rename"
    suffix: str = "-converted"
    filename_template: str = "{stem}{suffix}"

    def validate(self):
        if self.operation not in ("convert", "frame", "frames", "subtitles", "merge", "archive"):
            raise ValueError("Unknown conversion operation")
        if not self.format.isalnum() or len(self.format) > 12:
            raise ValueError("Invalid output format")
        if self.width < 0 or self.height < 0 or self.width > 32768 or self.height > 32768:
            raise ValueError("Dimensions must be between 0 and 32768")
        if not 0 <= self.quality <= 63 or not 1 <= self.image_quality <= 100:
            raise ValueError("Quality is out of range")
        if self.fps < 0 or self.fps > 240 or self.audio_bitrate < 8000:
            raise ValueError("Invalid frame rate or audio bitrate")
        if self.target_bytes < 0 or not 0 <= self.reduction < 100:
            raise ValueError("Invalid compression target")
        if self.trim_start < 0 or self.trim_end < 0:
            raise ValueError("Trim times cannot be negative")
        if self.trim_end and self.trim_end <= self.trim_start:
            raise ValueError("Trim end must follow trim start")
        if self.channels not in (0, 1, 2) or not 0 <= self.sample_rate <= 384000:
            raise ValueError("Invalid audio configuration")
        if self.rotation not in (0, 90, 180, 270) or not 0 < self.scale_percent <= 1000:
            raise ValueError("Invalid rotation or scaling")
        if self.metadata not in ("strip", "minimal", "preserve"):
            raise ValueError("Unknown metadata policy")
        if self.collision not in ("rename", "skip", "replace", "newer"):
            raise ValueError("Unknown collision policy")
        if any(c in self.suffix for c in '/\\:<>"|?*'):
            raise ValueError("Filename suffix contains a reserved character")
        if any(c in self.filename_template for c in '/\\:<>"|?*'):
            raise ValueError("Filename template contains a reserved character")
        for _, field, spec, conversion in string.Formatter().parse(self.filename_template):
            if field is not None and (
                field not in ("stem", "suffix", "format") or spec or conversion
            ):
                raise ValueError("Filename templates support only {stem}, {suffix}, and {format}")
        if self.crop:
            parts = self.crop.split(":")
            if len(parts) != 4 or not all(p.isdigit() for p in parts):
                raise ValueError("Crop must be width:height:x:y using nonnegative integers")
            if int(parts[0]) == 0 or int(parts[1]) == 0:
                raise ValueError("Crop dimensions must be positive")
        if not 0 <= self.volume <= 20:
            raise ValueError("Volume must be between 0 and 20")


@dataclasses.dataclass
class Job:
    source: str
    destination: str
    options: Options
    id: str = dataclasses.field(default_factory=lambda: uuid.uuid4().hex)
    state: State = State.QUEUED
    progress: float = 0
    stage: str = "Queued"
    created: float = dataclasses.field(default_factory=time.time)
    started: float = 0
    finished: float = 0
    output: str = ""
    error: str = ""
    diagnostic: str = ""
    info: dict = dataclasses.field(default_factory=dict)
    result: dict = dataclasses.field(default_factory=dict)
    plan: dict = dataclasses.field(default_factory=dict)
    watcher_id: str = ""
    preset_id: str = ""
    source_identity: str = ""
    sources: list[str] = dataclasses.field(default_factory=list)
    watcher_snapshot: dict = dataclasses.field(default_factory=dict)

    def transition(self, state: State):
        if self.state in TERMINAL:
            raise ValueError(f"Cannot transition terminal job {self.state}")
        if state not in {State.FAILED, State.CANCELLED, State.INTERRUPTED}:
            if ORDER.index(state) != ORDER.index(self.state) + 1:
                raise ValueError(f"Illegal transition {self.state} → {state}")
        self.state = state
        self.stage = state.value.title()
        if state in TERMINAL:
            self.finished = time.time()

    def to_dict(self) -> dict[str, Any]:
        value = dataclasses.asdict(self)
        value["state"] = self.state.value
        return value

    @classmethod
    def from_dict(cls, value):
        value = dict(value)
        value["options"] = Options(**value["options"])
        value["state"] = State(value["state"])
        return cls(**value)
