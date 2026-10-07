from __future__ import annotations

import dataclasses
import math

from .capabilities import AUDIO, LOSSLESS, VIDEO

CODEC_NAMES = {
    "libx264": "h264",
    "libx265": "hevc",
    "libvpx-vp9": "vp9",
    "libvpx": "vp8",
    "libaom-av1": "av1",
    "libmp3lame": "mp3",
    "libopus": "opus",
    "libvorbis": "vorbis",
    "libtheora": "theora",
}


def effective_duration(info, options):
    end = min(options.trim_end or info.duration, info.duration)
    duration = end - options.trim_start
    if duration <= 0:
        raise ValueError("Trim selection is outside the source duration")
    return duration


def target_bitrate(target_bytes, duration, audio_bitrate=0, margin=0.94):
    if duration <= 0:
        raise ValueError("Target-size encoding needs a known, positive duration")
    available = math.floor(target_bytes * 8 * margin / duration - audio_bitrate)
    if available < 16000:
        raise ValueError(
            "Target is too small for this duration and audio budget. Reduce audio bitrate or trim the file."
        )
    return available


def dimensions(info, options):
    width, height = info.width, info.height
    if options.crop:
        width, height, x, y = map(int, options.crop.split(":"))
        if x + width > info.width or y + height > info.height:
            raise ValueError("Crop rectangle extends outside the source")
    if options.rotation in (90, 270):
        width, height = height, width
    if options.width or options.height:
        if options.aspect:
            factor = min(
                options.width / width if options.width else float("inf"),
                options.height / height if options.height else float("inf"),
            )
            width, height = round(width * factor), round(height * factor)
        else:
            width, height = options.width or width, options.height or height
    width = max(1, round(width * options.scale_percent / 100))
    height = max(1, round(height * options.scale_percent / 100))
    if width * height > 100_000_000:
        raise ValueError("Output dimensions exceed the 100-megapixel safety limit")
    return width, height


class Planner:
    def __init__(self, cap):
        self.cap = cap

    def plan(self, info, options):
        options.validate()
        if options.operation in ("archive", "merge"):
            expected = "zip" if options.operation == "archive" else "pdf"
            if options.format != expected or options.target_bytes or options.reduction:
                raise ValueError(
                    "Archive/merge jobs require their native format and do not guarantee a target size"
                )
            return dict(
                backend="ZIP" if expected == "zip" else "PyMuPDF",
                codec="",
                output_format=expected,
                target_bytes=0,
                expected_bytes=info.size,
                warnings=[],
                passes=1,
                duration=0,
                hardware=False,
            )
        for index, kind in (
            (options.video_stream, "video"),
            (options.audio_stream, "audio"),
            (options.subtitle_stream, "subtitle"),
        ):
            if index >= 0 and not any(
                s.get("index") == index and s.get("codec_type") == kind for s in info.streams
            ):
                raise ValueError(f"Stream #{index} is not an available {kind} stream")
        fmt = options.format
        if options.operation in ("frame", "frames", "subtitles"):
            if info.category != "video":
                raise ValueError("This operation requires a video input")
            if options.operation == "subtitles" and options.subtitle_stream < 0:
                raise ValueError("Select a subtitle stream to extract")
        elif fmt not in self.cap.outputs(info.category, info.format):
            raise ValueError(f"{info.category} → {fmt} is unavailable with the installed backends")
        category = (
            "audio" if fmt in AUDIO and info.category in ("video", "audio") else info.category
        )
        target = options.target_bytes or (
            int(info.size * (1 - options.reduction / 100)) if options.reduction else 0
        )
        if target and options.lossless and info.category == "video":
            raise ValueError(
                "Lossless video cannot guarantee a size target. Choose quality-based lossless or turn off lossless."
            )
        plan = dict(
            backend="",
            output_format=fmt,
            codec="",
            audio_codec="",
            duration=0,
            width=0,
            height=0,
            bitrate=0,
            audio_bitrate=0,
            target_bytes=target,
            passes=1,
            hardware=False,
            expected_bytes=0,
            estimate="",
            warnings=[],
        )
        if info.category in ("video", "audio"):
            plan["backend"] = "FFmpeg"
            duration = effective_duration(info, options)
            plan["duration"] = duration
            if options.operation in ("frame", "frames", "subtitles"):
                if options.operation == "frame" and options.frame_time >= duration:
                    raise ValueError("Frame time is outside the selected media duration")
                plan["codec"] = "png" if options.operation != "subtitles" else "srt"
                return plan
            codecs = self.cap.codecs(fmt, category)
            codec = options.codec or codecs[0]
            if codec not in codecs:
                raise ValueError(f"Codec {codec} is not available for {fmt}")
            plan["codec"] = codec
            details = self.cap.encoder_details(codec)
            if category == "video" and fmt != "gif" and not options.remux:
                if (
                    details["pixel_formats"]
                    and options.pixel_format not in details["pixel_formats"]
                ):
                    raise ValueError("Pixel format is unavailable for the selected encoder")
                if codec in ("libx264", "libx265") and options.quality > 51:
                    raise ValueError("H.264 / H.265 CRF must be between 0 and 51")
            if (
                options.sample_rate
                and details["sample_rates"]
                and str(options.sample_rate) not in details["sample_rates"]
            ):
                raise ValueError("Sample rate is unavailable for the selected encoder")
            if category == "video":
                if options.lossless and codec not in (
                    "libx264",
                    "libx265",
                    "libvpx-vp9",
                    "libaom-av1",
                ):
                    raise ValueError("Lossless encoding is unavailable for this codec")
                width, height = dimensions(info, options)
                if fmt != "gif":
                    width, height = max(2, width // 2 * 2), max(2, height // 2 * 2)
                audio_codec = (
                    ""
                    if options.remove_audio or not info.audio_codec or not VIDEO[fmt][2]
                    else (options.audio_codec or VIDEO[fmt][2])
                )
                if audio_codec:
                    allowed_audio = self.cap.audio_codecs(fmt)
                    if audio_codec not in allowed_audio or audio_codec not in self.cap.encoders:
                        raise ValueError("Audio codec is incompatible with the output container")
                audio_rate = options.audio_bitrate if audio_codec else 0
                if audio_codec == "libopus" and audio_rate > 256000:
                    audio_rate = 256000
                    plan["warnings"].append(
                        "Opus audio bitrate limited to 256 kbps for channel compatibility."
                    )
                if target and audio_rate:
                    audio_rate = min(audio_rate, max(32000, int(target * 8 / duration * 0.15)))
                bitrate = (
                    target_bitrate(target, duration, audio_rate) if target else options.bitrate
                )
                if target:
                    # Conservative bits/pixel estimate. Explicit user dimensions are respected.
                    if not options.width and not options.height:
                        fps = options.fps or info.fps or 30
                        density = 0.075 if codec == "libx264" else 0.045
                        factor = min(1, math.sqrt(bitrate / max(1, width * height * fps * density)))
                        width = max(2, int(width * factor) // 2 * 2)
                        height = max(2, int(height * factor) // 2 * 2)
                    if bitrate < 150000:
                        plan["warnings"].append(
                            "This target will substantially reduce visual quality."
                        )
                plan.update(
                    width=width,
                    height=height,
                    audio_codec=audio_codec,
                    audio_bitrate=audio_rate,
                    bitrate=bitrate,
                )
                plan["passes"] = (
                    2
                    if target
                    and codec in {"libx264", "libvpx-vp9", "libvpx", "libaom-av1", "mpeg4"}
                    else 1
                )
                if (
                    options.acceleration != "software"
                    and not target
                    and not options.remux
                    and not options.lossless
                ):
                    prefix = {"libx264": "h264", "libx265": "hevc", "libaom-av1": "av1"}.get(codec)
                    if prefix:
                        for suffix in ("nvenc", "qsv", "amf"):
                            if f"{prefix}_{suffix}" in self.cap.encoders:
                                plan.update(
                                    codec=f"{prefix}_{suffix}", hardware=True, software_codec=codec
                                )
                                break
            else:
                if options.lossless and codec not in LOSSLESS:
                    raise ValueError("Choose a lossless codec/container for lossless audio")
                bitrate = target_bitrate(target, duration) if target else options.audio_bitrate
                if codec == "libopus" and bitrate > 256000:
                    bitrate = 256000
                    plan["warnings"].append(
                        "Opus bitrate limited to 256 kbps for channel compatibility."
                    )
                if target and codec in LOSSLESS:
                    raise ValueError(
                        "Lossless audio cannot guarantee a target size. Choose a lossy format."
                    )
                if codec == "libmp3lame":
                    if target and bitrate < 32000:
                        raise ValueError("MP3 cannot meet this target at a supported bitrate")
                    rates = (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
                    bitrate = max(
                        k * 1000 for k in rates if k * 1000 <= max(32000, min(320000, bitrate))
                    )
                plan["bitrate"] = bitrate
                if not info.lossless and info.audio_bitrate and bitrate > info.audio_bitrate * 1.08:
                    plan["warnings"].append(
                        "Increasing the bitrate cannot restore audio detail lost in the original lossy file."
                    )
            if options.remux:
                changed = (
                    options.width
                    or options.height
                    or options.fps
                    or options.crop
                    or options.rotation
                    or options.target_bytes
                    or options.reduction
                    or options.trim_start
                    or options.trim_end
                    or options.normalize
                    or options.volume != 1
                    or options.bitrate
                )
                if changed:
                    raise ValueError("Remux cannot apply edits or size targets. Turn off remux.")
                # Stream copy needs a compatible muxer, not an installed encoder.
                container_codecs = (VIDEO if category == "video" else AUDIO)[fmt][1]
                compatible = {CODEC_NAMES.get(c, c) for c in container_codecs}
                source_codec = info.video_codec if category == "video" else info.audio_codec
                if source_codec not in compatible:
                    raise ValueError("Source codec cannot be copied into this container")
                if (
                    category == "video"
                    and plan["audio_codec"]
                    and info.audio_codec
                    != CODEC_NAMES.get(plan["audio_codec"], plan["audio_codec"])
                ):
                    raise ValueError(
                        "Source audio cannot be copied into this container; use transcoding"
                    )
                plan.update(codec="copy", passes=1, hardware=False)
            if options.subtitles == "preserve" and fmt not in ("mkv", "mov", "mp4", "m4v"):
                raise ValueError("Subtitle preservation is supported for MKV and MP4/MOV only")
            rate = plan["bitrate"] + plan["audio_bitrate"]
            if options.remux:
                estimate = int(info.size)
            elif rate and (target or category == "audio" or options.bitrate):
                estimate = int(duration * rate / 8 * 1.025)
            else:
                estimate = 0
            plan["expected_bytes"] = estimate
            plan["estimate"] = (
                f"Estimated {estimate / 1_000_000:.2f} MB"
                if estimate
                else "Quality-based output size varies by source content"
            )
            if plan["width"]:
                plan["estimate"] += (
                    f" · {plan['width']}×{plan['height']} · {plan['bitrate'] / 1000:.0f} kbps"
                    if plan["bitrate"]
                    else f" · {plan['width']}×{plan['height']}"
                )
        else:
            plan["backend"] = "Pillow" if info.category == "image" and fmt != "pdf" else "PyMuPDF"
            if info.format not in ("pdf", "txt", "md", "html") and info.category == "document":
                plan["backend"] = "LibreOffice"
            if info.category == "image":
                plan["width"], plan["height"] = dimensions(info, options)
                if target and fmt not in ("jpg", "jpeg", "webp", "avif"):
                    raise ValueError("Image target-size compression requires JPEG, WebP, or AVIF")
        if plan["backend"] == "FFmpeg":
            plan["input_decoder_arguments"] = self.cap.decoding_arguments(info)
        return plan

    def arguments(self, info, options, plan, output, work, pass_number=0):
        args = [
            self.cap.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-threads",
            "2",
        ]
        if options.trim_start:
            args += ["-ss", str(options.trim_start)]
        args += self.cap.decoding_arguments(info)
        args += [
            "-protocol_whitelist",
            "file,pipe,crypto,data",
            "-filter_threads",
            "2",
            "-i",
            info.path,
            "-threads",
            "2",
        ]
        if options.trim_end:
            args += ["-t", str(plan["duration"])]
        args += ["-map_metadata", "0" if options.metadata == "preserve" else "-1"]
        if options.metadata != "preserve":
            args += ["-map_metadata:s", "-1"]
        if options.metadata == "minimal":
            for key, value in info.metadata.items():
                if key.lower() in {
                    "title",
                    "artist",
                    "album",
                    "album_artist",
                    "author",
                    "copyright",
                    "comment",
                    "genre",
                    "track",
                    "language",
                }:
                    args += ["-metadata", f"{key}={str(value)[:1000]}"]
        fmt = options.format
        if options.operation in ("frame", "frames"):
            filters = []
            if options.operation == "frame":
                args += ["-ss", str(options.frame_time), "-frames:v", "1"]
            else:
                if options.frame_interval <= 0:
                    raise ValueError("Frame interval must be positive")
                filters.append(f"fps=1/{options.frame_interval}")
            if filters:
                args += ["-vf", ",".join(filters)]
            return args + ["-an", "-c:v", "png", "-progress", "pipe:1", str(output)]
        if options.operation == "subtitles":
            return args + [
                "-map",
                f"0:{options.subtitle_stream}",
                "-c:s",
                "srt",
                "-f",
                "srt",
                str(output),
            ]
        audio_only = fmt in AUDIO
        codec = plan["codec"]
        if not audio_only:
            args += ["-map", f"0:{options.video_stream}" if options.video_stream >= 0 else "0:v:0"]
            if codec != "copy":
                filters = []
                if options.crop:
                    filters.append("crop=" + options.crop)
                filters += {90: ["transpose=1"], 180: ["hflip", "vflip"], 270: ["transpose=2"]}.get(
                    options.rotation, []
                )
                filters.append(f"scale={plan['width']}:{plan['height']}:flags=lanczos")
                if options.fps:
                    filters.append(f"fps={options.fps}")
                if options.subtitles == "burn":
                    if options.subtitle_stream < 0:
                        raise ValueError("Select a subtitle stream to burn")
                    # Subtitle is extracted to a controlled filename inside a safe temp directory.
                    filters.append("subtitles=filename=subtitles.srt")
                if fmt == "gif":
                    filters.append("split[a][b];[a]palettegen[p];[b][p]paletteuse")
                args += ["-vf", ",".join(filters), "-c:v", codec]
                if plan["bitrate"]:
                    args += ["-b:v", str(plan["bitrate"])]
                    if options.max_bitrate:
                        args += [
                            "-maxrate",
                            str(options.max_bitrate),
                            "-bufsize",
                            str(options.max_bitrate * 2),
                        ]
                elif codec in ("libx264", "libx265", "libvpx-vp9", "libvpx", "libaom-av1"):
                    args += ["-crf", str(0 if options.lossless else options.quality)]
                    if codec in ("libvpx-vp9", "libaom-av1"):
                        args += ["-b:v", "0"]
                elif plan["hardware"]:
                    if codec.endswith("_nvenc"):
                        args += ["-rc", "vbr", "-cq", str(options.quality), "-b:v", "0"]
                    elif codec.endswith("_qsv"):
                        args += ["-global_quality", str(options.quality)]
                    elif codec.endswith("_amf"):
                        args += [
                            "-rc",
                            "cqp",
                            "-qp_i",
                            str(options.quality),
                            "-qp_p",
                            str(options.quality),
                        ]
                if options.max_bitrate and not plan["bitrate"]:
                    args += [
                        "-maxrate",
                        str(options.max_bitrate),
                        "-bufsize",
                        str(options.max_bitrate * 2),
                    ]
                if codec in ("libx264", "libx265"):
                    if options.encoder_preset not in {
                        "ultrafast",
                        "superfast",
                        "veryfast",
                        "faster",
                        "fast",
                        "medium",
                        "slow",
                        "slower",
                        "veryslow",
                    }:
                        raise ValueError("Unknown encoding preset")
                    args += ["-preset", options.encoder_preset]
                    if codec == "libx265":
                        params = "pools=2:frame-threads=2" + (
                            ":lossless=1" if options.lossless else ""
                        )
                        args += ["-x265-params", params]
                if options.lossless and codec == "libvpx-vp9":
                    args += ["-lossless", "1"]
                if fmt != "gif":
                    if options.pixel_format not in {"yuv420p", "yuv420p10le", "yuv444p"}:
                        raise ValueError("Unsupported pixel format")
                    args += ["-pix_fmt", options.pixel_format]
                if pass_number:
                    args += ["-pass", str(pass_number), "-passlogfile", str(work / "pass")]
            else:
                args += ["-c:v", "copy"]
        else:
            args += ["-vn"]
        audio_codec = codec if audio_only else plan["audio_codec"]
        if audio_codec and pass_number != 1:
            args += [
                "-map",
                f"0:{options.audio_stream}" if options.audio_stream >= 0 else "0:a:0?",
                "-c:a",
                "copy" if options.remux else audio_codec,
            ]
            if not options.remux:
                if audio_codec not in LOSSLESS:
                    args += ["-b:a", str(plan["bitrate"] if audio_only else plan["audio_bitrate"])]
                if options.sample_rate:
                    args += ["-ar", str(options.sample_rate)]
                if options.channels:
                    args += ["-ac", str(options.channels)]
                filters = []
                if options.volume != 1:
                    filters.append(f"volume={options.volume}")
                if options.normalize:
                    filters.append("loudnorm=I=-16:TP=-1.5:LRA=11")
                if filters:
                    args += ["-af", ",".join(filters)]
            if audio_only and options.artwork and fmt in ("mp3", "m4a", "flac"):
                pictures = [s for s in info.streams if s.get("disposition", {}).get("attached_pic")]
                if pictures:
                    args += [
                        "-map",
                        f"0:{pictures[0]['index']}",
                        "-c:v",
                        "copy",
                        "-disposition:v",
                        "attached_pic",
                    ]
        else:
            args += ["-an"]
        if options.subtitles == "preserve" and not audio_only and pass_number != 1:
            args += [
                "-map",
                f"0:{options.subtitle_stream}?" if options.subtitle_stream >= 0 else "0:s?",
                "-c:s",
                "copy" if fmt == "mkv" else "mov_text",
            ]
        else:
            args += ["-sn"]
        for key, value in options.tags.items():
            if not isinstance(key, str) or not key.replace("_", "").isalnum():
                raise ValueError("Invalid metadata field")
            args += ["-metadata", f"{key}={str(value)[:1000]}"]
        if fmt in ("mp4", "mov", "m4v", "m4a") and pass_number != 1:
            args += ["-movflags", "+faststart"]
        muxer = AUDIO[fmt][0] if audio_only else VIDEO[fmt][0]
        args += ["-progress", "pipe:1", "-nostats"]
        if pass_number == 1:
            return args + ["-f", "null", os_devnull()]
        return args + ["-f", muxer, str(output)]


def os_devnull():
    import os

    return os.devnull


def options_copy(options, **changes):
    return dataclasses.replace(options, **changes)
