# Minimal FBX synchronization

CineSync accepts a required solved skeleton FBX alongside the Premiere XML.
One selected animation Take can supply multiple editorial shots from one capture
session lasting less than 24 hours. This version does not retarget animation to
a character rig, clean mocap, combine FBX takes, or manage multiple actors.

## Use the desktop app

1. On macOS, leave **Settings > Output > Maya mayapy executable** blank to use
   the newest installation with an executable under `/Applications/Autodesk`.
   For a different version or custom install, select its `mayapy` executable.
   A typical macOS location is
   `/Applications/Autodesk/maya2027/Maya.app/Contents/bin/mayapy`.
   Use **Save as…** to keep this setting in a profile; load that profile next time.
2. In **1. Import**, choose the Premiere XML and corresponding FBX. Click
   **Read FBX details**. This starts an isolated Maya standalone process.
3. Select a Take. Native FPS, embedded shoot TC, timecode FPS and local anchor
   are displayed read-only. Missing or unsupported embedded timecode blocks Build.
4. Click **Build**. Video files must contain embedded timecode matching the XML;
   all cameras must agree at each shot's first frame. The selected FBX must cover
   every selected shot at that same source timecode. Use shot review for a subset.
5. Open Maya yourself and click **Open in Maya**; confirm the incoming shot.

Changing the FBX, Take, XML, or settings requires a new Build. Existing packages
remain playable, but sources without embedded timecode cannot be rebuilt.

## Supported FBX timecode metadata

CineSync v1 uses a custom metadata format. The bundled reader looks for all four
properties on exactly one skeleton node, either a joint or its transform ancestor.

| Property | Type | Meaning |
| --- | --- | --- |
| `cineSyncSourceTimecode` | string | Non-drop `HH:MM:SS:FF` shoot TC |
| `cineSyncTimecodeFps` | number | Integer TC rate, matching editorial FPS |
| `cineSyncAnchorFrame` | number | Native local frame corresponding to that TC |
| `cineSyncTimecodeVersion` | number | `1` |

The [manual test generator](../tests/manual/mocap_self_test.py) writes these
properties before exporting its synthetic source FBX. Production exporters can
use the same fields when they have verified capture data. A vendor export that
uses a different layout requires an adapter. Unsupported inputs fail at
**Read FBX details**.

CineSync reads the embedded shoot timecode and counting rate from the MOV/MP4
`tmcd` track using ffprobe. Timecode tags on the video stream and container, when
present, must agree with that track. A plain tag or burned-in watermark alone is
insufficient. The video and FBX timecode counting rates must equal the integer
editorial rate. Drop-frame timecode is unsupported. Each video recording rate and
the FBX animation sampling rate must be a positive integer multiple of the
editorial rate. For a 30 fps edit, a 60 fps video supplies every second frame
and a 120 fps FBX supplies every fourth sample.

## Add a mocap software adapter

1. Inspect an original export from the target capture software alongside the
   known shoot timecode, counting rate, and frame alignment for the same take.
   Identify the source of those values in the FBX or in the capture system's
   matching recording metadata.
2. Add a reader to the [Maya FBX worker](../cinesync/adapters/maya/fbx_worker.py).
   Return the normalized `timecode` record accepted by
   [`validate_timecode()`](../cinesync/core/timecode.py), with `value` for the
   shoot TC, `fps` for its counting rate, `local_frame` for the corresponding
   frame in the imported take, `version` set to `1`, and `origin` identifying
   the vendor field or recording metadata used. Record this association after
   accounting for any trim or frame offset applied by the exporter. An
   animation start frame, filename or file creation time alone is insufficient.
3. Use the same reader for take inspection and shot export so the worker can
   recheck the metadata before baking. The shot exporter currently updates
   existing CineSync properties. When adding a vendor reader, create those
   properties on the output skeleton if needed, and write the shot's start
   timecode and its new local anchor at Maya 1001. The original vendor FBX can
   remain the direct input.
4. Verify a real vendor export against its matching video. Cover a nonzero
   local anchor, the first and last exported samples, and an error case for
   missing or conflicting metadata. Keep private captures outside source control.

## Time mapping

For editorial FPS `Fs`, native FBX FPS `Fm`, local anchor `L`, anchor TC `A`,
and shot source TC `S` (TCs converted to editorial frames):

```text
FBX local frame at Maya 1001 + j = L + (S - A + j) * Fm / Fs
```

Matching uses exact arithmetic and rejects noninteger sampling ratios such as
120/25. The timecode anchor must align to a native sample. Midnight
rollover is resolved only when exactly one source interval fits the FBX coverage.
The FBX's available interval ends one native sample after its last keyed frame.

For example, FBX frame 0 at 120 FPS corresponds to `15:14:20:00`. A 30 FPS shot
starting at `15:14:25:00`, lasting 150 frames, uses FBX range `[600, 1200)`.
The shot output is sampled at 30 FPS on Maya frames **1001–1150**, with output
frame 1150 sampling source frame 1196. No motion is time-stretched.

## Package and scene behavior

Build imports the source into an isolated mayapy scene, preserves joints and
their transform ancestors, converts units/axes through Maya's FBX importer,
and bakes transform animation at the shot FPS. Root motion is preserved.
The shot is rebased to 1001 and exported as FBX 2020:

```text
package/
  manifest.json
  footage/SH001/...
  audio/SH001.wav
  mocap/SH001/body.fbx
```

Manifest v1 adds an optional `shots[].mocap` list containing actor ID `body`,
relative FBX path, target FPS, start and duration, and original source metadata.
Source intervals are saved as numerator/denominator pairs to retain exact
fractions. The generated package can be moved without the original source FBX.
Old video-only manifests remain valid.

The original FBX is never edited. Inspection snapshots its size and modification
time; changes require reinspection. Video and FBX outputs are staged together,
and neither is published if any processing or validation fails. Job snapshots
and mayapy request/response logs live under the CineSync application-data
`jobs` directory and may contain private source paths.

In Maya, imported skeletons use an isolated CineSync namespace and **Add** mode.
The importer settings are restored afterward. Switching shots replaces only
nodes explicitly recorded as CineSync-owned; a video-only shot removes the
previous CineSync skeleton. If an artist parents another node under the imported
skeleton, replacement is blocked until that node is moved out. FBX import runs
before the scene-setup Undo group because the plug-in can clear the Undo queue.
New FBX and audio nodes are tracked explicitly; old audio survives until commit.
On failure, cleanup restores the frame rate, range, frame, selection and interactive
audio binding independently of FBX Undo. Only a matching, nonempty setup Undo group
is undone; unrelated artist commands are never undone. Missing old data or a failed
cleanup is reported as incomplete rollback. Save work before host acceptance testing.

Bone ownership is restricted to the dedicated import namespace. Global Maya
nodes such as track managers are never owned by a skeleton, including when
reading older ownership records. Each audio import creates an independent audio
DG node with explicit WAV filename, offset and manifest-derived playback bounds;
it does not use the stateful `sound` import command. Recovery preserves preexisting audio,
including artist-owned audio that CineSync does not manage.

## Validation

Maya-independent regression checks:

```sh
python3 -B -m unittest tests.test_mocap -v
```

For real FBX acceptance, run this from a terminal using the configured mayapy,
from the repository root (not in Maya Script Editor):

```sh
"/Applications/Autodesk/maya2027/Maya.app/Contents/bin/mayapy" tests/manual/mocap_self_test.py
```

It creates its own standalone scenes and synthetic FBX under ignored `test_runs/`.
Checks cover 120-to-25 FPS rejection, 120-to-30/60 FPS sampling, first/last frames, first and repeated
imports, early and partial import failures, player failure, artist-child protection,
switching to video-only shots, and audio replacement without a UI. Each scene case
starts fresh. Failures are collected rather than stopping the remaining independent
cases. `report.json` is updated after every case and includes tracebacks, owner-node
UUIDs, connections and scene diagnostics on failure. Injected failure cases may log
errors or Undo messages intentionally; use each PASS/FAIL and the report's final
status to judge the result. Maya, its FBX plug-in, and any required licensing must
work on the machine running the check.

The unit checks mock media generation. They do not establish real FBX importer,
Maya version, axis conversion, GUI, or rollback correctness; use the host check
and a known take to verify alignment against witness footage before production.
