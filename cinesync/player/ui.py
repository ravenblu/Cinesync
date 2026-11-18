"""Floating Maya reference player with bounded asynchronous QImage cache."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from cinesync.core.engine import (TimelineCallbacks, frames_to_smpte, image_offset,
                             image_path, load_manifest, smpte_to_frames)
from cinesync.config.settings import PlayerSettings
from .cache import FrameCache
from .qt import QtCore, QtGui, QtWidgets, maya_parent


class Feed(QtWidgets.QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.image = None
        self.hud = ""
        self.setMinimumSize(240, 160)

    def paintEvent(self, event) -> None:
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), QtGui.QColor("#12151a"))
        if self.image is not None and not self.image.isNull():
            size = self.image.size().scaled(self.size(), QtCore.Qt.KeepAspectRatio)
            target = QtCore.QRect((self.width() - size.width()) // 2,
                                 (self.height() - size.height()) // 2,
                                 size.width(), size.height())
            painter.drawImage(target, self.image)
        painter.fillRect(0, 0, self.width(), 64, QtGui.QColor(0, 0, 0, 180))
        painter.setPen(QtGui.QColor("white"))
        painter.drawText(self.rect().adjusted(8, 6, -8, -6),
                         QtCore.Qt.AlignTop | QtCore.Qt.TextWordWrap, self.hud)
        painter.end()


class CineSyncPlayer(QtWidgets.QDialog):
    def __init__(self, manifest_path: str, shot_id: Optional[str] = None,
                 settings: Optional[PlayerSettings] = None) -> None:
        super().__init__(maya_parent())
        self.root = Path(manifest_path).resolve().parent
        self.manifest = load_manifest(manifest_path)
        shots = self.manifest["shots"]
        self.shot = next((s for s in shots if s["shot_id"] == shot_id), None) if shot_id else shots[0]
        if self.shot is None:
            raise ValueError(f"Unknown shot: {shot_id}")
        self.fps = self.manifest["fps"]
        self.cameras = self.shot["cameras"]
        self.keys = list(self.cameras)
        preferred = [k for k in ("witness_front", "witness_side", "hmc_face") if k in self.cameras]
        self.keys = preferred + [k for k in self.keys if k not in preferred]
        self.mode = self.keys[0]
        self.closed = False
        self.targets = []
        self.displayed = {}
        self.target_huds = []
        self.displayed_huds = {}
        self.setWindowTitle(f"CineSync — {self.shot['shot_id']}")
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.Tool | QtCore.Qt.WindowStaysOnTopHint)
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        layout = QtWidgets.QVBoxLayout(self)
        bar = QtWidgets.QHBoxLayout()
        self.selector = QtWidgets.QComboBox()
        for key in self.keys:
            self.selector.addItem(self.cameras[key]["label"], key)
        self.selector.addItem("Body + Face [4]", "__split__")
        bar.addWidget(self.selector)
        setup = QtWidgets.QPushButton("Set up Maya shot")
        setup.clicked.connect(self.setup_scene)
        bar.addWidget(setup)
        opacity = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        opacity.setRange(20, 100)
        opacity.setValue(100)
        opacity.valueChanged.connect(lambda value: self.setWindowOpacity(value / 100.0))
        bar.addWidget(QtWidgets.QLabel("Opacity"))
        bar.addWidget(opacity)
        layout.addLayout(bar)
        feeds = QtWidgets.QHBoxLayout()
        self.feeds = [Feed(self), Feed(self)]
        for feed in self.feeds:
            feeds.addWidget(feed)
        layout.addLayout(feeds)
        self.settings = QtCore.QSettings("CineSync", "Player")
        self.resize(960, 420)
        geometry = self.settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)
        if not any(s.availableGeometry().intersects(self.frameGeometry())
                   for s in QtWidgets.QApplication.screens()):
            self.move(QtWidgets.QApplication.primaryScreen().availableGeometry().topLeft())
        self.player_settings = settings or PlayerSettings()
        self.cache = FrameCache(self.player_settings.cache_megabytes)
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.present)
        self.timer.start(16)
        self.selector.currentIndexChanged.connect(self.switch_camera)
        self.shortcuts = []
        shortcut_class = getattr(QtGui, "QShortcut", None) or QtWidgets.QShortcut
        for i in range(4):
            shortcut = shortcut_class(QtGui.QKeySequence(str(i + 1)), self)
            index = len(self.keys) if i == 3 else min(i, len(self.keys) - 1)
            shortcut.activated.connect(lambda index=index: self.selector.setCurrentIndex(index))
            self.shortcuts.append(shortcut)
        try:
            self.callbacks = TimelineCallbacks(self.on_time, lambda *_: self.close())
        except Exception:
            self.cache.close()
            self.timer.stop()
            raise
        self.on_time()

    def setup_scene(self) -> None:
        from cinesync.adapters.maya.scene import open_shot
        try:
            open_shot(self.manifest, self.shot, self.root, self.on_time)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "CineSync", str(exc))

    def switch_camera(self, *args) -> None:
        self.mode = self.selector.currentData()
        self.on_time()

    def active_keys(self) -> list:
        if self.mode != "__split__":
            return [self.mode]
        return [next((k for k in self.keys if self.cameras[k]["type"] == kind), self.keys[0])
                for kind in ("body", "face")]

    def on_time(self, *args) -> None:
        if self.closed:
            return
        import maya.cmds as cmds
        frame = cmds.currentTime(query=True)
        offset = image_offset(frame, self.shot)
        active = self.active_keys()
        self.targets = [image_path(self.root, self.cameras[k], offset) for k in active]
        pending = list(self.targets)
        for distance in range(1, self.player_settings.prefetch_frames + 1):
            for delta in (distance, -distance):
                if 0 <= offset + delta < self.shot["duration_frames"]:
                    pending.extend(image_path(self.root, self.cameras[k], offset + delta) for k in active)
        self.cache.request(pending)
        self.target_huds = []
        for i, feed in enumerate(self.feeds):
            feed.setVisible(i < len(active))
            if i >= len(active):
                continue
            camera = self.cameras[active[i]]
            tc = frames_to_smpte(smpte_to_frames(camera.get("source_timecode", self.shot["source_timecode"]), self.fps) + offset, self.fps)
            held = " | Boundary hold" if not self.shot["maya_start_frame"] <= frame < self.shot["maya_start_frame"] + self.shot["duration_frames"] else ""
            self.target_huds.append(f"TC: {tc} | Maya: {frame:g} | {camera['label']}{held}")
        self.present()

    def present(self) -> None:
        if not self.targets:
            return
        if all(self.displayed.get(i) == path for i, path in enumerate(self.targets)):
            for i, hud in enumerate(self.target_huds):
                if self.feeds[i].hud != hud:
                    self.feeds[i].hud = hud
                    self.displayed_huds[i] = hud
                    self.feeds[i].update()
            return
        images = [self.cache.get(path) for path in self.targets]
        invalid = [i for i, image in enumerate(images) if image is not None and image.isNull()]
        if invalid or any(image is None for image in images):
            # Keep the previous image and its timecode on a cache miss or decode failure.
            for i in range(len(self.targets)):
                status = 'Loading'
                if invalid:
                    status = ('Missing/unreadable image' if i in invalid
                              else 'Holding for unreadable reference image')
                base = self.displayed_huds.get(i, 'Waiting for reference images')
                hud = f'{base} | {status}'
                if self.feeds[i].hud != hud:
                    self.feeds[i].hud = hud
                    self.feeds[i].update()
            return
        # Publish one matching set on the GUI thread before any repaint.
        for i, (path, image) in enumerate(zip(self.targets, images)):
            hud = self.target_huds[i]
            self.feeds[i].image = image
            self.feeds[i].hud = hud
            self.displayed_huds[i] = hud
            self.displayed[i] = path
            self.feeds[i].update()

    def closeEvent(self, event) -> None:
        if not self.closed:
            self.closed = True
            self.settings.setValue("geometry", self.saveGeometry())
            self.timer.stop()
            self.callbacks.close()
            self.cache.close()
        super().closeEvent(event)


_player = None


def show(manifest_path: str, shot_id: Optional[str] = None,
         settings: Optional[PlayerSettings] = None) -> CineSyncPlayer:
    """Call from Maya's Python Script Editor on the main thread."""
    global _player
    if _player is not None:
        try:
            _player.close()
        except RuntimeError:
            pass
    _player = CineSyncPlayer(manifest_path, shot_id, settings)
    _player.show()
    return _player
