import re
from pathlib import Path

root = Path(SPECPATH).parent
VERSION = re.search(r'__version__ = "([^"]+)"', (root / 'xstack_version.py').read_text(encoding='utf-8')).group(1)
parts = ([int(n) for n in VERSION.split('.')] + [0, 0, 0, 0])[:4]
# Windows file version info is generated from the template for this version.
version_file = Path(workpath) / 'version-info.txt'
version_file.parent.mkdir(parents=True, exist_ok=True)
version_file.write_text((root / 'packaging/version.txt').read_text(encoding='utf-8').format(
    version_tuple=tuple(parts), version_full='.'.join(map(str, parts))), encoding='utf-8')
a = Analysis(
    [str(root / 'xStack.py')], pathex=[str(root)], binaries=[],
    datas=[(str(root / name), '.') for name in ('xStack.png', 'xStack.ico', 'starting_fig.png')],
    hiddenimports=['matplotlib.backends.backend_qtagg', 'matplotlib.backends.backend_svg',
                   'scipy.signal', 'scipy.ndimage', 'scipy.sparse', 'scipy.sparse.linalg'],
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    # xStack never uses pkg_resources; an old copy in the build interpreter's
    # site-packages otherwise adds a runtime hook that crashes at startup.
    excludes=['pkg_resources'], noarchive=False, optimize=0,
)
# Windows 10/11 provide these system libraries. Foreign copies discovered in
# injected tool paths can shadow the OS versions and break Qt's imported APIs.
a.binaries = [entry for entry in a.binaries
              if not Path(entry[0]).name.lower().startswith('api-ms-win-')
              and Path(entry[0]).name.lower() not in ('ucrtbase.dll', 'icuuc.dll', 'icudt78.dll')]
pyz = PYZ(a.pure)
common = dict(debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
              console=False, disable_windowed_traceback=False,
              icon=str(root / 'xStack.ico'), version=str(version_file))
app = EXE(pyz, a.scripts, [], exclude_binaries=True, name='xStack', **common)
folder = COLLECT(app, a.binaries, a.datas, strip=False, upx=False, name=f'xStack-{VERSION}')
portable = EXE(pyz, a.scripts, a.binaries, a.datas, [], name=f'xStack-{VERSION}-Portable', **common)
