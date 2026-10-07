"""Streaming archive compression with validation and no-clobber publication."""

from __future__ import annotations

import os
import tempfile
import zipfile
from pathlib import Path

from .engine import publish
from .process import Cancelled
from .watchers import scan_files


def compress_archive(sources, destination, cancel, progress=lambda count: None):
    destination = Path(destination).resolve()
    if destination.exists():
        raise FileExistsError("Archive destination already exists; choose a new filename")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".fileconverter-", dir=destination.parent) as directory:
        temporary = Path(directory) / "output.zip"
        written = 0
        names = set()
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True
        ) as archive:
            for root in sources:
                root = Path(root).resolve(strict=True)
                entries = scan_files(root, True, cancel) if root.is_dir() else [root]
                for source in entries:
                    if cancel.is_set():
                        raise Cancelled("Archive cancelled")
                    if source.resolve() in (temporary, destination):
                        continue
                    if source.is_symlink() or not source.is_file():
                        continue
                    name = (
                        str(Path(root.name) / source.relative_to(root)).replace(os.sep, "/")
                        if root.is_dir()
                        else source.name
                    )
                    if name in names:
                        raise ValueError(
                            "Archive sources have duplicate names. Select one common parent folder instead."
                        )
                    names.add(name)
                    original = (source.stat().st_size, source.stat().st_mtime_ns)
                    with (
                        source.open("rb") as reader,
                        archive.open(name, "w", force_zip64=True) as writer,
                    ):
                        while chunk := reader.read(1024 * 1024):
                            if cancel.is_set():
                                raise Cancelled("Archive cancelled")
                            writer.write(chunk)
                    if original != (source.stat().st_size, source.stat().st_mtime_ns):
                        raise ValueError(
                            "An archive source changed while being read; archive was discarded"
                        )
                    written += 1
                    progress(written)
        if not written:
            raise ValueError("No readable files were selected")
        with zipfile.ZipFile(temporary) as archive:
            for entry in archive.infolist():
                with archive.open(entry) as reader:
                    while reader.read(1024 * 1024):
                        if cancel.is_set():
                            raise Cancelled("Archive validation cancelled")
        publish(temporary, destination)
    return dict(output=str(destination), files=written, size=destination.stat().st_size, valid=True)
