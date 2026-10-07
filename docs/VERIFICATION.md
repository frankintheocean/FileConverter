# Verification record

Verification host: Linux x64, Python 3.12, 2026-10-07. These results establish
Linux execution of the application and engines; they do not establish a verified
Windows production release.

## Automated checks

- Ruff lint and formatting checks passed.
- Mypy passed for all 14 application modules using the Windows target configuration.
- Version 1.0.1 full suite: **104 passed in 66.54 seconds**, with no skips or failures.
- Real Qt navigation, source inspection, queue conversion,
  history and custom icon assets were exercised with the offscreen Qt platform.
- Real filesystem notifications, reconciliation, stable-file checks, actual MP3 bitrate
  matching, recursive discovery, restart state, priorities, duplicate/loop prevention,
  dry runs and malformed persistence were exercised. Saved job source-handling settings
  are tested against later watcher edits and corrupt snapshots.
- Failures cover corrupt/empty inputs, Unicode and special filenames, collisions,
  unavailable dependencies, missing sources, insufficient disk space, cancellation,
  interrupted state and hardware-to-software fallback.

## Actual conversion execution

Tests generate actual media and invoke the installed conversion backends. They do
not substitute fake converters or fake progress. Representative successful paths:

- MP4 → MOV, MOV → MP4, MKV → MP4, MP4 → WebM and GIF.
- 1080p → 720p and 4K → 1080p, audio extraction, frame extraction and subtitles.
- WAV → MP3 320 kbps, FLAC → MP3 and MP3 → WAV.
- PNG → JPG, JPG → WebP, AVIF and the supported image writer formats.
- TXT → DOCX → PDF with detected LibreOffice, image → PDF and PDF → images.
- Multiple image/PDF merge and streaming ZIP through the persisted job queue.

Two-pass video compression used a generated 104,676,445-byte noisy FFV1 source:

| Maximum (decimal bytes) | Validated output bytes |
| --- | ---: |
| 10,000,000 | 9,067,528 |
| 25,000,000 | 22,883,065 |

The outputs reopened and decoded successfully, and both remained under their
requested maximum. Separate tests validate audio under 45,000 bytes and image output
under 5,000 bytes. Infeasible targets fail without publishing oversized output.

## Dependencies and packaged startup

FFmpeg/ffprobe 7.1.5 were detected with encoders, decoders, muxers, demuxers, filters
and acceleration capabilities. Pillow writers include AVIF. LibreOfficeDev
26.8.0.0.alpha0 was available and actually converted documents. Availability of an
encoder in FFmpeg is distinct from working GPU hardware; no usable GPU was established.
The NVENC failure test verified a real software fallback.

The initial 1.0.0 development verification produced `dist/FileConverter/FileConverter` with the shared Qt/Python
runtime and image/PDF libraries. This Linux developer bundle uses system FFmpeg.
Its diagnostics, actual offscreen desktop startup, focus IPC and graceful maintenance
shutdown were exercised. Reproducible setup dependencies and diagnostics were also run.
The build includes dependency license notices and a complete application source archive.

## Icon and branding

The original source is `assets/app.svg`, with a 1024-pixel PNG and a transparent ICO
containing 16, 20, 24, 32, 40, 48, 64, 128 and 256-pixel images. Automated tests check
these assets. Real application screenshots are in `docs/screenshots/`.
Executable, installer, shortcuts, taskbar identity and tray configuration reference
the same artwork. Actual Windows shell rendering remains unverified.

## Windows release gates still outstanding

The native GitHub Windows runner produced and verified the 1.0.1 executable and installer.
The downloaded 201,323,295-byte Windows ZIP matched its published SHA256 and passed CRC checks.
Inno Setup source,
PE metadata, vendor integrity checks and `scripts/windows_verify.ps1` are supplied;
the first automated native install/repair/uninstall/reinstall and real conversion/size checks passed.
Version 1.0.4 adds upgrade, damaged-executable/metadata repair, native UI and actual PE icon checks.
Interactive items still require a Windows 10/11 x64 runner:

- Native executable/installer build and actual installed launch.
- Clean install, upgrade, interrupted/locked-file installation and same-version maintenance.
- Repair of executable, dependency, shortcuts, metadata and icon resources.
- Installed Apps uninstall, installer maintenance uninstall and reinstall, with settings
  and user-created outputs preserved.
- Native process JobObject behavior, taskbar/Start Menu/Desktop icon rendering,
  notifications, system tray, high-DPI/accessibility and removable/network-drive behavior.

The project does not include a publisher signing identity or automatic update service.
Public distribution also requires completing third-party corresponding-source and
license obligations described in `ATTRIBUTIONS.md` and `BUILD.md`. Optional LibreOffice
conversion availability and codec capabilities depend on the actual installation.
Markdown conversion preserves literal text rather than claiming rich document layout.


## Windows-only 1.0.1 packaging and destination update

Production build scripts now refuse Linux application builds. Non-Windows UI execution
is restricted to the explicit `FILECONVERTER_DEVELOPMENT=1` test mode. Source ZIP
packaging remains platform independent. Output mode tests cover a chosen folder,
source folders across a mixed-location batch, mode switching and missing destinations.

The native Windows tag workflow compiles FFmpeg/x264/LAME/zlib from pinned sources,
creates an actual installer and ZIP, runs installed repair/uninstall/reinstall and
real conversion/size checks, and publishes only after those succeed. Its generated
`verification/windows-verification.json` in a published Windows ZIP records the native
runtime results. This host's Linux verification does not stand in for that report.


## Native Windows release evidence

The first native Windows workflow completed successfully and published a ZIP containing
Setup, bundled application/engines, complete source and a runtime verification report.
Its actual conversions produced MOV (172,743 bytes), GIF (405,574 bytes), extracted PNG
(51,182 bytes), MP3 (82,590 bytes), and validated 10 MB/25 MB maximum outputs of
9,067,528 / 22,883,065 bytes. Installed desktop startup, focus and safe shutdown passed.

Final 1.0.4 native results are carried in `verification/windows-verification.json`,
`verification/windows-installer-verification.json` and `verification/windows-ui-tests.xml`
in the actual Windows ZIP. Release publication is blocked unless each native command
succeeds; a later packaging command cannot mask failed verification. The cached backend
is our previously source-built release, verified against a pinned whole-ZIP checksum
and all vendor file checksums before executing it; its complete engine source is retained.
