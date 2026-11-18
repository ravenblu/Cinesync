"""Production Maya entry window: choose a package and open one shot."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from cinesync.core.engine import load_manifest
from cinesync.player.qt import QtCore, QtWidgets, maya_parent


class Launcher(QtWidgets.QDialog):
    def __init__(self) -> None:
        super().__init__(maya_parent())
        self.setWindowTitle("CineSync Player — Open Shot")
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.Tool)
        self.resize(520, 180)
        self.settings = QtCore.QSettings("CineSync", "Player")
        self.manifest: Optional[dict] = None
        self.manifest_path: Optional[Path] = None

        layout = QtWidgets.QVBoxLayout(self)
        row = QtWidgets.QHBoxLayout()
        self.path_field = QtWidgets.QLineEdit()
        self.path_field.setPlaceholderText("Select a package manifest.json")
        self.path_field.returnPressed.connect(self.load_path)
        row.addWidget(self.path_field)
        browse = QtWidgets.QPushButton("Browse…")
        browse.clicked.connect(self.browse)
        row.addWidget(browse)
        layout.addLayout(row)
        self.info = QtWidgets.QLabel("Choose an exported CineSync package.")
        layout.addWidget(self.info)
        self.shots = QtWidgets.QComboBox()
        layout.addWidget(self.shots)
        self.open_button = QtWidgets.QPushButton("Open and set up Maya shot")
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self.open_shot)
        layout.addWidget(self.open_button)
        saved = self.settings.value("last_manifest", "")
        if saved:
            self.path_field.setText(saved)
            if Path(saved).is_file():
                self.load_path()

    def browse(self) -> None:
        start = self.path_field.text().strip() or str(Path.home())
        selected, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Select CineSync manifest", start, "JSON files (*.json)")
        if selected:
            self.path_field.setText(selected)
            self.load_path()

    def load_path(self) -> None:
        self.open_button.setEnabled(False)
        self.shots.clear()
        self.manifest = None
        self.manifest_path = None
        path = Path(self.path_field.text().strip()).expanduser()
        try:
            if path.name != "manifest.json":
                raise ValueError("Select the exported manifest.json file")
            manifest = load_manifest(str(path))
        except (OSError, ValueError, KeyError) as exc:
            self.info.setText(f"Cannot load package: {exc}")
            return
        self.manifest = manifest
        self.manifest_path = path.resolve()
        self.info.setText(f"{manifest['project']} / {manifest['sequence_name']} · "
                          f"{manifest['fps']} fps · {len(manifest['shots'])} shot(s)")
        for shot in manifest["shots"]:
            self.shots.addItem(
                f"{shot['shot_id']} · {shot['duration_frames']} frames · "
                f"TC {shot['source_timecode']}", shot["shot_id"])
        self.open_button.setEnabled(True)

    def open_shot(self) -> None:
        from .scene import open_shot
        from cinesync.player.ui import show

        if self.manifest is None or self.manifest_path is None:
            return
        shot_id = self.shots.currentData()
        shot = next(shot for shot in self.manifest["shots"]
                    if shot["shot_id"] == shot_id)
        self.open_button.setEnabled(False)
        try:
            # Validate all package media before changing the Maya scene.
            verified = load_manifest(str(self.manifest_path), check_files=True)
            shot = next(shot for shot in verified["shots"]
                        if shot["shot_id"] == shot_id)
            open_shot(verified, shot, self.manifest_path.parent,
                      lambda: show(str(self.manifest_path), shot_id))
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "CineSync", str(exc))
        else:
            self.settings.setValue("last_manifest", str(self.manifest_path))
            self.close()
        finally:
            self.open_button.setEnabled(True)


_launcher: Optional[Launcher] = None


def show_launcher() -> Launcher:
    """Open the production launcher from Maya's Python Script Editor."""
    global _launcher
    if _launcher is not None:
        try:
            _launcher.close()
        except RuntimeError:
            pass
    _launcher = Launcher()
    _launcher.show()
    return _launcher
