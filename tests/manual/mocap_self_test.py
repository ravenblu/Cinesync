"""Run with mayapy: generate FBX fixtures and verify real import/baking behavior.

This owns a disposable standalone scene; do not execute in Maya Script Editor.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def run(output: Path) -> None:
    import maya.cmds as cmds
    import maya.mel as mel
    from cinesync.adapters.maya.fbx import import_file, settings, skeleton_nodes
    from cinesync.adapters.maya.fbx_worker import export_item, native_fps, take_info
    from cinesync.core.engine import ValidationError
    from cinesync.core.mocap import map_shot
    from tests.manual.mocap_acceptance import Report, check_scene

    output.mkdir(parents=True, exist_ok=False)
    report = Report(output)
    print('Failure cases inject errors deliberately. Use PASS/FAIL and report.json '
          'to judge the final result.', flush=True)

    def make_source():
        cmds.file(new=True, force=True)
        cmds.currentUnit(time='120fps', linear='cm')
        cmds.undoInfo(state=True)
        root = cmds.joint(name='root')
        cmds.joint(name='child', position=(0, 5, 0))
        cmds.addAttr(root, longName='cineSyncSourceTimecode', dataType='string')
        cmds.setAttr(root + '.cineSyncSourceTimecode', '15:14:20:00', type='string')
        for name, value in (('cineSyncTimecodeFps', 30), ('cineSyncAnchorFrame', 0),
                            ('cineSyncTimecodeVersion', 1)):
            cmds.addAttr(root, longName=name, attributeType='double')
            cmds.setAttr(root + '.' + name, value)
        cmds.setKeyframe(root, attribute='tx', time=0, value=0)
        cmds.setKeyframe(root, attribute='tx', time=1199, value=1199)
        cmds.keyTangent(root, attribute='tx', inTangentType='linear', outTangentType='linear')
        cmds.playbackOptions(min=0, max=1199, animationStartTime=0, animationEndTime=1199)
        cmds.select(skeleton_nodes(), replace=True)
        source_file = output / 'source_120fps.fbx'
        with settings():
            mel.eval('FBXResetExport;')
            mel.eval('FBXExportFileVersion -v "FBX202000";')
            mel.eval('FBXExport -f ' + json.dumps(source_file.as_posix(), ensure_ascii=False) + ' -s;')
        take = take_info(str(source_file))[0]
        assert take['native_fps'] == 120, take
        return dict(take, file=str(source_file), size=source_file.stat().st_size,
                    mtime_ns=source_file.stat().st_mtime_ns)

    source = report.case('Generate and inspect 120 FPS source', make_source)
    if not source:
        report.finish()
        return

    def sample_fps(fps):
        shot = {'shot_id': f'SH{fps}', 'source_timecode': '15:14:25:00',
                'timeline_in_frame': 0, 'timeline_out_frame': fps * 5}
        cmds.file(new=True, force=True)
        import_file(Path(source['file']), 1, 'fixture', file_fps=True)
        root_node = next(node for node in skeleton_nodes()
                         if cmds.attributeQuery('cineSyncTimecodeFps', node=node, exists=True))
        cmds.setAttr(root_node + '.cineSyncTimecodeFps', fps)
        variant = output / f'source_120fps_tc{fps}.fbx'
        cmds.select(skeleton_nodes(), replace=True)
        with settings():
            mel.eval('FBXResetExport;')
            mel.eval('FBXExport -f ' + json.dumps(variant.as_posix()) + ' -s;')
        take = take_info(str(variant))[0]
        inspected = dict(take, file=str(variant), size=variant.stat().st_size,
                         mtime_ns=variant.stat().st_mtime_ns)
        if fps == 25:
            try:
                map_shot(shot, fps, inspected)
            except ValidationError as exc:
                assert 'integer multiple of 25' in str(exc), exc
            else:
                raise AssertionError('120 fps mocap must be rejected for a 25 fps sequence')
            return None
        entry = map_shot(shot, fps, inspected)
        target = output / entry['file']
        target.parent.mkdir(parents=True)
        export_item(dict(entry, output=str(target)))
        published = take_info(str(target))[0]
        assert published['timecode']['value'] == shot['source_timecode'], published
        from cinesync.core.mocap import rational
        assert rational(published['timecode']['local_frame']) == 1001, published
        cmds.file(new=True, force=True)
        import_file(target, 1, 'check', file_fps=True)
        assert native_fps() == fps, (
            f'Imported {target.name}: expected {fps} fps, '
            f'got {native_fps()} fps ({cmds.currentUnit(query=True, time=True)})')
        nodes = skeleton_nodes()
        times = cmds.keyframe(nodes, query=True, timeChange=True)
        end = 1001 + fps * 5 - 1
        assert abs(min(times) - 1001) < 1e-5, min(times)
        assert abs(max(times) - end) < 1e-5, max(times)
        imported_root = next(node for node in nodes if node.rsplit(':', 1)[-1] == 'root')
        for frame in (1001, 1002, end):
            expected = 600 + (frame - 1001) * 120 / fps
            actual = cmds.getAttr(imported_root + '.tx', time=frame)
            assert abs(actual - expected) < 1e-3, (frame, actual, expected)
        shot.update(maya_start_frame=1001, duration_frames=fps * 5,
                    audio_track=None, mocap=[entry])
        return shot

    samples = []
    for fps in (25, 30, 60):
        check = 'reject noninteger ratio' if fps == 25 else 'boundaries and root motion'
        sample = report.case(f'120-to-{fps} FPS: {check}',
                              lambda fps=fps: sample_fps(fps))
        if sample:
            samples.append((fps, sample))
    if samples:
        fps, shot = samples[-1]
        for mode in ('first', 'repeat', 'early_import', 'partial_import',
                     'player', 'artist_child', 'video_only', 'audio'):
            report.case(f'Scene: {mode}',
                         lambda mode=mode: check_scene(output, shot, fps, mode))
    report.finish()


def main() -> None:
    import maya.standalone

    output = (Path(sys.argv[1]).expanduser().resolve() if len(sys.argv) > 1 else
              Path(__file__).resolve().parents[2] / 'test_runs' /
              ('mocap-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f')))
    maya.standalone.initialize(name='python')
    try:
        run(output)
    finally:
        maya.standalone.uninitialize()


if __name__ == '__main__':
    main()
