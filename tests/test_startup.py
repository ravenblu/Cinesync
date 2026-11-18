import tempfile
import unittest
from pathlib import Path
from cinesync.adapters.maya.startup import configure, BEGIN


class StartupTests(unittest.TestCase):
    def test_preserve_replace_and_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / 'scripts'
            folder.mkdir()
            path = folder / 'userSetup.py'
            original = b'# Existing user startup\nvalue = 42\n'
            path.write_bytes(original)
            runtime = Path(__file__).resolve().parents[1]
            _, backup = configure(folder, runtime)
            self.assertEqual(backup.read_bytes(), original)
            configure(folder, runtime)
            self.assertEqual(path.read_text().count(BEGIN), 1)
            configure(folder, runtime, enabled=False)
            self.assertIn('value = 42', path.read_text())
            self.assertNotIn(BEGIN, path.read_text())

    def test_invalid_existing_code_is_not_modified(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / 'scripts'
            folder.mkdir()
            path = folder / 'userSetup.py'
            path.write_text('def broken(\n')
            with self.assertRaises(SyntaxError):
                configure(folder, Path(__file__).resolve().parents[1])
            self.assertEqual(path.read_text(), 'def broken(\n')
