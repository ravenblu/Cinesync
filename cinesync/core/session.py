"""Private local discovery for a running desktop session."""
import json
import os
import tempfile
from pathlib import Path


def session_path():
    if os.name == 'nt':
        base = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'CineSync'
    else:
        base = Path.home() / 'Library' / 'Application Support' / 'CineSync'
    return base / 'session.json'


def publish(token, port):
    path = session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix='.session-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(dict(protocol=1, token=token, port=port), stream)
        os.replace(temporary, path)
    finally:
        if Path(temporary).exists(): Path(temporary).unlink()


def withdraw(token):
    path = session_path()
    try:
        if json.loads(path.read_text()).get('token') == token: path.unlink()
    except (OSError, ValueError):
        pass
