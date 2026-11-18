# Architecture and limitations

## Implemented dependency flow

Premiere XML adapter → export configuration → FFmpeg packager → manifest → Maya ingest/player.

The desktop workflow uses an isolated mayapy worker to inspect Takes and bake
per-shot skeleton FBX files from embedded timecode. Pure rational
mapping lives in the core, process orchestration in the pipeline, and Maya FBX
operations in the adapter. JPEG/WAV and FBX generation share one staging/publish
transaction. The receiver, launcher, and player setup use the same scene
transaction for CineSync-owned skeletons. See [FBX synchronization](MOCAP.md).

`cinesync.core.engine` supports validation and timecode math without importing Maya. Maya API imports are deferred; Qt loads only in GUI modules. Tests depend on production code, never the reverse. Runtime packaging includes only `cinesync`.

FPS comes from input data. Manual tests use their own configuration. Cache size and prefetch distance are configurable. Scene setup and display use the shot anchor field; manifest v1 still enforces the 1001 anchor rule.

Developer launchers live under `tests/manual/`. External binaries, generated packages, and reports are ignored. Reproducible test source belongs in version control but is excluded from the installed product.

## Project configuration

`examples/project_config.json` currently describes the project, sequence, camera roles, track selection, and shot detection. `primary_track_clips` derives shot ranges and IDs from non-overlapping primary-track clips. Explicit `shots` override detection.

Use one-based `track` indices, or `track_name` when the XML preserves track names. Without role metadata, a mapping is required; camera roles cannot be inferred reliably from arbitrary footage.

`premiere.integration` and `minimum_major_version` describe the intended future plugin target. The current XML converter cannot reliably validate the running Premiere version from exported XML. These fields are not currently enforced.

## Desktop bridge implementation

The standalone PySide6 GUI edits profiles and imports exported XML,
packages selected shots in a worker thread, and sends completed packages to Maya.
A loopback HTTP service authenticates data-only requests with a session token.
Maya polls off-thread and handles user confirmation and scene mutations on its Qt thread.

Default configuration and schema are packaged under `cinesync/config/`. Profiles and jobs
live in the user application-data directory. The production installation does not depend
on tests. Production modules use their canonical package paths; old root-level forwarding modules have been removed.

The Maya receiver is implemented but not host-validated here. Live Premiere and Resolve integration, plus Windows builds, remain deferred. See DESKTOP.md for build and validation limits.

## Known limitations

- XML support is restricted to separate camera tracks, one covering clip per shot, and no effects, transitions, nesting, or retiming.
- Manifest validation uses dictionaries; a published JSON Schema and migration rules remain to be implemented.
- The core engine still combines data validation and Maya callback ownership.
- Ingest rollback, audio ownership handling, and scene protection need further work.
- The manual test checks top-level objects and audio, not all scene data. Use a dedicated empty test scene.
- Module reloading and old Qt window/callback lifetimes need additional host validation.
- Synthetic fixtures use fixed frame ranges and sample positions; they are not general media acceptance tests.
- Maya 2022 Python compatibility, Qt versions, playback throughput, and long-run memory behavior require validation.
