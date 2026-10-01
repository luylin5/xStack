# xStack Windows packages

Run `packaging/build.ps1` from PowerShell. It runs the regression tests, then builds with the first Python that has every xStack dependency (same search order as `run_xStack.bat`) and the project-local PyInstaller / Inno Setup compiler. Override `-Python` or `-Compiler` to use another prepared environment; `-SkipTests` skips the tests. Build intermediates go to `%LOCALAPPDATA%\xStack-build\`, outside OneDrive.

After building, check both executables (reports are written to `%LOCALAPPDATA%\xStack-build\smoke\`):

```powershell
py packaging\smoke_frozen.py release\1.6\xStack-1.6\xStack.exe
py packaging\smoke_frozen.py release\1.6\xStack-1.6-Portable.exe
```

The version comes from `xstack_version.py`; change it there and rerun the build (regenerate the splash with `branding/build_splash.py` and copy `branding/starting_fig.png` to the root, because the splash shows the version). Outputs for 1.6 in `release/1.6/`:

- `xStack-1.6-Setup.exe`: per-user installer, Start menu entry, optional desktop shortcut, and uninstaller. No administrator access required.
- `xStack-1.6-Portable.exe`: self-contained single EXE. No Python installation needed. Bundled dependencies extract to a temporary directory at launch, so startup can take longer than the installed edition.
- `xStack-1.6/`: intermediate onedir payload for the installer; keep its `_internal` folder with its EXE if using it directly.

Both editions target 64-bit Windows 10/11. They use the same application source, rounded icon, v1.6 high-DPI splash, 2-second splash timer, and ORCID link. The application entry point is `xStack.py`; Windows executable metadata, installer version and distribution names all use the version from `xstack_version.py`.

The packages are not code-signed. Build dependencies: PyInstaller from `.build-tools`, Inno Setup 6.7.3 from the official download at https://jrsoftware.org/isdl.php (download signature verified as Pyrsys B.V.).
