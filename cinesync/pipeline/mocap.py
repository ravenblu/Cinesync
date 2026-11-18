"""Run Maya's FBX tools in isolated, cancellable mayapy processes."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

from cinesync.core.engine import ValidationError
from cinesync.core.mocap import map_shot
from .control import current

MAC_MAYA_ROOT = Path('/Applications/Autodesk')


def resolve_mayapy(configured: str) -> Path:
    """Use the profile override or find the newest standard macOS installation."""
    def executable(path: Path) -> bool:
        try:
            return path.is_file() and os.access(path, os.X_OK)
        except OSError:
            return False

    if configured.strip():
        path = Path(configured.strip()).expanduser()
        if executable(path):
            return path.absolute()
        raise ValidationError(f'Mayapy is not executable: {path}. Check Settings > Output.')

    if sys.platform == 'darwin':
        installs = []
        try:
            for folder in MAC_MAYA_ROOT.iterdir():
                match = re.fullmatch(r'maya(\d{4}(?:\.\d+)*)', folder.name)
                if match:
                    version = tuple(map(int, match[1].split('.')))
                    installs.append((version, folder.name, folder))
        except OSError:
            pass
        for _, _, folder in sorted(installs, reverse=True):
            path = folder / 'Maya.app/Contents/bin/mayapy'
            if executable(path):
                return path
        raise ValidationError(
            f'Mayapy was not found under {MAC_MAYA_ROOT}. Select it in Settings > Output.')

    raise ValidationError('Select a runnable mayapy executable in Settings > Output')


def worker_path() -> Path:
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS) / 'maya_runtime/cinesync/adapters/maya/fbx_worker.py'
    return Path(__file__).resolve().parents[1] / 'adapters/maya/fbx_worker.py'


def invoke(mayapy: str, request: dict, work: Path) -> dict:
    executable = resolve_mayapy(mayapy)
    work.mkdir(parents=True, exist_ok=True)
    prefix = work / ('fbx-' + uuid.uuid4().hex[:8])
    request_file = prefix.with_suffix('.request.json')
    response_file = prefix.with_suffix('.response.json')
    log_file = prefix.with_suffix('.log')
    request_file.write_text(json.dumps(request, indent=2), encoding='utf-8')
    control = current()
    control.check()
    environment = os.environ.copy()
    # Do not inject the desktop Qt/Python runtime into Maya's interpreter.
    for name in ('PYTHONHOME', 'PYTHONPATH', 'QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH'):
        environment.pop(name, None)
    environment['MAYA_SKIP_USERSETUP_PY'] = '1'
    environment['MAYA_SKIP_USERSETUP_MEL'] = '1'
    environment['QT_QPA_PLATFORM'] = 'offscreen'
    with log_file.open('w', encoding='utf-8') as log:
        log.write(f'Mayapy: {executable}\n')
        log.flush()
        with subprocess.Popen(
            [str(executable), str(worker_path()), str(request_file), str(response_file)],
            stdout=log, stderr=subprocess.STDOUT, env=environment,
        ) as process, control.watch(process):
            try:
                process.wait(timeout=1800)
            except subprocess.TimeoutExpired as exc:
                process.kill()
                process.wait()
                raise ValidationError(f'mayapy timed out. See {log_file}') from exc
    if not response_file.is_file():
        raise ValidationError(f'mayapy did not return a result. Check Maya installation/licensing. See {log_file}')
    result = json.loads(response_file.read_text(encoding='utf-8'))
    if process.returncode or not result.get('ok'):
        raise ValidationError(f"FBX processing failed: {result.get('error', 'mayapy failed')}. See {log_file}")
    return result['result']


def inspect_fbx(path: str, mayapy: str, work: Path) -> dict:
    source = Path(path).expanduser().resolve()
    if not source.is_file() or source.suffix.lower() != '.fbx':
        raise ValidationError('Choose an existing FBX file')
    before = source.stat()
    result = invoke(mayapy, {'operation': 'inspect', 'file': str(source)}, work)
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValidationError('FBX changed while reading details. Read it again.')
    result['file'] = str(source)
    result['size'] = after.st_size
    result['mtime_ns'] = after.st_mtime_ns
    return result


def attach(recipe: dict, source: dict) -> None:
    for shot in recipe['shots']:
        shot['mocap'] = [map_shot(shot, recipe['fps'], source)]


def export_shots(staging: Path, manifest: dict, mayapy: str, work: Path) -> None:
    items = []
    for shot in manifest['shots']:
        for entry in shot.get('mocap', []):
            target = staging / entry['file']
            target.parent.mkdir(parents=True, exist_ok=True)
            items.append(dict(entry, output=str(target)))
    if items:
        invoke(mayapy, {'operation': 'export', 'items': items}, work)
