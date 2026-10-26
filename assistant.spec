# Build a single windowed EXE with Python, Tk and the local Node API runtime.
import shutil
from pathlib import Path

project = Path(SPECPATH)
node = shutil.which('node')
if not node:
    raise RuntimeError('Node.js is required when building')

a = Analysis(
    [str(project / 'app.py')],
    pathex=[str(project)],
    binaries=[(node, '.')],
    datas=[(str(project / 'api-server/node_modules'), 'api-server/node_modules'),
           (str(project / 'api-server/server.cjs'), 'api-server'),
           (str(project / 'docs/NODE-LICENSE.txt'), 'licenses'),
           (str(project / 'docs/PYTHON-LICENSE.txt'), 'licenses')],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=['selenium', 'pytest'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name='NeteaseMusicAssistant',
    debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=False, disable_windowed_traceback=False,
    uac_admin=False,
)
