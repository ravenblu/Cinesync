"""One-click synthetic end-to-end test; no downloads or Maya required."""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import traceback
import wave
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from cinesync.core.engine import validate_fps
FPS = validate_fps(json.loads(Path(__file__).with_name('config.json').read_text())['fps'])
if FPS not in (24, 25, 30, 48, 50, 60):
    raise ValueError('Synthetic test FPS must be 24, 25, 30, 48, 50 or 60')
DIGITS = (
    '111101101101111', '010110010010111', '111001111100111',
    '111001111001111', '101101111001001', '111100111001111',
    '111100111101111', '111001001001001', '111101111101111',
    '111101111001111',
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def executable(name: str, override: str = None) -> str:
    if override:
        found = shutil.which(override)
        if found:
            return found
        raise RuntimeError(f"Executable not found: {override}")
    suffix = '.exe' if os.name == 'nt' else ''
    local = ROOT / 'tools' / 'bin' / (name + suffix)
    if local.is_file():
        return str(local)
    return shutil.which(name) or ''


def frame_pixels(frame: int, camera: int) -> bytes:
    """320x180 RGB: decimal source frame plus seven machine-readable bits."""
    width, height = 320, 180
    color = [(36, 64, 104), (45, 95, 50), (110, 45, 70)][camera]
    pixels = bytearray(bytes(color) * (width * height))

    def box(x: int, y: int, w: int, h: int, rgb: tuple) -> None:
        row = bytes(rgb) * w
        for yy in range(y, y + h):
            offset = (yy * width + x) * 3
            pixels[offset:offset + len(row)] = row

    for digit_index, digit in enumerate(f'{frame:03}'):
        for index, on in enumerate(DIGITS[int(digit)]):
            if on == '1':
                box(30 + digit_index * 70 + index % 3 * 14,
                    20 + index // 3 * 14, 12, 12, (255, 255, 255))
    for bit in range(7):
        value = 240 if frame & (1 << bit) else 12
        box(20 + bit * 40, 135, 24, 24, (value, value, value))
    # Flash exactly when audio pulses begin (every second).
    if frame % FPS == 0:
        box(270, 15, 35, 90, (255, 255, 255))
    return bytes(pixels)


def make_media(folder: Path, ffmpeg: str) -> None:
    for camera in range(3):
        print(f'  Generating camera {camera + 1}/3...', flush=True)
        command = [ffmpeg, '-nostdin', '-v', 'error', '-f', 'rawvideo',
                   '-pixel_format', 'rgb24', '-video_size', '320x180',
                   '-framerate', str(FPS), '-i', 'pipe:0', '-an', '-c:v', 'png', '-timecode', '15:14:25:00', '-write_tmcd', '1',
                   str(folder / f'cam{camera}.mov')]
        with tempfile.TemporaryFile() as errors:
            with subprocess.Popen(command, stdin=subprocess.PIPE, stderr=errors) as process:
                try:
                    for frame in range(90):
                        process.stdin.write(frame_pixels(frame, camera))
                    process.stdin.close()
                    code = process.wait(timeout=120)
                except BaseException:
                    process.kill()
                    process.wait()
                    raise
                errors.seek(0)
                require(code == 0, errors.read().decode(errors='replace'))
    samples = bytearray()
    for sample in range(round(90 * 48000 / FPS)):
        phase = sample % 48000
        value = int(16000 * math.sin(2 * math.pi * 1000 * sample / 48000)) if phase < 2400 else 0
        samples.extend(struct.pack('<h', value))
    with wave.open(str(folder / 'sync.wav'), 'wb') as audio:
        audio.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
        audio.writeframes(samples)


def make_xml(folder: Path) -> tuple:
    root = ET.Element('xmeml', version='5')
    sequence = ET.SubElement(root, 'sequence')
    ET.SubElement(sequence, 'name').text = 'CineSync_SelfTest'
    rate = ET.SubElement(sequence, 'rate')
    ET.SubElement(rate, 'timebase').text = str(FPS)
    ET.SubElement(rate, 'ntsc').text = 'FALSE'
    media = ET.SubElement(sequence, 'media')
    for kind, paths in [('video', [folder / f'cam{i}.mov' for i in range(3)]),
                        ('audio', [folder / 'sync.wav'])]:
        group = ET.SubElement(media, kind)
        for index, path in enumerate(paths):
            clip = ET.SubElement(ET.SubElement(group, 'track'), 'clipitem')
            for tag, value in [('start', 0), ('end', 90), ('in', 0), ('out', 90)]:
                ET.SubElement(clip, tag).text = str(value)
            file = ET.SubElement(clip, 'file', id=f'{kind}{index}')
            ET.SubElement(file, 'pathurl').text = path.as_uri()
            rate = ET.SubElement(file, 'rate')
            ET.SubElement(rate, 'timebase').text = str(FPS)
            timecode = ET.SubElement(file, 'timecode')
            ET.SubElement(timecode, 'string').text = '15:14:25:00'
    xml = folder / 'edit.xml'
    ET.ElementTree(root).write(xml, encoding='utf-8', xml_declaration=True)
    config = json.loads((ROOT / 'examples' / 'tracks.json').read_text(encoding='utf-8'))
    config['sequence_name'] = 'CineSync_SelfTest'
    config['shots'][0].update(timeline_in_frame=10, timeline_out_frame=70)
    config_path = folder / 'tracks.json'
    config_path.write_text(json.dumps(config, indent=2), encoding='utf-8')
    return xml, config_path


def verify_package(manifest_path: Path, ffmpeg: str) -> None:
    from cinesync.core.engine import load_manifest, image_path
    data = load_manifest(str(manifest_path), check_files=True)
    shot = data['shots'][0]
    require(shot['duration_frames'] == 60, 'Wrong shot duration')
    require(shot['source_timecode'] == '15:14:25:10', 'Wrong source timecode')
    # Independently read the binary frame counter after lossy JPEG encoding.
    for camera in shot['cameras'].values():
        require(len(list((manifest_path.parent / camera['folder']).glob('*.jpg'))) == 60,
                'Wrong output image count')
        for offset in (0, 1, 20, 59):
            image = image_path(manifest_path.parent, camera, offset)
            result = subprocess.run([ffmpeg, '-nostdin', '-v', 'error', '-i', str(image),
                                     '-vf', 'scale=320:180', '-f', 'rawvideo',
                                     '-pix_fmt', 'rgb24', 'pipe:1'], capture_output=True, check=True)
            require(len(result.stdout) == 320 * 180 * 3, 'Wrong decoded image size')
            decoded = sum((1 << bit) for bit in range(7)
                          if result.stdout[(147 * 320 + 32 + bit * 40) * 3] > 128)
            require(decoded == offset + 10, f'Frame mismatch: expected {offset + 10}, got {decoded}')
    result = subprocess.run([ffmpeg, '-nostdin', '-v', 'error', '-i',
                             str(manifest_path.parent / shot['audio_track']),
                             '-f', 's16le', '-ac', '1', 'pipe:1'], capture_output=True, check=True)
    samples = [value[0] for value in struct.iter_unpack('<h', result.stdout)]
    require(len(samples) == round(70 * 48000 / FPS) - round(10 * 48000 / FPS), 'Wrong audio sample count')
    first = next((i for i, value in enumerate(samples) if abs(value) > 500), -1)
    require(48000 - round(10 * 48000 / FPS) <= first <= 48004 - round(10 * 48000 / FPS), f'Audio pulse misaligned: sample {first}')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ffmpeg')
    parser.add_argument('--ffprobe')
    args = parser.parse_args()
    folder = ROOT / 'test_runs' / datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    folder.mkdir(parents=True)
    report = {'status': 'FAIL', 'checks': [], 'folder': str(folder),
              'not_tested': ['Real Premiere export', 'Maya host (run Maya test separately)',
                             '60 fps performance and long-run memory stability']}
    exit_code = 1
    try:
        print('[1/4] Running unit tests...', flush=True)
        unit = subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
                              cwd=str(ROOT), capture_output=True, text=True)
        (folder / 'unit-tests.log').write_text(unit.stdout + unit.stderr, encoding='utf-8')
        require(unit.returncode == 0, 'Unit tests failed; see unit-tests.log')
        report['checks'].append('PASS: unit tests')
        ffmpeg, ffprobe = executable('ffmpeg', args.ffmpeg), executable('ffprobe', args.ffprobe)
        if not ffmpeg or not ffprobe:
            report['status'] = 'BLOCKED'
            raise RuntimeError('FFmpeg/ffprobe missing. Put both executables in tools/bin, '
                               'or install them on PATH, then double-click again. See docs/START_HERE.md.')
        for tool in (ffmpeg, ffprobe):
            version = subprocess.run([tool, '-version'], capture_output=True, text=True, check=True)
            report.setdefault('versions', []).append(version.stdout.splitlines()[0])
        print('[2/4] Generating numbered videos and sync audio...', flush=True)
        make_media(folder, ffmpeg)
        xml, config = make_xml(folder)
        print('[3/4] Converting XML and packaging real JPEG/WAV media...', flush=True)
        from cinesync.pipeline.premiere_xml import convert
        from cinesync.pipeline.export_prep import build
        recipe = convert(str(xml), str(config))
        recipe_path = folder / 'recipe.json'
        recipe_path.write_text(json.dumps(recipe, indent=2), encoding='utf-8')
        manifest = build(str(recipe_path), str(folder / 'package'), ffmpeg=ffmpeg, ffprobe=ffprobe)
        report['checks'].append('PASS: synthetic XML -> real FFmpeg package')
        print('[4/4] Checking image counters and audio sample alignment...', flush=True)
        verify_package(manifest, ffmpeg)
        report['checks'].append('PASS: three cameras, first/last/middle frames, audio alignment')
        launcher = ('import sys\n'
                    f'sys.path.insert(0, {str(ROOT)!r})\n'
                    'import importlib\n'
                    'from tests.manual import maya_self_test\n'
                    'importlib.reload(maya_self_test)\n'
                    f'maya_self_test.show({str(manifest)!r})\n')
        (folder / 'RUN_IN_MAYA.py').write_text(launcher, encoding='utf-8')
        report['status'] = 'PASS'
        exit_code = 0
    except Exception as exc:
        report['error'] = str(exc)
        (folder / 'error.log').write_text(traceback.format_exc(), encoding='utf-8')
        print(f'\n{report["status"]}: {exc}', flush=True)
        exit_code = 2 if report['status'] == 'BLOCKED' else 1
    finally:
        report_path = folder / 'report.json'
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
        lines = [f'CineSync self-test: {report["status"]}', *report['checks'],
                 report.get('error', ''), '', 'Not tested:', *report['not_tested'],
                 '', ('Maya: open RUN_IN_MAYA.py in Maya Python Script Editor and execute.'
                      if report['status'] == 'PASS' else 'Maya package was not generated by this run.'),
                 f'Report: {report_path}']
        (folder / 'REPORT.txt').write_text('\n'.join(lines), encoding='utf-8')
        print('\n' + '\n'.join(lines), flush=True)
    return exit_code


if __name__ == '__main__':
    sys.exit(main())
