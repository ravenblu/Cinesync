"""Profile persistence independent of Qt and DCC hosts."""
from __future__ import annotations
import copy
import json
import os
import tempfile
from pathlib import Path
from importlib.resources import files

from .settings import PlayerSettings
from cinesync.core.engine import ValidationError


def user_directory() -> Path:
    if os.name == 'nt':
        return Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'CineSync'
    return Path.home() / 'Library' / 'Application Support' / 'CineSync'


def defaults() -> dict:
    return json.loads(files('cinesync.config').joinpath('defaults.json').read_text(encoding='utf-8'))


def merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        # Camera role collections and explicit shot lists replace rather than accumulate.
        if isinstance(value, dict) and isinstance(result.get(key), dict) and key != 'cameras':
            result[key] = merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def validate(data: dict) -> dict:
    if type(data.get('config_version')) is not int or data['config_version'] != 1:
        raise ValidationError('Unsupported configuration version')
    if not isinstance(data.get('project'), str) or not data['project'].strip():
        raise ValidationError('Project name is required')
    if not isinstance(data.get('sequence_name'), str):
        raise ValidationError('Sequence name must be text')
    cameras = data.get('cameras')
    if not isinstance(cameras, dict) or not cameras or data.get('primary_camera') not in cameras:
        raise ValidationError('Choose a primary camera from the camera mappings')
    import re
    for key, camera in cameras.items():
        if not re.fullmatch(r'[A-Za-z0-9_-]+', key):
            raise ValidationError('Camera IDs must be filesystem-safe')
        if camera.get('type') not in ('body', 'face') or not camera.get('label'):
            raise ValidationError(f'Invalid camera role: {key}')
        number = camera.get('track')
        if 'track_name' not in camera and (type(number) is not int or number < 1):
            raise ValidationError('Camera track indices start at 1')
    if data['export']['height'] not in (720, 1080):
        raise ValidationError('Output height must be 720 or 1080')
    for key in ('output_directory', 'ffmpeg', 'ffprobe'):
        if not isinstance(data['export'].get(key), str):
            raise ValidationError(f'export.{key} must be text')
    minimum = data.get('premiere', {}).get('minimum_major_version')
    if type(minimum) is not int or minimum < 1:
        raise ValidationError('premiere.minimum_major_version must be a positive integer')
    if not isinstance(data['premiere'].get('audio_preset'), str):
        raise ValidationError('premiere.audio_preset must be a path string')
    if type(data.get('audio_required', False)) is not bool:
        raise ValidationError('audio_required must be a boolean')
    PlayerSettings(**data['player'])
    maya = data.get('maya', {})
    if not isinstance(maya, dict) or not isinstance(maya.get('mayapy', ''), str):
        raise ValidationError('maya.mayapy must be an executable path string')
    return data


def load(path: str = None) -> dict:
    value = defaults()
    if path:
        incoming = json.loads(Path(path).read_text(encoding='utf-8'))
        if incoming.get('config_version') != 1:
            raise ValidationError('Import legacy configuration using Import profile')
        value = merge(value, incoming)
    return validate(value)


def migrate(path: str) -> dict:
    incoming = json.loads(Path(path).read_text(encoding='utf-8'))
    if 'config_version' in incoming and incoming['config_version'] != 1:
        raise ValidationError('Cannot migrate an unknown configuration version')
    incoming['config_version'] = 1
    return validate(merge(defaults(), incoming))


def save(path: Path, data: dict) -> None:
    validate(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix='.profile-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()
