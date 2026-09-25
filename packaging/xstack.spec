from pathlib import Path

root = Path(SPECPATH).parent
a = Analysis(
    [str(root / 'xStack.py')], pathex=[str(root)], binaries=[],
    datas=[(str(root / name), '.') for name in ('xStack.png', 'xStack.ico', 'starting_fig.png')],
    hiddenimports=['matplotlib.backends.backend_qtagg', 'matplotlib.backends.backend_svg',
                   'scipy.signal', 'scipy.ndimage', 'scipy.sparse', 'scipy.sparse.linalg'],
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False, optimize=0,
)
# Windows 10/11 provide these system libraries. Foreign copies discovered in
# injected tool paths can shadow the OS versions and break Qt's imported APIs.
a.binaries = [entry for entry in a.binaries
              if not Path(entry[0]).name.lower().startswith('api-ms-win-')
              and Path(entry[0]).name.lower() not in ('ucrtbase.dll', 'icuuc.dll', 'icudt78.dll')]
pyz = PYZ(a.pure)
common = dict(debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
              console=False, disable_windowed_traceback=False,
              icon=str(root / 'xStack.ico'), version=str(root / 'packaging/version.txt'))
app = EXE(pyz, a.scripts, [], exclude_binaries=True, name='xStack', **common)
folder = COLLECT(app, a.binaries, a.datas, strip=False, upx=False, name='xStack-1.5')
portable = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='xStack-1.5-Portable', **common)
