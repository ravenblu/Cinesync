"""Restricted Premiere FCP7 XML adapter; explicit track roles and shot ranges."""
from __future__ import annotations

import sys

import argparse
import json
import re
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path
from urllib.parse import unquote, urlparse

if __name__ == "__main__" and not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cinesync.core.engine import (ValidationError, frames_to_smpte, sample_stride,
                                 smpte_to_frames, validate_fps)


def convert(xml_path: str, config_path: str) -> dict:
    tree = ET.parse(xml_path)
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    sequences = [s for s in tree.findall(".//sequence")
                 if s.findtext("name") == config["sequence_name"] and s.find("media") is not None]
    if len(sequences) != 1:
        raise ValidationError("Select one uniquely named sequence")
    sequence = sequences[0]
    fps = validate_fps(sequence.findtext("rate/timebase"))
    if sequence.findtext("rate/ntsc", "FALSE").upper() != "FALSE":
        raise ValidationError("Fractional sequence rate: conform before export")
    files = {f.get("id"): f for f in tree.findall(".//file") if f.find("pathurl") is not None}
    def resolve_track(kind: str, role: dict) -> int:
        tracks = sequence.findall(f"media/{kind}/track")
        if "track_name" in role:
            name = role["track_name"]
            matches = [i + 1 for i, track in enumerate(tracks)
                       if track.findtext("name") == name]
            if len(matches) != 1:
                raise ValidationError(
                    f"Expected one {kind} track named {name!r}; "
                    "this Premiere XML may omit track names, use a track index")
            return matches[0]
        number = role.get("track")
        if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= len(tracks):
            raise ValidationError(f"Invalid {kind} track index: {number!r}")
        return number

    def shot_specs() -> list:
        if "shots" in config:
            return config["shots"]
        detection = config.get("shot_detection", {})
        if detection.get("mode") != "primary_track_clips":
            raise ValidationError("Configure shots or shot_detection.mode=primary_track_clips")
        primary = config["cameras"][config["primary_camera"]]
        index = resolve_track("video", primary)
        track = sequence.findall("media/video/track")[index - 1]
        if track.findall("transitionitem"):
            raise ValidationError("Primary track transitions cannot define shot boundaries")
        spans = []
        for clip in track.findall("clipitem"):
            if clip.findtext("enabled", "TRUE").upper() == "FALSE":
                continue
            start, end = int(clip.findtext("start")), int(clip.findtext("end"))
            if not 0 <= start < end:
                raise ValidationError("Invalid primary track clip bounds")
            spans.append((start, end))
        spans.sort()
        if not spans or any(right > following for (_, right), (following, _) in zip(spans, spans[1:])):
            raise ValidationError("Primary track has no clips or overlapping clips")
        prefix = detection.get("shot_id_prefix", "SH")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", prefix):
            raise ValidationError("Invalid shot ID prefix")
        return [{"shot_id": f"{prefix}{i:03}", "timeline_in_frame": start,
                 "timeline_out_frame": end}
                for i, (start, end) in enumerate(spans, 1)]


    def extract(kind: str, track_number: int, start: int, end: int) -> dict:
        tracks = sequence.findall(f"media/{kind}/track")
        if not isinstance(track_number, int) or not 1 <= track_number <= len(tracks):
            raise ValidationError(f"Missing {kind} track {track_number}")
        track = tracks[track_number - 1]
        if track.findtext("enabled", "TRUE").upper() == "FALSE" or track.findall("transitionitem"):
            raise ValidationError("Disabled tracks and transitions are unsupported")
        clips = []
        for clip in track.findall("clipitem"):
            left, right = int(clip.findtext("start")), int(clip.findtext("end"))
            if left < 0 or right < 0:
                raise ValidationError("Transition-dependent clip bounds are unsupported")
            if left < end and right > start:
                clips.append(clip)
        if len(clips) != 1:
            raise ValidationError(f"Shot must be covered by one clip on {kind} track {track_number}")
        clip = clips[0]
        left, right = int(clip.findtext("start")), int(clip.findtext("end"))
        if left > start or right < end or clip.findtext("enabled", "TRUE").upper() == "FALSE":
            raise ValidationError("Clip does not cover shot")
        if clip.find("sequence") is not None or clip.find("filter") is not None:
            raise ValidationError("Nested clips/effects/retiming must be baked or removed")
        source_in, source_out = int(clip.findtext("in")), int(clip.findtext("out"))
        file = clip.find("file")
        if file is None or file.get("id") not in files:
            raise ValidationError("Clip has no resolvable media file")
        file = files[file.get("id")]
        source_rate = file.findtext("rate/timebase")
        source_ntsc = file.findtext("rate/ntsc", "FALSE").upper()
        media_url = file.findtext("pathurl")
        try:
            declared_rate = validate_fps(source_rate)
        except ValidationError as exc:
            raise ValidationError(
                f"XML {kind} track {track_number} declares rate {source_rate!r}; "
                f"expected {fps} fps: {media_url}") from exc
        if source_ntsc != "FALSE" or (kind == "audio" and declared_rate != fps):
            raise ValidationError(
                f"XML {kind} track {track_number} declares {source_rate} fps "
                f"(NTSC={source_ntsc}); expected {fps} fps non-NTSC: {media_url}")
        url = urlparse(media_url)
        if url.scheme != "file" or url.netloc not in ("", "localhost"):
            raise ValidationError("Only local file URLs are supported")
        path = unquote(url.path)
        if len(path) > 3 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        label = f"XML {kind} track {track_number}: {path}"
        stride = sample_stride(declared_rate, fps, label)
        clip_rate = validate_fps(clip.findtext("rate/timebase", str(declared_rate)))
        if clip.findtext("rate/ntsc", "FALSE").upper() != "FALSE":
            raise ValidationError(f"Fractional clip frame rate: {label}")
        if source_in < 0 or Fraction(source_out - source_in, clip_rate) != Fraction(right - left, fps):
            raise ValidationError(f"Retimed clip or inconsistent in/out range: {label}")
        offset = Fraction(source_in * declared_rate, clip_rate) + (start - left) * stride
        if offset.denominator != 1 or int(offset) % stride:
            raise ValidationError(f"Clip source in does not align to the {fps} fps timecode grid: {label}")
        result = {"media": path, "source_in_frame": int(offset)}
        if kind == "video":
            tc = file.findtext("timecode/string")
            if tc is None:
                raise ValidationError("Missing source timecode; supply/bake audited media metadata")
            tc_rate = validate_fps(file.findtext("timecode/rate/timebase", str(fps)))
            if tc_rate != fps or file.findtext("timecode/rate/ntsc", "FALSE").upper() != "FALSE":
                raise ValidationError(
                    f"XML timecode declares {tc_rate} fps; expected {fps} fps non-NTSC: {label}")
            result.update(source_fps=declared_rate, source_timecode=frames_to_smpte(
                smpte_to_frames(tc, fps) + int(offset) // stride, fps))
        return result

    result = {"project": config["project"], "sequence_name": config["sequence_name"],
              "fps": fps, "shots": []}
    for spec in shot_specs():
        start, end = spec["timeline_in_frame"], spec["timeline_out_frame"]
        if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end:
            raise ValidationError("Invalid shot range")
        shot = dict(spec, cameras={})
        for key, role in config["cameras"].items():
            camera = extract("video", resolve_track("video", role), start, end)
            camera.update(label=role["label"], type=role["type"])
            shot["cameras"][key] = camera
        shot["source_timecode"] = shot["cameras"][config["primary_camera"]]["source_timecode"]
        if config.get("audio_track"):
            audio_role = config["audio_track"]
            audio_index = resolve_track("audio", audio_role if isinstance(audio_role, dict) else {"track": audio_role})
            shot["audio"] = extract("audio", audio_index, start, end)
        result["shots"].append(shot)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xml")
    parser.add_argument("config")
    parser.add_argument("output")
    args = parser.parse_args()
    try:
        recipe = convert(args.xml, args.config)
        with open(args.output, "x", encoding="utf-8") as output:
            json.dump(recipe, output, ensure_ascii=False, indent=2)
    except (ValidationError, OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"XML import failed: {exc}\n")
