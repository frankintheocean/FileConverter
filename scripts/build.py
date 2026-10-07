"""Native-platform build. Windows packages require a verified, licensed FFmpeg vendor bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(args):
    subprocess.run([str(a) for a in args], cwd=ROOT, check=True)


def verify_vendor(folder):
    manifest_path = folder / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for name in ("ffmpeg.exe", "ffprobe.exe", "LICENSE", "BUILD.txt", "SOURCE.tar.xz"):
        path = folder / name
        if not path.is_file():
            raise ValueError(f"Missing vendor file: {name}")
        with path.open("rb") as file:
            digest = hashlib.file_digest(file, "sha256").hexdigest()
        if manifest.get("sha256", {}).get(name) != digest:
            raise ValueError(f"Vendor integrity check failed: {name}")
    for path in folder.rglob("*"):
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ValueError(f"Vendor links are not allowed: {path.name}")
        if path.is_file() and path != manifest_path:
            name = path.relative_to(folder).as_posix()
            with path.open("rb") as file:
                digest = hashlib.file_digest(file, "sha256").hexdigest()
            if manifest.get("sha256", {}).get(name) != digest:
                raise ValueError(f"Unverified vendor file: {name}")
    # Require auditable origin and redistributable build configuration.
    if not manifest.get("origin", "").startswith("https://"):
        raise ValueError("Record the trusted HTTPS vendor origin in manifest.json")
    configuration = (folder / "BUILD.txt").read_text(encoding="utf-8")
    if "--enable-nonfree" in configuration:
        raise ValueError("Nonfree FFmpeg builds cannot be redistributed")
    for executable in ("ffmpeg.exe", "ffprobe.exe"):
        result = subprocess.run(
            [str(folder / executable), "-version"], capture_output=True, text=True, check=True
        )
        if "--enable-nonfree" in result.stdout:
            raise ValueError("Executable is configured nonfree")
    return manifest


def collect_licenses(destination):
    from importlib.metadata import distributions

    destination.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "LICENSE", destination / "FileConverter-GPL-3.0.txt")
    shutil.copyfile(ROOT / "docs/ATTRIBUTIONS.md", destination / "ATTRIBUTIONS.md")
    candidates = [
        Path(sysconfig.get_path("stdlib")) / "LICENSE.txt",
        Path(sys.base_prefix) / "LICENSE.txt",
    ]
    python_license = next((path for path in candidates if path.is_file()), None)
    if python_license is None:
        raise ValueError("CPython license text is required for runtime redistribution")
    shutil.copyfile(python_license, destination / "Python-LICENSE.txt")
    for distribution in distributions():
        name = distribution.metadata.get("Name", "dependency")
        files = distribution.files or []
        for file in files:
            if "license" in str(file).lower() or "copying" in str(file).lower():
                source = Path(distribution.locate_file(file))
                if source.is_file():
                    relative = Path(str(file))
                    safe = "-".join(part for part in relative.parts if part not in ("..", "."))
                    shutil.copyfile(source, destination / f"{name}-{safe}")


def source_files():
    for name in (
        "src",
        "scripts",
        "tests",
        "assets",
        "docs",
        "packaging",
        ".github",
        "pyproject.toml",
        "README.md",
        "LICENSE",
        ".gitignore",
    ):
        root = ROOT / name
        paths = sorted(root.rglob("*")) if root.is_dir() else [root]
        for path in paths:
            if (
                path.is_file()
                and not any(
                    part in ("__pycache__", ".pytest_cache", ".ruff_cache")
                    or part.endswith(".egg-info")
                    for part in path.parts
                )
                and path.suffix != ".pyc"
            ):
                yield path


def source_archive(destination):
    archive_path = ROOT / "artifacts/FileConverter-source-1.0.5.tar.gz"
    archive_path.parent.mkdir(exist_ok=True)
    with tarfile.open(archive_path, "w:gz") as archive:
        for path in source_files():
            archive.add(path, arcname="FileConverter/" + path.relative_to(ROOT).as_posix())
    shutil.copyfile(archive_path, destination / archive_path.name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--vendor", type=Path)
    parser.add_argument("--installer", action="store_true")
    args = parser.parse_args()
    if os.name != "nt":
        parser.error(
            "FileConverter is Windows only; build the application and installer on Windows"
        )
    vendor = args.vendor.resolve() if args.vendor else None
    if os.name == "nt":
        if not vendor:
            parser.error(
                "Windows builds require --vendor with the verified FFmpeg bundle; see docs/BUILD.md"
            )
        verify_vendor(vendor)
    run([sys.executable, ROOT / "scripts/icons.py"])
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--windowed",
        "--name",
        "FileConverter",
        "--paths",
        "src",
        "--add-data",
        f"assets{os.pathsep}assets",
        "--collect-all",
        "pymupdf",
        "--exclude-module",
        "PySide6.QtWebEngineCore",
        "--exclude-module",
        "PySide6.QtWebEngineWidgets",
        "--exclude-module",
        "PySide6.QtWebEngineQuick",
    ]
    if os.name == "nt":
        command += ["--icon", "assets/app.ico", "--version-file", "packaging/windows-version.txt"]
    command += ["scripts/entry.py"]
    run(command)
    destination = ROOT / "dist/FileConverter"
    collect_licenses(destination / "licenses")
    if vendor:
        shutil.copytree(vendor, destination / "vendor", dirs_exist_ok=True)
    # Retain the corresponding app source beside binaries for GPL redistribution.
    source_archive(destination)
    executable = destination / ("FileConverter.exe" if os.name == "nt" else "FileConverter")
    run([executable, "--diagnostics", "--data-dir", ROOT / "build/diagnostic-state"])
    if args.installer:
        compiler = shutil.which("ISCC") or str(
            Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)"))
            / "Inno Setup 6/ISCC.exe"
        )
        run([compiler, ROOT / "packaging/installer.iss"])
        run([sys.executable, ROOT / "scripts/release_zip.py"])
    print(f"Built {destination}")


if __name__ == "__main__":
    main()
