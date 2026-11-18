"""Verify synthetic fixture and honest blocked-report behavior."""
import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.manual import self_test
from cinesync.pipeline.premiere_xml import convert


class SelfTestTests(unittest.TestCase):
    def test_generated_counter_matches_every_source_frame(self):
        for frame in range(90):
            pixels = self_test.frame_pixels(frame, 0)
            self.assertEqual(len(pixels), 320 * 180 * 3)
            decoded = sum(1 << bit for bit in range(7)
                          if pixels[(147 * 320 + 32 + bit * 40) * 3] > 128)
            self.assertEqual(decoded, frame)

    def test_fixture_has_nonzero_trim_and_all_cameras(self):
        with tempfile.TemporaryDirectory() as tmp:
            xml, config = self_test.make_xml(Path(tmp))
            recipe = convert(str(xml), str(config))
            shot = recipe['shots'][0]
            self.assertEqual(shot['timeline_out_frame'] - shot['timeline_in_frame'], 60)
            self.assertEqual(shot['source_timecode'], '15:14:25:10')
            self.assertEqual(len(shot['cameras']), 3)
            self.assertTrue(all(c['source_in_frame'] == 10 for c in shot['cameras'].values()))
            self.assertEqual(shot['audio']['source_in_frame'], 10)

    def test_project_config_discovers_shot_from_primary_track(self):
        with tempfile.TemporaryDirectory() as tmp:
            xml, config_path = self_test.make_xml(Path(tmp))
            config = json.loads(config_path.read_text())
            config.pop('shots')
            config['shot_detection'] = {'mode': 'primary_track_clips',
                                        'shot_id_prefix': 'SH'}
            config['audio_track'] = {'track': 1}
            config_path.write_text(json.dumps(config))
            result = convert(str(xml), str(config_path))
            self.assertEqual(result['fps'], self_test.FPS)
            self.assertEqual(len(result['shots']), 1)
            self.assertEqual(result['shots'][0]['timeline_out_frame'], 90)
            self.assertEqual(result['shots'][0]['shot_id'], 'SH001')

    def test_missing_dependency_reports_blocked_without_maya_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = subprocess.CompletedProcess([], 0, 'test passed', '')
            with patch.object(self_test, 'ROOT', root), patch('sys.argv', ['self_test.py']), \
                    patch.object(self_test, 'executable', return_value=''), \
                    patch('tests.manual.self_test.subprocess.run', return_value=result), \
                    contextlib.redirect_stdout(io.StringIO()):
                code = self_test.main()
            self.assertEqual(code, 2)
            report_path = next(root.glob('test_runs/*/report.json'))
            self.assertEqual(json.loads(report_path.read_text())['status'], 'BLOCKED')
            self.assertFalse((root / 'RUN_IN_MAYA.py').exists())


if __name__ == '__main__':
    unittest.main()
