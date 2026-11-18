"""Optional headless GUI smoke tests; skipped without the desktop extra."""
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')


@unittest.skipUnless(importlib.util.find_spec('PySide6'), 'Install the desktop extra for Qt tests')
class DesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.application = QApplication.instance() or QApplication([])

    def test_split_player_holds_pair_until_both_frames_are_ready(self):
        from types import SimpleNamespace
        from cinesync.player.ui import CineSyncPlayer
        old_body, old_face = object(), object()
        body, face = Mock(), Mock()
        body.isNull.return_value = face.isNull.return_value = False
        ready = {'body': body}
        feeds = [SimpleNamespace(image=old_body, hud='', update=Mock()),
                 SimpleNamespace(image=old_face, hud='', update=Mock())]
        player = SimpleNamespace(
            targets=['body', 'face'], displayed={}, feeds=feeds,
            cache=SimpleNamespace(get=ready.get), target_huds=['new body', 'new face'],
            displayed_huds={0: 'old body', 1: 'old face'})
        CineSyncPlayer.present(player)
        self.assertIs(feeds[0].image, old_body)
        self.assertIs(feeds[1].image, old_face)
        self.assertEqual(feeds[1].hud, 'old face | Loading')
        ready['face'] = face
        face.isNull.return_value = True
        CineSyncPlayer.present(player)
        self.assertIs(feeds[0].image, old_body)
        self.assertIs(feeds[1].image, old_face)
        self.assertEqual(feeds[1].hud, 'old face | Missing/unreadable image')
        self.assertEqual(player.displayed, {})
        face.isNull.return_value = False
        CineSyncPlayer.present(player)
        self.assertIs(feeds[0].image, body)
        self.assertIs(feeds[1].image, face)
        self.assertEqual(feeds[1].hud, 'new face')
        self.assertEqual(player.displayed, {0: 'body', 1: 'face'})

    def test_profile_form_and_sequence_picker(self):
        from PySide6 import QtCore
        from cinesync.app.main import Window
        from tests.manual.self_test import make_xml
        with tempfile.TemporaryDirectory() as tmp:
            app_settings = QtCore.QSettings(str(Path(tmp) / 'settings.ini'),
                                             QtCore.QSettings.IniFormat)
            xml, _ = make_xml(Path(tmp))
            with patch('cinesync.app.main.Bridge', side_effect=OSError('Disabled for UI test')), \
                    patch('cinesync.app.main.loader.user_directory', return_value=Path(tmp)), \
                    patch('cinesync.app.main.QtCore.QSettings', return_value=app_settings):
                window = Window()
                try:
                    window.xml.setText(str(xml))
                    window.read_sequences()
                    profile = window.collect()
                    self.assertEqual(profile['sequence_name'], 'CineSync_SelfTest')
                    self.assertEqual(len(profile['cameras']), 3)
                    window.cache.setValue(512)
                    self.assertEqual(window.collect()['player']['cache_megabytes'], 512)
                    self.assertTrue(window.build_button.isEnabled())
                    self.assertFalse(window.send_button.isEnabled())
                    self.assertTrue(window.settings_dialog.isAncestorOf(window.profile_name))
                    self.assertFalse(window.audio_panel.isVisible())
                    window.audio_required.setChecked(True)
                    self.assertFalse(window.audio_panel.isHidden())
                    self.assertTrue(window.collect()['audio_required'])
                    window.audio_required.setChecked(False)
                    self.assertTrue(window.audio_panel.isHidden())
                    with self.assertRaisesRegex(ValueError, 'FBX is required'):
                        window.collect_mocap()
                    self.assertEqual(window.windowTitle(), 'CineSync')
                    self.assertEqual(window.pages.currentIndex(), 0)
                    self.assertTrue(window.settings_button.isHidden())
                    self.assertFalse(window.progress.isVisible())
                    self.assertFalse(window.maya_group.isEnabled())
                    self.assertFalse(window.send_button.isEnabled())
                    window.bridge = Mock()
                    window.bridge.connected.return_value = True
                    window.poll()
                    self.assertEqual(window.pages.currentIndex(), 1)
                    self.assertFalse(window.settings_button.isHidden())
                    self.assertFalse(window.maya_group.isEnabled())
                    self.assertFalse(window.send_button.isEnabled())
                    manifest = Path(tmp) / 'manifest.json'
                    manifest.write_text('{}')
                    window.manifest = manifest
                    window.package_signature = window.signature()
                    window.idle()
                    self.assertTrue(window.send_button.isEnabled())
                    window.project.setText('Changed after export')
                    window.idle()
                    self.assertFalse(window.send_button.isEnabled())
                    window.project.setText(profile['project'])
                    window.bridge.connected.return_value = False
                    window.idle()
                    self.assertTrue(window.send_button.isEnabled())
                    self.assertTrue(window.maya_group.isEnabled())
                    fbx = Path(tmp) / 'capture.fbx'
                    fbx.write_bytes(b'FBX fixture')
                    window.fbx.setText(str(fbx))
                    details = {
                        'file': str(fbx), 'size': fbx.stat().st_size,
                        'mtime_ns': fbx.stat().st_mtime_ns,
                        'takes': [{'take_index': 1, 'take_name': 'Take 001',
                                   'native_fps': 120, 'first_frame': 0, 'last_frame': 1199,
                                   'timecode': {'version': 1, 'value': '15:14:20:00',
                                                'fps': 30, 'local_frame': 0,
                                                'origin': 'fbx:cinesync-v1:root'}}],
                    }
                    window.fbx_read(details)
                    self.assertEqual(window.collect_mocap()['native_fps'], 120)
                    window.package_signature = window.signature()
                    window.idle()
                    self.assertTrue(window.send_button.isEnabled())
                    window.fbx.setText(str(Path(tmp) / 'other.fbx'))
                    window.idle()
                    self.assertFalse(window.send_button.isEnabled())
                finally:
                    window.bridge = None
                    window.close()
                    self.application.processEvents()
