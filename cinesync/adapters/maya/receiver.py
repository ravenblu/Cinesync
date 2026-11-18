"""Nonblocking Maya receiver. Scene operations run only on the Qt thread."""
from __future__ import annotations
import json
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from cinesync.player.qt import QtCore, QtWidgets, maya_parent
from cinesync.core.engine import load_manifest
from cinesync.config.settings import PlayerSettings

_receiver = None


class Receiver(QtCore.QObject):
    def __init__(self, token: str, port: int):
        super().__init__(maya_parent())
        self.token, self.url = token, f'http://127.0.0.1:{port}'
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.future = None
        self.outbox = []
        self.seen = set()
        self.busy = False
        self.stopped = False
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(500)
        self.heartbeat_stop = threading.Event()
        self.heartbeat_thread = threading.Thread(
            target=self.heartbeat, name='CineSync Maya heartbeat', daemon=True)
        self.heartbeat_thread.start()
        QtWidgets.QApplication.instance().aboutToQuit.connect(self.stop)

    def heartbeat(self):
        """Keep discovery alive even while Maya's main thread runs a modal dialog."""
        while not self.heartbeat_stop.is_set():
            request = Request(self.url + '/v1/heartbeat/maya', headers={
                'Authorization': 'Bearer ' + self.token})
            try:
                with urlopen(request, timeout=2) as response:
                    response.read()
            except Exception:
                pass
            if self.heartbeat_stop.wait(1.0):
                return

    def request(self, payload=None):
        path = '/v1/result' if payload else '/v1/poll/maya'
        raw = json.dumps(payload).encode() if payload else None
        request = Request(self.url + path, data=raw, headers={
            'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
        try:
            with urlopen(request, timeout=2) as response:
                return json.load(response)
        except HTTPError as exc:
            if payload and exc.code == 409:
                return {'discarded': True}
            raise

    def poll(self):
        if self.stopped or self.busy:
            return
        if self.future is not None:
            if not self.future.done(): return
            try:
                result = self.future.result()
                if self.posting:
                    self.outbox.pop(0)
                elif result.get('job'):
                    self.receive(result['job'])
            except Exception:
                pass  # Retain undelivered result; reconnect on the next timer tick.
            finally:
                self.future = None
        self.posting = bool(self.outbox)
        self.future = self.executor.submit(self.request, self.outbox[0] if self.outbox else None)

    def receive(self, job):
        self.busy = True
        status, result = 'failed', {}
        try:
            if job.get('protocol') != 1 or job.get('operation') != 'open':
                raise ValueError('Unsupported bridge request')
            if job['id'] in self.seen:
                raise ValueError('Duplicate delivery rejected')
            self.seen.add(job['id'])
            payload = job['payload']
            path = Path(payload['manifest'])
            manifest = load_manifest(str(path), check_files=True)
            shot = next(s for s in manifest['shots'] if s['shot_id'] == payload['shot_id'])
            settings = PlayerSettings(**payload.get('player', {}))
            start = shot['maya_start_frame']
            message = (f"Open {shot['shot_id']} in the current scene?\n"
                       f"FPS: {manifest['fps']}\nRange: {start}–{start + shot['duration_frames'] - 1}\n"
                       f"Audio: {shot.get('audio_track') or 'None'}\n"
                       f"Mocap: {'body skeleton' if shot.get('mocap') else 'None'}\n"
                       'The scene frame rate, playback range and CineSync-owned audio/mocap will change.')
            if QtWidgets.QMessageBox.question(maya_parent(), 'CineSync incoming shot', message,
                    QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                    QtWidgets.QMessageBox.No) != QtWidgets.QMessageBox.Yes:
                status, result = 'cancelled', {'error': 'Declined in Maya'}
            else:
                self.apply(manifest, shot, path, settings)
                status, result = 'complete', {'message': f"Opened {shot['shot_id']}"}
        except Exception as exc:
            result = {'error': str(exc)}
            QtWidgets.QMessageBox.warning(maya_parent(), 'CineSync delivery failed', str(exc))
        finally:
            self.outbox.append({'protocol': 1, 'id': job['id'], 'status': status, 'result': result})
            self.busy = False

    def apply(self, manifest, shot, path, settings):
        from .scene import open_shot
        from cinesync.player.ui import show
        open_shot(manifest, shot, path.parent,
                  lambda: show(str(path), shot['shot_id'], settings=settings),
                  confirm_fps=False)

    def stop(self):
        if not self.stopped:
            self.stopped = True
            self.timer.stop()
            self.heartbeat_stop.set()
            self.executor.shutdown(wait=False)


def start(token: str, port: int = 8765):
    """Pair with the desktop session; call from Maya's Python Script Editor."""
    global _receiver
    if _receiver is not None:
        _receiver.stop()
    _receiver = Receiver(token, port)
    return _receiver

_connector = None


class AutoConnector(QtCore.QObject):
    """Discover desktop restarts without embedding session credentials in scripts."""
    def __init__(self):
        super().__init__(maya_parent())
        self.credentials = None
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1500)
        QtWidgets.QApplication.instance().aboutToQuit.connect(self.timer.stop)
        self.refresh()

    def refresh(self):
        from cinesync.core.session import session_path
        try:
            data = json.loads(session_path().read_text())
            if data.get('protocol') != 1 or not isinstance(data.get('token'), str):
                return
            port = data.get('port')
            if type(port) is not int or not 1 <= port <= 65535:
                return
            credentials = (data['token'], port)
            if credentials != self.credentials:
                start(*credentials)
                self.credentials = credentials
        except (OSError, ValueError):
            pass


def connect():
    """Enable automatic desktop discovery for this Maya session; safe to call repeatedly."""
    global _connector
    if _connector is not None:
        _connector.timer.stop()
        _connector.deleteLater()
    _connector = AutoConnector()
    return _connector
