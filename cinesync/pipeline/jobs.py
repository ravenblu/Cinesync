"""Shared offline/live export preparation used by the desktop worker."""
from __future__ import annotations
import copy
import json
import shutil
from pathlib import Path
from fractions import Fraction

from cinesync.config.loader import validate
from cinesync.core.engine import ValidationError
from .premiere_xml import convert
from .export_prep import build, probe, verify_camera_timecodes
from .control import active


def executable(value: str) -> str:
    found = shutil.which(value)
    if not found and value in ('ffmpeg', 'ffprobe'):
        for folder in ('/opt/homebrew/bin', '/usr/local/bin'):
            path = Path(folder) / value
            if path.is_file():
                found = str(path)
                break
    if not found:
        raise ValidationError(f'Executable not found: {value}. Select it in Output settings.')
    return found


def prepare(xml: str, profile: dict, work: Path, mixed_audio: str = '',
            mocap_source: dict = None) -> dict:
    validate(profile)
    work.mkdir(parents=True, exist_ok=True)
    config = copy.deepcopy(profile)
    if mixed_audio:
        config['audio_track'] = None
    configuration = work / 'project.json'
    configuration.write_text(json.dumps(config, indent=2), encoding='utf-8')
    recipe = convert(xml, str(configuration))
    verify_camera_timecodes(recipe, work, executable(profile['export']['ffprobe']))
    if mixed_audio:
        audio = Path(mixed_audio).resolve()
        if not audio.is_file():
            raise ValidationError('Premiere mixdown is missing')
        streams = probe(audio, executable(profile['export']['ffprobe']))['streams']
        if len(streams) != 1 or streams[0].get('codec_name') != 'pcm_s24le' or int(streams[0].get('sample_rate', 0)) != 48000:
            raise ValidationError('The audio preset must produce audio-only 48 kHz/24-bit PCM WAV')
        stream = streams[0]
        duration = Fraction(stream['duration_ts']) * Fraction(stream['time_base'])
        end = max(shot['timeline_out_frame'] for shot in recipe['shots'])
        if duration + Fraction(1, 48000) < Fraction(end, recipe['fps']):
            raise ValidationError('Mixdown does not cover the selected sequence range')
        for shot in recipe['shots']:
            shot['audio'] = {'media': str(audio), 'source_in_frame': shot['timeline_in_frame']}
    if profile.get('audio_required'):
        missing = [shot['shot_id'] for shot in recipe['shots'] if not shot.get('audio')]
        if missing:
            raise ValidationError('Audio is required for shots: ' + ', '.join(missing))
    if mocap_source:
        recipe['mocap_source'] = copy.deepcopy(mocap_source)
        (work / 'session.json').write_text(
            json.dumps({'version': 1, 'xml': str(Path(xml).resolve()),
                        'mocap_source': mocap_source}, indent=2), encoding='utf-8')
    return recipe


def package(recipe: dict, selected: list, profile: dict, work: Path, control) -> Path:
    if not selected:
        raise ValidationError('Select at least one shot')
    chosen = copy.deepcopy(recipe)
    chosen['shots'] = [shot for shot in chosen['shots'] if shot['shot_id'] in selected]
    if len(chosen['shots']) != len(set(selected)):
        raise ValidationError('Shot selection no longer matches the sequence')
    if profile.get('audio_required'):
        missing = [shot['shot_id'] for shot in chosen['shots'] if not shot.get('audio')]
        if missing:
            raise ValidationError('Audio is required for shots: ' + ', '.join(missing))
    source = chosen.pop('mocap_source', None)
    if source:
        from .mocap import attach
        source_file = Path(source['file'])
        if (not source_file.is_file() or source_file.stat().st_size != source['size']
                or source_file.stat().st_mtime_ns != source['mtime_ns']):
            raise ValidationError('FBX changed after inspection. Read FBX details again.')
        attach(chosen, source)
    settings = profile['export']
    if not settings['output_directory'].strip():
        raise ValidationError('Select an output directory')
    ffmpeg, ffprobe = executable(settings['ffmpeg']), executable(settings['ffprobe'])
    recipe_path = work / 'recipe.json'
    recipe_path.write_text(json.dumps(chosen, indent=2), encoding='utf-8')
    destination = Path(settings['output_directory']).expanduser() / work.name
    hook = None
    if source:
        from .mocap import export_shots
        mayapy = profile.get('maya', {}).get('mayapy', '')
        hook = lambda staging, manifest: export_shots(staging, manifest, mayapy, work)
    with active(control):
        return build(str(recipe_path), str(destination), settings['height'], ffmpeg,
                     ffprobe, stage_hook=hook)
