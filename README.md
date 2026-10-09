# 🔄 FileConverter

A Windows app for converting and compressing videos, audio, images and documents. Files stay on your computer.

## 📥 Get the app

Download the [Windows release](https://github.com/frankintheocean/FileConverter/releases/latest), extract the ZIP and run **Setup**. The bundle includes the app and conversion engines; no Python installation is needed. LibreOffice is optional for Office conversions.

Rerun the matching Setup to **repair** or **uninstall**. Converted files are never targeted by uninstall, and app data is retained by default.

## ✨ Features

- 🎬 Convert supported media, trim clips, extract audio and work with subtitles.
- 🖼️ Resize, crop and rotate images; merge images or PDFs.
- 📦 Compress to a target size or create ZIP archives.
- 📋 Manage a persistent queue, presets, history and watched folders.
- 📁 Save beside each source or choose an output folder; safe names protect originals.
- 🎨 Use system, light or dark themes with keyboard navigation.

Pause holds new jobs while active encodes finish. Interrupted jobs can be retried; partial encodings are not resumed. Available formats depend on the installed engines, and document conversions may change layout.

## 🛠️ Run from source

Use **Python 3.11–3.13**, FFmpeg/ffprobe on PATH and a graphical desktop:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m fileconverter.app
```

Build a Windows installer with:

```powershell
python scripts/build.py --vendor vendor/windows-x64 --installer
```

See the [build guide](docs/BUILD.md) for vendor dependencies and packaging.

## 🧪 Release status

The existing 1.0.7 verification report records native Windows installation, repair, uninstall and conversion checks, plus **109 local tests**. The installer is unsigned. Interactive DPI, accessibility, notifications and interrupted-install behavior still need review. [Validation details →](docs/VERIFICATION.md)

## 🔐 Data & help

Settings and history live in `%LOCALAPPDATA%\FileConverter`. Normal conversions do not upload files. Diagnostic reports can include local paths; review them before sharing.

📚 [Full user and technical guide](docs/USER_GUIDE.md) · [Release guide](docs/RELEASE.md) · [Attributions](docs/ATTRIBUTIONS.md) · [License](LICENSE)
