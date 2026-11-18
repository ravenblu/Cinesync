"""Build an atomic CineSync package from an explicit editorial recipe."""
from __future__ import annotations

import sys

import argparse
import copy
import json
import re
import shutil
import subprocess
import tempfile
from fractions import Fraction
from pathlib import Path
from typing import Callable, Optional

if __name__ == "__main__" and not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cinesync.core.engine import (ValidationError, integer, validate_fps,
                             validate_manifest, smpte_to_frames, frames_to_smpte, sample_stride)


def run(args: list) -> str:
    from cinesync.pipeline.control import current
    control = current()
    control.check()
    with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True) as process, control.watch(process):
        output, errors = process.communicate()
    if process.returncode:
        raise ValidationError(f"{args[0]} failed: {errors[-4000:]}")
    return output


def probe(path: Path, ffprobe: str) -> dict:
    return json.loads(run([ffprobe, "-v", "error", "-show_streams", "-show_format",
                           "-of", "json", str(path)]))


def read_media_rate(stream: dict, label: str, path: Path) -> int:
    raw = stream.get('avg_frame_rate')
    try:
        rate = Fraction(raw)
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        raise ValidationError(
            f'{label} is unreadable ({raw!r}): {path}') from exc
    if rate <= 0:
        raise ValidationError(f'{label} is unreadable ({raw!r}): {path}')
    if rate.denominator != 1:
        raise ValidationError(f'{label} is {rate} fps; expected an integer fps: {path}')
    return int(rate)


def video_stream(data: dict, path: Path) -> dict:
    stream = next((s for s in data.get('streams', []) if s.get('codec_type') == 'video'), None)
    if stream is None:
        raise ValidationError(f'No video stream: {path}')
    return stream


def video_timecode(path: Path, fps: int, ffprobe: str) -> dict:
    before = path.stat()
    data = probe(path, ffprobe)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValidationError(f'Video changed during timecode inspection: {path}')
    video = video_stream(data, path)
    recording_fps = read_media_rate(video, 'Video recording rate', path)
    sample_stride(recording_fps, fps, f'Video recording rate for {path}')
    tracks = [stream for stream in data['streams'] if stream.get('codec_tag_string') == 'tmcd']
    if not tracks:
        raise ValidationError(
            f'Missing embedded tmcd timecode track with a readable counting rate: {path}')

    evidence = []

    def read_tags(container: dict, origin: str) -> list:
        tags = []
        for key, value in container.get('tags', {}).items():
            if key.lower() != 'timecode':
                continue
            try:
                canonical = frames_to_smpte(smpte_to_frames(value, fps), fps)
            except ValidationError as exc:
                raise ValidationError(f'Invalid non-drop timecode on {origin}: {path}: {exc}') from exc
            tags.append({'origin': origin, 'value': canonical})
        return tags

    for track in tracks:
        origin = f"tmcd:{track.get('index', '?')}"
        rate = read_media_rate(track, f'Timecode rate on {origin}', path)
        if rate != fps:
            raise ValidationError(f'Timecode rate on {origin} is {rate} fps; expected {fps} fps: {path}')
        tags = read_tags(track, origin)
        if not tags:
            raise ValidationError(f'Missing timecode value on {origin}: {path}')
        evidence.extend(tags)
    evidence.extend(read_tags(video, f"stream:{video.get('index', '?')}"))
    evidence.extend(read_tags(data.get('format', {}), 'format'))
    if len({item['value'] for item in evidence}) != 1:
        details = ', '.join(f"{item['origin']}={item['value']}" for item in evidence)
        raise ValidationError(f'Conflicting embedded video timecodes ({details}): {path}')
    return {'value': evidence[0]['value'], 'fps': fps, 'evidence': evidence,
            'recording_fps': recording_fps, 'size': after.st_size, 'mtime_ns': after.st_mtime_ns}


def verify_camera_timecodes(recipe: dict, recipe_root: Path, ffprobe: str) -> None:
    fps = validate_fps(recipe['fps'])
    inspected = {}
    for shot in recipe['shots']:
        expected = smpte_to_frames(shot['source_timecode'], fps)
        for key, camera in shot['cameras'].items():
            media = (recipe_root / camera['media']).resolve()
            if media not in inspected:
                inspected[media] = video_timecode(media, fps, ffprobe)
            tc = inspected[media]
            source_fps = validate_fps(camera.get('source_fps', fps))
            if source_fps != tc['recording_fps']:
                raise ValidationError(
                    f"{shot['shot_id']} / {key}: video is {tc['recording_fps']} fps; "
                    f"XML or recipe declares {source_fps} fps: {media}")
            stride = sample_stride(source_fps, fps, f"{shot['shot_id']} / {key} recording rate")
            offset, remainder = divmod(integer(camera['source_in_frame'], 'source in'), stride)
            if remainder:
                raise ValidationError(f"{shot['shot_id']} / {key}: source in is off the {fps} fps timecode grid")
            previous = camera.get('timecode_metadata')
            if previous and any(previous.get(key) != tc[key]
                                for key in ('size', 'mtime_ns', 'value', 'fps', 'recording_fps')):
                raise ValidationError(f'Video changed after inspection: {media}')
            actual = (smpte_to_frames(tc['value'], fps) + offset) % (86400 * fps)
            declared = smpte_to_frames(camera['source_timecode'], fps)
            if actual != declared:
                raise ValidationError(
                    f"{shot['shot_id']} / {key}: XML TC {camera['source_timecode']} "
                    f"disagrees with embedded TC {frames_to_smpte(actual, fps)}")
            if actual != expected:
                delta = (actual - expected + 43200 * fps) % (86400 * fps) - 43200 * fps
                raise ValidationError(
                    f"{shot['shot_id']} / {key}: TC {frames_to_smpte(actual, fps)} "
                    f"differs from primary TC {shot['source_timecode']} by {delta} frames")
            camera['timecode_metadata'] = copy.deepcopy(tc)


def verify_video(path: Path, fps: int, ffprobe: str) -> None:
    """Inspect every decoded timestamp: reject VFR instead of trusting averages."""
    from cinesync.pipeline.control import current
    control = current()
    control.check()
    video = video_stream(probe(path, ffprobe), path)
    actual = read_media_rate(video, 'Video recording rate', path)
    if actual != fps:
        raise ValidationError(f'Video recording rate is {actual} fps; expected {fps} fps: {path}')
    args = [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_frames",
            "-show_entries", "frame=best_effort_timestamp", "-of", "csv=p=0", str(path)]
    timebase = Fraction(video["time_base"])
    previous = None
    count = 0
    with tempfile.TemporaryFile(mode="w+") as errors:
        with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=errors, text=True) as process, control.watch(process):
            try:
                for line in process.stdout:
                    value = line.strip().split(",")[0]
                    if not value:
                        continue
                    stamp = int(value)
                    if previous is not None:
                        interval = (stamp - previous) * timebase
                        if interval <= 0 or abs(interval - Fraction(1, fps)) > timebase:
                            raise ValidationError(
                                f"VFR or discontinuous timestamps; expected {fps} fps: {path}")
                    previous = stamp
                    count += 1
            except Exception:
                process.kill()
                raise
            if process.wait() or not count:
                raise ValidationError(f"Cannot inspect video timestamps: {path}")


def build(recipe_path: str, output: str, height: int = 720,
          ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe",
          stage_hook: Optional[Callable[[Path, dict], None]] = None) -> Path:
    from cinesync.pipeline.control import current
    current().check()
    recipe_file = Path(recipe_path).resolve()
    recipe = json.loads(recipe_file.read_text(encoding="utf-8"))
    fps = validate_fps(recipe["fps"])
    if height not in (720, 1080):
        raise ValidationError("Height must be 720 or 1080")
    destination = Path(output).resolve()
    if destination.exists():
        raise ValidationError("Output already exists; choose a new package directory")
    verify_camera_timecodes(recipe, recipe_file.parent, ffprobe)
    manifest = copy.deepcopy(recipe)
    manifest["schema_version"] = 1
    jobs = []
    videos = {}
    for shot in manifest["shots"]:
        shot["maya_start_frame"] = 1001
        shot["duration_frames"] = shot["timeline_out_frame"] - shot["timeline_in_frame"]
        sid = shot["shot_id"]
        for key, camera in shot["cameras"].items():
            if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
                raise ValidationError("Unsafe camera ID")
            media = (recipe_file.parent / camera.pop("media")).resolve()
            start = integer(camera.pop("source_in_frame"), "source in")
            source_fps = validate_fps(camera.get('source_fps', fps))
            camera.update(source_media=str(media), source_in_frame=start, source_fps=source_fps)
            camera.update(folder=f"footage/{sid}/{key}", pattern="frame.######.jpg", start_frame=1)
            jobs.append(("video", media, start, shot, camera))
            videos[media] = source_fps
        audio = shot.pop("audio", None)
        shot["audio_track"] = f"audio/{sid}.wav" if audio else None
        if audio:
            start = integer(audio["source_in_frame"], "audio source in")
            jobs.append(("audio", (recipe_file.parent / audio["media"]).resolve(), start, shot, None))
    validate_manifest(manifest, destination)
    for _, media, _, _, _ in jobs:
        if not media.is_file():
            raise ValidationError(f"Missing source: {media}")
    for media, source_fps in videos.items():
        verify_video(media, source_fps, ffprobe)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".cinesync-", dir=str(destination.parent)))
    try:
        for kind, media, start, shot, camera in jobs:
            current().check()
            duration = shot["duration_frames"]
            base = [ffmpeg, "-nostdin", "-v", "error", "-i", str(media)]
            if kind == "video":
                folder = staging / camera["folder"]
                folder.mkdir(parents=True)
                stride = sample_stride(camera["source_fps"], fps, "Video recording rate")
                stop = start + duration * stride
                filters = (f"trim=start_frame={start}:end_frame={stop},"
                           f"select=not(mod(n\\,{stride})),"
                           f"setpts=PTS-STARTPTS,scale=-2:{height}")
                run(base + ["-map", "0:v:0", "-an", "-vf", filters,
                            "-fps_mode", "passthrough", "-frames:v", str(duration),
                            "-q:v", "4", "-start_number", "1", str(folder / "frame.%06d.jpg")])
            else:
                target = staging / shot["audio_track"]
                target.parent.mkdir(exist_ok=True)
                # Trim on the resampled sample grid; no floating-point seek.
                first = round(Fraction(start * 48000, fps))
                last = round(Fraction((start + duration) * 48000, fps))
                filters = f"aresample=48000,atrim=start_sample={first}:end_sample={last},asetpts=PTS-STARTPTS"
                run(base + ["-map", "0:a:0", "-vn", "-af", filters,
                            "-c:a", "pcm_s24le", "-ar", "48000", str(target)])
                stream = probe(target, ffprobe)["streams"][0]
                actual = Fraction(stream["duration_ts"]) * Fraction(stream["time_base"])
                if stream["codec_name"] != "pcm_s24le" or int(stream["sample_rate"]) != 48000 or abs(actual - Fraction(last - first, 48000)) > Fraction(1, 48000):
                    raise ValidationError(f"Audio duration/format mismatch: {media}")
        if stage_hook is not None:
            current().check()
            stage_hook(staging, manifest)
        for shot in manifest['shots']:
            for camera in shot['cameras'].values():
                snapshot = camera['timecode_metadata']
                stat = Path(camera['source_media']).stat()
                if (stat.st_size, stat.st_mtime_ns) != (snapshot['size'], snapshot['mtime_ns']):
                    raise ValidationError('Video changed during Build: ' + camera['source_media'])
        validate_manifest(manifest, staging, check_files=True)
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        if destination.exists():
            raise ValidationError("Output appeared during export")
        current().check()
        staging.rename(destination)
    except BaseException:
        shutil.rmtree(staging)
        raise
    return destination / "manifest.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recipe")
    parser.add_argument("output")
    parser.add_argument("--height", type=int, choices=(720, 1080), default=720)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    args = parser.parse_args()
    try:
        print(build(args.recipe, args.output, args.height, args.ffmpeg, args.ffprobe))
    except (ValidationError, OSError, KeyError, ValueError) as exc:
        parser.exit(2, f"Export failed: {exc}\n")


if __name__ == "__main__":
    main()
