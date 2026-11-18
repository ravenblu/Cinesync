"""Exact shot-to-FBX time mapping; no Maya or Qt dependency."""
from __future__ import annotations

from .engine import ValidationError, integer, sample_stride, smpte_to_frames, validate_fps
from .timecode import packed, rational, validate_timecode


def map_shot(shot: dict, fps: int, source: dict) -> dict:
    """Return the source-local frame interval, allowing one midnight rollover.

    The available FBX range ends one native sample after its last key. All
    ranges are half-open; the output has exactly duration_frames samples.
    """
    fps = validate_fps(fps)
    native = integer(source['native_fps'], 'FBX native FPS', 1)
    integer(source['take_index'], 'FBX Take index', 1)
    if not isinstance(source.get('take_name'), str) or not source['take_name']:
        raise ValidationError('Select an FBX Take')
    first = rational(source['first_frame'])
    last = rational(source['last_frame'])
    tc = validate_timecode(source.get('timecode'))
    if tc['fps'] != fps:
        raise ValidationError(
            f"{shot['shot_id']}: FBX timecode FPS is {tc['fps']}; expected {fps} fps "
            f"to match the video timecode and editorial rate ({source.get('file', source['take_name'])})")
    stride = sample_stride(native, fps, f"{shot['shot_id']}: FBX animation sampling rate")
    anchor = rational(tc['local_frame'])
    if anchor.denominator != 1:
        raise ValidationError(f"{shot['shot_id']}: FBX timecode anchor is off the native frame grid")
    if last < first or (last - first + 1) / native >= 86400:
        raise ValidationError('FBX must contain less than 24 hours of animation')
    if not first <= anchor <= last:
        raise ValidationError('FBX sync anchor is outside its animation range')
    tc_anchor = smpte_to_frames(tc['value'], fps)
    tc_shot = smpte_to_frames(shot['source_timecode'], fps)
    duration = integer(shot['timeline_out_frame'] - shot['timeline_in_frame'],
                       'shot duration', 1)
    length = duration * stride
    candidates = []
    for day in (-1, 0, 1):
        start = anchor + (tc_shot - tc_anchor + day * 86400 * fps) * stride
        if first <= start and start + length <= last + 1:
            candidates.append(start)
    if len(candidates) != 1:
        raise ValidationError(
            f"{shot['shot_id']}: FBX does not uniquely cover source TC "
            f"{shot['source_timecode']} for {duration} frames; check the sync anchor and Take")
    start = candidates[0]
    return {
        'actor_id': 'body',
        'file': f"mocap/{shot['shot_id']}/body.fbx",
        'maya_start_frame': 1001,
        'duration_frames': duration,
        'fps': fps,
        'source': dict(source, source_in_frame=packed(start),
                       source_out_frame=packed(start + length)),
    }
