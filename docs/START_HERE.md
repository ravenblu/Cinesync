# Local self-test

## Generate and verify a package

From a fresh clone, run:

```sh
python -B -m tests.manual.self_test
```

For a double-click workflow, use `tests/manual/run_self_test.command` on macOS or `tests/manual/run_self_test.bat` on Windows. These developer launchers are tracked; generated media remains ignored.

Python 3.9+, FFmpeg, and ffprobe are required. Put both platform-compatible executables in `tools/bin/` or on PATH. On macOS/Linux these files must be executable; on Windows use ffmpeg.exe and ffprobe.exe. No software is downloaded automatically.

The test runs unit tests, generates three numbered camera videos and sync audio, creates synthetic XML, invokes the real packager, and verifies JPEG frame counters and audio sample alignment.

FPS comes from `tests/manual/config.json`: default 30; supported fixture rates are 24, 25, 30, 48, 50, and 60. Source clips contain 90 frames; source frames 10–69 are packaged as a 60-frame shot. Large digits show zero-based source frame numbers. White flashes and audio pulses occur at one-second intervals.

- **PASS:** offline tests completed; continue in Maya.
- **BLOCKED:** dependencies are missing; media packaging has not passed.
- **FAIL:** inspect `test_runs/<timestamp>/REPORT.txt`, `report.json`, and any error log.

Each run creates a new output directory. Avoid launching concurrent tests. Generated files are retained for diagnosis; delete old run directories when no longer needed.

## Run in Maya

After an offline PASS, the run directory contains `RUN_IN_MAYA.py` with the correct local paths.

1. Save your current work and open an empty Maya scene.
2. Open or paste `RUN_IN_MAYA.py` into the Python Script Editor and execute it.
3. Click **Run Maya Self-Test**.

The test configures frame rate/range/audio, checks duplicate ingest and window reopening, verifies decoded images at boundary/middle/out-of-range/subframe positions, exercises split view, and checks cleanup.

Do not scrub or close the player while the test runs. Results appear in the test window and `maya-report.json`. The test changes the empty scene and leaves the player/audio available afterward.

## Manual checks

Play the shot and compare white flashes against audio pulses. Scrub, orbit the viewport, focus the player, and try keys 1/2/3/4 and the opacity slider.

Automatic PASS does not validate a real Premiere export, all Maya versions, 60 fps performance, or long-run memory stability. The test XML is generated programmatically; Premiere is not operated.

Optional executable overrides:

```sh
python -B -m tests.manual.self_test --ffmpeg /path/to/ffmpeg --ffprobe /path/to/ffprobe
```
