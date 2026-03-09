# xStack
xStack is a desktop app for PXRD data management, stacked plotting, and quick peak-related analysis.

## What It Does

- Load multiple PXRD patterns and compare them in a stacked view.
- Support drag-and-drop import and drag-to-reorder file list.
- Provide interactive zoom, pan, curve scaling, and label editing.
- Include built-in PXRD tools: smoothing, background subtraction, peak analysis.
- Export to CSV / SVG and copy SVG directly to clipboard.
- Save and reopen full sessions with `.pxrdproj`.

## Quick Start

1. Install dependencies:

```bash
pip install numpy matplotlib pyqt6 scipy
```

2. Run:

```bash
python xStack_v1.2.py
```

3. In app:

- Add files with `Add PXRD Files` or drag files into the window.
- Adjust plot options in `Plot Parameters`.
- Open `PXRD Tools` for live preview, then click `Apply`.
- Export with `Export CSV`, `Export SVG`, or `Copy SVG`.

## Supported Data Formats

| Type | Extensions | Notes |
|---|---|---|
| Text 2-column | `.txt`, `.dat`, `.xy`, `.xye`, `.chi`, `.asc`, `.uxd`, `.ras`, `.csv` | Parses first two numeric columns as `2theta, intensity` |
| RAW | `.raw` | Tries text first, then Bruker / Rigaku binary parsers |
| XRDML | `.xrdml` | Reads `dataPoints` intensities and positions |

### RAW Compatibility

- Bruker signatures: `RAW `, `RAW2`, `RAW1.01`, `RAW4.00`
- Rigaku SmartLab-style signature: `FI\0\0` with `DA\0\0` payload

## Main Interactions

### Mouse

- Left drag on blank plot: X-range zoom
- Middle drag: X pan
- Double click blank plot: reset full view
- Double click curve label: edit curve label text
- Double click title / axis labels: edit text
- Double click curve line: change curve color
- Mouse wheel: scale selected curve (or all if none selected)

### Shortcuts

- `Ctrl+Shift+C`: copy current figure as SVG
- `Ctrl+Z`: undo (snapshot-based, up to 30 steps)
- `Delete`: remove selected curve

## PXRD Tools

The tools dialog uses live preview on a mini canvas. Main data is unchanged until `Apply`.

- `Smooth (Savitzky-Golay)`: odd window size, requires `scipy.signal.savgol_filter`
- `Smooth (Moving Average)`: moving average convolution
- `Smooth (Gaussian)`: requires `scipy.ndimage.gaussian_filter1d`
- `Background Subtraction`: arPLS baseline correction
- `Peak Analysis`: peak position, intensity, Bragg d-spacing, FWHM, Scherrer size

## Export

### CSV

- Builds a unified 2000-point `2theta` grid from global X range
- Interpolates each loaded curve to that grid

### SVG

- WYSIWYG export from current canvas state
- Background mode: `Transparent` or `White`

## Project Save Format (`.pxrdproj`)

`.pxrdproj` is a zip package containing:

- `project.json`: UI state, labels, colors, ranges, curve order, etc.
- `data/uid_*.npz`: compressed `x/y` arrays per curve

## Packaging

Included specs:

- `xStack_v1.spec`
- `xStack_v1_app.spec`

Note: current spec files use `xStack_v1.py` as entry script.  
If you package `xStack_v1.2.py`, update the script path in spec (or pass entry script via CLI).

## Known Limits

- Some vendor-specific RAW variants may not be parseable.
- PXRD live preview is centered on selected curve workflow.

## Directory (This Module)

```text
8_xStack/
├─ xStack_v1.2.py
├─ xStack.ico
├─ starting_fig.png
├─ xStack_v1.spec
└─ xStack_v1_app.spec
```
## Author & Attribution

- Author: Yu-Lin Lu
- Affiliation: Cooper Group, University of Liverpool
- AI Assistance: Parts of this project (including code and/or documentation) were generated with assistance from `GPT-5.3-Codex`.
