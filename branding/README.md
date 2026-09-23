# xStack branding

Approved application icon: `../design-crystal-icon/xStack-crystal-rounded.svg`, with a rounded navy background and transparent corners. The splash uses the same crystal with a full navy background (`xStack-crystal-soft.svg`).
Root `xStack.png`, `xStack.ico`, and `xStack_large.ico` use this identity.

`build_splash.py` composes the approved mark and editable text into `starting_fig.png` and `starting_fig.svg`. The PNG is 1800 × 1170; the application preserves all pixels and sets a device pixel ratio of 3, displaying it at 600 × 390 logical pixels with full resolution available on HiDPI screens. The right whitespace is tightened, with version and copyright aligned to the new right margin; text and icon sizes stay the same. `splash-preview.png` previews the 1x size. Clicking the splash opens https://orcid.org/0000-0001-9846-8127.

Current copy: xStack, PXRD Viewer, View / Compare / Analyze, v1.5, Developed by Yu-Lin Lu, © 2026. Edit the script to update this information, run it, then copy `branding/starting_fig.png` to the project root. Information is included in the artwork, so the application no longer draws the old footer overlay.

`previous/` preserves the former icon and splash files. Packaged executables already in dist folders require rebuilding to include the new assets.
