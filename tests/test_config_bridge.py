"""Profiles, protocol security, and cancellation regression checks."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from cinesync.config import loader
from cinesync.core.engine import ValidationError
from cinesync.core.bridge import Bridge
from cinesync.pipeline.control import Control, Cancelled


class ConfigTests(unittest.TestCase):
    def test_migration_preserves_source_and_replaces_camera_collection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'old.json'
            old = {'project': 'P', 'sequence_name': 'S', 'primary_camera': 'face',
                   'cameras': {'face': {'track': 2, 'label': 'Face', 'type': 'face'}}}
            path.write_text(json.dumps(old))
            migrated = loader.migrate(str(path))
            self.assertEqual(list(migrated['cameras']), ['face'])
            self.assertEqual(json.loads(path.read_text()), old)
            new = Path(tmp) / 'profile.json'
            loader.save(new, migrated)
            self.assertEqual(loader.load(str(new)), migrated)

    def test_unknown_version_rejected(self):
        value = loader.defaults()
        value['config_version'] = 200
        with self.assertRaises(ValidationError): loader.validate(value)

    def test_cancellation_terminates_only_owned_process(self):
        control = Control()
        with subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)']) as process:
            with self.assertRaises(Cancelled):
                with control.watch(process):
                    control.cancel()
                    process.wait(timeout=3)
            self.assertIsNotNone(process.returncode)


class BridgeTests(unittest.TestCase):
    def test_authentication_delivery_and_duplicate_result(self):
        try:
            bridge = Bridge(port=0)
        except PermissionError:
            self.skipTest('Environment prohibits local TCP listeners; run on a desktop host')
        try:
            base = f'http://127.0.0.1:{bridge.port}'
            with self.assertRaises(HTTPError) as denied:
                urlopen(base + '/v1/poll/maya')
            self.assertEqual(denied.exception.code, 401)
            headers = {'Authorization': 'Bearer ' + bridge.token, 'Content-Type': 'application/json'}
            self.assertIsNone(bridge.submit('maya', 'open', {'manifest': 'offline.json', 'shot_id': 'SH000'}))
            with urlopen(Request(base + '/v1/heartbeat/maya', headers=headers)) as response:
                self.assertTrue(json.load(response)['ok'])
            self.assertTrue(bridge.connected('maya'))
            key = bridge.submit('maya', 'open', {'manifest': 'test.json', 'shot_id': 'SH001'})
            with urlopen(Request(base + '/v1/poll/maya', headers=headers)) as response:
                self.assertEqual(json.load(response)['job']['id'], key)
            with urlopen(Request(base + '/v1/poll/maya', headers=headers)) as response:
                self.assertIsNone(json.load(response)['job'])
            result = json.dumps({'protocol': 1, 'id': key, 'status': 'complete', 'result': {}}).encode()
            with urlopen(Request(base + '/v1/result', data=result, headers=headers)) as response:
                self.assertTrue(json.load(response)['ok'])
            with self.assertRaises(HTTPError) as duplicate:
                urlopen(Request(base + '/v1/result', data=result, headers=headers))
            self.assertEqual(duplicate.exception.code, 409)
        finally:
            bridge.close()
