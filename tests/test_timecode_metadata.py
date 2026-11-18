"""Embedded metadata is required; editorial placement cannot override it."""
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from types import ModuleType

from cinesync.core.engine import ValidationError
from cinesync.core.mocap import map_shot
from cinesync.core.timecode import validate_timecode
from cinesync.pipeline.export_prep import video_timecode, verify_camera_timecodes


def video_probe(tc='15:14:20:00', video_rate='30/1', tc_rate='30/1'):
    return {'streams': [
        {'index': 0, 'codec_type': 'video', 'avg_frame_rate': video_rate,
         'tags': {'timecode': tc}},
        {'index': 1, 'codec_type': 'data', 'codec_tag_string': 'tmcd',
         'avg_frame_rate': tc_rate, 'tags': {'timecode': tc}},
    ]}


class MetadataTests(unittest.TestCase):
    def test_fbx_reader_requires_unique_complete_metadata(self):
        from cinesync.adapters.maya.fbx_worker import embedded_timecode
        maya = ModuleType('maya')
        maya.cmds = Mock()
        values = {'cineSyncSourceTimecode': '15:14:20:00',
                  'cineSyncTimecodeFps': 30.0, 'cineSyncAnchorFrame': 0.0,
                  'cineSyncTimecodeVersion': 1.0}
        maya.cmds.attributeQuery.side_effect = lambda field, **kwargs: field in values
        maya.cmds.getAttr.side_effect = lambda field: values[field.split('.')[-1]]
        with patch.dict(sys.modules, {'maya': maya, 'maya.cmds': maya.cmds}):
            self.assertEqual(embedded_timecode(['root'])['value'], '15:14:20:00')
            with self.assertRaisesRegex(ValueError, 'ambiguous'):
                embedded_timecode(['root', 'second_root'])
            values.pop('cineSyncAnchorFrame')
            with self.assertRaisesRegex(ValueError, 'Incomplete'):
                embedded_timecode(['root'])
            values.clear()
            with self.assertRaisesRegex(ValueError, 'Missing'):
                embedded_timecode(['root'])

    def test_video_requires_tmcd_and_rejects_conflicting_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / 'source.mov'
            media.touch()
            data = video_probe()
            track = data['streams'].pop()
            with patch('cinesync.pipeline.export_prep.probe', return_value=data):
                with self.assertRaisesRegex(ValidationError, 'Missing embedded tmcd'):
                    video_timecode(media, 30, 'ffprobe')
                data['streams'].append(track)
                tc = video_timecode(media, 30, 'ffprobe')
                self.assertEqual(tc['value'], '15:14:20:00')
                self.assertEqual(tc['evidence'][0]['origin'], 'tmcd:1')
                self.assertEqual(len(tc['evidence']), 2)
                data['format'] = {'tags': {'timecode': '15:14:21:00'}}
                with self.assertRaisesRegex(ValidationError, 'Conflicting embedded'):
                    video_timecode(media, 30, 'ffprobe')

    def test_video_and_timecode_rates_are_independently_validated(self):
        cases = [
            (video_probe(video_rate='24/1'), '24 fps; expected an integer multiple of 30'),
            (video_probe(video_rate='50/1'), '50 fps; expected an integer multiple of 30'),
            (video_probe(video_rate='30000/1001'), 'Video recording rate is 30000/1001 fps'),
            (video_probe(tc_rate='24/1'), 'Timecode rate on tmcd:1 is 24 fps; expected 30'),
            (video_probe(tc_rate='30000/1001'), 'Timecode rate on tmcd:1 is 30000/1001 fps'),
            (video_probe(tc_rate='0/0'), 'Timecode rate on tmcd:1 is unreadable'),
            (video_probe(tc='15:14:20;00'), 'Invalid non-drop timecode on tmcd:1'),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / 'source.mov'
            media.touch()
            for data, error in cases:
                with self.subTest(error=error), patch(
                        'cinesync.pipeline.export_prep.probe', return_value=data):
                    with self.assertRaisesRegex(ValidationError, error):
                        video_timecode(media, 30, 'ffprobe')

    def test_editorial_offset_midnight_conflict_and_camera_mismatch(self):
        recipe = {'fps': 30, 'shots': [{
            'shot_id': 'SH001', 'source_timecode': '00:00:00:00',
            'cameras': {
                key: {'media': key + '.mov', 'source_in_frame': 30,
                      'source_timecode': '00:00:00:00'} for key in ('body', 'face')}}]}
        def probe(path, tool):
            return video_probe(tc='23:59:59:00', video_rate='60/1' if path.stem == 'face' else '30/1')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            face = recipe['shots'][0]['cameras']['face']
            face.update(source_fps=60, source_in_frame=60)
            for key in ('body', 'face'):
                (root / (key + '.mov')).touch()
            with patch('cinesync.pipeline.export_prep.probe', side_effect=probe):
                verify_camera_timecodes(recipe, root, 'ffprobe')
                self.assertIn('timecode_metadata', recipe['shots'][0]['cameras']['face'])
                bad = copy.deepcopy(recipe)
                bad['shots'][0]['cameras']['face']['source_timecode'] = '00:00:00:01'
                with self.assertRaisesRegex(ValidationError, 'XML TC'):
                    verify_camera_timecodes(bad, root, 'ffprobe')
                bad['shots'][0]['cameras']['face']['source_in_frame'] = 61
                with self.assertRaisesRegex(ValidationError, 'off the 30 fps timecode grid'):
                    verify_camera_timecodes(bad, root, 'ffprobe')
                bad['shots'][0]['cameras']['face']['source_in_frame'] = 62
                with self.assertRaisesRegex(ValidationError, 'by 1 frames'):
                    verify_camera_timecodes(bad, root, 'ffprobe')
                (root / 'face.mov').write_bytes(b'changed')
                with self.assertRaisesRegex(ValidationError, 'changed after inspection'):
                    verify_camera_timecodes(recipe, root, 'ffprobe')

    def test_fbx_missing_metadata_manual_anchor_and_wrong_rate_rejected(self):
        source = {'native_fps': 120, 'take_index': 1, 'take_name': 'Take',
                  'first_frame': 0, 'last_frame': 1199,
                  'source_timecode_at_anchor': '15:14:20:00', 'local_frame_at_anchor': 0}
        shot = {'shot_id': 'SH001', 'source_timecode': '15:14:25:00',
                'timeline_in_frame': 0, 'timeline_out_frame': 150}
        with self.assertRaisesRegex(ValidationError, 'embedded FBX'):
            map_shot(shot, 30, source)
        source['timecode'] = {'version': 1, 'value': '15:14:20:00', 'fps': 25,
                              'local_frame': 0, 'origin': 'fbx:cinesync-v1:root'}
        with self.assertRaisesRegex(ValidationError, 'FPS'):
            map_shot(shot, 30, source)
        source['timecode']['fps'] = 30
        self.assertEqual(map_shot(shot, 30, source)['maya_start_frame'], 1001)
        for value in (None, {}, {'version': 1}, dict(source['timecode'], value='invalid')):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                validate_timecode(value)
