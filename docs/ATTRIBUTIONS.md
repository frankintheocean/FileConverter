# Dependency notices

FileConverter and its original artwork are GPL-3.0 licensed; see the repository LICENSE.
No Apple assets or licensed font files are distributed. Installed system fonts are used.

Runtime dependencies:

| Dependency | Upstream | License |
| --- | --- | --- |
| Python | https://www.python.org/ | PSF |
| PySide6 / Qt | https://www.qt.io/ | LGPL-3.0 / GPL-3.0; module-specific notices apply |
| Pillow | https://python-pillow.github.io/ | HPND |
| PyMuPDF / MuPDF | https://pymupdf.readthedocs.io/ | AGPL-3.0; the combined distribution must comply with AGPL obligations |
| watchdog | https://github.com/gorakhargosh/watchdog | Apache-2.0 |
| Send2Trash | https://github.com/arsenetar/send2trash | BSD-3-Clause |
| FFmpeg / ffprobe | https://ffmpeg.org/ | LGPL-2.1+ or GPL-2.0+/GPL-3.0+ depending on build flags |
| LibreOffice (optional, separately installed) | https://www.libreoffice.org/ | MPL-2.0 / LGPL-3.0+ |

Build dependencies: PyInstaller (GPL with distribution exception), pytest (MIT),
ruff (MIT), setuptools (MIT), Inno Setup (upstream license, https://jrsoftware.org/).

The build gathers installed dependency license files into `licenses/`. A Windows
vendor bundle must include its actual FFmpeg license, exact build configuration,
corresponding complete source and checksums. Never redistribute `--enable-nonfree`
builds. Codec patents and distribution permissions vary by jurisdiction.

PyMuPDF imposes AGPL obligations on the combined application. Distribute the complete
corresponding application source and build instructions with releases, including this
repository's GPL license and the upstream AGPL notice. Do not distribute closed-source
binaries unless you have separately secured appropriate commercial licenses.

Qt remains in separate shared libraries in the onedir bundle, enabling replacement
under the LGPL. Include the Qt license texts and corresponding-source access in the
release. No secure signing identity or update service is configured by this repository.
