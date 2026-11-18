"""Authenticated, loopback-only, data-only application bridge."""
from __future__ import annotations
import json
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional


class Bridge:
    def __init__(self, port: int = 8765) -> None:
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.jobs = {}
        self.clients = {}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, status, value):
                raw = json.dumps(value).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def authorized(self):
                return secrets.compare_digest(self.headers.get('Authorization', ''),
                                              'Bearer ' + owner.token)

            def do_GET(self):
                if not self.authorized():
                    return self.respond(401, {'error': 'Pairing token required'})
                if self.path == '/v1/heartbeat/maya':
                    with owner.lock:
                        owner.clients['maya'] = time.monotonic()
                    return self.respond(200, {'protocol': 1, 'ok': True})
                role = self.path.removeprefix('/v1/poll/')
                if role != 'maya':
                    return self.respond(404, {'error': 'Unknown endpoint'})
                with owner.lock:
                    owner.clients[role] = time.monotonic()
                    item = next((j for j in owner.jobs.values()
                                 if j['role'] == role and j['status'] == 'queued'), None)
                    if item:
                        item['status'] = 'running'
                        item['updated'] = time.monotonic()
                    payload = dict(item) if item else None
                self.respond(200, {'protocol': 1, 'job': payload})

            def do_POST(self):
                if not self.authorized():
                    return self.respond(401, {'error': 'Pairing token required'})
                if self.path != '/v1/result':
                    return self.respond(404, {'error': 'Unknown endpoint'})
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 2_000_000:
                        raise ValueError('Invalid payload size')
                    data = json.loads(self.rfile.read(size))
                    if data.get('protocol') != 1 or data.get('status') not in ('complete', 'failed', 'cancelled'):
                        raise ValueError('Invalid result')
                    with owner.lock:
                        job = owner.jobs[data['id']]
                        if job['status'] != 'running':
                            return self.respond(409, {'error': 'Job already resolved'})
                        job.update(status=data['status'], result=data.get('result', {}),
                                   updated=time.monotonic())
                    self.respond(200, {'ok': True})
                except (ValueError, KeyError, TypeError) as exc:
                    self.respond(400, {'error': str(exc)})

        self.server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def submit(self, role: str, operation: str, payload: dict) -> Optional[str]:
        if (role, operation) != ('maya', 'open'):
            raise ValueError('Unsupported bridge operation')
        with self.lock:
            # Drop offline clicks silently; never leave work queued for a later Maya launch.
            if time.monotonic() - self.clients.get(role, 0) >= 3:
                return None
            if any(j['role'] == role and j['status'] in ('queued', 'running') for j in self.jobs.values()):
                raise ValueError(f'{role} already has a pending request')
            key = uuid.uuid4().hex
            self.jobs[key] = dict(id=key, protocol=1, role=role, operation=operation,
                                  payload=payload, status='queued', updated=time.monotonic())
            return key

    def status(self, key: str) -> dict:
        with self.lock:
            return dict(self.jobs[key])

    def connected(self, role: str) -> bool:
        with self.lock:
            return time.monotonic() - self.clients.get(role, 0) < 3

    def abandon(self, key: str) -> None:
        with self.lock:
            job = self.jobs[key]
            if job['status'] in ('queued', 'running'):
                job.update(status='cancelled', result={'error': 'Request abandoned by desktop'},
                           updated=time.monotonic())

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
