# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['xStack_v1.4.py'],
    pathex=[],
    binaries=[],
    datas=[('xStack.png', '.'), ('xStack.ico', '.'), ('starting_fig.png', '.')],
    hiddenimports=['matplotlib.backends.backend_qtagg', 'matplotlib.backends.backend_svg', 'scipy.signal', 'scipy.ndimage', 'scipy.sparse', 'scipy.sparse.linalg'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='xStack_v1.4',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['xStack.png'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='xStack_v1.4',
)
