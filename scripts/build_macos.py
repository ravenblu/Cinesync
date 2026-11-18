"""Build a local, unsigned/ad-hoc signed macOS .app on the current architecture."""
import shutil
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
if sys.platform != 'darwin':
    raise SystemExit('Run this build on macOS')
runtime = root / 'build' / 'maya_runtime' / 'cinesync'
if runtime.exists():
    shutil.rmtree(runtime)
# Stage importable modules and the two public config resources. A recursive
# copy would also ship local media, credentials and developer profiles.
source = root / 'cinesync'
# The desktop UI and editorial pipeline run in the app's interpreter.
for package in ('', 'core', 'config', 'adapters', 'adapters/maya', 'player'):
    target = runtime / package
    target.mkdir(parents=True, exist_ok=True)
    for module in (source / package).glob('*.py'):
        shutil.copy2(module, target / module.name)
for name in ('defaults.json', 'schema.json'):
    shutil.copy2(source / 'config' / name, runtime / 'config' / name)
subprocess.run([
    sys.executable, '-m', 'PyInstaller', '--noconfirm', '--windowed', '--name', 'CineSync Bridge',
    '--osx-bundle-identifier', 'org.cinesync.bridge', '--paths', str(root),
    '--collect-submodules', 'cinesync',
    '--add-data', f'{root / "LICENSE"}:.',
    '--add-data', f'{root / "cinesync/config/defaults.json"}:cinesync/config',
    '--add-data', f'{root / "cinesync/config/schema.json"}:cinesync/config',
    '--add-data', f'{runtime}:maya_runtime/cinesync',
    str(root / 'cinesync/app/__main__.py')
], cwd=str(root), check=True)
