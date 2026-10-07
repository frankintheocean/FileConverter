# Building and releasing

FileConverter is Windows only. Use Python 3.11–3.13 on Windows. PyInstaller does not cross-compile.

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m ruff format --check .
```

Install Inno Setup 6 from https://jrsoftware.org/isinfo.php. The Windows release
build requires an audited x64 FFmpeg bundle. Acquire a redistributable GPL build
from a trusted upstream listed at https://ffmpeg.org/download.html, verify the
publisher's checksums/signatures, and retain its corresponding complete source.
The application never downloads conversion executables at runtime.

Prepare a vendor directory containing `ffmpeg.exe`, `ffprobe.exe`, `LICENSE`,
`BUILD.txt` (version, configure flags, full build instructions and source origin),
`SOURCE.tar.xz` (corresponding complete source including external-codec sources),
and `manifest.json` containing an `origin` HTTPS URL and a `sha256` object mapping
every bundled filename (including any runtime DLLs) to their actual verified SHA-256 digests. The build rejects
missing files, checksum differences and `--enable-nonfree` builds. This manifest
is an integrity gate, not a substitute for verifying the publisher's authenticity.

```powershell
.venv\Scripts\python scripts\build.py --vendor C:\VerifiedFFmpeg --installer
```

Outputs:

- `dist/FileConverter/FileConverter.exe`, Qt/Python shared runtime, vendored engines,
  icon assets and dependency license notices.
- `artifacts/FileConverter-1.0.7-Windows-x64-Setup.exe`.

End users need neither Python nor development tools. LibreOffice is an optional
separate dependency; unavailable Office conversion pairs are hidden. Documents
are processed in isolated LibreOffice profiles. Do not bundle LibreOffice without
its complete distribution notices and source obligations.

Sign the executable and installer using the publisher's Authenticode identity
before public release, then verify signatures with `signtool verify /pa`. No
signing identity has been invented here. Publish the complete repository source,
build configuration, third-party corresponding sources and notices alongside
binaries. The source archive inside the binary directory includes the complete application project, build configuration, tests and documentation.
Third-party corresponding sources must also accompany releases.

Linux is supported for engine/unit/UI tests only, not application distribution.
`python scripts/build.py` refuses a non-Windows host. Source-only ZIP packaging
is available through `python scripts/release_zip.py --source-only`.

## Maintenance

Install per-user under `%LOCALAPPDATA%\Programs\FileConverter`. Running the same
version installer opens Repair / Uninstall; Cancel is the wizard's native Cancel.
Repair recopies all shipped files regardless of version, restores shortcuts and
registry entries, and recreates the uninstaller. Windows Restart Manager detects
locked files. Inno Setup supplies disk-space checks and rollback for recoverable
installation errors. Repair does not alter `%LOCALAPPDATA%\FileConverter` or outputs.
Upgrades use the same AppId and installation directory. Converted files are never
installed files and are never targeted by uninstall.

Uninstall asks whether to remove settings/history, defaulting to No. Silent uninstall
preserves data. The app closes through a local IPC request, cancels owned encoders,
and stops watchers before installer/uninstaller proceeds. If graceful close fails,
maintenance stops with a remediation message instead of terminating unrelated tools.

Run `scripts/windows_verify.ps1` on a disposable Windows VM after building. Also
exercise interactive maintenance, accessibility, taskbar identity, notifications,
DPI, system themes, removable/network locations and interrupted installs; headless
Linux tests cannot establish Windows desktop behavior.


## Reproducible native CI build

The Windows workflow builds the vendor bundle with MSYS2/MinGW64 using
`scripts/build_ffmpeg_windows.sh`, pinned FFmpeg/x264/LAME/zlib/dav1d commits and static
linking. The build stores pristine corresponding source plus the recipe in
`SOURCE.tar.xz`, generates checksums and verifies the resulting executables before
PyInstaller/Inno packaging. No executable is fetched from an arbitrary release mirror.
Custom broader codec builds may be supplied using the same vendor validation contract.

The release ZIP includes Setup, application files, complete project source and
notices. It is produced only when both actual Windows executables exist; source-only
packaging never fabricates an installer. Tag builds publish only after automated
native install/repair/uninstall/reinstall and real media smoke checks have passed.
Visual shell integration, accessibility, upgrade and interrupted-install checks still
require the interactive checklist. Automated publishing does not add Authenticode signing.
