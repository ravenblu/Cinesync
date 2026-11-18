"""Independent host scene cases and persistent diagnostics for mocap self-test."""
import json
import traceback
from unittest.mock import patch


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def diagnostics():
    import maya.cmds as cmds

    try:
        owners = []
        for node in cmds.ls(type='network') or []:
            if cmds.attributeQuery('cineSyncMocapOwned', node=node, exists=True):
                owners.append({'name': node, 'uuid': cmds.ls(node, uuid=True),
                               'owned': cmds.getAttr(node + '.cineSyncMocapOwned'),
                               'members': cmds.listConnections(
                                   node + '.cineSyncMocapNodes', source=True,
                                   destination=False) or []})
        audio = [{'name': node, 'uuid': cmds.ls(node, uuid=True),
                  'file': cmds.getAttr(node + '.filename'),
                  'offset': cmds.getAttr(node + '.offset'),
                  'owned': cmds.attributeQuery('cineSyncOwned', node=node, exists=True)}
                 for node in cmds.ls(type='audio') or []]
        return {'unit': cmds.currentUnit(query=True, time=True),
                'frame': cmds.currentTime(query=True), 'owners': owners,
                'audio': audio,
                'joints': cmds.ls(type='joint', long=True) or [],
                'networks': cmds.ls(type='network') or []}
    except Exception as exc:
        return {'diagnostic_error': str(exc)}


class Report:
    def __init__(self, output):
        self.output = output
        self.data = {'status': 'running', 'cases': []}
        self.save()

    def save(self):
        (self.output / 'report.json').write_text(
            json.dumps(self.data, indent=2), encoding='utf-8')

    def passed(self, name):
        self.data['cases'].append({'name': name, 'status': 'passed'})
        print(f'PASS: {name}', flush=True)
        self.save()

    def case(self, name, action):
        print(f'RUN: {name}', flush=True)
        try:
            result = action()
        except Exception as exc:
            self.data['cases'].append({'name': name, 'status': 'failed',
                                       'error': str(exc), 'traceback': traceback.format_exc(),
                                       'scene': diagnostics()})
            print(f'FAIL: {name}: {exc}', flush=True)
            self.save()
        else:
            self.passed(name)
            return result

    def finish(self):
        failures = [case for case in self.data['cases'] if case['status'] == 'failed']
        self.data['status'] = 'failed' if failures else 'passed'
        self.save()
        print(f"Report: {self.output / 'report.json'}", flush=True)
        if failures:
            raise RuntimeError(f'{len(failures)} mocap checks failed; '
                               'see report.json for all errors and scene diagnostics')
        print('CineSync mocap checks passed:', self.output, flush=True)


def check_scene(output, sample, fps, mode):
    import maya.cmds as cmds
    from cinesync.adapters.maya import fbx
    from cinesync.adapters.maya.mocap import MocapImport
    from cinesync.adapters.maya.scene import open_shot

    cmds.file(new=True, force=True)
    cmds.undoInfo(state=True)
    artist = cmds.createNode('transform', name='artistOwned')
    cmds.setAttr(artist + '.tx', 33)
    shot = dict(sample)
    manifest = {'fps': fps}

    def receive(player=lambda: None):
        open_shot(manifest, shot, output, player, confirm_fps=False)

    def expect_owned(count=1, joints=2):
        owners = MocapImport.owned_sessions()
        bones = cmds.ls(type='joint', long=True) or []
        require(len(owners) == count,
                f'Expected {count} owner(s), found {len(owners)}: {owners}; joints={bones}')
        require(len(bones) == joints, f'Expected {joints} joints, found {bones}')
        require(cmds.objExists(artist) and cmds.getAttr(artist + '.tx') == 33,
                'Artist-owned transform was changed or removed')

    receive()
    expect_owned()
    if mode == 'first':
        return
    old_ids = set(cmds.ls(MocapImport.owned_sessions() + cmds.ls(type='joint'), uuid=True))
    if mode == 'repeat':
        receive()
        expect_owned()
        require(not any(cmds.ls(key) for key in old_ids), 'Old owned nodes survived replacement')
        return
    if mode == 'video_only':
        shot['mocap'] = []
        receive()
        expect_owned(0, 0)
        return
    if mode == 'audio':
        import wave
        audio_file = output / 'dialog.wav'
        with wave.open(str(audio_file), 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(3)
            audio.setframerate(48000)
            audio.writeframes(b'\0' * (48000 * 5 * 3))
        shot['audio_track'] = 'dialog.wav'
        for _ in range(2):
            receive()
            expect_owned()
            audio_nodes = [node for node in cmds.ls(type='audio') or []
                           if cmds.attributeQuery('cineSyncOwned', node=node, exists=True)]
            require(len(audio_nodes) == 1, f'Expected one owned audio node: {audio_nodes}')
            require(cmds.getAttr(audio_nodes[0] + '.offset') == 1001, 'Audio offset is not 1001')
        # Unrelated artist audio is part of the rollback baseline too.
        artist_audio = cmds.createNode('audio', name=':artistAudio')
        cmds.setAttr(artist_audio + '.filename', str(audio_file), type='string')
        cmds.setAttr(artist_audio + '.offset', 17)
        require(cmds.objExists(':artistAudio'), 'Could not create artist audio fixture')
        old_audio_ids = set(cmds.ls(type='audio', uuid=True) or [])

        def fail_after_audio():
            raise ValueError('Injected player failure after audio setup')

        try:
            receive(fail_after_audio)
        except RuntimeError as exc:
            require('Injected player failure' in str(exc), str(exc))
            require('rollback incomplete' not in str(exc), str(exc))
        else:
            raise AssertionError('Audio failure was not propagated')
        expect_owned()
        actual_audio_ids = set(cmds.ls(type='audio', uuid=True) or [])
        require(actual_audio_ids == old_audio_ids,
                f'Audio rollback mismatch: missing={sorted(old_audio_ids - actual_audio_ids)}, '
                f'new={sorted(actual_audio_ids - old_audio_ids)}; '
                f'nodes={cmds.ls(type="audio")}')
        return

    cmds.currentUnit(time='film', updateAnimation=False)
    cmds.playbackOptions(min=1, max=2000, animationStartTime=1, animationEndTime=2000)
    cmds.currentTime(23.75)
    expected_unit = cmds.currentUnit(query=True, time=True)
    protected_child = None
    if mode == 'artist_child':
        root = cmds.ls(type='joint', long=True)[0]
        protected_child = cmds.createNode('transform', name='artistChild', parent=root)
    real_import = fbx.import_file

    def import_failure(*args, **kwargs):
        if mode == 'partial_import':
            real_import(*args, **kwargs)
        raise ValueError('Injected FBX import failure')

    def player_failure():
        raise ValueError('Injected player failure')

    try:
        if mode in ('early_import', 'partial_import'):
            with patch.object(fbx, 'import_file', side_effect=import_failure):
                receive()
        elif mode == 'player':
            receive(player_failure)
        elif mode == 'artist_child':
            receive()
        else:
            raise ValueError(f'Unknown test mode: {mode}')
    except RuntimeError as exc:
        expected = 'artist-owned' if mode == 'artist_child' else 'Injected'
        require(expected in str(exc), f'Unexpected failure: {exc}')
        require('rollback incomplete' not in str(exc), str(exc))
    else:
        raise AssertionError(f'{mode}: failure was not propagated')
    expect_owned()
    require(all(cmds.ls(key) for key in old_ids), 'Previous owner or skeleton was lost')
    require(cmds.currentUnit(query=True, time=True) == expected_unit, 'Scene FPS was not restored')
    require(cmds.currentTime(query=True) == 23.75, 'Current frame was not restored')
    require(cmds.playbackOptions(query=True, min=True) == 1, 'Playback min was not restored')
    require(cmds.playbackOptions(query=True, max=True) == 2000, 'Playback max was not restored')
    require(not cmds.ls('cineSyncReceiveTransaction*', type='network'), 'Transaction marker leaked')
    if protected_child:
        require(cmds.objExists(protected_child), 'Artist-owned child was deleted')
