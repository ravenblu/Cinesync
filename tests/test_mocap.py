"""Exact source mapping and atomic FBX packaging without a Maya dependency."""
import copy
import json
import sys
import tempfile
import unittest
import wave
from types import ModuleType
from pathlib import Path
from unittest.mock import Mock, patch

from cinesync.config import loader
from cinesync.core.engine import ValidationError, validate_manifest
from cinesync.core.mocap import map_shot, rational
from cinesync.pipeline import jobs
from cinesync.pipeline.control import Control
from cinesync.pipeline.mocap import invoke, resolve_mayapy


def source(**changes):
    result = {'file': '/unused/source.fbx', 'take_index': 1, 'take_name': 'Take 001',
              'native_fps': 120, 'first_frame': 0, 'last_frame': 1199,
              'local_frame_at_anchor': 0, 'source_timecode_at_anchor': '15:14:20:00'}
    result.update(changes)
    result['timecode'] = dict(version=1, value=result.pop('source_timecode_at_anchor'),
                              fps=result.pop('timecode_fps', 30),
                              local_frame=result.pop('local_frame_at_anchor'),
                              origin='fbx:cinesync-v1:source')
    return result


def shot(tc='15:14:25:00', duration=150):
    return {'shot_id': 'SH001', 'source_timecode': tc,
            'timeline_in_frame': 90, 'timeline_out_frame': 90 + duration}


def stamp_cameras(recipe, root, ffprobe):
    for shot in recipe['shots']:
        for camera in shot['cameras'].values():
            stat = (root / camera['media']).stat()
            camera['timecode_metadata'] = {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}


class AudioRequirementTests(unittest.TestCase):
    def test_required_audio_rejects_missing_shot_audio(self):
        profile = loader.defaults()
        recipe = {'fps': 30, 'shots': [shot()]}
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            with patch.object(jobs, 'convert', return_value=recipe), \
                    patch.object(jobs, 'verify_camera_timecodes'), \
                    patch.object(jobs, 'executable', return_value='ffprobe'):
                self.assertEqual(jobs.prepare('sequence.xml', profile, work), recipe)
                profile['audio_required'] = True
                with self.assertRaisesRegex(ValidationError, 'SH001'):
                    jobs.prepare('sequence.xml', profile, work)
            with self.assertRaisesRegex(ValidationError, 'SH001'):
                jobs.package(recipe, ['SH001'], profile, work, Control())


class MappingTests(unittest.TestCase):
    def test_30_video_120_mocap_exact_range(self):
        result = map_shot(shot(), 30, source())
        self.assertEqual(rational(result['source']['source_in_frame']), 600)
        self.assertEqual(rational(result['source']['source_out_frame']), 1200)
        self.assertEqual(result['maya_start_frame'], 1001)
        self.assertEqual(result['duration_frames'], 150)

    def test_only_integer_sampling_ratios_and_aligned_anchors_are_allowed(self):
        cut = shot('15:14:20:01', 25)
        with self.assertRaisesRegex(ValidationError, 'integer multiple of 25'):
            map_shot(cut, 25, source(timecode_fps=25))
        result = map_shot(cut, 25, source(native_fps=100, timecode_fps=25))
        self.assertEqual(rational(result['source']['source_in_frame']), 4)
        self.assertEqual(rational(result['source']['source_out_frame']), 104)
        with self.assertRaisesRegex(ValidationError, 'off the native frame grid'):
            map_shot(cut, 25, source(native_fps=100, timecode_fps=25, local_frame_at_anchor=0.5))

    def test_same_rate_and_nonzero_internal_anchor(self):
        result = map_shot(shot('15:14:19:00', 30), 30,
                          source(native_fps=30, first_frame=101, last_frame=400,
                                 local_frame_at_anchor=131))
        self.assertEqual(rational(result['source']['source_in_frame']), 101)

    def test_midnight_rollover_and_multiple_shots(self):
        take = source(source_timecode_at_anchor='23:59:59:00')
        first = map_shot(shot('00:00:01:00', 30), 30, take)
        second = map_shot(shot('00:00:03:00', 30), 30, take)
        self.assertEqual(rational(first['source']['source_in_frame']), 240)
        self.assertEqual(rational(second['source']['source_in_frame']), 480)

    def test_invalid_anchor_rate_and_incomplete_coverage(self):
        for take in (source(source_timecode_at_anchor=''), source(native_fps=119.88),
                     source(local_frame_at_anchor=-1), source(last_frame=1198),
                     source(last_frame=86400 * 120)):
            with self.subTest(take=take), self.assertRaises(ValidationError):
                map_shot(shot(), 30, take)


class PackagingTests(unittest.TestCase):
    def test_mac_mayapy_uses_newest_executable_when_path_is_blank(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for version in ('2026', '2027.2', '2027.10', '2028'):
                path = root / f'maya{version}/Maya.app/Contents/bin/mayapy'
                path.parent.mkdir(parents=True)
                path.touch()
                if version != '2028':
                    path.chmod(0o755)
            with patch('cinesync.pipeline.mocap.MAC_MAYA_ROOT', root), \
                    patch('cinesync.pipeline.mocap.sys.platform', 'darwin'):
                self.assertEqual(resolve_mayapy(''), root / 'maya2027.10/Maya.app/Contents/bin/mayapy')
                older = root / 'maya2026/Maya.app/Contents/bin/mayapy'
                self.assertEqual(resolve_mayapy(str(older)), older)
                with self.assertRaisesRegex(ValidationError, 'not executable'):
                    resolve_mayapy(str(path))

    def test_mocap_published_only_after_all_staged_assets_validate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media = root / 'source.mov'
            fbx = root / 'source.fbx'
            media.touch()
            fbx.write_bytes(b'original FBX')
            recipe = json.loads(Path('examples/recipe.json').read_text())
            recipe['shots'][0].pop('audio')
            recipe['shots'][0]['timeline_out_frame'] = 2
            recipe['shots'][0]['source_timecode'] = '15:14:20:00'
            for camera in recipe['shots'][0]['cameras'].values():
                camera['media'] = str(media)
            recipe['mocap_source'] = source(file=str(fbx), size=fbx.stat().st_size,
                                             mtime_ns=fbx.stat().st_mtime_ns)
            profile = loader.defaults()
            profile['export']['output_directory'] = str(root / 'packages')
            profile['maya']['mayapy'] = '/mock/mayapy'

            def images(args):
                for frame in (1, 2):
                    Path(args[-1] % frame).write_bytes(b'jpeg fixture')

            def export(staging, manifest, *args):
                for item in manifest['shots']:
                    target = staging / item['mocap'][0]['file']
                    target.parent.mkdir(parents=True)
                    target.write_bytes(b'normalized shot FBX')

            with patch('cinesync.pipeline.jobs.executable', side_effect=lambda value: value), \
                    patch('cinesync.pipeline.export_prep.verify_video'), \
                    patch('cinesync.pipeline.export_prep.verify_camera_timecodes', side_effect=stamp_cameras), \
                    patch('cinesync.pipeline.export_prep.run', side_effect=images), \
                    patch('cinesync.pipeline.mocap.export_shots', side_effect=export):
                work = root / 'success'
                work.mkdir()
                manifest_path = jobs.package(recipe, ['SH010'], profile, work, Control())
                manifest = json.loads(manifest_path.read_text())
                validate_manifest(manifest, manifest_path.parent, check_files=True)
                self.assertNotIn('mocap_source', manifest)
                self.assertTrue((manifest_path.parent / 'mocap/SH010/body.fbx').is_file())
                invalid = copy.deepcopy(manifest)
                invalid['shots'][0]['mocap'][0]['file'] = '../escape.fbx'
                with self.assertRaises(ValidationError):
                    validate_manifest(invalid, manifest_path.parent)
                with patch('cinesync.pipeline.mocap.export_shots', side_effect=ValueError('FBX failed')):
                    work = root / 'failure'
                    work.mkdir()
                    with self.assertRaisesRegex(ValueError, 'FBX failed'):
                        jobs.package(recipe, ['SH010'], profile, work, Control())
                    self.assertFalse((root / 'packages/failure').exists())
                    self.assertFalse(list((root / 'packages').glob('.cinesync-*')))
                fbx.write_bytes(b'changed FBX')
                with self.assertRaisesRegex(ValidationError, 'changed after inspection'):
                    jobs.package(recipe, ['SH010'], profile, root / 'changed', Control())

    def test_worker_protocol_reports_errors_and_keeps_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = root / 'fake_worker.py'
            worker.write_text(
                'import json, sys\n'
                'from pathlib import Path\n'
                'request = json.loads(Path(sys.argv[1]).read_text())\n'
                'ok = request["operation"] == "inspect"\n'
                'Path(sys.argv[2]).write_text(json.dumps({"ok": ok, '
                '"result": {"takes": []}, "error": "fixture failure"}))\n'
                'print("worker diagnostics")\n', encoding='utf-8')
            with patch('cinesync.pipeline.mocap.worker_path', return_value=worker):
                self.assertEqual(invoke(sys.executable, {'operation': 'inspect'}, root), {'takes': []})
                with self.assertRaisesRegex(ValidationError, 'fixture failure'):
                    invoke(sys.executable, {'operation': 'fail'}, root)
            self.assertEqual(len(list(root.glob('*.log'))), 2)


class FBXAdapterTests(unittest.TestCase):
    def test_file_import_rate_and_range_policy(self):
        """Host file flags must agree with the FBX plug-in rate settings."""
        from cinesync.adapters.maya.fbx import import_file

        maya = ModuleType('maya')
        maya.cmds = Mock()
        maya.mel = Mock()
        maya.cmds.file.return_value = ['source:root']
        with patch.dict(sys.modules, {'maya': maya, 'maya.cmds': maya.cmds,
                                      'maya.mel': maya.mel}):
            for file_fps in (True, False):
                with self.subTest(file_fps=file_fps):
                    result = import_file(Path('/fixture.fbx'), 1, 'source',
                                         file_fps=file_fps)
                    self.assertEqual(result, ['source:root'])
                    options = maya.cmds.file.call_args.kwargs
                    self.assertEqual(options['importFrameRate'], file_fps)
                    self.assertEqual(options['importTimeRange'], 'keep')
                    self.assertFalse(options['executeScriptNodes'])
                    maya.mel.eval.assert_any_call(
                        f'FBXImportSetMayaFrameRate -v {str(file_fps).lower()};')

    def test_ingest_uses_timeline_ui_only_for_interactive_audio(self):
        from cinesync.adapters.maya.ingest import ingest

        maya = ModuleType('maya')
        maya.cmds = Mock()
        maya.mel = Mock()
        maya.api = ModuleType('maya.api')
        om = Mock()
        om.MTime.return_value.asUnits.return_value = 1 / 24
        om.MFnDependencyNode.return_value.typeName = 'audio'
        om.MFnDependencyNode.return_value.hasAttribute.return_value = False
        maya.api.OpenMaya = om
        maya.cmds.ls.return_value = []
        maya.cmds.createNode.return_value = 'cineSyncAudio_test'
        # The sound command may reuse existing file data; never call it here.
        maya.cmds.sound.side_effect = AssertionError('sound must not create or edit audio')
        maya.cmds.objExists.return_value = True
        maya.cmds.addAttr.side_effect = RuntimeError('Found no valid items to add the attribute to.')
        modules = {'maya': maya, 'maya.cmds': maya.cmds, 'maya.mel': maya.mel,
                   'maya.api': maya.api, 'maya.api.OpenMaya': om}
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, modules):
            root = Path(tmp)
            with wave.open(str(root / 'dialog.wav'), 'wb') as audio:
                audio.setnchannels(1)
                audio.setsampwidth(3)
                audio.setframerate(48000)
                audio.writeframes(b'\0' * 48000 * 3)
            for batch, audio in ((True, None), (True, 'dialog.wav'),
                                 (False, None), (False, 'dialog.wav')):
                with self.subTest(batch=batch, audio=audio):
                    maya.cmds.reset_mock()
                    maya.mel.reset_mock()
                    om.reset_mock()
                    maya.cmds.about.return_value = batch
                    maya.mel.eval.return_value = 'timeSlider1'
                    item = {'maya_start_frame': 1001, 'duration_frames': 150,
                            'audio_track': audio}
                    ingest({'fps': 30}, item, root, confirm_fps=False)
                    maya.cmds.currentTime.assert_called_once_with(1001)
                    maya.cmds.confirmDialog.assert_not_called()
                    if audio:
                        maya.cmds.createNode.assert_called_once()
                        self.assertEqual(maya.cmds.createNode.call_args.args, ('audio',))
                        self.assertTrue(maya.cmds.createNode.call_args.kwargs['name'].startswith(':cineSyncAudio_'))
                        node = 'cineSyncAudio_test'
                        maya.cmds.sound.assert_not_called()
                        maya.cmds.setAttr.assert_any_call(
                            node + '.filename', str((root / audio).resolve()), type='string')
                        maya.cmds.setAttr.assert_any_call(node + '.offset', 1001)
                        maya.cmds.setAttr.assert_any_call(node + '.sourceEnd', 150)
                        maya.cmds.setAttr.assert_any_call(node + '.endFrame', 1151)
                        maya.cmds.addAttr.assert_not_called()
                        om.MSelectionList.return_value.add.assert_called_once_with(node)
                        om.MFnDependencyNode.return_value.addAttribute.assert_called_once_with(
                            om.MFnNumericAttribute.return_value.create.return_value)
                        om.MFnDependencyNode.return_value.findPlug.assert_called_once_with(
                            'cineSyncOwned', False)
                        om.MFnDependencyNode.return_value.findPlug.return_value.setBool.assert_called_once_with(True)
                    if batch or not audio:
                        maya.mel.eval.assert_not_called()
                        maya.cmds.timeControl.assert_not_called()
                    else:
                        maya.cmds.timeControl.assert_any_call(
                            'timeSlider1', edit=True, sound=node, displaySound=True)
            maya.cmds.about.return_value = True
            with self.assertRaisesRegex(ValidationError, 'confirm_fps=False'):
                ingest({'fps': 30}, item, root)


if __name__ == '__main__':
    unittest.main()
