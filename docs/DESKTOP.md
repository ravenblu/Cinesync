# CineSync Desktop (macOS preview)

## Build and launch

Double-click `scripts/build_macos.command` on a Mac with Python 3.9+ and internet access. It installs desktop/build dependencies into the repository's `.venv`, then produces `dist/CineSync Bridge.app`. No system Python packages are changed.

For development:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install '.[desktop,build]'
.venv/bin/python -m cinesync.app
```

The application is a development build, not notarized for public distribution. PyInstaller builds for the current Python architecture. Actual GUI and bundle verification must be performed on that target Mac. FFmpeg/ffprobe are external dependencies: select their executables in Output settings if not found automatically.

## Minimal workflow

On first launch, CineSync displays only Maya connection setup. After it connects to Maya once, CineSync remembers the setup and opens the workspace directly on later launches, including when Maya is closed.

1. **Import.** Choose the exported editorial XML. Sequences load automatically; a selector appears only if multiple sequences exist.
2. **Build.** One button checks and packages the references. Optionally enable **Review and select shots before building** to review a subset first, then click Build again.
3. **Open in Maya.** The section becomes active after a successful build whose inputs and settings still match. Launch Maya yourself before clicking. If its receiver is unavailable, CineSync silently drops the click and does not queue it for a later Maya launch.

The gear button at the top right opens profile import/load/save, camera mapping, optional mixed audio, output settings, and diagnostics. The main Build section has no Cancel or Show output buttons. Closing while an export is running requests cancellation; wait for it to stop before closing again.

The Import section requires one motion-capture FBX. Select mayapy in Output
settings, read its embedded shoot timecode, and choose a Take before building. See [FBX synchronization](MOCAP.md).

No percentage is shown while idle. A working indicator is visible only during processing or a pending Maya request. Errors and completion messages appear beneath the steps; detailed logs and output access are in Settings.

## Editorial handoff

Premiere automation is deferred. Export Final Cut Pro XML manually, then use the desktop GUI for configuration, preflight, JPEG/WAV packaging, and Maya delivery. Camera tracks must be separate and synchronized; video nesting, retiming, transitions, and effects are outside the current parser's scope. Resolve is also deferred.

For mixed audio, export a full-sequence 48 kHz/24-bit PCM WAV from Premiere, beginning at sequence-relative frame zero, and choose it in the GUI. The GUI validates the file and trims it per shot. If no mixdown is supplied, it uses the configured source audio track and cannot reproduce Premiere's mix processing.

## Maya receiver

On first launch, CineSync shows only Maya connection setup. After CineSync has connected to Maya once, it remembers that setup and opens the main workspace directly on later launches, including when Maya is closed. XML import and package building work offline. The **Open in Maya** section depends on the completed package, not Maya's live status. If Maya is not running when clicked, the click is silently dropped and is not queued for later.

For first-time setup, click **First-time Maya setup…**, then **Install automatic startup…**. Select the `scripts` directory for the Maya version, for example `~/Library/Preferences/Autodesk/maya/2026/scripts`.

The installer appends a marked, deferred receiver startup block to `userSetup.py`, preserves existing code, and creates a timestamped backup before modifying an existing file. Reinstallation replaces only that block. Invalid startup Python or malformed markers are rejected without changing the file. **Remove automatic startup…** removes the CineSync block.

Restart Maya after installing. Startup scripts must be enabled in Maya preferences. The receiver discovers the desktop session automatically, even if Maya starts first; no token copying or Shelf click is needed. Keep the source checkout or app bundle at the installed runtime path. Reinstall startup if that path moves.

**Connect once (copy code)** remains available for a session-only connection. It does not modify Maya startup files.

The desktop publishes a private `session.json` in the user's CineSync application-data directory. Maya reads it to connect to the authenticated localhost service. Closing the desktop removes its own discovery record. Scene changes still require confirmation when opening an incoming shot.

## Recovery

- Closing during export requests cancellation of only job-owned FFmpeg/ffprobe processes and cleans unpublished staging output.
- If a host disconnects, **Abandon host request** releases the desktop's pending request. It does not undo a host operation already in progress. Late results are discarded.
- A package is published only after validation; existing package directories are never overwritten.
- Profiles and job metadata are stored under `~/Library/Application Support/CineSync/`. Job files may contain private absolute paths and must not be committed or shared without review.

## Validation status

Automated profile migration, cancellation, existing pipeline checks, and syntax checks run independently of GUI/DCC hosts. Local TCP tests skip explicitly if the execution environment prohibits listening sockets. Skips do not establish integration success.

This implementation environment could not download PySide6/PyInstaller and prohibited local TCP listeners even after escalation. Consequently no `.app`, screenshot-based GUI validation, or Maya delivery acceptance is claimed. Use this guide on a local Mac to complete those gates.

### Import layout

The Import section uses a single row: Video on the left and Mocap on the right.
Both inputs are required in the desktop workflow. Select and inspect a solved FBX,
choose its take, and verify the read-only embedded source timecode before building.

Audio is optional by default. Enable **Settings → Audio → Audio required** to
show a third Audio column. Use the configured XML audio track, or supply a
complete-sequence 48 kHz / 24-bit WAV starting at sequence-relative frame zero.
Required audio is checked before building. The setting is saved as
`audio_required` in the project profile. With audio optional, the configured XML
audio track is still extracted; set its index to 0 to omit it.
