"""Install/remove a bounded startup block without replacing user startup code."""
import ast
import io
import os
import tempfile
import tokenize
from datetime import datetime
from pathlib import Path

BEGIN = '# BEGIN CINESYNC STARTUP'
END = '# END CINESYNC STARTUP'


def configure(scripts_directory, runtime_directory, enabled=True):
    directory = Path(scripts_directory).expanduser()
    if directory.name != 'scripts' or not directory.is_dir():
        raise ValueError('Choose an existing Maya scripts directory')
    runtime = Path(runtime_directory).resolve()
    if enabled and not (runtime / 'cinesync/adapters/maya/receiver.py').is_file():
        raise ValueError('CineSync Maya runtime was not found')
    path = directory / 'userSetup.py'
    original = path.read_bytes() if path.exists() else b''
    encoding = tokenize.detect_encoding(io.BytesIO(original).readline)[0] if original else 'utf-8'
    text = original.decode(encoding)
    if text.count(BEGIN) != text.count(END) or text.count(BEGIN) > 1:
        raise ValueError('Existing CineSync startup markers are invalid; no changes made')
    if BEGIN in text:
        first, last = text.index(BEGIN), text.index(END)
        if first > last:
            raise ValueError('Invalid startup block order')
        text = text[:first] + text[last + len(END):].lstrip('\r\n')
    if enabled:
        text = text.rstrip() + '\n\n' + f'''{BEGIN}
import maya.utils as _cinesync_utils

def _cinesync_startup():
    import sys
    import maya.cmds as cmds
    if cmds.about(batch=True):
        return
    runtime = {str(runtime)!r}
    if runtime not in sys.path:
        sys.path.insert(0, runtime)
    try:
        from cinesync.adapters.maya.receiver import connect
        connect()
    except Exception as error:
        cmds.warning("CineSync startup: " + str(error))

_cinesync_utils.executeDeferred(_cinesync_startup)
{END}
'''
    ast.parse(text, filename=str(path))
    updated = text.encode(encoding)
    backup = None
    if original and updated != original:
        backup = directory / ('userSetup.py.cinesync-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.bak')
        with backup.open('xb') as stream:
            stream.write(original)
    fd, temporary = tempfile.mkstemp(prefix='.cinesync-startup-', dir=str(directory))
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(updated)
        os.replace(temporary, path)
    finally:
        if Path(temporary).exists(): Path(temporary).unlink()
    return path, backup
