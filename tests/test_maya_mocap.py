"""Scene ownership contracts exercised without a Maya installation."""
import sys
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from cinesync.adapters.maya import fbx
from cinesync.adapters.maya import ingest as ingest_module
from cinesync.adapters.maya.mocap import MocapImport
from cinesync.adapters.maya.scene import open_shot


class SceneCommands:
    """A small DG model, including network nodes omitted by unfiltered ls."""

    def __init__(self):
        self.nodes = {}
        self.namespaces = set()
        self.serial = 0
        self.unit = 'film'
        self.frame = 23.75
        self.range = dict(min=1, max=2000, animationStartTime=1, animationEndTime=2000)
        self.selection = []
        self.undo_calls = 0
        self.undo_queue = []

    def createNode(self, kind, name):
        self.serial += 1
        name = name.lstrip(':').replace('#', str(self.serial))
        self.nodes[f'id-{self.serial}'] = {
            'name': name, 'kind': kind, 'attrs': {}, 'links': [], 'parent': None}
        return name

    def key(self, value):
        return next((key for key, node in self.nodes.items()
                     if value in (key, node['name'], ':' + node['name'])), None)

    def ls(self, *args, **kwargs):
        values = list(self.nodes)
        if args and args[0] != '::*':
            names = args[0] if isinstance(args[0], list) else list(args)
            values = [self.key(name) for name in names if self.key(name)]
        elif not kwargs.get('type') and not kwargs.get('dependencyNodes'):
            values = [key for key in values if self.nodes[key]['kind'] != 'network']
        if kwargs.get('type'):
            values = [key for key in values if self.nodes[key]['kind'] == kwargs['type']]
        return values if kwargs.get('uuid') else [self.nodes[key]['name'] for key in values]

    def objExists(self, value):
        return self.key(value) is not None

    def nodeType(self, value):
        return self.nodes[self.key(value)]['kind']

    def addAttr(self, node, longName, **kwargs):
        self.nodes[self.key(node)]['attrs'][longName] = None

    def attributeQuery(self, attribute, node, **kwargs):
        return attribute in self.nodes[self.key(node)]['attrs']

    def setAttr(self, plug, value, **kwargs):
        node, attribute = plug.split('.', 1)
        self.nodes[self.key(node)]['attrs'][attribute] = value

    def getAttr(self, plug):
        node, attribute = plug.split('.', 1)
        return self.nodes[self.key(node)]['attrs'][attribute]

    def connectAttr(self, source, destination):
        source_node = source.split('.', 1)[0]
        owner = destination.split('.', 1)[0]
        self.nodes[self.key(owner)]['links'].append(self.key(source_node))

    def listConnections(self, plug, **kwargs):
        owner = plug.split('.', 1)[0]
        return [self.nodes[key]['name'] for key in self.nodes[self.key(owner)]['links']
                if key in self.nodes]

    def objectType(self, node, **kwargs):
        return self.nodeType(node) in ('joint', 'transform')

    def listRelatives(self, node, **kwargs):
        parent = self.key(node)
        children = [key for key, value in self.nodes.items() if value['parent'] == parent]
        return [self.nodes[key]['name'] for key in children]

    def delete(self, values):
        for value in values if isinstance(values, list) else [values]:
            key = self.key(value)
            if key:
                self.delete(self.listRelatives(value))
                del self.nodes[key]

    def namespace(self, **kwargs):
        if 'exists' in kwargs:
            return kwargs['exists'] in self.namespaces
        self.namespaces.discard(kwargs['removeNamespace'].lstrip(':'))

    def namespaceInfo(self, *args, **kwargs):
        return []

    def currentUnit(self, query=False, time=None, **kwargs):
        if query:
            return self.unit
        self.unit = time

    def currentTime(self, value=None, query=False):
        if query:
            return self.frame
        self.frame = value

    def playbackOptions(self, query=False, **kwargs):
        if query:
            return self.range[next(iter(kwargs))]
        self.range.update(kwargs)

    def select(self, values=None, clear=False, **kwargs):
        self.selection = [] if clear else values

    def undoInfo(self, query=False, **kwargs):
        if query:
            if kwargs.get('state'):
                return True
            if kwargs.get('undoQueueEmpty'):
                return not self.undo_queue
            if kwargs.get('undoName'):
                return self.undo_queue[-1] if self.undo_queue else ''

    def undo(self):
        self.undo_calls += 1
        raise RuntimeError('There are no more commands to undo.')


class SceneFixture(unittest.TestCase):
    def setUp(self):
        self.scene = SceneCommands()
        maya = ModuleType('maya')
        maya.cmds = self.scene
        self.modules = patch.dict(sys.modules, {'maya': maya, 'maya.cmds': self.scene})
        self.modules.start()
        self.importer = patch.object(fbx, 'import_file', side_effect=self.import_skeleton)
        self.importer.start()
        self.shot = {'shot_id': 'SH001', 'mocap': [{'file': 'mocap/SH001/body.fbx'}]}
        self.artist = self.scene.createNode('transform', 'artistOwned')

    def tearDown(self):
        self.importer.stop()
        self.modules.stop()

    def import_skeleton(self, path, take, namespace):
        self.scene.namespaces.add(namespace)
        return [self.scene.createNode('joint', namespace + ':root'),
                self.scene.createNode('animCurve', namespace + ':root_tx')]

    def import_shot(self):
        transaction = MocapImport()
        transaction.stage(self.shot, Path('/package'))
        transaction.commit()
        return transaction


class OwnershipTests(SceneFixture):
    def test_global_track_manager_is_never_owned_or_deleted(self):
        managers = []

        def importer(path, take, namespace):
            managers.append(self.scene.createNode('trackInfoManager', 'trackInfoManager#'))
            return self.import_skeleton(path, take, namespace)

        with patch.object(fbx, 'import_file', side_effect=importer):
            previous = self.import_shot()
            first_manager_id = self.scene.key(managers[0])
            self.assertNotIn(first_manager_id, previous.created)
            # Simulate contaminated metadata written by the earlier version.
            owner = MocapImport.owned_sessions()[0]
            self.scene.connectAttr(managers[0] + '.message', owner + '.cineSyncMocapNodes[99]')
            self.import_shot()
            self.assertTrue(all(self.scene.objExists(node) for node in managers))
            self.assertEqual(len(self.scene.ls(type='joint')), 1)

    def test_first_and_repeated_import_keep_new_owner_and_replace_old_only(self):
        first = self.import_shot()
        self.assertEqual(len(MocapImport.owned_sessions()), 1)
        first_ids = set(first.created)
        second = self.import_shot()
        self.assertEqual(len(MocapImport.owned_sessions()), 1)
        self.assertEqual(len(self.scene.ls(type='joint')), 1)
        self.assertTrue(first_ids.isdisjoint(self.scene.nodes))
        self.assertTrue(second.created.issubset(self.scene.nodes))
        self.assertTrue(self.scene.objExists(self.artist))

    def test_partial_import_failure_removes_new_nodes_and_preserves_old_session(self):
        previous = self.import_shot()
        transaction = MocapImport()

        def partial(path, take, namespace):
            self.import_skeleton(path, take, namespace)
            raise ValueError('Partial FBX failure')

        with patch.object(fbx, 'import_file', side_effect=partial):
            with self.assertRaisesRegex(ValueError, 'Partial FBX failure'):
                transaction.stage(self.shot, Path('/package'))
        transaction.rollback()
        self.assertTrue(previous.created.issubset(self.scene.nodes))
        self.assertEqual(len(MocapImport.owned_sessions()), 1)
        self.assertEqual(len(self.scene.ls(type='joint')), 1)

    def test_uncommitted_import_rolls_back_without_touching_previous_owner(self):
        previous = self.import_shot()
        transaction = MocapImport()
        transaction.stage(self.shot, Path('/package'))
        transaction.rollback()
        self.assertTrue(previous.created.issubset(self.scene.nodes))
        self.assertEqual(len(MocapImport.owned_sessions()), 1)

    def test_artist_child_blocks_replacement_and_survives_cleanup(self):
        previous = self.import_shot()
        root = self.scene.ls(type='joint')[0]
        self.scene.nodes[self.scene.key(self.artist)]['parent'] = self.scene.key(root)
        transaction = MocapImport()
        transaction.stage(self.shot, Path('/package'))
        with self.assertRaisesRegex(RuntimeError, 'artist-owned'):
            transaction.commit()
        transaction.rollback()
        self.assertTrue(previous.created.issubset(self.scene.nodes))
        self.assertTrue(self.scene.objExists(self.artist))

    def test_video_only_shot_removes_previous_owned_skeleton(self):
        self.import_shot()
        transaction = MocapImport()
        transaction.stage({'shot_id': 'SH002'}, Path('/package'))
        transaction.commit()
        self.assertEqual(MocapImport.owned_sessions(), [])
        self.assertEqual(self.scene.ls(type='joint'), [])
        self.assertTrue(self.scene.objExists(self.artist))


class SceneRollbackTests(SceneFixture):
    """Exercise the real scene wrapper with a cleared FBX Undo queue."""

    def fake_ingest(self, manifest, shot, root, **kwargs):
        self.assertFalse(kwargs['replace_audio'])
        self.assertEqual(self.scene.unit, '60fps')
        self.scene.unit = '60fps'
        self.scene.frame = 1001
        self.scene.range.update(min=1001, max=1150)
        if shot.get('audio_track'):
            node = self.scene.createNode('audio', 'newAudio')
            self.scene.addAttr(node, longName='cineSyncOwned')
            self.scene.setAttr(node + '.cineSyncOwned', True)

    def receive(self, player=lambda: None):
        self.shot.update(maya_start_frame=1001, duration_frames=150,
                         audio_track='audio.wav')
        with patch.object(ingest_module, 'playback_slider', return_value=None), \
                patch.object(ingest_module, 'ingest', side_effect=self.fake_ingest):
            open_shot({'fps': 60}, self.shot, Path('/package'), player, confirm_fps=False)

    def test_partial_import_has_no_undo_dependency_or_marker(self):
        previous = self.import_shot()

        def partial(path, take, namespace):
            self.assertEqual(self.scene.unit, '60fps')
            self.import_skeleton(path, take, namespace)
            self.scene.undo_queue.clear()
            raise ValueError('Injected partial import')

        with patch.object(fbx, 'import_file', side_effect=partial):
            with self.assertRaisesRegex(RuntimeError, 'scene settings restored'):
                self.receive()
        self.assertEqual(self.scene.undo_calls, 0)
        self.assertEqual(self.scene.unit, 'film')
        self.assertEqual(self.scene.frame, 23.75)
        self.assertEqual(self.scene.range['min'], 1)
        self.assertTrue(previous.created.issubset(self.scene.nodes))
        self.assertEqual(len(self.scene.ls(type='joint')), 1)
        self.assertFalse(any(node['name'].startswith('cineSyncReceiveTransaction')
                             for node in self.scene.nodes.values()))

    def test_player_failure_keeps_old_audio_and_cleans_new_audio_without_undo(self):
        previous = self.import_shot()
        old_audio = self.scene.createNode('audio', 'oldAudio')
        self.scene.addAttr(old_audio, longName='cineSyncOwned')
        self.scene.setAttr(old_audio + '.cineSyncOwned', True)

        def fail():
            # Even an unrelated queue entry must never be undone by recovery.
            self.scene.undo_queue[:] = ['Artist move']
            raise ValueError('Injected player failure')

        with self.assertRaisesRegex(RuntimeError, 'scene settings restored'):
            self.receive(fail)
        self.assertEqual(self.scene.undo_calls, 0)
        self.assertEqual(self.scene.ls(type='audio'), [old_audio])
        self.assertTrue(previous.created.issubset(self.scene.nodes))
        self.assertEqual(len(MocapImport.owned_sessions()), 1)
        self.assertEqual(self.scene.frame, 23.75)
        self.assertEqual(self.scene.unit, 'film')

    def test_success_replaces_audio_only_after_player_succeeds(self):
        old_audio = self.scene.createNode('audio', 'oldAudio')
        self.scene.addAttr(old_audio, longName='cineSyncOwned')
        self.scene.setAttr(old_audio + '.cineSyncOwned', True)

        def player():
            self.assertTrue(self.scene.objExists(old_audio))

        self.receive(player)
        self.assertFalse(self.scene.objExists(old_audio))
        self.assertEqual(self.scene.ls(type='audio'), ['newAudio'])
        self.assertEqual(len(MocapImport.owned_sessions()), 1)

    def test_reused_audio_identity_is_rejected_before_commit(self):
        previous = self.import_shot()
        old_audio = self.scene.createNode('audio', 'oldAudio')
        self.scene.addAttr(old_audio, longName='cineSyncOwned')
        self.scene.setAttr(old_audio + '.cineSyncOwned', True)
        self.shot.update(maya_start_frame=1001, duration_frames=150, audio_track='audio.wav')
        with patch.object(ingest_module, 'playback_slider', return_value=None), \
                patch.object(ingest_module, 'ingest'):
            with self.assertRaisesRegex(RuntimeError, 'distinct new node'):
                open_shot({'fps': 60}, self.shot, Path('/package'), lambda: None,
                          confirm_fps=False)
        self.assertTrue(self.scene.objExists(old_audio))
        self.assertTrue(previous.created.issubset(self.scene.nodes))
