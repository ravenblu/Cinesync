# CineSync user guide

## Editorial preparation

Work from a delivery copy of the edited Premiere sequence. Expand all reference cameras to separate tracks, such as V1 front, V2 side, and V3 HMC. Flattening only the selected multicam angle does not preserve all reference feeds.

Use an integer sequence rate and constant source video rates that are integer multiples of it. Embedded timecode counts at the sequence rate. The first implementation rejects effects, transitions, retiming, nested sequences, and multicam containers. It does not parse `.prproj` files.

Use an aligned mixdown for audio. The XML converter selects the first audio stream from the specified source; it does not reproduce Premiere mixing, channel routing, or volume automation. Export a mixed WAV beforehand if needed and supply the correct source offset.

Export the selected sequence using **File > Export > Final Cut Pro XML**, and review Premiere's translation report.

Configure `examples/project_config.json` to derive shots from primary-camera clips, or `examples/tracks.json` to specify explicit ranges. Track indices start at 1. Timeline frames are relative to sequence start, not displayed sequence timecode; out points are exclusive.

```sh
python -m cinesync.pipeline.premiere_xml edit.xml examples/project_config.json export_config.json
```

Every shot must be covered by one uninterrupted clip on each selected track. Source videos require valid source timecode. Shot timecode is calculated from file start timecode plus source offset, with midnight rollover.

Alternatively, populate `examples/recipe.json` directly. Media paths may be absolute or relative to the configuration file. `source_in_frame` is the offset from the first decoded media frame or audio origin; `source_timecode` identifies the shot's first source frame. At the shot in-point all camera timecodes must agree.

## Packaging

Use Python 3.9+ and FFmpeg/ffprobe supporting `-fps_mode` (FFmpeg 6+ recommended).

```sh
python -m cinesync.pipeline.export_prep export_config.json packages/SEQ01 --height 720
```

Options include `--height 1080`, `--ffmpeg /path/to/ffmpeg`, and `--ffprobe /path/to/ffprobe`.

Fractional rates, variable rates, and noninteger source-to-sequence ratios are rejected. Conform deliberately before export. JPEG uses `-q:v 4`, not an exact “quality 85” value. Output preserves aspect ratio at 720 or 1080 pixels high. Audio is 48 kHz, 24-bit PCM WAV.

The packager scans source timestamps, trims video by decoded frame, and trims audio on the resampled sample grid. It validates output frame existence, audio format, and duration before publishing the package directory. Existing destinations are rejected; failed exports clean up their staging directory. Use trusted XML and configuration inputs.

```text
SEQ01/
  manifest.json
  footage/SH001/witness_front/frame.000001.jpg
  footage/SH001/hmc_face/frame.000001.jpg
  audio/SH001.wav
```

Manifest v1 records project, sequence, FPS, shots, source timecode, exclusive editorial ranges, duration, Maya anchor, camera sequences, and audio. Package paths are relative and cannot escape the package. An editorial range `[0, 150)` has 150 frames and maps to Maya frames 1001–1150.

## Maya launcher

Add the repository root to Maya's Python path and run in the Python Script Editor:

```python
import sys
sys.path.insert(0, r"/path/to/CutScenePipelineTool")
from cinesync.adapters.maya.launcher import show_launcher
show_launcher()
```

Choose `manifest.json` and a shot, then click **Open and set up Maya shot**. Frame-rate changes require confirmation. Scene setup preserves existing animation keyframe numbers and replaces only audio nodes carrying CineSync's ownership attribute. Save the scene before using scene setup.

Direct API use:

```python
from cinesync.player import ui
player = ui.show('/path/to/package/manifest.json', shot_id='SH001')
```

For this direct API, click **Set up Maya shot**, or prepare the correct frame rate, playback range, and audio yourself.

Keys 1/2/3 select front/side/HMC when using the example camera IDs; 4 selects body+face split view. Hotkeys require player focus. Additional cameras are available through the dropdown. Window position and size persist; opacity starts at 100% each session.

Subframes are floored. Out-of-range frames hold the boundary image and its timecode, displaying `Boundary hold`. Default cache budget is 256 MiB with ±30 frames of prefetch. A background thread decodes QImage; widgets remain on the GUI thread. Pending reads are replaced during scrubbing. Slow I/O may briefly show blank imagery instead of labeling an old image with a new timecode. Displayed images and the in-flight decoded image are additional to the cache budget.

Closing the player, creating/opening a scene, or exiting Maya removes callbacks. Repeated `show()` calls close the previous player. Close windows before reloading runtime modules. Each player handles one shot; use the launcher or call `show()` again to switch shots.

Use Maya's bundled PySide6 or PySide2; do not install a conflicting Qt binding into Maya. Maya 2022 requires Python 3 mode and still needs compatibility validation against its bundled Python version.

## Validation

See [local testing](START_HERE.md) for synthetic media and Maya smoke tests. Unit packaging tests mock FFmpeg and cannot establish real-media correctness.

For acceptance, use numbered multi-camera footage and audio sync pulses. Check first/last frames, random scrubbing, long-shot end alignment, repeated opening/closing, and scene transitions. Measure actual throughput and peak memory for 720p/1080p single and split views. Real Premiere XML dialects and Maya versions need separate validation.

## References

- [Adobe: export Final Cut Pro XML](https://helpx.adobe.com/premiere/desktop/render-and-export/export-files/export-a-project-as-a-final-cut-pro-xml-file.html)
- [FFmpeg documentation](https://ffmpeg.org/ffmpeg.html)
- [Autodesk: MEventMessage](https://help.autodesk.com/cloudhelp/2024/ENU/MAYA-API-REF/py_ref/class_open_maya_1_1_m_event_message.html)
- [Qt threading support](https://doc.qt.io/qt-6.10/threads-modules.html)
