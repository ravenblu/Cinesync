"""Button-driven Maya smoke test for the generated synthetic package."""
from __future__ import annotations

import json
import time
from pathlib import Path

from cinesync.player.qt import QtCore, QtWidgets, maya_parent

_dialog = None


class TestDialog(QtWidgets.QDialog):
    def __init__(self, manifest_path: str) -> None:
        super().__init__(maya_parent())
        self.manifest_path = manifest_path
        self.player = None
        self.results = []
        self.running = False
        self.setWindowTitle('CineSync — Maya Self-Test')
        self.resize(620, 380)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(QtWidgets.QLabel(
            'Open an empty scene, then click the button below. Saved empty scenes are allowed.\n'
            'The test reads the package frame rate and range, then imports test audio.\n'
            'The player stays open afterward for playback and scrubbing.'))
        self.start_button = QtWidgets.QPushButton('Run Maya Self-Test')
        self.start_button.clicked.connect(self.start)
        layout.addWidget(self.start_button)
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log)
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.poll)

    def check(self, condition: bool, description: str) -> None:
        if not condition:
            raise RuntimeError(description)
        self.results.append(description)
        self.log.appendPlainText('PASS: ' + description)

    def start(self) -> None:
        import maya.cmds as cmds
        import maya.api.OpenMaya as om
        from cinesync.player import ui as cinesync_ui
        from cinesync.adapters.maya.ingest import ingest
        from cinesync.core.engine import load_manifest

        # Maya can mark an empty scene dirty after UI/startup operations.
        # The object/audio checks below are the reliable safety gate.
        extra = set(cmds.ls(assemblies=True) or []) - {'persp', 'top', 'front', 'side'}
        if extra or cmds.ls(type='audio'):
            QtWidgets.QMessageBox.information(self, 'Empty Scene Required', 'The scene contains user objects or audio. Create an empty scene and try again.')
            return
        self.results = []
        self.log.clear()
        self.start_button.setEnabled(False)
        self.running = True
        try:
            data = load_manifest(self.manifest_path, check_files=True)
            shot = data['shots'][0]
            self.check(data['sequence_name'] == 'CineSync_SelfTest'
                       and shot['duration_frames'] == 60,
                       'Generated test package validated')
            units = {24: 'film', 25: 'pal', 30: 'ntsc', 48: 'show', 50: 'palf', 60: 'ntscf'}
            cmds.currentUnit(time=units.get(data['fps'], f"{data['fps']}fps"), updateAnimation=False)
            self.player = cinesync_ui.show(self.manifest_path, shot['shot_id'])
            ingest(data, shot, Path(self.manifest_path).parent)
            first_audio = cmds.ls(type='audio') or []
            ingest(data, shot, Path(self.manifest_path).parent)
            audio = cmds.ls(type='audio') or []
            self.check(len(first_audio) == len(audio) == 1, 'Repeated ingest has one audio node')
            self.check(cmds.getAttr(audio[0] + '.offset') == 1001, 'Audio starts at frame 1001')
            self.check(cmds.playbackOptions(query=True, min=True) == 1001
                       and cmds.playbackOptions(query=True, max=True) == 1060,
                       'Playback range is 1001–1060')
            fps = 1.0 / om.MTime(1, om.MTime.uiUnit()).asUnits(om.MTime.kSeconds)
            self.check(abs(fps - data['fps']) < 1e-8, f"Maya frame rate matches package: {data['fps']} fps")
            old = self.player
            self.player = cinesync_ui.show(self.manifest_path, shot['shot_id'])
            self.check(not old.callbacks.ids and old.cache.stopped,
                       'Reopening closes old callbacks and decoder')
            self.check(len(self.player.callbacks.ids) == 4, 'Four lifecycle/time callbacks registered')
            # These steps exercise real timeChanged callbacks, not direct on_time calls.
            self.cases = [(1001, 10), (1013, 22), (1060, 69), (1000, 10),
                          (1100, 69), (1013.5, 22)]
            self.case = 0
            self.stage = 'single'
            self.advance()
            self.timer.start(40)
        except Exception as exc:
            self.finish(str(exc))

    def advance(self) -> None:
        import maya.cmds as cmds
        frame, self.expected = self.cases[self.case]
        cmds.currentTime(frame, edit=True)
        self.deadline = time.monotonic() + 15

    def poll(self) -> None:
        try:
            if self.player.closed:
                raise RuntimeError('Player closed during test')
            feeds = self.player.feeds[:len(self.player.targets)]
            ready = bool(feeds)
            for feed in feeds:
                image = feed.image
                if image is None or image.isNull():
                    ready = False
                    continue
                value = 0
                for bit in range(7):
                    x = int((32 + bit * 40) * image.width() / 320)
                    y = int(147 * image.height() / 180)
                    if image.pixelColor(x, y).red() > 128:
                        value |= 1 << bit
                if value != self.expected:
                    ready = False
            if not ready:
                if time.monotonic() > self.deadline:
                    raise RuntimeError(f'Timed out waiting for decoded source frame {self.expected}')
                return
            self.check(True, f'{self.stage}: Maya {self.cases[self.case][0]} -> source frame {self.expected}')
            self.case += 1
            if self.case < len(self.cases):
                self.advance()
            elif self.stage == 'single':
                self.stage = 'split'
                self.player.selector.setCurrentIndex(self.player.selector.count() - 1)
                self.check(len(self.player.targets) == 2, 'Split view has two feeds')
                self.case = 0
                self.advance()
            else:
                callbacks = self.player.callbacks
                cache = self.player.cache
                self.player.close()
                self.check(not callbacks.ids and cache.stopped, 'Window close cleans callbacks/cache')
                from cinesync.player import ui as cinesync_ui
                self.player = cinesync_ui.show(self.manifest_path)
                self.finish()
        except Exception as exc:
            self.finish(str(exc))

    def finish(self, error: str = '') -> None:
        self.timer.stop()
        self.running = False
        self.start_button.setEnabled(True)
        status = 'FAIL' if error else 'PASS'
        self.log.appendPlainText(f'\n{status}: {error or "Automatic checks complete. You can now test playback."}')
        report = {'status': status, 'checks': self.results, 'error': error,
                  'not_tested': ['Audible synchronization', 'Hotkeys/opacity/window geometry',
                                 'Scene-open callbacks', '60 fps and long-run memory stability']}
        path = Path(self.manifest_path).parent.parent / 'maya-report.json'
        try:
            path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
            self.log.appendPlainText('Report: ' + str(path))
        except OSError as exc:
            self.log.appendPlainText('Could not save report: ' + str(exc))

    def closeEvent(self, event) -> None:
        if self.running:
            self.finish('Test cancelled by user')
            if self.player is not None:
                self.player.close()
        super().closeEvent(event)


def show(manifest_path: str) -> TestDialog:
    global _dialog
    if _dialog is not None:
        try:
            _dialog.close()
        except RuntimeError:
            pass
    _dialog = TestDialog(manifest_path)
    _dialog.show()
    return _dialog
