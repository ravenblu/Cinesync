# Contributing

Use English for source code, comments, identifiers, UI text, documentation, and repository filenames. Contributor discussions may use another language when appropriate.

Report reproduction steps, Maya/Python/Qt/OS versions, and actual media frame rates. Remove private paths, project names, and confidential footage details from shared logs.

- Production code lives in `cinesync/` and must not depend on tests or local developer tools.
- Update examples and documentation when changing input/output contracts. Timeline out points are exclusive.
- Follow the [mocap adapter guide](docs/MOCAP.md#add-a-mocap-software-adapter) when adding support for another capture tool's export metadata.
- Run `python -B -m unittest discover -s tests -v` for host-independent tests.
- Follow `docs/START_HERE.md` for FFmpeg and Maya checks, and report which checks actually ran.
- Do not commit generated footage, manifests, reports, local absolute paths, external binaries, or virtual environments.
- Review `git status --short` and `git diff --cached` before committing. Ignore rules do not untrack existing files.
