"""Standalone desktop application. Uses its own Qt runtime, never Maya's."""
from __future__ import annotations
import copy
import json
import sys
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets as W

from cinesync.config import loader
from cinesync.core.bridge import Bridge
from cinesync.pipeline import jobs
from cinesync.pipeline import mocap
from cinesync.core.timecode import rational, validate_timecode
from cinesync.pipeline.control import Control, active


class Worker(QtCore.QThread):
    succeeded = QtCore.Signal(object)
    failed = QtCore.Signal(str)

    def __init__(self, function, control, parent):
        super().__init__(parent)
        self.function, self.control = function, control

    def run(self):
        try:
            with active(self.control):
                value = self.function()
            self.succeeded.emit(value)
        except Exception as exc:
            self.failed.emit(str(exc))


class Window(W.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('CineSync')
        self.resize(640, 460)
        self.app_settings = QtCore.QSettings('CineSync', 'CineSync')
        self.has_connected = self.app_settings.value('maya/connected_before', False, type=bool)
        self.auto_build = False
        self.settings_dialog = W.QDialog(self)
        self.settings_dialog.setWindowTitle("CineSync Settings")
        self.settings_dialog.resize(900, 600)
        settings_layout = W.QVBoxLayout(self.settings_dialog)
        self.profile = loader.load()
        self.recipe = None
        self.prepared_profile = None
        self.worker = None
        self.work = None
        self.manifest = None
        self.pending = None
        self.bridge = None
        self.inspection_signature = None
        self.package_signature = None
        self.fbx_details = None
        central = W.QWidget()
        self.setCentralWidget(central)
        shell = W.QVBoxLayout(central)
        header = W.QHBoxLayout()
        title = W.QLabel('CineSync')
        title.setStyleSheet('font-size: 24px; font-weight: 600; padding: 10px 0;')
        header.addWidget(title, 1)
        self.settings_button = W.QToolButton()
        self.settings_button.setText('⚙')
        self.settings_button.setToolTip('Settings')
        self.settings_button.setAccessibleName('Settings')
        self.settings_button.clicked.connect(self.settings_dialog.show)
        header.addWidget(self.settings_button)
        self.settings_button.hide()
        shell.addLayout(header)
        self.pages = W.QStackedWidget()
        shell.addWidget(self.pages)
        self.onboarding = W.QWidget()
        onboarding_layout = W.QVBoxLayout(self.onboarding)
        onboarding_layout.addStretch()
        onboarding_layout.addWidget(W.QLabel('Connect Maya to get started.'))
        self.status = W.QLabel('Waiting for Maya')
        onboarding_layout.addWidget(self.status)
        self.pages.addWidget(self.onboarding)
        main_page = W.QWidget()
        outer = W.QVBoxLayout(main_page)
        self.pages.addWidget(main_page)
        if self.has_connected:
            self.pages.setCurrentIndex(1)
            self.settings_button.show()
        profiles = W.QHBoxLayout()
        self.profile_name = W.QComboBox()
        self.refresh_profiles()
        profiles.addWidget(self.profile_name, 1)
        for label, callback in [('Load', self.load_profile), ('Import configuration…', self.import_profile), ('Save as…', self.save_profile)]:
            button = W.QPushButton(label)
            button.clicked.connect(callback)
            profiles.addWidget(button)

        settings_layout.addLayout(profiles)
        self.tabs = W.QTabWidget()
        settings_layout.addWidget(self.tabs)
        source = W.QWidget()
        form = W.QFormLayout(source)
        self.xml = self.path_row(form, 'Editorial XML', False, 'XML (*.xml)')
        self.sequence = W.QComboBox()
        self.sequence.setEditable(False)
        form.addRow('Sequence', self.sequence)
        self.sequence_label = form.labelForField(self.sequence)
        self.sequence.hide()
        self.sequence_label.hide()
        self.project = W.QLineEdit()

        audio_options = W.QWidget()
        audio_settings = W.QFormLayout(audio_options)
        self.audio_required = W.QCheckBox('Audio required')
        audio_settings.addRow(self.audio_required)
        audio_settings.addRow(W.QLabel('Use the configured XML audio track or a complete-sequence WAV.'))
        self.tabs.addTab(audio_options, 'Audio')
        source_group = W.QGroupBox('1. Import')
        self.source_group = source_group
        source_layout = W.QHBoxLayout(source_group)
        self.video_panel = W.QGroupBox('Video · Required')
        video_layout = W.QVBoxLayout(self.video_panel)
        video_layout.addWidget(source)
        source_layout.addWidget(self.video_panel, 1)
        self.mocap_panel = W.QGroupBox('Mocap · Required')
        mocap_form = W.QFormLayout(self.mocap_panel)
        self.fbx = self.path_row(mocap_form, 'Motion capture FBX', False, 'FBX (*.fbx)')
        read_fbx = W.QPushButton('Read FBX details')
        read_fbx.clicked.connect(self.read_fbx)
        mocap_form.addRow(read_fbx)
        self.fbx_take = W.QComboBox()
        mocap_form.addRow('Animation Take', self.fbx_take)
        self.fbx_info = W.QLabel('Choose a solved skeleton FBX, then read its details.')
        self.fbx_info.setWordWrap(True)
        mocap_form.addRow(self.fbx_info)
        source_layout.addWidget(self.mocap_panel, 1)
        self.audio_panel = W.QGroupBox('Audio · Required')
        audio_form = W.QFormLayout(self.audio_panel)
        self.mix = self.path_row(audio_form, 'Sequence WAV', False, 'WAV (*.wav)')
        audio_help = W.QLabel('Use a complete-sequence 48 kHz / 24-bit WAV starting at frame zero, '
                             'or leave empty to extract the configured XML audio track.')
        audio_help.setWordWrap(True)
        audio_form.addRow(audio_help)
        source_layout.addWidget(self.audio_panel, 1)
        self.audio_panel.hide()
        self.audio_required.toggled.connect(self.audio_panel.setVisible)
        self.fbx.editingFinished.connect(self.read_fbx)
        self.fbx_take.currentIndexChanged.connect(self.fbx_take_changed)
        outer.addWidget(source_group)
        mapping = W.QWidget()
        mapping_layout = W.QVBoxLayout(mapping)
        self.cameras = W.QTableWidget(0, 5)
        self.cameras.setHorizontalHeaderLabels(['Camera ID', 'Label', 'Type: body / face', 'Track index', 'Track name (optional)'])
        self.cameras.horizontalHeader().setSectionResizeMode(W.QHeaderView.Stretch)
        mapping_layout.addWidget(self.cameras)
        row = W.QHBoxLayout()
        add = W.QPushButton('Add camera'); add.clicked.connect(lambda: self.cameras.insertRow(self.cameras.rowCount()))
        remove = W.QPushButton('Remove selected'); remove.clicked.connect(lambda: self.cameras.removeRow(self.cameras.currentRow()))
        row.addWidget(add); row.addWidget(remove)
        mapping_layout.addLayout(row)
        map_form = W.QFormLayout()
        self.primary = W.QLineEdit()
        self.audio = W.QSpinBox(); self.audio.setRange(0, 128)
        map_form.addRow('Project', self.project)
        map_form.addRow('Primary camera ID', self.primary)
        map_form.addRow('Audio track (0 = none)', self.audio)
        mapping_layout.addLayout(map_form)
        self.tabs.addTab(mapping, 'Camera mapping')
        output = W.QWidget(); output_form = W.QFormLayout(output)
        self.output = self.path_row(output_form, 'Package output directory', True)
        self.height = W.QComboBox(); self.height.addItems(['720', '1080'])
        output_form.addRow('Image height', self.height)
        self.ffmpeg = self.path_row(output_form, 'FFmpeg executable', False)
        self.ffprobe = self.path_row(output_form, 'ffprobe executable', False)
        self.mayapy = self.path_row(output_form, 'Maya mayapy executable', False)
        self.mayapy.setPlaceholderText('/Applications/Autodesk/maya2027/Maya.app/Contents/bin/mayapy')
        self.mayapy.setToolTip(
            'Leave blank to detect the newest Maya under /Applications/Autodesk on macOS. '
            'Choose an executable to use a different installation.')
        self.cache = W.QSpinBox(); self.cache.setRange(16, 16384)
        self.prefetch = W.QSpinBox(); self.prefetch.setRange(0, 300)
        output_form.addRow('Player cache (MiB)', self.cache)
        output_form.addRow('Prefetch frames each side', self.prefetch)
        self.tabs.addTab(output, 'Output')
        advanced = W.QWidget(); adv = W.QVBoxLayout(advanced)
        adv.addWidget(W.QLabel('Complete profile JSON. Apply to update the form; unknown versions are rejected.'))
        self.json_editor = W.QPlainTextEdit(); adv.addWidget(self.json_editor)
        apply_button = W.QPushButton('Apply JSON'); apply_button.clicked.connect(self.apply_json); adv.addWidget(apply_button)
        self.tabs.addTab(advanced, 'Advanced profile')
        self.shots = W.QListWidget(); self.shots.setMaximumHeight(140)
        build_group = W.QGroupBox('2. Build')
        self.build_group = build_group
        build_layout = W.QVBoxLayout(build_group)
        self.review = W.QCheckBox('Review and select shots before building')
        build_layout.addWidget(self.review)
        build_layout.addWidget(self.shots)
        self.shots.hide()
        self.review.toggled.connect(self.shots.setVisible)
        outer.addWidget(build_group)
        buttons = W.QHBoxLayout()
        self.inspect_button = W.QPushButton('Inspect / preflight'); self.inspect_button.clicked.connect(self.inspect)
        self.build_button = W.QPushButton('Build'); self.build_button.clicked.connect(self.generate)
        self.send_button = W.QPushButton('Open in Maya'); self.send_button.clicked.connect(self.send)
        open_button = W.QPushButton('Show output'); open_button.clicked.connect(self.open_output)
        self.inspect_button.hide()
        adv.addWidget(open_button)
        for button in (self.build_button,):
            buttons.addWidget(button)
        build_layout.addLayout(buttons)
        maya_group = W.QGroupBox("3. Open in Maya")
        self.maya_group = maya_group
        maya_layout = W.QVBoxLayout(maya_group)
        maya_layout.addWidget(self.send_button)
        outer.addWidget(maya_group)
        self.progress = W.QLabel('Working…')
        self.progress.hide()
        outer.addWidget(self.progress)
        self.log = W.QPlainTextEdit(); self.log.setReadOnly(True)
        self.log.setMaximumHeight(180)
        adv.addWidget(self.log)
        self.feedback = W.QLabel('')
        self.feedback.setWordWrap(True)
        outer.addWidget(self.feedback)
        maya_button = W.QPushButton('First-time Maya setup…'); maya_button.clicked.connect(self.copy_maya)
        reset_button = W.QPushButton('Abandon host request')
        reset_button.clicked.connect(self.abandon)
        onboarding_layout.addWidget(maya_button)
        onboarding_layout.addStretch()
        for group in (self.source_group, self.build_group, self.maya_group):
            effect = W.QGraphicsOpacityEffect(group)
            group.setGraphicsEffect(effect)
        manage_setup = W.QPushButton('Manage Maya startup…')
        manage_setup.clicked.connect(self.copy_maya)
        adv.addWidget(manage_setup)
        adv.addWidget(reset_button)
        self.populate()
        try:
            self.bridge = Bridge()
            from cinesync.core.session import publish
            publish(self.bridge.token, self.bridge.port)
        except OSError as exc:
            self.status.setText(f'Maya connection service unavailable: {exc}')
            self.log.appendPlainText(str(exc))
        self.timer = QtCore.QTimer(self); self.timer.timeout.connect(self.poll); self.timer.start(500)
        self.xml.editingFinished.connect(self.read_sequences)
        self.idle()

    def path_row(self, layout, label, directory=False, pattern='All files (*)'):
        row = W.QHBoxLayout(); field = W.QLineEdit(); button = W.QPushButton('Browse…')
        def choose():
            if directory:
                path = W.QFileDialog.getExistingDirectory(self, label, field.text())
            else:
                path, _ = W.QFileDialog.getOpenFileName(self, label, field.text(), pattern)
            if path:
                field.setText(path)
                field.editingFinished.emit()
        button.clicked.connect(choose); row.addWidget(field); row.addWidget(button); layout.addRow(label, row)
        return field

    def refresh_profiles(self):
        self.profile_name.clear()
        folder = loader.user_directory() / 'profiles'
        if folder.exists():
            for path in sorted(folder.glob('*.json')):
                self.profile_name.addItem(path.stem, str(path))

    def error(self, message):
        self.auto_build = False
        self.log.appendPlainText('ERROR: ' + str(message))
        self.feedback.setText(str(message))
        if self.work and self.worker:
            try:
                (self.work / 'report.json').write_text(json.dumps({'status': 'failed', 'error': str(message)}, indent=2))
            except OSError:
                pass
        W.QMessageBox.warning(self, 'CineSync', str(message))
        self.idle()

    def populate(self):
        p = self.profile
        self.project.setText(p['project']); self.sequence.setEditText(p['sequence_name'])
        self.primary.setText(p['primary_camera'])
        self.audio_required.setChecked(p.get('audio_required', False))
        self.audio_panel.setVisible(self.audio_required.isChecked())
        audio = p.get('audio_track') or 0
        self.audio.setValue(audio.get('track', 0) if isinstance(audio, dict) else audio)
        self.cameras.setRowCount(len(p['cameras']))
        for row, (key, camera) in enumerate(p['cameras'].items()):
            values = [key, camera['label'], camera['type'], str(camera.get('track', '')), camera.get('track_name', '')]
            for column, value in enumerate(values): self.cameras.setItem(row, column, W.QTableWidgetItem(value))
        for field, key in ((self.output, 'output_directory'), (self.ffmpeg, 'ffmpeg'), (self.ffprobe, 'ffprobe')):
            value = p['export'][key]
            if key == 'output_directory' and not value:
                value = str(loader.user_directory() / 'packages')
            field.setText(value)
        self.height.setCurrentText(str(p['export']['height']))
        self.mayapy.setText(p.get('maya', {}).get('mayapy', ''))
        self.cache.setValue(p['player']['cache_megabytes']); self.prefetch.setValue(p['player']['prefetch_frames'])
        self.json_editor.setPlainText(json.dumps(p, indent=2))
        self.recipe = None
        self.manifest = None

    def collect(self):
        p = copy.deepcopy(self.profile)
        p.update(project=self.project.text().strip(), sequence_name=self.sequence.currentText().strip(), primary_camera=self.primary.text().strip())
        p['cameras'] = {}
        for row in range(self.cameras.rowCount()):
            values = [(self.cameras.item(row, c).text().strip() if self.cameras.item(row, c) else '') for c in range(5)]
            key, label, kind, index, name = values
            if key in p['cameras']: raise ValueError('Camera IDs must be unique')
            camera = dict(label=label, type=kind)
            if name: camera['track_name'] = name
            else: camera['track'] = int(index)
            p['cameras'][key] = camera
        p['audio_required'] = self.audio_required.isChecked()
        p['audio_track'] = self.audio.value() or None
        p['export'].update(output_directory=self.output.text().strip(), ffmpeg=self.ffmpeg.text().strip(), ffprobe=self.ffprobe.text().strip(), height=int(self.height.currentText()))
        p['player'] = dict(cache_megabytes=self.cache.value(), prefetch_frames=self.prefetch.value())
        p['maya'] = dict(mayapy=self.mayapy.text().strip())
        return loader.validate(p)

    def load_profile(self):
        try:
            if not self.profile_name.currentData(): return
            self.profile = loader.load(self.profile_name.currentData()); self.populate()
        except Exception as exc: self.error(exc)

    def import_profile(self):
        path, _ = W.QFileDialog.getOpenFileName(self, 'Import profile (original is preserved)', '', 'JSON (*.json)')
        if path:
            try: self.profile = loader.migrate(path); self.populate()
            except Exception as exc: self.error(exc)

    def save_profile(self):
        try:
            p = self.collect()
            name, ok = W.QInputDialog.getText(self, 'Save profile', 'Profile name')
            if not ok: return
            import re
            if not re.fullmatch(r'[A-Za-z0-9_-]+', name): raise ValueError('Use letters, digits, underscores, or hyphens')
            path = loader.user_directory() / 'profiles' / (name + '.json')
            if path.exists() and W.QMessageBox.question(self, 'Replace profile', f'Replace {name}?') != W.QMessageBox.Yes: return
            loader.save(path, p); self.profile = p; self.refresh_profiles()
            self.json_editor.setPlainText(json.dumps(p, indent=2))
        except Exception as exc: self.error(exc)

    def apply_json(self):
        try:
            self.profile = loader.validate(loader.merge(loader.defaults(), json.loads(self.json_editor.toPlainText())))
            self.populate()
        except Exception as exc: self.error(exc)

    def start_worker(self, function, success):
        self.control = Control()
        if self.worker is not None:
            self.worker.deleteLater()
        self.worker = Worker(function, self.control, self)
        self.worker.succeeded.connect(success); self.worker.failed.connect(self.error)
        self.worker.finished.connect(self.worker_finished)
        self.inspect_button.setEnabled(False); self.build_button.setEnabled(False); self.send_button.setEnabled(False)
        self.progress.setText('Working…')
        self.progress.show()
        self.worker.start()
        self.idle()

    def idle(self):
        busy = (self.worker is not None and self.worker.isRunning()) or self.pending is not None
        self.inspect_button.setEnabled(not busy)
        self.section_enabled(self.source_group, not busy)
        self.section_enabled(self.build_group, not busy and self.sequence.count() > 0 and bool(self.xml.text().strip()))
        self.settings_button.setEnabled(not busy)
        self.build_button.setEnabled(not busy and bool(self.xml.text().strip()))
        try:
            unchanged = self.package_signature is not None and self.signature() == self.package_signature
        except (ValueError, KeyError, OSError, TypeError):
            unchanged = False
        ready = self.manifest is not None and self.manifest.is_file() and unchanged
        self.section_enabled(self.maya_group, not busy and ready)
        self.send_button.setEnabled(not busy and ready)
        self.send_button.setToolTip('' if ready else
            'Build successfully. Rebuild after changing inputs or settings.')
        self.progress.setVisible(busy)

    @staticmethod
    def section_enabled(group, enabled):
        group.setEnabled(enabled)
        group.graphicsEffect().setOpacity(1.0 if enabled else 0.4)

    def signature(self):
        def stamp(value):
            if not value.strip(): return None
            path = Path(value).expanduser().resolve()
            info = path.stat()
            return str(path), info.st_size, info.st_mtime_ns
        fbx = (stamp(self.fbx.text()), self.fbx_take.currentData())
        return self.collect(), stamp(self.xml.text()), stamp(self.mix.text()), fbx

    def read_fbx(self):
        if self.worker is not None and self.worker.isRunning():
            return
        self.auto_build = False
        self.recipe = None
        self.manifest = None
        self.fbx_details = None
        self.fbx_take.clear()
        self.fbx_info.setText('Reading FBX…')
        try:
            path = self.fbx.text().strip()
            mayapy = self.mayapy.text().strip()
            work = loader.user_directory() / 'jobs' / ('inspect-fbx-' + uuid.uuid4().hex[:8])
            self.start_worker(lambda: mocap.inspect_fbx(path, mayapy, work), self.fbx_read)
        except Exception as exc:
            self.error(exc)

    def fbx_read(self, details):
        self.fbx_details = details
        self.fbx_take.blockSignals(True)
        self.fbx_take.clear()
        for take in details['takes']:
            self.fbx_take.addItem(take['take_name'], take)
        self.fbx_take.setCurrentIndex(0 if len(details['takes']) == 1 else -1)
        self.fbx_take.blockSignals(False)
        self.fbx_take_changed()

    def fbx_take_changed(self, *args):
        take = self.fbx_take.currentData()
        if not take:
            self.fbx_info.setText('Select one animation Take.')
            return
        try:
            tc = validate_timecode(take.get('timecode'))
            self.fbx_info.setText(
                f"{take['native_fps']} fps · Source TC {tc['value']} at local frame "
                f"{rational(tc['local_frame'])} · TC {tc['fps']} fps · {tc['origin']}")
        except (ValueError, KeyError) as exc:
            self.fbx_info.setText(str(exc))

    def collect_mocap(self):
        if not self.fbx.text().strip():
            raise ValueError('Motion capture FBX is required.')
        take = self.fbx_take.currentData()
        if not self.fbx_details or not take:
            raise ValueError('Read FBX details and select one animation Take first.')
        validate_timecode(take.get('timecode'))
        path = Path(self.fbx.text().strip()).expanduser().resolve()
        details = self.fbx_details
        if (str(path) != details['file'] or not path.is_file()
                or path.stat().st_size != details['size']
                or path.stat().st_mtime_ns != details['mtime_ns']):
            raise ValueError('FBX changed. Read FBX details again.')
        return dict(take, file=str(path), size=details['size'], mtime_ns=details['mtime_ns'])

    def read_sequences(self):
        self.recipe = None
        self.manifest = None
        self.package_signature = None
        try:
            tree = ET.parse(self.xml.text())
            names = [s.findtext('name') for s in tree.findall('.//sequence') if s.find('media') is not None]
            self.sequence.clear(); self.sequence.addItems(list(dict.fromkeys(n for n in names if n)))
            self.sequence.setVisible(self.sequence.count() > 1)
            self.sequence_label.setVisible(self.sequence.count() > 1)
            self.recipe = None
            self.manifest = None
            self.idle()
        except Exception as exc: self.error(exc)

    def generate(self):
        try:
            if self.review.isChecked() and self.recipe is not None and self.signature() == self.inspection_signature:
                self.build()
                return
            self.auto_build = not self.review.isChecked()
            self.inspect()
        except Exception as exc:
            self.error(exc)

    def worker_finished(self):
        self.idle()
        if self.auto_build and self.recipe is not None:
            self.auto_build = False
            QtCore.QTimer.singleShot(0, self.build)

    def inspect(self):
        try:
            self.profile = self.collect()
            if not self.profile['sequence_name']: raise ValueError('Select a sequence first')
            self.work = loader.user_directory() / 'jobs' / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
            self.work.mkdir(parents=True)
            self.prepared_profile = copy.deepcopy(self.profile)
            self.prepared_mocap = self.collect_mocap()
            self.inspection_signature = self.signature()
            self.manifest = None
            self.recipe = None
            self.prepare(self.xml.text(), self.mix.text())
        except Exception as exc: self.error(exc)

    def prepare(self, xml, mix):
        self.feedback.setText('Checking editorial data…')
        self.log.appendPlainText('Reading editorial data and validating audio…')
        profile, work = copy.deepcopy(self.prepared_profile), self.work
        source = copy.deepcopy(self.prepared_mocap)
        self.start_worker(lambda: jobs.prepare(xml, profile, work, mix, source), self.prepared)

    def prepared(self, recipe):
        self.recipe = recipe; self.shots.clear()
        for shot in recipe['shots']:
            item = W.QListWidgetItem(f"{shot['shot_id']}  |  {shot['timeline_in_frame']}–{shot['timeline_out_frame']}  |  {recipe['fps']} fps")
            item.setData(QtCore.Qt.UserRole, shot['shot_id']); item.setCheckState(QtCore.Qt.Checked)
            self.shots.addItem(item)
        self.log.appendPlainText(f"Ready: {len(recipe['shots'])} shots at {recipe['fps']} fps")

    def build(self):
        try:
            profile = self.collect()
            if self.signature() != self.inspection_signature:
                raise ValueError('Inputs or settings changed. Build the reference package again.')
            selected = [self.shots.item(i).data(QtCore.Qt.UserRole) for i in range(self.shots.count()) if self.shots.item(i).checkState() == QtCore.Qt.Checked]
            self.feedback.setText('Building reference package…')
            self.log.appendPlainText('Checking sources and exporting JPEG/WAV and shot FBX…')
            self.start_worker(lambda: jobs.package(self.recipe, selected, profile, self.work, self.control), self.built)
        except Exception as exc: self.error(exc)

    def built(self, manifest):
        self.manifest = Path(manifest)
        self.package_signature = self.inspection_signature
        self.log.appendPlainText(f'Package ready: {manifest}')
        self.feedback.setText(f'Build complete: {self.manifest.parent}')
        (self.work / 'report.json').write_text(json.dumps({'status': 'complete', 'manifest': str(manifest)}, indent=2))

    def cancel(self):
        if self.worker and self.worker.isRunning():
            self.control.cancel(); self.log.appendPlainText('Cancellation requested…')

    def send(self):
        try:
            self.idle()
            if not self.send_button.isEnabled():
                raise ValueError('Complete the build and connect Maya before opening a shot.')
            if self.bridge is None or not self.bridge.connected('maya'):
                return
            data = json.loads(self.manifest.read_text())
            shot, ok = W.QInputDialog.getItem(self, 'Send to Maya', 'Shot', [s['shot_id'] for s in data['shots']], 0, False)
            if not ok: return
            self.pending = self.bridge.submit('maya', 'open', {'manifest': str(self.manifest), 'shot_id': shot, 'player': self.prepared_profile['player']})
            if self.pending is None:
                return
            self.log.appendPlainText('Waiting for confirmation in Maya…'); self.idle()
        except Exception as exc: self.error(exc)

    def poll(self):
        self.idle()
        if not self.bridge: return
        if self.pages.currentIndex() == 0 and self.bridge.connected('maya'):
            self.has_connected = True
            self.app_settings.setValue('maya/connected_before', True)
            self.pages.setCurrentIndex(1)
            self.settings_button.show()
        if not self.pending: return
        job = self.bridge.status(self.pending)
        if job['status'] in ('queued', 'running'): return
        self.pending = None
        if job['status'] != 'complete':
            self.error(job.get('result', {}).get('error', job['status'])); return
        result = job.get('result', {})
        self.log.appendPlainText('Maya: ' + result.get('message', 'Shot opened'))
        self.idle()

    def abandon(self):
        if self.pending and self.bridge:
            answer = W.QMessageBox.question(self, 'Abandon request',
                'This stops waiting only. Maya may still be processing the shot. '
                'Check Maya before submitting another request. Continue?')
            if answer == W.QMessageBox.Yes:
                self.bridge.abandon(self.pending)
                self.pending = None
                self.idle()

    def copy_maya(self):
        root = str(Path(sys._MEIPASS) / 'maya_runtime') if getattr(sys, 'frozen', False) else str(Path(__file__).resolve().parents[2])
        dialog = W.QMessageBox(self)
        dialog.setWindowTitle('Maya connection setup')
        dialog.setText('Install automatic startup once. Maya will connect automatically on future launches. '
                       'Existing startup code is preserved and backed up.')
        install = dialog.addButton('Install automatic startup…', W.QMessageBox.ActionRole)
        remove = dialog.addButton('Remove automatic startup…', W.QMessageBox.ActionRole)
        once = dialog.addButton('Connect once (copy code)', W.QMessageBox.ActionRole)
        dialog.addButton(W.QMessageBox.Cancel)
        dialog.exec()
        chosen = dialog.clickedButton()
        if chosen == once:
            code = f"import sys\nsys.path.insert(0, {root!r})\nfrom cinesync.adapters.maya.receiver import connect\nconnect()\n"
            W.QApplication.clipboard().setText(code)
            W.QMessageBox.information(self, 'Connect this session', 'Code copied. Execute it once in Maya Python Script Editor.')
        elif chosen in (install, remove):
            directory = W.QFileDialog.getExistingDirectory(self, 'Select the Maya version scripts folder',
                str(Path.home() / 'Library/Preferences/Autodesk/maya'))
            if not directory:
                return
            try:
                from cinesync.adapters.maya.startup import configure
                path, backup = configure(directory, root, enabled=chosen == install)
                message = f"Updated: {path}\n"
                if backup: message += f"Backup: {backup}\n"
                message += 'Restart Maya to apply. Startup scripts must be enabled in Maya preferences.'
                W.QMessageBox.information(self, 'Maya startup updated', message)
            except Exception as exc:
                self.error(exc)

    def open_output(self):
        if self.manifest: QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(self.manifest.parent)))

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.cancel(); event.ignore(); self.log.appendPlainText('Wait for cancellation, then close again.'); return
        if self.pending:
            self.error('A Maya request is still active. Finish it in Maya or abandon the request before closing.'); event.ignore(); return
        if self.bridge:
            from cinesync.core.session import withdraw
            withdraw(self.bridge.token)
            self.bridge.close()
        event.accept()


def main():
    application = W.QApplication.instance() or W.QApplication(sys.argv)
    application.setApplicationName('CineSync')
    application.setStyle('Fusion')
    application.setStyleSheet('QWidget { font-size: 13px; } QPushButton { padding: 7px 12px; } QLineEdit, QComboBox { padding: 5px; } QTabWidget::pane { padding: 10px; }')
    window = Window(); window.show()
    return application.exec()


if __name__ == '__main__':
    sys.exit(main())
