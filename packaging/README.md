# xStack 1.5 Windows packages

Run `packaging/build.ps1` from PowerShell. It uses the verified Python 3.14 environment and project-local PyInstaller / Inno Setup compiler. Override `-Python` or `-Compiler` to use another prepared environment.

Outputs in `release/1.5/`:

- `xStack-1.5-Setup.exe`: per-user installer, Start menu entry, optional desktop shortcut, and uninstaller. No administrator access required.
- `xStack-1.5-Portable.exe`: self-contained single EXE. No Python installation needed. Bundled dependencies extract to a temporary directory at launch, so startup can take longer than the installed edition.
- `xStack-1.5/`: intermediate onedir payload for the installer; keep its `_internal` folder with its EXE if using it directly.

Both editions target 64-bit Windows 10/11. They use the same application source, rounded icon, v1.5 high-DPI splash, 2-second splash timer, and ORCID link. The application entry point is `xStack.py`; Windows executable metadata and distribution names use version 1.5.

The packages are not code-signed. Build dependencies: PyInstaller from `.build-tools`, Inno Setup 6.7.3 from the official download at https://jrsoftware.org/isdl.php (download signature verified as Pyrsys B.V.).
