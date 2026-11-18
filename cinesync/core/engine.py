"""Validated package model and Maya-independent integer timecode math."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Callable


class ValidationError(ValueError):
    """Invalid or unsupported editorial/package data."""


def integer(value: object, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValidationError(f"{name} must be an integer >= {minimum}")
    return value


def validate_fps(value: object) -> int:
    if isinstance(value, bool):
        raise ValidationError("Boolean FPS is invalid")
    try:
        fps = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("Invalid FPS") from exc
    if not math.isfinite(fps) or not fps.is_integer() or not 1 <= fps <= 120:
        raise ValidationError("FPS must be an exact integer in 1..120; conform fractional/VFR media first")
    return int(fps)


def sample_stride(source_fps: int, output_fps: int, label: str) -> int:
    """Return the number of source samples per output frame."""
    if source_fps < output_fps or source_fps % output_fps:
        raise ValidationError(
            f"{label} is {source_fps} fps; expected an integer multiple of {output_fps} fps")
    return source_fps // output_fps


def smpte_to_frames(tc_str: str, fps: int) -> int:
    fps = validate_fps(fps)
    if not isinstance(tc_str, str) or not re.fullmatch(r"\d{2}:\d{2}:\d{2}:\d{2,3}", tc_str):
        raise ValidationError(f"Invalid non-drop SMPTE: {tc_str!r}")
    hours, minutes, seconds, frames = map(int, tc_str.split(":"))
    if hours >= 24 or minutes >= 60 or seconds >= 60 or frames >= fps:
        raise ValidationError(f"Out-of-range SMPTE: {tc_str}")
    return ((hours * 60 + minutes) * 60 + seconds) * fps + frames


def frames_to_smpte(total_frames: int, fps: int) -> str:
    fps = validate_fps(fps)
    if isinstance(total_frames, bool) or not isinstance(total_frames, int):
        raise ValidationError("Timecode requires integer frames")
    seconds, frame = divmod(total_frames % (86400 * fps), fps)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}:{frame:02}"


def safe_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValidationError("Package paths must be relative")
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise ValidationError(f"Path escapes package: {relative}") from exc
    return path


def image_offset(maya_frame: float, shot: dict) -> int:
    """Floor subframes and hold boundary images outside the shot."""
    return max(0, min(shot["duration_frames"] - 1,
                      math.floor(maya_frame) - shot["maya_start_frame"]))


def image_path(root: Path, camera: dict, offset: int) -> Path:
    pattern = camera["pattern"]
    token = re.search(r"#+", pattern)
    name = pattern.replace(token.group(), str(camera["start_frame"] + offset).zfill(len(token.group())))
    return safe_path(root, camera["folder"]) / name


def validate_manifest(data: dict, root: Path, check_files: bool = False) -> dict:
    if data.get("schema_version", 1) != 1:
        raise ValidationError("Unsupported manifest version")
    fps = validate_fps(data["fps"])
    for field in ("project", "sequence_name"):
        if not isinstance(data.get(field), str) or not data[field].strip():
            raise ValidationError(f"Missing {field}")
    if not isinstance(data.get("shots"), list) or not data["shots"]:
        raise ValidationError("No shots")
    ids = set()
    for shot in data["shots"]:
        sid = shot["shot_id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", sid) or sid in ids:
            raise ValidationError("Shot IDs must be unique filesystem-safe identifiers")
        ids.add(sid)
        start = integer(shot["timeline_in_frame"], "timeline in")
        end = integer(shot["timeline_out_frame"], "timeline out")
        duration = integer(shot["duration_frames"], "duration", 1)
        if end - start != duration or shot["maya_start_frame"] != 1001:
            raise ValidationError("Invalid duration or Maya anchor")
        smpte_to_frames(shot["source_timecode"], fps)
        if not isinstance(shot.get("cameras"), dict) or not shot["cameras"]:
            raise ValidationError("Shot has no cameras")
        for camera in shot["cameras"].values():
            integer(camera["start_frame"], "image start")
            if camera["type"] not in ("body", "face") or not camera["label"]:
                raise ValidationError("Invalid camera type/label")
            if not re.fullmatch(r"[A-Za-z0-9_.-]*#+[A-Za-z0-9_.-]*\.jpg", camera["pattern"]):
                raise ValidationError("Expected a safe JPEG pattern with one hash group")
            safe_path(root, camera["folder"])
            smpte_to_frames(camera.get("source_timecode", shot["source_timecode"]), fps)
            if check_files:
                for offset in range(duration):
                    if not image_path(root, camera, offset).is_file():
                        raise ValidationError(f"Missing image: {image_path(root, camera, offset)}")
        if shot.get("audio_track"):
            audio = safe_path(root, shot["audio_track"])
            if check_files and not audio.is_file():
                raise ValidationError(f"Missing audio: {audio}")
        mocap = shot.get('mocap', [])
        if not isinstance(mocap, list) or len(mocap) > 1:
            raise ValidationError('This version supports one mocap body per shot')
        for entry in mocap:
            if (entry.get('actor_id') != 'body'
                    or entry.get('maya_start_frame') != 1001
                    or type(entry.get('duration_frames')) is not int
                    or entry['duration_frames'] != duration
                    or validate_fps(entry.get('fps')) != fps):
                raise ValidationError('Invalid mocap actor, frame rate or shot range')
            path = safe_path(root, entry['file'])
            if path.suffix.lower() != '.fbx':
                raise ValidationError('Mocap assets must be FBX files')
            if check_files and (not path.is_file() or path.stat().st_size == 0):
                raise ValidationError(f'Missing or empty mocap FBX: {path}')
    return data


def load_manifest(path: str, check_files: bool = False) -> dict:
    file = Path(path).resolve()
    try:
        return validate_manifest(json.loads(file.read_text(encoding="utf-8")), file.parent, check_files)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValidationError(f"Malformed manifest: {exc}") from exc


class TimelineCallbacks:
    """Own all Maya callback IDs; imports Maya only when instantiated."""

    def __init__(self, on_time: Callable, on_scene: Callable) -> None:
        import maya.api.OpenMaya as om
        self.om = om
        self.ids = []
        try:
            self.ids.append(om.MEventMessage.addEventCallback("timeChanged", on_time))
            for event in (om.MSceneMessage.kBeforeNew, om.MSceneMessage.kBeforeOpen,
                          om.MSceneMessage.kMayaExiting):
                self.ids.append(om.MSceneMessage.addCallback(event, on_scene))
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        while self.ids:
            self.om.MMessage.removeCallback(self.ids.pop())
