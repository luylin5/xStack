import sys
import math
import queue
import threading
import hashlib
import os
import json
import zipfile
import io
import re
import html
import struct
import tempfile
import numpy as np
import xml.etree.ElementTree as ET
import csv

from PyQt6.QtWidgets import QApplication, QMainWindow, QWidget, QVBoxLayout, QLabel, QLineEdit, QDoubleSpinBox, QSpinBox, QFileDialog, QMessageBox, QComboBox, QColorDialog, QInputDialog, QListWidget, QListWidgetItem, QAbstractItemView, QTreeWidget, QTreeWidgetItem, QDialog, QDialogButtonBox, QFormLayout, QStackedWidget, QStyledItemDelegate, QStyleOptionViewItem, QStyle, QSplashScreen
from PyQt6.QtCore import Qt, pyqtSignal, QByteArray, QMimeData, QUrl, QEvent, QSize, QRectF, QTimer, QSettings, QObject, QLockFile
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtGui import QDrag, QTextDocument, QPalette, QPixmap, QImage, QDesktopServices, QIcon, QPainter, QColor, QFont

import matplotlib
matplotlib.use("QtAgg")
matplotlib.rcParams["font.family"] = "Arial"
from matplotlib.patches import Rectangle


from xstack_ui import MainWindowUiMixin, pattern_icon


# ---------------- Data Loading ---------------- #

TEXT_PXRD_EXTS = (
    ".txt", ".dat", ".xy", ".xye", ".chi",
    ".asc", ".uxd", ".ras", ".csv",
)
BINARY_PXRD_EXTS = (".raw",)
SUPPORTED_PXRD_EXTS = set(TEXT_PXRD_EXTS + BINARY_PXRD_EXTS + (".xrdml",))
PXRD_FILE_DIALOG_FILTER = (
    f"PXRD ({' '.join(f'*{ext}' for ext in (TEXT_PXRD_EXTS + BINARY_PXRD_EXTS))} *.xrdml);;All (*)"
)

def resolve_app_icon_path():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        "xStack.ico",
        "xStack.png",
        "NMOF_PXRD_Browser.ico",
        "NMOF_PXRD_Browser.png",
    ]
    for name in candidates:
        path = os.path.join(base_dir, name)
        if os.path.isfile(path):
            return path
    return ""

def load_two_column_file(path):
    x_vals, y_vals = [], []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip().replace(",", " ")
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                x, y = float(parts[0]), float(parts[1])
                if not (math.isfinite(x) and math.isfinite(y)):
                    continue
                x_vals.append(x)
                y_vals.append(y)
            except ValueError:
                continue
    if not x_vals:
        raise ValueError("No valid two-column numeric data found in file.")
    return np.array(x_vals), np.array(y_vals)


def _read_exact(handle, nbytes):
    data = handle.read(nbytes)
    if len(data) != nbytes:
        raise ValueError("Unexpected end of RAW file.")
    return data


def _read_u16_le(handle):
    return struct.unpack("<H", _read_exact(handle, 2))[0]


def _read_u32_le(handle):
    return struct.unpack("<I", _read_exact(handle, 4))[0]


def _read_f32_le(handle):
    return struct.unpack("<f", _read_exact(handle, 4))[0]


def _read_f64_le(handle):
    return struct.unpack("<d", _read_exact(handle, 8))[0]


def _validate_step_count(step_count):
    max_points = 20_000_000
    if step_count <= 0 or step_count > max_points:
        raise ValueError(f"Invalid number of points in RAW file: {step_count}")


def _load_bruker_raw_v1(handle):
    handle.seek(4, os.SEEK_SET)
    step_count = _read_u32_le(handle)
    if step_count == int.from_bytes(b"RAW ", "little"):
        # Multi-range files may store a marker where step count is expected.
        step_count = _read_u32_le(handle)

    _read_f32_le(handle)  # time per step
    step_size = _read_f32_le(handle)
    _read_u32_le(handle)  # scan mode
    handle.seek(4, os.SEEK_CUR)
    start_2theta = _read_f32_le(handle)

    handle.seek(12 + 32 + 4 + 4 + 72, os.SEEK_CUR)
    _read_u32_le(handle)  # following range pointer

    _validate_step_count(step_count)
    y = np.frombuffer(_read_exact(handle, step_count * 4), dtype="<f4").astype(np.float64)
    x = start_2theta + step_size * np.arange(step_count, dtype=np.float64)
    return x, y


def _load_bruker_raw_v2(handle):
    handle.seek(4, os.SEEK_SET)
    range_count = _read_u16_le(handle)
    if range_count <= 0:
        raise ValueError("Bruker RAW2 has no ranges.")

    handle.seek(162 + 20 + 2 + 4 + 4 + 4 + 8 + 4 + 42, os.SEEK_CUR)

    header_len = _read_u16_le(handle)
    step_count = _read_u16_le(handle)
    handle.seek(4, os.SEEK_CUR)
    _read_f32_le(handle)  # seconds per step
    step_size = _read_f32_le(handle)
    start_2theta = _read_f32_le(handle)
    handle.seek(26, os.SEEK_CUR)
    _read_u16_le(handle)  # supplementary header temp marker

    if header_len < 48:
        raise ValueError(f"Invalid RAW2 header length: {header_len}")
    extra_header = header_len - 48
    if extra_header:
        handle.seek(extra_header, os.SEEK_CUR)

    _validate_step_count(step_count)
    y = np.frombuffer(_read_exact(handle, step_count * 4), dtype="<f4").astype(np.float64)
    x = start_2theta + step_size * np.arange(step_count, dtype=np.float64)
    return x, y


def _load_bruker_raw_v3(handle):
    # RAW1.01 (Bruker binary v3): first range header starts at byte 712.
    handle.seek(712, os.SEEK_SET)
    range_header_start = handle.tell()

    header_len = _read_u32_le(handle)
    step_count = _read_u32_le(handle)
    _read_f64_le(handle)  # theta
    start_2theta = _read_f64_le(handle)
    if header_len < 304:
        raise ValueError(f"Unexpected RAW1.01 range header length: {header_len}")

    handle.seek(range_header_start + 176, os.SEEK_SET)
    step_size = _read_f64_le(handle)
    handle.seek(range_header_start + 256, os.SEEK_SET)
    supplementary_headers_size = _read_u32_le(handle)

    _validate_step_count(step_count)
    handle.seek(range_header_start + header_len, os.SEEK_SET)
    if supplementary_headers_size > 0:
        handle.seek(supplementary_headers_size, os.SEEK_CUR)

    y = np.frombuffer(_read_exact(handle, step_count * 4), dtype="<f4").astype(np.float64)
    x = start_2theta + step_size * np.arange(step_count, dtype=np.float64)
    return x, y


def _find_raw4_first_range_marker(handle):
    # RAW4.00: metadata header is fixed-length up to byte 61.
    handle.seek(61, os.SEEK_SET)
    while True:
        segment_type = _read_u32_le(handle)
        if segment_type in (0, 160):
            return segment_type
        segment_len = _read_u32_le(handle)
        if segment_len < 8:
            raise ValueError(f"Invalid RAW4 segment length: {segment_len}")
        handle.seek(segment_len - 8, os.SEEK_CUR)


def _decode_raw4_intensity_block(raw_bytes, datum_size):
    if datum_size == 8:
        return np.frombuffer(raw_bytes, dtype="<f8").astype(np.float64)
    if datum_size == 4:
        return np.frombuffer(raw_bytes, dtype="<f4").astype(np.float64)
    if datum_size == 2:
        return np.frombuffer(raw_bytes, dtype="<u2").astype(np.float64)
    if datum_size == 1:
        return np.frombuffer(raw_bytes, dtype=np.uint8).astype(np.float64)
    raise ValueError(f"Unsupported RAW4 datum size: {datum_size}")


def _load_bruker_raw_v4(handle):
    _find_raw4_first_range_marker(handle)
    range_start = handle.tell() - 4

    handle.seek(range_start + 72, os.SEEK_SET)
    start_2theta = _read_f64_le(handle)
    step_size = _read_f64_le(handle)
    step_count = _read_u32_le(handle)
    _validate_step_count(step_count)

    handle.seek(range_start + 136, os.SEEK_SET)
    datum_size = _read_u32_le(handle)
    header_size = _read_u32_le(handle)
    if header_size < 0:
        raise ValueError("Invalid RAW4 header size.")

    data_offset = range_start + 160 + header_size
    handle.seek(data_offset, os.SEEK_SET)
    y_raw = _read_exact(handle, datum_size * step_count)
    y = _decode_raw4_intensity_block(y_raw, datum_size)
    x = start_2theta + step_size * np.arange(step_count, dtype=np.float64)
    return x, y


def load_bruker_raw_binary(path):
    with open(path, "rb") as f:
        head8 = _read_exact(f, 8)
        if head8.startswith(b"RAW1.01"):
            return _load_bruker_raw_v3(f)
        if head8.startswith(b"RAW4.00"):
            return _load_bruker_raw_v4(f)
        if head8.startswith(b"RAW2"):
            return _load_bruker_raw_v2(f)
        if head8.startswith(b"RAW "):
            return _load_bruker_raw_v1(f)
        raise ValueError("Unknown RAW signature. Not a supported Bruker RAW binary file.")


def _iter_tag_offsets(blob, tag):
    pos = 0
    while True:
        pos = blob.find(tag, pos)
        if pos < 0:
            return
        yield pos
        pos += 1


def _is_finite(v):
    return np.isfinite(v).item() if isinstance(v, np.generic) else np.isfinite(v)


def _scan_axis_triplet(region, point_count):
    if point_count <= 1 or len(region) < 12:
        return None

    best = None
    for i in range(0, len(region) - 12 + 1):
        start, end, step = struct.unpack_from("<fff", region, i)
        if not (_is_finite(start) and _is_finite(end) and _is_finite(step)):
            continue
        if abs(step) < 1e-7 or abs(step) > 10:
            continue
        if abs(start) > 720 or abs(end) > 720:
            continue
        if (step > 0 and end < start) or (step < 0 and end > start):
            continue

        calc_end = start + step * (point_count - 1)
        err = abs(calc_end - end)
        tol = max(1e-3, abs(step) * 0.2, abs(end) * 1e-4)
        if err > tol:
            continue

        in_pxrd_window = (-10 <= start <= 180) and (-10 <= end <= 180)
        roundness = (
            abs(step - round(step, 6))
            + abs(start - round(start, 4))
            + abs(end - round(end, 4))
        )
        score = err + roundness * 1e-2 + (0.0 if in_pxrd_window else 5.0)
        candidate = (score, float(start), float(step))
        if best is None or candidate[0] < best[0]:
            best = candidate

    if best is None:
        return None
    return best[1], best[2]


def _scan_axis_pair(region, point_count):
    if point_count <= 1 or len(region) < 8:
        return None

    best = None
    for i in range(0, len(region) - 8 + 1):
        start, end = struct.unpack_from("<ff", region, i)
        if not (_is_finite(start) and _is_finite(end)):
            continue
        if abs(start) > 720 or abs(end) > 720:
            continue
        step = (end - start) / float(point_count - 1)
        if abs(step) < 1e-7 or abs(step) > 10:
            continue

        in_pxrd_window = (-10 <= start <= 180) and (-10 <= end <= 180)
        roundness = (
            abs(step - round(step, 6))
            + abs(start - round(start, 4))
            + abs(end - round(end, 4))
        )
        score = roundness + (0.0 if in_pxrd_window else 5.0)
        candidate = (score, float(start), float(step))
        if best is None or candidate[0] < best[0]:
            best = candidate

    if best is None:
        return None
    return best[1], best[2]


def _extract_rigaku_axis(raw_bytes, da_pos, point_count):
    pi_positions = [p for p in _iter_tag_offsets(raw_bytes, b"PI\x00\x00") if p < da_pos]
    region_start = pi_positions[-1] if pi_positions else 0
    region = raw_bytes[region_start:da_pos]

    axis = _scan_axis_triplet(region, point_count)
    if axis is None:
        axis = _scan_axis_pair(region, point_count)
    if axis is None:
        raise ValueError("Could not locate start/end/step metadata in Rigaku RAW header.")
    return axis


def _find_rigaku_da_payload(raw_bytes):
    best = None
    for da_pos in _iter_tag_offsets(raw_bytes, b"DA\x00\x00"):
        if da_pos + 20 > len(raw_bytes):
            continue
        block_size = struct.unpack_from("<I", raw_bytes, da_pos + 8)[0]
        point_count = struct.unpack_from("<I", raw_bytes, da_pos + 16)[0]
        try:
            _validate_step_count(point_count)
        except ValueError:
            continue

        data_offset = da_pos + 20
        data_bytes = point_count * 4
        data_end = data_offset + data_bytes
        if data_end > len(raw_bytes):
            continue

        # Prefer candidates whose payload ends near EOF and whose declared size is consistent.
        tail = len(raw_bytes) - data_end
        size_mismatch = abs((data_bytes + 8) - int(block_size))
        score = tail * 10 + size_mismatch
        candidate = (score, da_pos, point_count, data_offset)
        if best is None or candidate[0] < best[0]:
            best = candidate

    if best is None:
        raise ValueError("DA data block not found in Rigaku RAW file.")
    return best[1], best[2], best[3]


def load_rigaku_raw_binary(path):
    with open(path, "rb") as f:
        raw_bytes = f.read()

    if len(raw_bytes) < 32:
        raise ValueError("File is too small to be a Rigaku RAW file.")
    if not raw_bytes.startswith(b"FI\x00\x00"):
        raise ValueError("Missing FI signature (not Rigaku SmartLab RAW).")

    da_pos, point_count, data_offset = _find_rigaku_da_payload(raw_bytes)
    start_2theta, step_size = _extract_rigaku_axis(raw_bytes, da_pos, point_count)

    data_end = data_offset + point_count * 4
    y = np.frombuffer(raw_bytes[data_offset:data_end], dtype="<f4").astype(np.float64)
    x = start_2theta + step_size * np.arange(point_count, dtype=np.float64)
    return x, y


def load_raw_file(path):
    text_error = None
    try:
        return load_two_column_file(path)
    except Exception as exc:
        text_error = exc

    binary_errors = []
    for loader_name, loader in (
        ("Bruker RAW", load_bruker_raw_binary),
        ("Rigaku RAW", load_rigaku_raw_binary),
    ):
        try:
            return loader(path)
        except Exception as exc:
            binary_errors.append(f"{loader_name}: {exc}")

    details = "\n".join(binary_errors) if binary_errors else "No binary RAW parser available."
    raise ValueError(
        "Failed to parse .raw file.\n"
        f"Text parser error: {text_error}\n"
        f"Binary RAW parser errors:\n{details}"
    )


def load_xrdml_file(path):
    tree = ET.parse(path)
    root = tree.getroot()

    def tag_name(elem):
        return elem.tag.split("}")[-1]

    data_points = next((e for e in root.iter() if tag_name(e) == "dataPoints"), None)
    if data_points is None:
        raise ValueError("dataPoints not found in XRDML.")

    intens_elem = next((e for e in data_points if tag_name(e) == "intensities"), None)
    if intens_elem is None:
        raise ValueError("intensities not found in XRDML.")
    y_vals = [float(v) for v in "".join(intens_elem.itertext()).replace(",", " ").split() if v]
    y = np.array(y_vals)

    pos_elem = next((e for e in data_points if tag_name(e) == "positions"), None)
    if pos_elem is None:
        raise ValueError("positions not found in XRDML.")

    start_elem = next((e for e in pos_elem if tag_name(e) == "startPosition"), None)
    end_elem = next((e for e in pos_elem if tag_name(e) == "endPosition"), None)

    if start_elem is not None and end_elem is not None:
        start = float("".join(start_elem.itertext()).strip())
        end = float("".join(end_elem.itertext()).strip())
        x = np.linspace(start, end, len(y))
    else:
        pos_vals = [float(v) for v in "".join(pos_elem.itertext()).replace(",", " ").split() if v]
        x = np.linspace(pos_vals[0], pos_vals[-1], len(y)) if len(pos_vals) >= 2 else np.array(pos_vals)
    return x, y


def load_pattern_file(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".xrdml":
        return load_xrdml_file(path)
    if ext == ".raw":
        return load_raw_file(path)
    return load_two_column_file(path)


def powerxrd_braggs(twotheta_deg, wavelength=1.5406):
    twotheta = np.asarray(twotheta_deg, dtype=np.float64)
    theta = np.deg2rad(twotheta / 2.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        d_hkl = wavelength / (2.0 * np.sin(theta))
    d_hkl = np.where(twotheta < 5.0, np.inf, d_hkl)
    if np.ndim(twotheta_deg) == 0:
        return float(d_hkl)
    return d_hkl


def powerxrd_scherrer(k_factor, wavelength, beta_rad, theta_rad):
    denom = beta_rad * np.cos(theta_rad)
    if abs(denom) < 1e-12:
        return np.inf
    return (k_factor * wavelength) / denom


def powerxrd_savgol_smooth(x, y, window_points, return_effective_window=False):
    n_req = int(window_points)
    if n_req < 3:
        raise ValueError("Window size must be at least 3 points for Savitzky-Golay smoothing.")
    x_arr = np.asarray(x, dtype=np.float64)
    y_arr = np.asarray(y, dtype=np.float64)
    if x_arr.size != y_arr.size:
        raise ValueError("x and y must have the same length.")
    if y_arr.size < 3:
        raise ValueError("At least 3 points are required for Savitzky-Golay smoothing.")

    try:
        from scipy.signal import savgol_filter
    except Exception as exc:
        raise ImportError("Savitzky-Golay smoothing requires scipy.signal.savgol_filter.") from exc

    # Savitzky-Golay needs odd window length and window <= data length.
    n_eff = n_req
    if n_eff % 2 == 0:
        n_eff += 1
    if n_eff > y_arr.size:
        n_eff = int(y_arr.size if (y_arr.size % 2 == 1) else (y_arr.size - 1))
    if n_eff < 3:
        raise ValueError("Not enough points for the requested smooth window.")

    polyorder = min(3, n_eff - 1)
    y_smoothed = savgol_filter(y_arr, window_length=n_eff, polyorder=polyorder, mode="interp")
    x_smoothed = np.array(x_arr, copy=True)
    y_smoothed = np.asarray(y_smoothed, dtype=np.float64)
    if return_effective_window:
        return x_smoothed, y_smoothed, n_eff
    return x_smoothed, y_smoothed


def powerxrd_moving_average_filter(x, y, window_points, return_effective_window=False):
    n_req = int(window_points)
    if n_req < 1:
        raise ValueError("Window size must be at least 1 point for moving average smoothing.")
    x_arr = np.asarray(x, dtype=np.float64)
    y_arr = np.asarray(y, dtype=np.float64)
    if x_arr.size != y_arr.size:
        raise ValueError("x and y must have the same length.")
    if y_arr.size < 1:
        raise ValueError("At least 1 point is required for moving average smoothing.")

    n_eff = min(n_req, int(y_arr.size))
    if n_eff < 1:
        raise ValueError("Not enough points for the requested smooth window.")
    kernel = np.ones(n_eff, dtype=np.float64) / float(n_eff)
    y_smoothed = np.convolve(y_arr, kernel, mode="same")
    x_smoothed = np.array(x_arr, copy=True)
    y_smoothed = np.asarray(y_smoothed, dtype=np.float64)
    if return_effective_window:
        return x_smoothed, y_smoothed, n_eff
    return x_smoothed, y_smoothed


def powerxrd_gaussian_smooth(x, y, sigma_points):
    sigma = float(sigma_points)
    if sigma <= 0:
        raise ValueError("Sigma must be > 0 for Gaussian smoothing.")
    x_arr = np.asarray(x, dtype=np.float64)
    y_arr = np.asarray(y, dtype=np.float64)
    if x_arr.size != y_arr.size:
        raise ValueError("x and y must have the same length.")
    if y_arr.size < 1:
        raise ValueError("At least 1 point is required for Gaussian smoothing.")

    try:
        from scipy.ndimage import gaussian_filter1d
    except Exception as exc:
        raise ImportError("Gaussian smoothing requires scipy.ndimage.gaussian_filter1d.") from exc

    y_smoothed = gaussian_filter1d(y_arr, sigma=sigma, mode="nearest")
    return np.array(x_arr, copy=True), np.asarray(y_smoothed, dtype=np.float64)


def powerxrd_moving_average(x, y, window_points, return_effective_window=False):
    """
    Compatibility wrapper kept for existing call sites.
    """
    return powerxrd_savgol_smooth(
        x,
        y,
        window_points,
        return_effective_window=return_effective_window,
    )


def powerxrd_background_subtraction(x, y, lam=1e6, max_iter=30, conv=1e-3):
    """
    arPLS baseline correction.
    Returns background-subtracted intensity (clipped at 0).
    """
    x_arr = np.asarray(x, dtype=np.float64)
    y_arr = np.asarray(y, dtype=np.float64)
    if x_arr.size != y_arr.size:
        raise ValueError("x and y must have the same length.")
    length = y_arr.size
    if length < 5:
        return x_arr.copy(), np.maximum(y_arr, 0.0)

    lam = float(lam)
    max_iter = int(max_iter)
    conv = float(conv)
    if lam <= 0:
        raise ValueError("lam must be > 0.")
    if max_iter < 1:
        raise ValueError("max_iter must be >= 1.")
    if conv <= 0:
        raise ValueError("conv must be > 0.")

    try:
        from scipy import sparse
        from scipy.sparse.linalg import spsolve
    except Exception as exc:
        raise ImportError("arPLS requires scipy (sparse + spsolve).") from exc

    # Second-order difference operator.
    diff_mat = sparse.diags([1.0, -2.0, 1.0], [0, 1, 2], shape=(length - 2, length), format="csc")
    penalty = lam * (diff_mat.T @ diff_mat)
    weights = np.ones(length, dtype=np.float64)
    baseline = np.zeros(length, dtype=np.float64)

    for _ in range(max_iter):
        w_mat = sparse.diags(weights, 0, shape=(length, length), format="csc")
        baseline = spsolve(w_mat + penalty, weights * y_arr)

        residual = y_arr - baseline
        neg = residual[residual < 0]
        if neg.size < 2:
            break
        mean_neg = float(np.mean(neg))
        std_neg = float(np.std(neg))
        if std_neg < 1e-12:
            break

        # arPLS logistic reweighting; clip exponent for numeric stability.
        exponent = 2.0 * (residual - (2.0 * std_neg - mean_neg)) / std_neg
        exponent = np.clip(exponent, -60.0, 60.0)
        new_weights = 1.0 / (1.0 + np.exp(exponent))

        denom = max(float(np.linalg.norm(weights)), 1e-12)
        rel = float(np.linalg.norm(new_weights - weights) / denom)
        weights = new_weights
        if rel < conv:
            break

    corrected = y_arr - baseline
    corrected = np.maximum(corrected, 0.0)
    return x_arr.copy(), corrected


def estimate_peak_fwhm(x, y, peak_idx):
    x_arr = np.asarray(x, dtype=np.float64)
    y_arr = np.asarray(y, dtype=np.float64)
    if x_arr.size != y_arr.size or x_arr.size < 3:
        return None, None, None, None
    if peak_idx < 0 or peak_idx >= x_arr.size:
        return None, None, None, None

    peak_y = float(y_arr[peak_idx])
    baseline = float(np.min(y_arr))
    if not np.isfinite(peak_y) or not np.isfinite(baseline) or peak_y <= baseline:
        return None, None, None, None
    half_y = baseline + 0.5 * (peak_y - baseline)

    left_x = None
    for i in range(peak_idx, 0, -1):
        y0 = float(y_arr[i - 1])
        y1 = float(y_arr[i])
        if (y0 <= half_y <= y1) or (y1 <= half_y <= y0):
            dy = y1 - y0
            frac = 0.0 if abs(dy) < 1e-12 else (half_y - y0) / dy
            left_x = float(x_arr[i - 1] + frac * (x_arr[i] - x_arr[i - 1]))
            break

    right_x = None
    for i in range(peak_idx, x_arr.size - 1):
        y0 = float(y_arr[i])
        y1 = float(y_arr[i + 1])
        if (y0 >= half_y >= y1) or (y1 >= half_y >= y0):
            dy = y1 - y0
            frac = 0.0 if abs(dy) < 1e-12 else (half_y - y0) / dy
            right_x = float(x_arr[i] + frac * (x_arr[i + 1] - x_arr[i]))
            break

    if left_x is None or right_x is None or right_x <= left_x:
        return None, left_x, right_x, half_y
    return (right_x - left_x), left_x, right_x, half_y


# ---------------- Reorderable List Widget ---------------- #

class ReorderListWidget(QListWidget):
    orderChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)

    def dropEvent(self, event):
        super().dropEvent(event)
        self.orderChanged.emit()


class PxrdFileBrowserTree(QTreeWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setColumnCount(1)
        self.setHeaderLabel("Files")
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAlternatingRowColors(True)
        self.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

    def startDrag(self, supported_actions):
        selected_items = self.selectedItems()
        file_paths = []
        for item in selected_items:
            p = item.data(0, Qt.ItemDataRole.UserRole)
            if p and os.path.isfile(p):
                file_paths.append(p)

        if not file_paths:
            return

        # Keep stable ordering for multi-file drag to avoid nondeterministic order after drop.
        file_paths = sorted(file_paths, key=lambda p: p.lower())
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(path) for path in file_paths])
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)


class ChemicalTextDelegate(QStyledItemDelegate):
    def __init__(self, html_formatter=None, parent=None):
        super().__init__(parent)
        self._html_formatter = html_formatter or (lambda text: html.escape("" if text is None else str(text)))

    def _document(self, text, option):
        role = QPalette.ColorRole.HighlightedText if option.state & QStyle.StateFlag.State_Selected else QPalette.ColorRole.Text
        color = option.palette.color(role).name()
        doc = QTextDocument()
        doc.setDefaultFont(option.font)
        doc.setDocumentMargin(0)
        doc.setHtml(f'<span style="color: {color};">{self._html_formatter(text)}</span>')
        return doc

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        widget = opt.widget
        style = widget.style() if widget else QApplication.style()
        text = opt.text
        opt.text = ""
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)
        text_rect = style.subElementRect(QStyle.SubElement.SE_ItemViewItemText, opt, widget)
        doc = self._document(text, opt)
        doc.setTextWidth(-1)
        text_h = doc.size().height()
        y_offset = max(0.0, (float(text_rect.height()) - text_h) / 2.0)
        painter.save()
        painter.translate(text_rect.left(), text_rect.top() + y_offset)
        painter.setClipRect(QRectF(0, 0, text_rect.width(), text_rect.height()))
        doc.drawContents(painter, QRectF(0, 0, text_rect.width(), text_rect.height()))
        painter.restore()

    def sizeHint(self, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        doc = self._document(opt.text, opt)
        doc.setTextWidth(-1)
        return QSize(
            max(int(doc.idealWidth()) + 6, opt.fontMetrics.horizontalAdvance(opt.text) + 6),
            opt.fontMetrics.height() + 6,
        )


# ---------------- Main UI ---------------- #

class ClickableSplashScreen(QSplashScreen):
    def __init__(self, pixmap, url, overlay_text=""):
        super().__init__(pixmap, Qt.WindowType.WindowStaysOnTopHint)
        self._url = QUrl(url)
        self._overlay_lines = [line for line in str(overlay_text).splitlines() if line.strip()]
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(url)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._url.isValid():
            QDesktopServices.openUrl(self._url)
        event.accept()

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._overlay_lines:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        title_font = QFont("Arial")
        title_font.setBold(True)
        title_font.setPixelSize(15)

        body_font = QFont("Arial")
        body_font.setBold(False)
        body_font.setPixelSize(15)

        fonts = [title_font] + [body_font] * max(0, len(self._overlay_lines) - 1)
        line_gap = max(2, int(self.height() * 0.01))
        left_margin = max(10, int(self.width() * 0.015))
        bottom_margin = max(8, int(self.height() * 0.03))

        heights = []
        for f in fonts:
            painter.setFont(f)
            heights.append(painter.fontMetrics().height())
        total_h = sum(heights) + line_gap * max(0, len(self._overlay_lines) - 1)

        y = self.height() - bottom_margin - total_h
        shadow = QColor(0, 0, 0, 150)
        fg = QColor(255, 255, 255, 245)

        for text, f, h in zip(self._overlay_lines, fonts, heights):
            painter.setFont(f)
            fm = painter.fontMetrics()
            baseline = y + fm.ascent()
            painter.setPen(shadow)
            painter.drawText(left_margin + 1, baseline + 1, text)
            painter.setPen(fg)
            painter.drawText(left_margin, baseline, text)
            y += h + line_gap


def read_pattern_batch(paths):
    """Read only data in a worker; never touch Qt or matplotlib here."""
    loaded, errors, seen = [], [], set()
    for path in paths:
        key = os.path.normcase(os.path.abspath(path))
        if key in seen or os.path.splitext(path)[1].lower() not in SUPPORTED_PXRD_EXTS:
            continue
        seen.add(key)
        try:
            x, y = load_pattern_file(path)
            loaded.append((path, x, y))
        except Exception as exc:
            errors.append(f"{path}\n{exc}")
    return loaded, errors


def _read_patterns_to_queue(paths, results):
    results.put(read_pattern_batch(paths))


class FileOpenService(QObject):
    """One window per user; subsequent launches queue files in that window."""

    def __init__(self, name=None):
        super().__init__()
        identity = hashlib.sha256(os.path.expanduser("~").encode("utf-8")).hexdigest()[:20]
        self.name = name or ("xStack-files-" + identity)
        self.lock = QLockFile(os.path.join(tempfile.gettempdir(), self.name + ".lock"))
        self.lock.setStaleLockTime(0)
        self.server = QLocalServer(self)
        self.server.newConnection.connect(self._accept)
        self.window = None
        self.pending = []
        self.dispatching = False
        self.clients = {}
        self.owns_lock = False

    def start_or_forward(self, paths):
        if self.lock.tryLock(0):
            self.owns_lock = True
            QLocalServer.removeServer(self.name)
            if not self.server.listen(self.name):
                self.close()
                raise RuntimeError("Cannot start file-open service: " + self.server.errorString())
            self.pending.append(paths)
            return True
        socket = QLocalSocket(self)
        for _ in range(5):
            socket.connectToServer(self.name)
            if socket.waitForConnected(1000):
                break
            socket.abort()
            # Another process may still be starting its local server.
            import time
            time.sleep(0.1)
        else:
            raise RuntimeError("The existing xStack window is not responding. Close it and try again.")
        payload = json.dumps(paths, ensure_ascii=False).encode("utf-8") + b"\n"
        socket.write(payload)
        if socket.bytesToWrite() and not socket.waitForBytesWritten(5000):
            raise RuntimeError("Could not send files to the existing xStack window.")
        if not socket.bytesAvailable() and not socket.waitForReadyRead(10000):
            raise RuntimeError("xStack did not confirm receipt. Check the existing window before trying again.")
        if bytes(socket.readAll()) != b"OK\n":
            raise RuntimeError("xStack rejected the file-open request.")
        socket.disconnectFromServer()
        return False

    def _accept(self):
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            self.clients[socket] = bytearray()
            socket.readyRead.connect(lambda sock=socket: self._receive(sock))
            socket.disconnected.connect(lambda sock=socket: self._discard(sock))
            if socket.bytesAvailable():
                self._receive(socket)

    def _discard(self, socket):
        self.clients.pop(socket, None)
        socket.deleteLater()

    def _receive(self, socket):
        buffer = self.clients.get(socket)
        if buffer is None:
            return
        buffer.extend(bytes(socket.readAll()))
        if len(buffer) > 1024 * 1024:
            socket.disconnectFromServer()
            return
        if b"\n" not in buffer:
            return
        try:
            paths = json.loads(bytes(buffer).decode("utf-8"))
            if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
                raise ValueError("Invalid file list")
        except (ValueError, UnicodeError):
            socket.disconnectFromServer()
            return
        self.pending.append(paths)
        self.clients.pop(socket, None)
        socket.write(b"OK\n")
        socket.flush()
        socket.disconnectFromServer()
        QTimer.singleShot(0, self._dispatch)

    def attach_window(self, window):
        self.window = window
        QTimer.singleShot(0, self._dispatch)

    def _dispatch(self):
        if self.window is None or self.dispatching:
            return
        self.dispatching = True
        try:
            while self.pending:
                paths = self.pending.pop(0)
                if paths:
                    self.window.open_external_files(paths, background=True)
                if self.window.isMinimized():
                    self.window.showNormal()
                self.window.raise_()
                self.window.activateWindow()
        finally:
            self.dispatching = False

    def close(self):
        if self.owns_lock:
            self.server.close()
            self.lock.unlock()
            self.owns_lock = False


class PXRDMultiCompareApp(MainWindowUiMixin, QMainWindow):
    browser_tree_type = PxrdFileBrowserTree
    file_list_type = ReorderListWidget
    text_delegate_type = ChemicalTextDelegate

    def __init__(self, startup_paths=None, settings=None):
        super().__init__()
        self.startup_paths = [os.path.abspath(p) for p in (startup_paths or [])]
        self.settings = settings if settings is not None else QSettings("xStack", "xStack")
        self.setWindowTitle("xStack")
        icon_path = resolve_app_icon_path()
        if icon_path:
            self.setWindowIcon(QIcon(icon_path))
        self.resize(1200, 850)
        self.setMinimumSize(980, 640)
        self.setAcceptDrops(True)

        # Default project directory (open/save)
        default_root_dir = r"C:\Users"
        if not os.path.isdir(default_root_dir):
            default_root_dir = os.path.expanduser("~")
        self.project_dir = default_root_dir
        self.browser_root_dir = str(self.settings.value("browser/root_dir", default_root_dir))
        self.supported_exts = set(SUPPORTED_PXRD_EXTS)
        try:
            os.makedirs(self.project_dir, exist_ok=True)
        except Exception:
            pass

        # Data and state
        self.patterns = []
        self.axes = []
        self.plot_lines = []
        self.label_texts = []
        self.hover_annotation = None
        self.cursor_vlines = []
        self.cursor_vline = None
        self.cursor_text = None
        self.ax = None
        self.title_artist = None
        self.x_label_artist = None
        self.y_label_artist = None
        self.zoom_rect = None
        self.zoom_start_vlines = []
        self.zoom_end_vlines = []
        self.analysis_artists = []
        self.applied_peak_overlay = None
        self.preview_ax = None
        self.press_event = None
        self.pan_event = None

        # Label group drag (global offset in data coordinates)
        self.dragging_label = False
        self.drag_start_data = None
        self.drag_start_global_offset = None
        self.drag_label_axis = None
        self.drag_label_idx = None
        self.dragging_curve_idx = None
        self.drag_curve_axis = None
        self.drag_curve_start_ydata = None
        self.drag_curve_start_shift = None
        self.drag_curve_moved = False
        self.global_label_offset = (0.0, 0.0)

        self.xlim_full = self.ylim_full = None
        self._base_gap_current = 1.0
        self.base_fig_dpi = 100.0
        self.canvas_view_scale = 1.3
        self.canvas_view_scale_min = 0.25
        self.canvas_view_scale_max = 4.0

        # Axis label/title text
        self.title_text = ""
        self.x_axis_label_text = "2θ (deg)"
        self.y_axis_label_text = "Intensity (a.u.)"
        self.show_top_labels = False
        self.show_curve_labels = True
        # Fixed values after removing manual controls.
        self.fixed_y_headroom = 1.2
        self.fixed_label_y_spacing = 1.0
        self.fixed_label_x_position = 0.04
        self.fixed_label_y_position = 0.90

        # Unique id for each pattern
        self._uid_counter = 0

        # Selected curve
        self.selected_idx = None

        # Undo stack
        self.undo_stack = []
        self.undo_limit = 30
        self._restoring_undo = False

        # Widgets
        self.color_mode_combo = None
        self.svg_bg_combo = None
        self.sort_by_filename_cb = None
        self.superimpose_cb = None
        self.curve_lw_spin = None     # Curve line width
        self.frame_lw_spin = None     # Frame line width (including ticks)
        self.curve_offset_spin = None # Global vertical offset for all curves
        self.curve_label_fs_spin = None
        self.tick_fs_spin = None
        self.axis_label_fs_spin = None
        self.title_fs_spin = None
        self.xmin_spin = None
        self.xmax_spin = None
        self.curve_labels_btn = None
        self.browser_root_edit = None
        self.browser_search_edit = None
        self.del_idx_edit = None
        self.x_axis_label_edit = None
        self.y_axis_label_edit = None
        self.powerxrd_tools_dialog = None
        self._powerxrd_live_updating = False
        self._powerxrd_preview_data = None
        self.preview_ax = None
        self._clipboard_temp_png = None
        self.scale_overlay = None
        self.canvas_scroll = None
        self.system_display_scale = 1.0
        self._screen_change_connected = False

        self._import_pending = []
        self._import_active = False
        self._import_results = queue.Queue()
        self._import_timer = QTimer(self)
        self._import_timer.setInterval(40)
        self._import_timer.timeout.connect(self._poll_import)
        self._browser_search_timer = QTimer(self)
        self._browser_search_timer.setSingleShot(True)
        self._browser_search_timer.setInterval(300)
        self._browser_search_timer.timeout.connect(self.populate_browser_tree)
        self._cursor_draw_timer = QTimer(self)
        self._cursor_draw_timer.setSingleShot(True)
        self._cursor_draw_timer.setInterval(33)
        self._cursor_draw_timer.timeout.connect(self.canvas_draw_idle)
        self._init_ui()
        self._factory_plot_defaults = self._collect_plot_defaults()
        self.restore_plot_defaults()

    # ---------------- UI ---------------- #

    _plot_default_numbers = (
        "curve_label_fs_spin", "curve_lw_spin", "tick_fs_spin", "frame_lw_spin",
        "axis_label_fs_spin", "curve_offset_spin", "fig_w_spin", "fig_h_spin",
        "xmin_spin", "xmax_spin",
    )
    _plot_default_checks = ("norm_cb", "live_norm_cb", "superimpose_cb", "curve_labels_btn")

    def _collect_plot_defaults(self):
        values = {name: getattr(self, name).value() for name in self._plot_default_numbers}
        values.update({name: getattr(self, name).isChecked() for name in self._plot_default_checks})
        values["color_mode"] = self.color_mode_combo.currentData()
        return values

    def reset_plot_defaults(self):
        """Restore built-in parameters; persistence remains an explicit Save action."""
        if self.patterns:
            self._push_undo_snapshot()
        self._apply_plot_defaults(self._factory_plot_defaults, reset_colors=True)
        self.statusBar().showMessage("Original plot defaults restored. Use Save parameters to keep them for future sessions.", 7000)

    def save_plot_defaults(self):
        if self.xmin_spin.value() >= self.xmax_spin.value():
            QMessageBox.warning(self, "Plot defaults", "Display range requires X min < X max.")
            return
        values = self._collect_plot_defaults()
        self.settings.setValue("plot/defaults", json.dumps(values))
        self.settings.sync()
        if self.settings.status() != QSettings.Status.NoError:
            QMessageBox.warning(self, "Plot defaults", "Could not save plotting defaults.")
            return
        self.statusBar().showMessage("Plot defaults saved for future sessions", 5000)

    def restore_plot_defaults(self):
        try:
            values = json.loads(str(self.settings.value("plot/defaults", "{}")))
        except (ValueError, TypeError):
            return
        self._apply_plot_defaults(values)

    def _apply_plot_defaults(self, values, reset_colors=False):
        if not isinstance(values, dict):
            return
        for name in self._plot_default_numbers + self._plot_default_checks:
            if name not in values:
                continue
            control = getattr(self, name)
            previous = control.blockSignals(True)
            try:
                value = values[name]
                if name in self._plot_default_checks:
                    if isinstance(value, bool):
                        control.setChecked(value)
                elif isinstance(value, (int, float)) and np.isfinite(value):
                    if isinstance(control, QSpinBox):
                        value = int(value)
                    control.setValue(value)
            finally:
                control.blockSignals(previous)
        if self.xmin_spin.value() >= self.xmax_spin.value():
            for control, value in ((self.xmin_spin, 2.0), (self.xmax_spin, 30.0)):
                previous = control.blockSignals(True)
                control.setValue(value)
                control.blockSignals(previous)
        index = self.color_mode_combo.findData(values.get("color_mode", "black"))
        previous = self.color_mode_combo.blockSignals(True)
        self.color_mode_combo.setCurrentIndex(max(0, index))
        self.color_mode_combo.blockSignals(previous)
        self.show_curve_labels = self.curve_labels_btn.isChecked()
        self.curve_labels_btn.setText("Curve labels: On" if self.show_curve_labels else "Curve labels: Off")
        if reset_colors:
            for i, pattern in enumerate(self.patterns):
                pattern["color"] = self._color_for_index(i)
        self._on_fig_size_changed(0)

    def _get_system_display_scale(self):
        screen = None
        try:
            win = self.windowHandle()
            if win is not None:
                screen = win.screen()
        except Exception:
            screen = None
        if screen is None:
            try:
                screen = QApplication.primaryScreen()
            except Exception:
                screen = None
        if screen is None:
            return 1.0

        vals = []
        try:
            dpr = float(screen.devicePixelRatio())
            if np.isfinite(dpr) and dpr > 0:
                vals.append(dpr)
        except Exception:
            pass
        try:
            logical_dpi = float(screen.logicalDotsPerInch())
            dpi_scale = logical_dpi / 96.0
            if np.isfinite(dpi_scale) and dpi_scale > 0:
                vals.append(dpi_scale)
        except Exception:
            pass
        if not vals:
            return 1.0
        return float(max(vals))

    def _apply_canvas_size_lock(self):
        if not hasattr(self, "canvas") or self.canvas is None:
            return
        if not hasattr(self, "fig") or self.fig is None:
            return
        if self.fig_w_spin is None or self.fig_h_spin is None:
            return
        scale = float(getattr(self, "canvas_view_scale", 1.0))
        if not np.isfinite(scale):
            scale = 1.0
        scale = min(
            float(getattr(self, "canvas_view_scale_max", 4.0)),
            max(float(getattr(self, "canvas_view_scale_min", 0.25)), scale),
        )
        self.canvas_view_scale = scale

        base_dpi = float(getattr(self, "base_fig_dpi", 100.0))
        view_dpi = base_dpi * scale
        system_scale = float(self._get_system_display_scale())
        if not np.isfinite(system_scale) or system_scale <= 0:
            system_scale = 1.0
        self.system_display_scale = system_scale
        self.fig.set_dpi(view_dpi)

        # Compensate OS display scaling so canvas visual size stays consistent.
        logical_dpi = view_dpi / system_scale
        w_px = max(1, int(round(float(self.fig_w_spin.value()) * logical_dpi)))
        h_px = max(1, int(round(float(self.fig_h_spin.value()) * logical_dpi)))
        self.canvas.setMinimumSize(w_px, h_px)
        self.canvas.setMaximumSize(w_px, h_px)
        self._update_scale_overlay()

    def _set_canvas_view_scale(self, new_scale):
        if not np.isfinite(new_scale):
            return
        scale_min = float(getattr(self, "canvas_view_scale_min", 0.25))
        scale_max = float(getattr(self, "canvas_view_scale_max", 4.0))
        scale = min(scale_max, max(scale_min, float(new_scale)))
        if abs(scale - float(getattr(self, "canvas_view_scale", 1.0))) < 1e-12:
            self._update_scale_overlay()
            return
        self.canvas_view_scale = scale
        self._apply_canvas_size_lock()
        if self.fig_w_spin is not None and self.fig_h_spin is not None:
            self.fig.set_size_inches(self.fig_w_spin.value(), self.fig_h_spin.value(), forward=True)
        if self.plot_lines:
            self._apply_horizontal_clip_only()
        if self.canvas:
            self.canvas.draw_idle()

    def _reposition_scale_overlay(self):
        if self.scale_overlay is None:
            return
        if self.canvas_scroll is None:
            return
        viewport = self.canvas_scroll.viewport()
        if viewport is None:
            return
        margin = 10
        x = max(margin, viewport.width() - self.scale_overlay.width() - margin)
        self.scale_overlay.move(x, margin)
        self.scale_overlay.raise_()

    def _update_scale_overlay(self):
        if self.scale_overlay is None:
            return
        scale = float(getattr(self, "canvas_view_scale", 1.0))
        if not np.isfinite(scale):
            scale = 1.0
        self.scale_overlay.setText(f"Zoom: {scale:.0%}")
        self.scale_overlay.adjustSize()
        self._reposition_scale_overlay()

    def _on_screen_changed(self, _screen):
        self._apply_canvas_size_lock()
        if self.canvas:
            self.canvas.draw_idle()

    def showEvent(self, event):
        super().showEvent(event)
        if not bool(getattr(self, "_screen_change_connected", False)):
            win = self.windowHandle()
            if win is not None:
                try:
                    win.screenChanged.connect(self._on_screen_changed)
                    self._screen_change_connected = True
                except Exception:
                    pass
        self._apply_canvas_size_lock()

    def _on_fig_size_changed(self, _value):
        self._apply_canvas_size_lock()
        self.update_plot()

    def open_startup_files(self):
        self.open_external_files(self.startup_paths)

    def open_external_files(self, paths, background=False):
        projects, patterns, errors = [], [], []
        for path in paths:
            ext = os.path.splitext(path)[1].lower()
            if not background and not os.path.isfile(path):
                errors.append(f"File not found: {path}")
            elif ext == ".pxrdproj":
                projects.append(path)
            elif ext in self.supported_exts:
                patterns.append(path)
            else:
                errors.append(f"Unsupported file type: {path}")
        if len(projects) == 1:
            if self.patterns:
                errors.append("A project cannot replace the current curves through an external open. Use Open project to load it explicitly.")
            else:
                self.load_project_path(projects[0], show_success=False)
        elif projects:
            errors.append("Please open each PXRD project in a separate window.")
        if patterns:
            (self.queue_patterns_from_paths if background else self.add_patterns_from_paths)(patterns)
        if errors:
            QMessageBox.warning(self, "Open files", "\n".join(errors))

    # ---------------- Frame Line Width ---------------- #

    def _plot_axes(self):
        axes = [ax for ax in getattr(self, "axes", []) if ax is not None]
        if axes:
            return axes
        ax = getattr(self, "ax", None)
        return [ax] if ax is not None else []

    def _primary_ax(self):
        axes = self._plot_axes()
        return axes[0] if axes else None

    def _is_plot_axis(self, ax):
        return ax is not None and ax in self._plot_axes()

    def _is_superimpose_mode(self):
        return bool(self.superimpose_cb and self.superimpose_cb.isChecked())

    def _set_shared_xlim(self, x0, x1):
        for ax in self._plot_axes():
            ax.set_xlim(x0, x1, emit=False)
        if getattr(self, "preview_ax", None) is not None:
            try:
                self.preview_ax.set_xlim(x0, x1)
            except Exception:
                pass
        self._apply_live_normalization()
        self._apply_horizontal_clip_only()

    def _normalization_divisor(self, x, y, xlim=None):
        values = np.asarray(y, dtype=float)
        finite = np.isfinite(values)
        if self.live_norm_cb.isChecked() and xlim is not None:
            lo, hi = sorted(xlim)
            visible = finite & (np.asarray(x) >= lo) & (np.asarray(x) <= hi)
            # Empty windows use the full curve; zero/negative maxima are not divided.
            if np.any(visible):
                finite = visible
        elif not self.norm_cb.isChecked() and not self.live_norm_cb.isChecked():
            return 1.0
        maximum = float(np.max(values[finite])) if np.any(finite) else 0.0
        return maximum if maximum > 0 else 1.0

    def on_live_normalization_toggled(self, _checked):
        ax = self._primary_ax()
        xlim = ax.get_xlim() if ax is not None else None
        self.update_plot(preserve_view=False)
        if xlim is not None and self.patterns:
            self._set_shared_xlim(*xlim)
            self.reposition_labels()
            self.canvas.draw_idle()

    def _apply_live_normalization(self):
        if not self.live_norm_cb.isChecked() or not self.plot_lines:
            return
        xlim = self._primary_ax().get_xlim()
        lo, hi = sorted(xlim)
        bounds = []
        for pattern, info in zip(self.patterns, self.plot_lines):
            x, y = np.asarray(pattern["x"]), np.asarray(pattern["y"])
            divisor = self._normalization_divisor(x, y, xlim)
            base = y / divisor * float(pattern.get("scale", 1.0)) + self.curve_offset_spin.value()
            drawn = base + float(pattern.get("y_shift", 0.0))
            info["y_base"], info["y"] = base, drawn
            info["line"].set_ydata(drawn)
            visible = drawn[(x >= lo) & (x <= hi) & np.isfinite(drawn)]
            if visible.size:
                bounds.extend((float(np.min(visible)), float(np.max(visible))))
        bottom = min(0.0, min(bounds)) if bounds else 0.0
        top = max(bounds) if bounds else 1.0
        span = max(top - bottom, 1e-12)
        top = bottom + span * max(1.0, float(self.fixed_y_headroom))
        if top <= bottom + 1e-12:
            top = bottom + 1.0
        self._base_gap_current = top - bottom
        self._set_shared_ylim(bottom, top)
        preview = self._powerxrd_preview_data
        if isinstance(preview, dict) and self.preview_ax is not None:
            nx, ny = np.asarray(preview["new_x"]), np.asarray(preview["new_y"])
            adjusted = ny / self._normalization_divisor(nx, ny, xlim)
            for line in self.preview_ax.lines:
                if line.get_label() == "Adjusted":
                    line.set_ydata(adjusted)
            visible = adjusted[(nx >= lo) & (nx <= hi) & np.isfinite(adjusted)]
            if visible.size:
                low, high = min(0.0, float(visible.min())), float(visible.max())
                self.preview_ax.set_ylim(low, low + max(high - low, 1e-6) * 1.2)
        self._redraw_applied_peak_overlay()
        self.reposition_labels()
        self.canvas.draw_idle()

    def _set_shared_ylim(self, y0, y1):
        for ax in self._plot_axes():
            ax.set_ylim(y0, y1, emit=False)
        self._apply_horizontal_clip_only()

    def _apply_figure_layout(self):
        # Top labels are disabled, so keep only a small margin for the axes title.
        self.fig.subplots_adjust(top=0.90, bottom=0.14, hspace=0.0)

    def _apply_horizontal_clip_only(self):
        # Clip each curve only in X via data coordinates so display/export match.
        if not self.plot_lines:
            return
        for info in self.plot_lines:
            line = info.get("line")
            ax = info.get("ax")
            if line is None or ax is None:
                continue
            x0, x1 = ax.get_xlim()
            if not (np.isfinite(x0) and np.isfinite(x1)):
                line.set_clip_path(None)
                continue
            left, right = (x0, x1) if x0 <= x1 else (x1, x0)
            if abs(right - left) < 1e-12:
                line.set_clip_path(None)
                line.set_clip_on(False)
                continue

            y0, y1 = ax.get_ylim()
            if not (np.isfinite(y0) and np.isfinite(y1)):
                y0, y1 = -1.0, 1.0
            bottom, top = (y0, y1) if y0 <= y1 else (y1, y0)
            span = max(top - bottom, 1.0)
            pad = span * 20.0
            y_lo = bottom - pad
            y_hi = top + pad
            clip_rect = Rectangle(
                (left, y_lo),
                right - left,
                y_hi - y_lo,
                transform=ax.transData,
            )
            line.set_clip_path(clip_rect)
            line.set_clip_on(True)

    def _reset_full_view(self):
        if not (self.xlim_full and self.ylim_full):
            return False
        self._set_shared_xlim(self.xlim_full[0], self.xlim_full[1])
        if not self.live_norm_cb.isChecked():
            self._set_shared_ylim(self.ylim_full[0], self.ylim_full[1])
        self.reposition_labels()
        self.canvas.draw_idle()
        return True

    def _clear_zoom_guides(self):
        for line in self.zoom_start_vlines:
            try:
                line.remove()
            except Exception:
                pass
        for line in self.zoom_end_vlines:
            try:
                line.remove()
            except Exception:
                pass
        self.zoom_start_vlines = []
        self.zoom_end_vlines = []

    def _show_zoom_guides(self, x_start, x_end):
        axes = self._plot_axes()
        if not axes:
            return
        if len(self.zoom_start_vlines) != len(axes) or len(self.zoom_end_vlines) != len(axes):
            self._clear_zoom_guides()
            for ax in axes:
                self.zoom_start_vlines.append(
                    ax.axvline(x_start, color="#111111", linestyle="--", linewidth=1.0, alpha=0.9, visible=True)
                )
                self.zoom_end_vlines.append(
                    ax.axvline(x_end, color="#111111", linestyle="--", linewidth=1.0, alpha=0.9, visible=True)
                )
            return
        for line in self.zoom_start_vlines:
            line.set_xdata([x_start, x_start])
            line.set_visible(True)
        for line in self.zoom_end_vlines:
            line.set_xdata([x_end, x_end])
            line.set_visible(True)

    def _clear_analysis_artists(self):
        for artist in getattr(self, "analysis_artists", []):
            try:
                artist.remove()
            except Exception:
                pass
        self.analysis_artists = []

    def _redraw_applied_peak_overlay(self):
        self._clear_analysis_artists()
        info = self.applied_peak_overlay if isinstance(self.applied_peak_overlay, dict) else None
        if not info:
            return
        if not self.patterns or not self._plot_axes():
            return

        idx = -1
        uid = info.get("curve_uid", None)
        if uid is not None:
            try:
                uid_i = int(uid)
                for i, p in enumerate(self.patterns):
                    if int(p.get("uid", -1)) == uid_i:
                        idx = i
                        break
            except Exception:
                idx = -1
        if idx < 0:
            try:
                idx = int(info.get("curve_idx", -1))
            except Exception:
                self.applied_peak_overlay = None
                return
        if idx < 0 or idx >= len(self.patterns):
            self.applied_peak_overlay = None
            return

        peak_x = info.get("peak_x", None)
        peak_y = info.get("peak_y", None)
        left_x = info.get("left_x", None)
        right_x = info.get("right_x", None)
        half_y = info.get("half_y", None)

        try:
            peak_x = float(peak_x)
            peak_y = float(peak_y)
        except Exception:
            self.applied_peak_overlay = None
            return
        if not (np.isfinite(peak_x) and np.isfinite(peak_y)):
            self.applied_peak_overlay = None
            return

        pattern = self.patterns[idx]
        x_arr = np.asarray(pattern.get("x", []), dtype=np.float64)
        y_arr = np.asarray(pattern.get("y", []), dtype=np.float64)
        if x_arr.size < 2 or y_arr.size < 2 or x_arr.size != y_arr.size:
            return

        ax = None
        if 0 <= idx < len(self.plot_lines):
            ax = self.plot_lines[idx].get("ax")
        if ax is None:
            ax = self._primary_ax()
        if ax is None:
            self.applied_peak_overlay = None
            return
        divisor = self._normalization_divisor(x_arr, y_arr, ax.get_xlim())
        scale = float(pattern.get("scale", 1.0))
        curve_offset = float(self.curve_offset_spin.value()) if self.curve_offset_spin else 0.03
        curve_offset += float(pattern.get("y_shift", 0.0))
        amp = scale / divisor

        peak_y_draw = peak_y * amp + curve_offset
        self.analysis_artists.append(
            ax.plot([peak_x], [peak_y_draw], marker="o", color="#d32f2f", markersize=5, zorder=10)[0]
        )
        self.analysis_artists.append(
            ax.axvline(peak_x, color="#d32f2f", linestyle="--", linewidth=1.0, alpha=0.9)
        )

        try:
            lx = float(left_x) if left_x is not None else None
            rx = float(right_x) if right_x is not None else None
            hy = float(half_y) if half_y is not None else None
        except Exception:
            lx = rx = hy = None

        if (
            lx is not None and rx is not None and hy is not None
            and np.isfinite(lx) and np.isfinite(rx) and np.isfinite(hy)
        ):
            half_y_draw = hy * amp + curve_offset
            self.analysis_artists.append(
                ax.hlines(half_y_draw, lx, rx, colors="#1565c0", linestyles="-", linewidth=1.2)
            )

    def _build_empty_plot(self):
        self.fig.clear()
        self._apply_figure_layout()

        self.plot_lines = []
        self.label_texts = []
        self.cursor_vlines = []
        self.cursor_vline = None
        self.cursor_text = None
        self.zoom_rect = None
        self.zoom_start_vlines = []
        self.zoom_end_vlines = []
        self.analysis_artists = []
        self.applied_peak_overlay = None
        self.preview_ax = None
        self.press_event = None
        self.pan_event = None
        self.drag_label_idx = None
        self.dragging_curve_idx = None
        self.drag_curve_axis = None
        self.drag_curve_start_ydata = None
        self.drag_curve_start_shift = None
        self.drag_curve_moved = False
        self.ax = None
        self.axes = []
        self.title_artist = None
        self.x_label_artist = None
        self.y_label_artist = None
        self.fig.suptitle("")

    def apply_frame_style(self, frame_lw: float):
        axes = self._plot_axes()
        if not axes:
            return
        tick_len = max(2.0, 3.5 * frame_lw)
        last_idx = len(axes) - 1
        for i, ax in enumerate(axes):
            for name, spine in ax.spines.items():
                spine.set_linewidth(frame_lw)
                spine.set_zorder(0.5)
                if name == "top":
                    spine.set_visible(i == 0)
                elif name == "bottom":
                    spine.set_visible(i == last_idx)
                else:
                    spine.set_visible(True)
            ax.tick_params(axis="both", which="both", width=frame_lw, length=tick_len)

    # ---------------- Curve Color Mode ---------------- #

    def _current_color_mode(self):
        if not self.color_mode_combo:
            return "black"
        mode = self.color_mode_combo.currentData()
        mode = str(mode) if mode is not None else "black"
        return mode if mode in ("black", "colorful") else "black"

    def _current_svg_background(self):
        if not self.svg_bg_combo:
            return "transparent"
        mode = self.svg_bg_combo.currentData()
        mode = str(mode) if mode is not None else "transparent"
        return mode if mode in ("transparent", "white") else "transparent"

    def _palette_color(self, idx):
        colors = [
            "#ED1C24",  # RGB(237, 28, 36)
            "#343695",  # RGB(52, 54, 149)
            "#B0A06B",  # RGB(176, 160, 107)
            "#1EB8F1",  # RGB(30, 184, 241)
            "#0DAB59",  # RGB(13, 171, 89)
            "#F9A642",  # RGB(249, 166, 66)
            "#A2459F",  # RGB(162, 69, 159)
            "#59A3AC",  # RGB(89, 163, 172)
        ]
        return colors[int(idx) % len(colors)]

    def _color_for_index(self, idx):
        return "#000000" if self._current_color_mode() == "black" else self._palette_color(idx)

    def _apply_color_mode_to_patterns(self):
        if not self.patterns:
            return
        mode = self._current_color_mode()
        for i, p in enumerate(self.patterns):
            p["color"] = "#000000" if mode == "black" else self._palette_color(i)
        self.update_plot(preserve_view=True)
        self.refresh_files_list()

    def on_color_mode_changed(self, _idx):
        if self.patterns and not self._restoring_undo:
            self._push_undo_snapshot()
        self._apply_color_mode_to_patterns()

    # ---------------- SVG/PNG: Generate in Memory + Copy ---------------- #

    def _make_svg_bytes(self, background_mode=None):
        # Export exactly what is currently on canvas: no style rewrite, no auto-cropping.
        if self.canvas:
            self.canvas.draw()

        # Keep X-only clipping aligned with the current renderer bbox.
        self._apply_horizontal_clip_only()
        if self.canvas:
            self.canvas.draw()

        bg_mode = str(background_mode) if background_mode is not None else self._current_svg_background()
        if bg_mode not in ("transparent", "white"):
            bg_mode = "transparent"

        buf = io.BytesIO()
        save_kwargs = {
            "format": "svg",
            "bbox_inches": None,
            "pad_inches": 0.0,
        }
        if bg_mode == "transparent":
            # Must keep face/edge as "none"; otherwise an explicit white facecolor can override transparency.
            save_kwargs.update({
                "transparent": True,
                "facecolor": "none",
                "edgecolor": "none",
            })
        else:
            save_kwargs.update({
                "transparent": False,
                "facecolor": "white",
                "edgecolor": "white",
            })
        self.fig.savefig(buf, **save_kwargs)
        return buf.getvalue()

    def _make_png_bytes(self, background_mode=None):
        # Export exactly what is currently on canvas as raster PNG.
        if self.canvas:
            self.canvas.draw()

        # Keep X-only clipping aligned with the current renderer bbox.
        self._apply_horizontal_clip_only()
        if self.canvas:
            self.canvas.draw()

        bg_mode = str(background_mode) if background_mode is not None else self._current_svg_background()
        if bg_mode not in ("transparent", "white"):
            bg_mode = "transparent"

        buf = io.BytesIO()
        save_kwargs = {
            "format": "png",
            "bbox_inches": None,
            "pad_inches": 0.0,
            "dpi": float(getattr(self, "base_fig_dpi", 100.0)),
        }
        if bg_mode == "transparent":
            save_kwargs.update({
                "transparent": True,
                # Keep alpha=0 but use white RGB to avoid black appearance
                # in viewers that do not handle premultiplied alpha correctly.
                "facecolor": (1.0, 1.0, 1.0, 0.0),
                "edgecolor": (1.0, 1.0, 1.0, 0.0),
            })
        else:
            save_kwargs.update({
                "transparent": False,
                "facecolor": "white",
                "edgecolor": "white",
            })
        self.fig.savefig(buf, **save_kwargs)
        return buf.getvalue()

    def copy_svg_to_clipboard(self):
        if not self.patterns:
            QMessageBox.information(self, "Info", "There is no plot to copy.")
            return
        try:
            svg_bytes = self._make_svg_bytes()
            mime = QMimeData()
            mime.setData("image/svg+xml", QByteArray(svg_bytes))
            try:
                mime.setText(svg_bytes.decode("utf-8"))
            except Exception:
                pass
            QApplication.clipboard().setMimeData(mime)
            QMessageBox.information(self, "Success", "SVG copied to clipboard.")
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def copy_png_to_clipboard(self):
        if not self.patterns:
            QMessageBox.information(self, "Info", "There is no plot to copy.")
            return
        try:
            png_bytes = self._make_png_bytes()
            image = QImage.fromData(png_bytes, "PNG")

            # Remove previous temp file (if any) before creating a new one.
            self._cleanup_clipboard_temp_png()
            with tempfile.NamedTemporaryFile(
                mode="wb",
                suffix=".png",
                prefix="xstack_clip_",
                delete=False,
            ) as tmp:
                tmp.write(png_bytes)
                temp_path = tmp.name

            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(temp_path)])
            if not image.isNull():
                mime.setImageData(image)
            QApplication.clipboard().setMimeData(mime)
            self._clipboard_temp_png = temp_path
            QMessageBox.information(self, "Success", "PNG copied to clipboard.")
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def _cleanup_clipboard_temp_png(self):
        path = getattr(self, "_clipboard_temp_png", None)
        self._clipboard_temp_png = None
        if not path:
            return
        try:
            if os.path.isfile(path):
                os.remove(path)
        except Exception:
            pass

    # ---------------- Project File: Save/Load ---------------- #

    def _collect_project_state(self):
        state = {
            "version": 1,
            "applied_peak_overlay": self.applied_peak_overlay,
            "title_text": self.title_text,
            "x_axis_label_text": self.x_axis_label_text,
            "y_axis_label_text": self.y_axis_label_text,
            "show_top_labels": bool(self.show_top_labels),
            "show_curve_labels": bool(self.show_curve_labels),
            "browser_root_dir": self.browser_root_dir,
            "global_label_offset": [float(self.global_label_offset[0]), float(self.global_label_offset[1])],
            "selected_idx": int(self.selected_idx) if self.selected_idx is not None else -1,
            "ui": {
                "norm": bool(self.norm_cb.isChecked()),
                "live_norm": self.live_norm_cb.isChecked(),
                "superimpose": bool(self._is_superimpose_mode()),
                "default_black": bool(self._current_color_mode() == "black"),
                "color_mode": self._current_color_mode(),
                "svg_background": self._current_svg_background(),
                "stack_gap": str(self.fixed_y_headroom),
                "curve_line_width": float(self.curve_lw_spin.value()) if self.curve_lw_spin else 1.0,
                "frame_line_width": float(self.frame_lw_spin.value()) if self.frame_lw_spin else 1.5,
                "curve_y_offset": float(self.curve_offset_spin.value()) if self.curve_offset_spin else 0.03,
                "label_y": float(self.fixed_label_y_spacing),
                "lx": float(self.fixed_label_x_position),
                "ly": float(self.fixed_label_y_position),
                "fig_w": float(self.fig_w_spin.value()),
                "fig_h": float(self.fig_h_spin.value()),
                "curve_label_fs": int(self.curve_label_fs_spin.value()) if self.curve_label_fs_spin else 10,
                "tick_fs": int(self.tick_fs_spin.value()) if self.tick_fs_spin else 10,
                "axis_label_fs": int(self.axis_label_fs_spin.value()) if self.axis_label_fs_spin else 10,
                "title_fs": int(self.title_fs_spin.value()) if self.title_fs_spin else 10,
                "x_display_min": float(self.xmin_spin.value()) if self.xmin_spin else 2.0,
                "x_display_max": float(self.xmax_spin.value()) if self.xmax_spin else 30.0,
            },
            "view": {
                "xlim": list(self._primary_ax().get_xlim()) if self._primary_ax() else None,
                "ylim": list(self._primary_ax().get_ylim()) if self._primary_ax() else None,
            },
            "patterns": []
        }

        for p in self.patterns:
            state["patterns"].append({
                "uid": int(p["uid"]),
                "label": str(p.get("label", "")),
                "filename": str(p.get("filename", "")),
                "source_path": str(p.get("source_path", "")),
                "color": p.get("color", None),
                "scale": float(p.get("scale", 1.0)),
                "y_shift": float(p.get("y_shift", 0.0)),
                "label_dx": float(p.get("label_dx", 0.0)),
                "label_dy": float(p.get("label_dy", 0.0)),
            })
        return state

    # ---------------- Undo ---------------- #

    def _capture_undo_snapshot(self):
        ax = self._primary_ax()
        view_xlim = view_ylim = None
        if ax is not None:
            try:
                view_xlim = tuple(float(v) for v in ax.get_xlim())
                view_ylim = tuple(float(v) for v in ax.get_ylim())
            except Exception:
                view_xlim = view_ylim = None

        patterns = []
        for p in self.patterns:
            patterns.append({
                "uid": int(p.get("uid", 0)),
                "x": np.array(p.get("x", []), copy=True),
                "y": np.array(p.get("y", []), copy=True),
                "label": str(p.get("label", "")),
                "filename": str(p.get("filename", "")),
                "source_path": str(p.get("source_path", "")),
                "color": p.get("color", None),
                "scale": float(p.get("scale", 1.0)),
                "y_shift": float(p.get("y_shift", 0.0)),
                "label_dx": float(p.get("label_dx", 0.0)),
                "label_dy": float(p.get("label_dy", 0.0)),
            })

        return {
            "patterns": patterns,
            "applied_peak_overlay": dict(self.applied_peak_overlay) if self.applied_peak_overlay else None,
            "uid_counter": int(self._uid_counter),
            "selected_idx": int(self.selected_idx) if self.selected_idx is not None else None,
            "global_label_offset": (
                float(self.global_label_offset[0]),
                float(self.global_label_offset[1]),
            ),
            "title_text": str(self.title_text),
            "x_axis_label_text": str(self.x_axis_label_text),
            "y_axis_label_text": str(self.y_axis_label_text),
            "show_curve_labels": bool(self.show_curve_labels),
            "xlim_full": tuple(self.xlim_full) if self.xlim_full is not None else None,
            "ylim_full": tuple(self.ylim_full) if self.ylim_full is not None else None,
            "view_xlim": view_xlim,
            "view_ylim": view_ylim,
            "ui": {
                "norm": bool(self.norm_cb.isChecked()) if self.norm_cb else True,
                "live_norm": self.live_norm_cb.isChecked(),
                "superimpose": bool(self._is_superimpose_mode()),
                "color_mode": self._current_color_mode(),
                "svg_background": self._current_svg_background(),
                "stack_gap": str(self.fixed_y_headroom),
                "curve_line_width": float(self.curve_lw_spin.value()) if self.curve_lw_spin else 1.5,
                "frame_line_width": float(self.frame_lw_spin.value()) if self.frame_lw_spin else 1.5,
                "curve_y_offset": float(self.curve_offset_spin.value()) if self.curve_offset_spin else 0.03,
                "label_y": float(self.fixed_label_y_spacing),
                "lx": float(self.fixed_label_x_position),
                "ly": float(self.fixed_label_y_position),
                "fig_w": float(self.fig_w_spin.value()) if self.fig_w_spin else 7.0,
                "fig_h": float(self.fig_h_spin.value()) if self.fig_h_spin else 6.0,
                "curve_label_fs": int(self.curve_label_fs_spin.value()) if self.curve_label_fs_spin else 10,
                "tick_fs": int(self.tick_fs_spin.value()) if self.tick_fs_spin else 10,
                "axis_label_fs": int(self.axis_label_fs_spin.value()) if self.axis_label_fs_spin else 10,
                "title_fs": int(self.title_fs_spin.value()) if self.title_fs_spin else 10,
                "x_display_min": float(self.xmin_spin.value()) if self.xmin_spin else 2.0,
                "x_display_max": float(self.xmax_spin.value()) if self.xmax_spin else 30.0,
            },
        }

    def _push_undo_snapshot(self):
        if self._restoring_undo:
            return
        self.undo_stack.append(self._capture_undo_snapshot())
        if len(self.undo_stack) > self.undo_limit:
            del self.undo_stack[0]

    def _restore_undo_snapshot(self, snapshot):
        self._restoring_undo = True
        try:
            self.patterns = []
            for p in snapshot.get("patterns", []):
                self.patterns.append({
                    "uid": int(p.get("uid", 0)),
                    "x": np.array(p.get("x", []), copy=True),
                    "y": np.array(p.get("y", []), copy=True),
                    "label": str(p.get("label", "")),
                    "filename": str(p.get("filename", "")),
                    "source_path": str(p.get("source_path", "")),
                    "color": p.get("color", None),
                    "scale": float(p.get("scale", 1.0)),
                    "y_shift": float(p.get("y_shift", 0.0)),
                    "label_dx": float(p.get("label_dx", 0.0)),
                    "label_dy": float(p.get("label_dy", 0.0)),
                })
            self.applied_peak_overlay = snapshot.get("applied_peak_overlay")

            max_uid = max((int(p.get("uid", 0)) for p in self.patterns), default=0)
            self._uid_counter = max(int(snapshot.get("uid_counter", 0)), max_uid)
            self.selected_idx = snapshot.get("selected_idx", None)
            off = snapshot.get("global_label_offset", (0.0, 0.0))
            self.global_label_offset = (float(off[0]), float(off[1]))

            self.title_text = str(snapshot.get("title_text", self.title_text))
            self.x_axis_label_text = str(snapshot.get("x_axis_label_text", self.x_axis_label_text))
            self.y_axis_label_text = str(snapshot.get("y_axis_label_text", self.y_axis_label_text))
            self.show_curve_labels = bool(snapshot.get("show_curve_labels", self.show_curve_labels))

            if self.curve_labels_btn:
                self.curve_labels_btn.blockSignals(True)
                self.curve_labels_btn.setChecked(self.show_curve_labels)
                self.curve_labels_btn.setText("Curve labels: On" if self.show_curve_labels else "Curve labels: Off")
                self.curve_labels_btn.blockSignals(False)

            if self.x_axis_label_edit is not None:
                self.x_axis_label_edit.blockSignals(True)
                self.x_axis_label_edit.setText(self.x_axis_label_text)
                self.x_axis_label_edit.blockSignals(False)
            if self.y_axis_label_edit is not None:
                self.y_axis_label_edit.blockSignals(True)
                self.y_axis_label_edit.setText(self.y_axis_label_text)
                self.y_axis_label_edit.blockSignals(False)

            ui = snapshot.get("ui", {})
            self.live_norm_cb.blockSignals(True)
            self.live_norm_cb.setChecked(bool(ui.get("live_norm", False)))
            self.live_norm_cb.blockSignals(False)
            if self.norm_cb:
                self.norm_cb.blockSignals(True)
                self.norm_cb.setChecked(bool(ui.get("norm", True)))
                self.norm_cb.blockSignals(False)
            if self.superimpose_cb:
                self.superimpose_cb.blockSignals(True)
                self.superimpose_cb.setChecked(bool(ui.get("superimpose", False)))
                self.superimpose_cb.blockSignals(False)
            if self.color_mode_combo:
                mode = str(ui.get("color_mode", "black"))
                idx_mode = self.color_mode_combo.findData(mode)
                self.color_mode_combo.blockSignals(True)
                self.color_mode_combo.setCurrentIndex(idx_mode if idx_mode >= 0 else 0)
                self.color_mode_combo.blockSignals(False)
            if self.svg_bg_combo:
                svg_bg = str(ui.get("svg_background", "transparent"))
                if svg_bg not in ("transparent", "white"):
                    svg_bg = "transparent"
                idx_bg = self.svg_bg_combo.findData(svg_bg)
                self.svg_bg_combo.blockSignals(True)
                self.svg_bg_combo.setCurrentIndex(idx_bg if idx_bg >= 0 else 0)
                self.svg_bg_combo.blockSignals(False)

            spin_values = [
                (self.curve_lw_spin, float(ui.get("curve_line_width", 1.5))),
                (self.frame_lw_spin, float(ui.get("frame_line_width", 1.5))),
                (self.curve_offset_spin, float(ui.get("curve_y_offset", 0.03))),
                (self.fig_w_spin, float(ui.get("fig_w", 7.0))),
                (self.fig_h_spin, float(ui.get("fig_h", 6.0))),
                (self.curve_label_fs_spin, int(ui.get("curve_label_fs", 10))),
                (self.tick_fs_spin, int(ui.get("tick_fs", 10))),
                (self.axis_label_fs_spin, int(ui.get("axis_label_fs", 10))),
                (self.title_fs_spin, int(ui.get("title_fs", 10))),
                (self.xmin_spin, float(ui.get("x_display_min", 2.0))),
                (self.xmax_spin, float(ui.get("x_display_max", 30.0))),
            ]
            for widget, value in spin_values:
                if widget is None:
                    continue
                widget.blockSignals(True)
                widget.setValue(value)
                widget.blockSignals(False)

            if self.patterns:
                self.update_plot(preserve_view=False)
                vx = snapshot.get("view_xlim", None)
                vy = snapshot.get("view_ylim", None)
                if vx and vy:
                    try:
                        self._set_shared_xlim(float(vx[0]), float(vx[1]))
                        if not self.live_norm_cb.isChecked():
                            self._set_shared_ylim(float(vy[0]), float(vy[1]))
                    except Exception:
                        pass
                self.reposition_labels()
                self.canvas.draw_idle()
            else:
                self.clear_plot()

            self.xlim_full = tuple(snapshot.get("xlim_full")) if snapshot.get("xlim_full") else None
            self.ylim_full = tuple(snapshot.get("ylim_full")) if snapshot.get("ylim_full") else None

            self.refresh_files_list()
            if self.selected_idx is not None and self.files_list:
                if 0 <= self.selected_idx < self.files_list.count():
                    self.files_list.setCurrentRow(self.selected_idx)
                else:
                    self.selected_idx = None
        finally:
            self._restoring_undo = False

    def undo_last_action(self):
        if not self.undo_stack:
            return
        snapshot = self.undo_stack.pop()
        self._restore_undo_snapshot(snapshot)

    def save_project(self):
        if not self.patterns:
            QMessageBox.information(self, "Info", "There are no curves to save.")
            return

        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save project",
            self.project_dir,
            "PXRD Project (*.pxrdproj)"
        )
        if not path:
            return
        if not path.lower().endswith(".pxrdproj"):
            path += ".pxrdproj"

        try:
            state = self._collect_project_state()

            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                project_json = json.dumps(state, ensure_ascii=False, indent=2)
                zf.writestr("project.json", project_json)

                for p in self.patterns:
                    uid = int(p["uid"])
                    x = np.asarray(p["x"])
                    y = np.asarray(p["y"])
                    buf = io.BytesIO()
                    np.savez_compressed(buf, x=x, y=y)
                    zf.writestr(f"data/uid_{uid}.npz", buf.getvalue())

            QMessageBox.information(self, "Success", "Project file saved.")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save:\n{e}")

    def load_project(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open project",
            self.project_dir,
            "PXRD Project (*.pxrdproj)"
        )
        if not path:
            return
        self.load_project_path(path)

    def load_project_path(self, path, show_success=True):
        previous = self._capture_undo_snapshot()
        previous_browser_root = self.browser_root_dir
        changing_state = False
        try:
            with zipfile.ZipFile(path, "r") as zf:
                raw = zf.read("project.json").decode("utf-8")
                state = json.loads(raw)

                if "patterns" not in state or "ui" not in state:
                    raise ValueError("Project file is missing required fields.")

                loaded_patterns = []
                loaded_uids = set()
                loaded_uid_counter = 0
                for meta in state["patterns"]:
                    uid = int(meta["uid"])
                    npz_bytes = zf.read(f"data/uid_{uid}.npz")
                    buf = io.BytesIO(npz_bytes)
                    with np.load(buf, allow_pickle=False) as arr:
                        x = np.asarray(arr["x"], dtype=float)
                        y = np.asarray(arr["y"], dtype=float)
                    if (x.ndim != 1 or y.ndim != 1 or not x.size or x.size != y.size
                            or not np.all(np.isfinite(x)) or not np.all(np.isfinite(y))):
                        raise ValueError("Invalid curve coordinates in project.")
                    if uid in loaded_uids:
                        raise ValueError("Duplicate curve UID in project.")
                    loaded_uids.add(uid)

                    loaded_uid_counter = max(loaded_uid_counter, uid)

                    loaded_patterns.append({
                        "uid": uid,
                        "x": x,
                        "y": y,
                        "label": meta.get("label", f"uid_{uid}"),
                        "filename": meta.get("filename", meta.get("label", f"uid_{uid}")),
                        "source_path": meta.get("source_path", ""),
                        "color": meta.get("color", None),
                        "scale": float(meta.get("scale", 1.0)),
                        "y_shift": float(meta.get("y_shift", 0.0)),
                        "label_dx": float(meta.get("label_dx", 0.0)),
                        "label_dy": float(meta.get("label_dy", 0.0)),
                    })

                changing_state = True
                if self.powerxrd_tools_dialog is not None:
                    self.powerxrd_tools_dialog.close()
                self.patterns = []
                self.selected_idx = None

                ui = state.get("ui", {})
                self.live_norm_cb.blockSignals(True)
                self.live_norm_cb.setChecked(bool(ui.get("live_norm", False)))
                self.live_norm_cb.blockSignals(False)
                self.norm_cb.setChecked(bool(ui.get("norm", True)))
                if self.superimpose_cb:
                    self.superimpose_cb.blockSignals(True)
                    self.superimpose_cb.setChecked(bool(ui.get("superimpose", False)))
                    self.superimpose_cb.blockSignals(False)
                if self.color_mode_combo:
                    mode = str(ui.get("color_mode", "black" if bool(ui.get("default_black", True)) else "colorful"))
                    if mode not in ("black", "colorful"):
                        mode = "black"
                    idx_mode = self.color_mode_combo.findData(mode)
                    self.color_mode_combo.blockSignals(True)
                    self.color_mode_combo.setCurrentIndex(idx_mode if idx_mode >= 0 else 0)
                    self.color_mode_combo.blockSignals(False)
                if self.svg_bg_combo:
                    svg_bg = str(ui.get("svg_background", "transparent"))
                    if svg_bg not in ("transparent", "white"):
                        svg_bg = "transparent"
                    idx_bg = self.svg_bg_combo.findData(svg_bg)
                    self.svg_bg_combo.blockSignals(True)
                    self.svg_bg_combo.setCurrentIndex(idx_bg if idx_bg >= 0 else 0)
                    self.svg_bg_combo.blockSignals(False)

                if self.curve_lw_spin:
                    self.curve_lw_spin.setValue(float(ui.get("curve_line_width", 1.0)))
                if self.frame_lw_spin:
                    self.frame_lw_spin.setValue(float(ui.get("frame_line_width", 1.5)))
                if self.curve_offset_spin:
                    self.curve_offset_spin.setValue(float(ui.get("curve_y_offset", 0.03)))

                self.fig_w_spin.setValue(float(ui.get("fig_w", 7.0)))
                self.fig_h_spin.setValue(float(ui.get("fig_h", 6.0)))
                legacy_fs = int(ui.get("fs", 10))
                if self.curve_label_fs_spin:
                    self.curve_label_fs_spin.setValue(int(ui.get("curve_label_fs", legacy_fs)))
                if self.tick_fs_spin:
                    self.tick_fs_spin.setValue(int(ui.get("tick_fs", legacy_fs)))
                if self.axis_label_fs_spin:
                    self.axis_label_fs_spin.setValue(int(ui.get("axis_label_fs", legacy_fs)))
                if self.title_fs_spin:
                    self.title_fs_spin.setValue(int(ui.get("title_fs", legacy_fs)))
                if self.xmin_spin:
                    self.xmin_spin.setValue(float(ui.get("x_display_min", 2.0)))
                if self.xmax_spin:
                    self.xmax_spin.setValue(float(ui.get("x_display_max", 30.0)))

                self.title_text = str(state.get("title_text", ""))
                self.x_axis_label_text = str(state.get("x_axis_label_text", "2θ (deg)"))
                self.y_axis_label_text = str(state.get("y_axis_label_text", "Intensity (a.u.)"))
                self.browser_root_dir = str(state.get("browser_root_dir", self.browser_root_dir))
                if self.browser_root_edit:
                    self.browser_root_edit.setText(self.browser_root_dir)
                self.show_top_labels = False
                self.show_curve_labels = bool(state.get("show_curve_labels", True))
                if self.curve_labels_btn:
                    self.curve_labels_btn.blockSignals(True)
                    self.curve_labels_btn.setChecked(self.show_curve_labels)
                    self.curve_labels_btn.setText("Curve labels: On" if self.show_curve_labels else "Curve labels: Off")
                    self.curve_labels_btn.blockSignals(False)

                if self.x_axis_label_edit is not None:
                    self.x_axis_label_edit.blockSignals(True)
                    self.x_axis_label_edit.setText(self.x_axis_label_text)
                    self.x_axis_label_edit.blockSignals(False)

                if self.y_axis_label_edit is not None:
                    self.y_axis_label_edit.blockSignals(True)
                    self.y_axis_label_edit.setText(self.y_axis_label_text)
                    self.y_axis_label_edit.blockSignals(False)

                off = state.get("global_label_offset", [0.0, 0.0])
                self.global_label_offset = (float(off[0]), float(off[1]))


            self.patterns = loaded_patterns
            self._uid_counter = loaded_uid_counter
            self.applied_peak_overlay = state.get("applied_peak_overlay")
            sel = int(state.get("selected_idx", -1))
            if 0 <= sel < len(self.patterns):
                self.selected_idx = sel
            else:
                self.selected_idx = None

            self.populate_browser_tree()
            self.refresh_files_list()
            self.update_plot(preserve_view=False)

            view = state.get("view", {})
            xlim = view.get("xlim", None)
            ylim = view.get("ylim", None)
            if xlim and ylim:
                try:
                    self._set_shared_xlim(float(xlim[0]), float(xlim[1]))
                    if not self.live_norm_cb.isChecked():
                        self._set_shared_ylim(float(ylim[0]), float(ylim[1]))
                    self.reposition_labels()
                    self.canvas.draw_idle()
                except Exception:
                    pass

            if not self.patterns:
                self.clear_plot()
            self.undo_stack.clear()
            if show_success:
                QMessageBox.information(self, "Success", "Project file loaded.")
        except Exception as e:
            if changing_state:
                self.browser_root_dir = previous_browser_root
                self.browser_root_edit.setText(previous_browser_root)
                self._restore_undo_snapshot(previous)
                self.populate_browser_tree()
            QMessageBox.critical(self, "Error", f"Failed to load:\n{e}")

    # ---------------- List Refresh/Sorting ---------------- #

    def refresh_files_list(self):
        dialog = self.powerxrd_tools_dialog
        if dialog is not None and getattr(dialog, "pattern_objects", []) != [id(p) for p in self.patterns]:
            dialog.close()
        self.files_list.blockSignals(True)
        self.files_list.clear()
        for p in self.patterns:
            scale = p.get("scale", 1.0)
            suffix = f"  ×{scale:.2f}" if abs(scale - 1.0) > 1e-6 else ""
            item = QListWidgetItem(f"{p['label']}{suffix}")
            item.setIcon(pattern_icon(p.get("color") or "#000000"))
            item.setSizeHint(QSize(0, 23))
            item.setData(Qt.ItemDataRole.UserRole, p["uid"])
            item.setToolTip(
                f"{p['filename']}\n{p.get('source_path', '')}\n"
                f"Label: {p['label']}\nColor: {p.get('color', '')}\nScale: {scale:g}"
            )
            self.files_list.addItem(item)
        self.loaded_count_label.setText(str(len(self.patterns)))

        if self.selected_idx is not None and 0 <= self.selected_idx < self.files_list.count():
            self.files_list.setCurrentRow(self.selected_idx)

        self.files_list.blockSignals(False)

    def on_files_order_changed(self):
        uid_order = []
        for i in range(self.files_list.count()):
            uid_order.append(self.files_list.item(i).data(Qt.ItemDataRole.UserRole))
        old_uid_order = [p["uid"] for p in self.patterns]
        if uid_order != old_uid_order:
            self._push_undo_snapshot()

        uid_to_pattern = {p["uid"]: p for p in self.patterns}
        new_patterns = [uid_to_pattern[uid] for uid in uid_order if uid in uid_to_pattern]

        if len(new_patterns) != len(self.patterns):
            remain = [p for p in self.patterns if p["uid"] not in set(uid_order)]
            new_patterns.extend(remain)

        old_uid = None
        if self.selected_idx is not None and 0 <= self.selected_idx < len(self.patterns):
            old_uid = self.patterns[self.selected_idx]["uid"]

        self.patterns = new_patterns

        if old_uid is not None:
            for i, p in enumerate(self.patterns):
                if p["uid"] == old_uid:
                    self.selected_idx = i
                    break

        self.refresh_files_list()
        self.update_plot()

    def on_list_selection_changed(self, row):
        if row is None or row < 0 or row >= len(self.patterns):
            self.selected_idx = None
            self.apply_selection_highlight()
            return
        self.selected_idx = row
        self.apply_selection_highlight()

    def apply_selection_highlight(self):
        if not self.plot_lines:
            return
        base_lw = float(self.curve_lw_spin.value()) if self.curve_lw_spin else 1.0
        for i, info in enumerate(self.plot_lines):
            line = info.get("line")
            if line is None:
                continue
            if self.selected_idx is not None and i == self.selected_idx:
                line.set_linewidth(base_lw * 2.0)
                line.set_alpha(1.0)
                line.set_zorder(5)
            else:
                line.set_linewidth(base_lw)
                line.set_alpha(0.45 if self.selected_idx is not None else 1.0)
                line.set_zorder(2)
        if self.canvas:
            self.canvas.draw_idle()

    # ---------------- Axis Label Text ---------------- #

    def on_axis_label_text_changed(self):
        if self.x_axis_label_edit is not None:
            self.x_axis_label_text = self.x_axis_label_edit.text()
        if self.y_axis_label_edit is not None:
            self.y_axis_label_text = self.y_axis_label_edit.text()
        if self.x_label_artist:
            self.x_label_artist.set_text(self._format_chemical_plot(self.x_axis_label_text))
        if self.y_label_artist:
            self.y_label_artist.set_text(self._format_chemical_plot(self.y_axis_label_text))
        self.canvas.draw_idle()

    # ---------------- Core Features ---------------- #

    def _format_chemical_html(self, text):
        if text is None:
            return ""
        return html.escape(str(text))

    def _format_chemical_plot(self, text):
        if text is None:
            return ""
        return str(text)

    def _natural_sort_key(self, text):
        parts = re.split(r"(\d+)", text.lower())
        return [int(p) if p.isdigit() else p for p in parts]

    def _mtime_sort_key(self, path):
        try:
            return os.path.getmtime(path)
        except Exception:
            return 0.0

    def _list_display_name_from_path(self, path):
        # Generic display name: file stem only.
        return os.path.splitext(os.path.basename(path))[0]

    def _append_suffix(self, text, suffix):
        raw = str(text or "").strip()
        if not raw:
            return raw
        tag = str(suffix or "").strip()
        if not tag:
            return raw
        suffix_token = f"_{tag}"
        if raw.lower().endswith(suffix_token.lower()):
            return raw
        return f"{raw}{suffix_token}"

    def _append_suffix_to_filename(self, filename, suffix):
        raw = str(filename or "").strip()
        if not raw:
            return raw
        stem, ext = os.path.splitext(raw)
        suffix_token = f"_{str(suffix or '').strip()}"
        if not suffix_token.strip("_"):
            return raw
        if stem.lower().endswith(suffix_token.lower()):
            return raw
        return f"{stem}{suffix_token}{ext}"

    def _mark_pattern_processed(self, idx, suffix):
        if idx < 0 or idx >= len(self.patterns):
            return
        p = self.patterns[idx]
        p["label"] = self._append_suffix(p.get("label", ""), suffix)
        p["filename"] = self._append_suffix_to_filename(p.get("filename", ""), suffix)

    def _resolve_transform_targets(self, target_mode="auto"):
        if not self.patterns:
            return []
        mode = str(target_mode or "auto").lower()
        if mode == "all":
            return list(range(len(self.patterns)))
        if mode == "selected":
            if self.selected_idx is not None and 0 <= self.selected_idx < len(self.patterns):
                return [self.selected_idx]
            row = self.files_list.currentRow() if self.files_list else -1
            if 0 <= row < len(self.patterns):
                return [row]
            return []

        if self.selected_idx is not None and 0 <= self.selected_idx < len(self.patterns):
            return [self.selected_idx]
        row = self.files_list.currentRow() if self.files_list else -1
        if 0 <= row < len(self.patterns):
            return [row]
        choice = QMessageBox.question(
            self,
            "No Curve Selected",
            "No curve is selected. Apply this operation to all loaded curves?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if choice == QMessageBox.StandardButton.Yes:
            return list(range(len(self.patterns)))
        return []

    def _resolve_single_target_index(self, show_message=True):
        if not self.patterns:
            return None
        if self.selected_idx is not None and 0 <= self.selected_idx < len(self.patterns):
            return self.selected_idx
        row = self.files_list.currentRow() if self.files_list else -1
        if 0 <= row < len(self.patterns):
            return row
        if len(self.patterns) == 1:
            return 0
        if show_message:
            QMessageBox.information(self, "PXRD", "Please select one curve first.")
        return None

    def _resolve_explicit_selected_index(self, show_message=True):
        if not self.patterns:
            return None
        if self.selected_idx is not None and 0 <= self.selected_idx < len(self.patterns):
            return self.selected_idx
        row = self.files_list.currentRow() if self.files_list else -1
        if 0 <= row < len(self.patterns):
            return row
        if show_message:
            QMessageBox.information(self, "PXRD", "Please select one curve first.")
        return None

    def _snapshot_patterns_data(self):
        snapshot = []
        for p in self.patterns:
            snapshot.append(
                {
                    "uid": int(p.get("uid", 0)),
                    "x": np.array(p.get("x", []), copy=True),
                    "y": np.array(p.get("y", []), copy=True),
                    "label": str(p.get("label", "")),
                    "filename": str(p.get("filename", "")),
                    "source_path": str(p.get("source_path", "")),
                    "color": p.get("color", None),
                    "scale": float(p.get("scale", 1.0)),
                    "y_shift": float(p.get("y_shift", 0.0)),
                    "label_dx": float(p.get("label_dx", 0.0)),
                    "label_dy": float(p.get("label_dy", 0.0)),
                }
            )
        return snapshot

    def _restore_patterns_data(self, snapshot):
        current_selected = self.selected_idx
        self.patterns = []
        for p in snapshot:
            self.patterns.append(
                {
                    "uid": int(p.get("uid", 0)),
                    "x": np.array(p.get("x", []), copy=True),
                    "y": np.array(p.get("y", []), copy=True),
                    "label": str(p.get("label", "")),
                    "filename": str(p.get("filename", "")),
                    "source_path": str(p.get("source_path", "")),
                    "color": p.get("color", None),
                    "scale": float(p.get("scale", 1.0)),
                    "y_shift": float(p.get("y_shift", 0.0)),
                    "label_dx": float(p.get("label_dx", 0.0)),
                    "label_dy": float(p.get("label_dy", 0.0)),
                }
            )
        if current_selected is None or not (0 <= current_selected < len(self.patterns)):
            self.selected_idx = None
        else:
            self.selected_idx = current_selected

    def open_powerxrd_tools_dialog(self):
        if not self.patterns:
            QMessageBox.information(self, "PXRD", "There are no loaded curves.")
            return
        selected_idx_for_tools = self._resolve_explicit_selected_index(show_message=False)
        if selected_idx_for_tools is None:
            QMessageBox.information(self, "PXRD", "Please select one curve first, then open PXRD tools.")
            return

        if self.powerxrd_tools_dialog is not None:
            try:
                self.powerxrd_tools_dialog.show()
                self.powerxrd_tools_dialog.raise_()
                self.powerxrd_tools_dialog.activateWindow()
                return
            except Exception:
                self.powerxrd_tools_dialog = None

        dialog = QDialog(self)
        self.powerxrd_tools_dialog = dialog
        dialog.pattern_objects = [id(p) for p in self.patterns]
        dialog.setWindowTitle("PXRD tools")
        dialog.setModal(False)
        dialog.resize(460, 320)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

        baseline_patterns = self._snapshot_patterns_data()
        pending_apply = None

        def _safe_int(v, fallback=0):
            try:
                return int(v)
            except Exception:
                return int(fallback)

        def _curve_label(idx):
            if idx < 0 or idx >= len(baseline_patterns):
                return "N/A"
            p = baseline_patterns[idx]
            return str(p.get("label", p.get("filename", f"Curve {idx + 1}")))

        layout = QVBoxLayout(dialog)
        top_form = QFormLayout()
        tool_combo = QComboBox()
        tool_combo.addItem("Smooth (Savitzky-Golay)", "smooth_savgol")
        tool_combo.addItem("Smooth (moving average)", "smooth_ma")
        tool_combo.addItem("Smooth (Gaussian)", "smooth_gaussian")
        tool_combo.addItem("Background subtraction", "bgsub")
        tool_combo.addItem("Peak analysis", "peak")
        top_form.addRow("Tool:", tool_combo)
        layout.addLayout(top_form)

        stack = QStackedWidget()
        layout.addWidget(stack, 1)

        smooth_page = QWidget()
        smooth_form = QFormLayout(smooth_page)
        smooth_window_label = QLabel("Window size (odd points):")
        smooth_window_spin = QSpinBox()
        smooth_window_spin.setRange(3, 10001)
        smooth_window_spin.setSingleStep(2)
        smooth_window_spin.setValue(5)
        smooth_sigma_label = QLabel("Sigma (points):")
        smooth_sigma_spin = QDoubleSpinBox()
        smooth_sigma_spin.setRange(0.1, 10000.0)
        smooth_sigma_spin.setDecimals(3)
        smooth_sigma_spin.setSingleStep(0.1)
        smooth_sigma_spin.setValue(1.0)
        smooth_form.addRow(smooth_window_label, smooth_window_spin)
        smooth_form.addRow(smooth_sigma_label, smooth_sigma_spin)
        stack.addWidget(smooth_page)

        bg_page = QWidget()
        bg_form = QFormLayout(bg_page)
        bg_lambda_spin = QDoubleSpinBox()
        bg_lambda_spin.setRange(1.0, 1e12)
        bg_lambda_spin.setDecimals(0)
        bg_lambda_spin.setSingleStep(1e5)
        bg_lambda_spin.setValue(1e6)
        bg_iter_spin = QSpinBox()
        bg_iter_spin.setRange(1, 200)
        bg_iter_spin.setValue(30)
        bg_conv_spin = QDoubleSpinBox()
        bg_conv_spin.setRange(1e-6, 1.0)
        bg_conv_spin.setDecimals(6)
        bg_conv_spin.setSingleStep(1e-4)
        bg_conv_spin.setValue(1e-3)
        bg_form.addRow("Smoothing parameter (λ):", bg_lambda_spin)
        bg_form.addRow("Max iterations:", bg_iter_spin)
        bg_form.addRow("Convergence tolerance:", bg_conv_spin)
        stack.addWidget(bg_page)

        peak_page = QWidget()
        peak_form = QFormLayout(peak_page)
        peak_curve_combo = QComboBox()
        for i, pattern in enumerate(self.patterns):
            label = pattern.get("label", pattern.get("filename", f"Curve {i + 1}"))
            peak_curve_combo.addItem(f"{i + 1}. {label}", i)
        preferred_idx = self._resolve_explicit_selected_index(show_message=False)
        if preferred_idx is None:
            preferred_idx = 0
        preferred_idx = max(0, min(preferred_idx, peak_curve_combo.count() - 1))
        peak_curve_combo.setCurrentIndex(preferred_idx)

        peak_xmin_spin = QDoubleSpinBox()
        peak_xmin_spin.setRange(-9999.0, 9999.0)
        peak_xmin_spin.setDecimals(4)
        peak_xmin_spin.setSingleStep(0.1)
        peak_xmax_spin = QDoubleSpinBox()
        peak_xmax_spin.setRange(-9999.0, 9999.0)
        peak_xmax_spin.setDecimals(4)
        peak_xmax_spin.setSingleStep(0.1)
        peak_wavelength_spin = QDoubleSpinBox()
        peak_wavelength_spin.setRange(0.01, 10.0)
        peak_wavelength_spin.setDecimals(5)
        peak_wavelength_spin.setValue(1.5406)
        peak_k_spin = QDoubleSpinBox()
        peak_k_spin.setRange(0.01, 5.0)
        peak_k_spin.setDecimals(3)
        peak_k_spin.setValue(0.9)

        def _fill_peak_range_for_curve(curve_idx):
            if curve_idx < 0 or curve_idx >= len(self.patterns):
                return
            x_vals = np.asarray(self.patterns[curve_idx].get("x", []), dtype=np.float64)
            if x_vals.size == 0:
                return
            x_min = float(np.min(x_vals))
            x_max = float(np.max(x_vals))
            if self.xmin_spin is not None and self.xmax_spin is not None:
                disp_min = float(self.xmin_spin.value())
                disp_max = float(self.xmax_spin.value())
                if disp_min < disp_max:
                    x_min = max(x_min, disp_min)
                    x_max = min(x_max, disp_max)
            if x_min >= x_max:
                x_min = float(np.min(x_vals))
                x_max = float(np.max(x_vals))
            peak_xmin_spin.setValue(x_min)
            peak_xmax_spin.setValue(x_max)

        _fill_peak_range_for_curve(preferred_idx)
        peak_curve_combo.currentIndexChanged.connect(
            lambda _row: _fill_peak_range_for_curve(
                _safe_int(peak_curve_combo.currentData(), fallback=-1)
            )
        )

        peak_form.addRow("Curve:", peak_curve_combo)
        peak_form.addRow("X min (deg):", peak_xmin_spin)
        peak_form.addRow("X max (deg):", peak_xmax_spin)
        peak_form.addRow("Wavelength (Å):", peak_wavelength_spin)
        peak_form.addRow("Scherrer K:", peak_k_spin)
        stack.addWidget(peak_page)

        live_note = QLabel("The preview plot updates automatically. Changes are applied only when you click Apply.")
        live_note.setProperty("role", "muted")
        live_note.setWordWrap(True)
        layout.addWidget(live_note)

        status_label = QLabel("Main curves stay unchanged until you click Apply.")
        status_label.setWordWrap(True)
        status_label.setProperty("role", "muted")
        layout.addWidget(status_label)

        button_box = QDialogButtonBox()
        apply_btn = button_box.addButton("Apply", QDialogButtonBox.ButtonRole.AcceptRole)
        close_btn = button_box.addButton(QDialogButtonBox.StandardButton.Close)
        close_btn.clicked.connect(dialog.close)
        layout.addWidget(button_box)

        def _sync_tool_page_and_params():
            tool = str(tool_combo.currentData() or "smooth_savgol")
            if tool in ("smooth_savgol", "smooth_ma", "smooth_gaussian"):
                stack.setCurrentWidget(smooth_page)
                if tool == "smooth_savgol":
                    smooth_window_label.setText("Window size (odd points):")
                    smooth_window_spin.setMinimum(3)
                    smooth_window_spin.setSingleStep(2)
                    if smooth_window_spin.value() < 3:
                        smooth_window_spin.setValue(3)
                    if smooth_window_spin.value() % 2 == 0:
                        smooth_window_spin.setValue(smooth_window_spin.value() + 1)
                    smooth_window_label.show()
                    smooth_window_spin.show()
                    smooth_sigma_label.hide()
                    smooth_sigma_spin.hide()
                elif tool == "smooth_ma":
                    smooth_window_label.setText("Window size (points):")
                    smooth_window_spin.setMinimum(1)
                    smooth_window_spin.setSingleStep(1)
                    if smooth_window_spin.value() < 1:
                        smooth_window_spin.setValue(1)
                    smooth_window_label.show()
                    smooth_window_spin.show()
                    smooth_sigma_label.hide()
                    smooth_sigma_spin.hide()
                else:
                    smooth_window_label.hide()
                    smooth_window_spin.hide()
                    smooth_sigma_label.show()
                    smooth_sigma_spin.show()
            elif tool == "bgsub":
                stack.setCurrentWidget(bg_page)
            else:
                stack.setCurrentWidget(peak_page)

        def _apply_live():
            nonlocal baseline_patterns, pending_apply
            if self._powerxrd_live_updating:
                return
            self._powerxrd_live_updating = True
            try:
                if not self.patterns:
                    status_label.setText("No loaded curves.")
                    return

                baseline_patterns = self._snapshot_patterns_data()
                tool = str(tool_combo.currentData() or "smooth_savgol")
                pending_apply = None

                if tool in ("smooth_savgol", "smooth_ma", "smooth_gaussian"):
                    preview_idx = self._resolve_explicit_selected_index(show_message=False)
                    if preview_idx is None:
                        status_label.setText("Select one curve in the main list for preview.")
                        self._powerxrd_preview_data = None
                        self.update_plot(preserve_view=True)
                        return
                    if preview_idx < 0 or preview_idx >= len(baseline_patterns):
                        status_label.setText("Selected curve index is out of range.")
                        self._powerxrd_preview_data = None
                        self.update_plot(preserve_view=True)
                        return

                    px = baseline_patterns[preview_idx]["x"]
                    py = baseline_patterns[preview_idx]["y"]
                    if tool == "smooth_savgol":
                        window = int(smooth_window_spin.value())
                        x_new, y_new, window_eff = powerxrd_savgol_smooth(
                            px, py, window, return_effective_window=True
                        )
                        method_name = "Savitzky-Golay"
                        method_desc = (
                            f"n={window_eff}"
                            + (f", requested={window}" if window_eff != window else "")
                        )
                    elif tool == "smooth_ma":
                        window = int(smooth_window_spin.value())
                        x_new, y_new, window_eff = powerxrd_moving_average_filter(
                            px, py, window, return_effective_window=True
                        )
                        method_name = "Moving Average"
                        method_desc = (
                            f"n={window_eff}"
                            + (f", requested={window}" if window_eff != window else "")
                        )
                    else:
                        sigma = float(smooth_sigma_spin.value())
                        x_new, y_new = powerxrd_gaussian_smooth(px, py, sigma)
                        method_name = "Gaussian"
                        method_desc = f"sigma={sigma:g}"
                    transformed = {preview_idx: (x_new, y_new)}

                    orig_x = np.asarray(baseline_patterns[preview_idx]["x"], dtype=np.float64)
                    orig_y = np.asarray(baseline_patterns[preview_idx]["y"], dtype=np.float64)
                    new_x, new_y = transformed[preview_idx]
                    pending_apply = {
                        "tool": "smooth",
                        "smooth_method": tool,
                        "smooth_method_name": method_name,
                        "transformed": transformed,
                    }
                    self._powerxrd_preview_data = {
                        "tool": "smooth",
                        "curve_idx": preview_idx,
                        "title": f"Preview | Smooth ({method_name}) | { _curve_label(preview_idx) }",
                        "orig_x": np.array(orig_x, copy=True),
                        "orig_y": np.array(orig_y, copy=True),
                        "new_x": np.array(new_x, copy=True),
                        "new_y": np.array(new_y, copy=True),
                    }
                    result = {
                        "ok": True,
                        "message": f"Preview: {method_name} ({method_desc}) on selected curve.",
                    }
                elif tool == "bgsub":
                    preview_idx = self._resolve_explicit_selected_index(show_message=False)
                    if preview_idx is None:
                        status_label.setText("Select one curve in the main list for preview.")
                        self._powerxrd_preview_data = None
                        self.update_plot(preserve_view=True)
                        return
                    if preview_idx < 0 or preview_idx >= len(baseline_patterns):
                        status_label.setText("Selected curve index is out of range.")
                        self._powerxrd_preview_data = None
                        self.update_plot(preserve_view=True)
                        return

                    lam = float(bg_lambda_spin.value())
                    max_iter = int(bg_iter_spin.value())
                    conv = float(bg_conv_spin.value())
                    px = baseline_patterns[preview_idx]["x"]
                    py = baseline_patterns[preview_idx]["y"]
                    x_new, y_new = powerxrd_background_subtraction(
                        px, py, lam=lam, max_iter=max_iter, conv=conv
                    )
                    transformed = {preview_idx: (x_new, y_new)}

                    orig_x = np.asarray(baseline_patterns[preview_idx]["x"], dtype=np.float64)
                    orig_y = np.asarray(baseline_patterns[preview_idx]["y"], dtype=np.float64)
                    new_x, new_y = transformed[preview_idx]
                    pending_apply = {"tool": "bgsub", "transformed": transformed}
                    self._powerxrd_preview_data = {
                        "tool": "bgsub",
                        "curve_idx": preview_idx,
                        "title": f"Preview | Background subtraction (arPLS) | { _curve_label(preview_idx) }",
                        "orig_x": np.array(orig_x, copy=True),
                        "orig_y": np.array(orig_y, copy=True),
                        "new_x": np.array(new_x, copy=True),
                        "new_y": np.array(new_y, copy=True),
                    }
                    result = {
                        "ok": True,
                        "message": (
                            f"Preview: arPLS (lambda={lam:g}, iter={max_iter}, conv={conv:g}) on selected curve."
                        ),
                    }
                else:
                    target_idx = _safe_int(peak_curve_combo.currentData(), fallback=-1)
                    if target_idx < 0 or target_idx >= len(baseline_patterns):
                        status_label.setText("Select a valid curve for peak preview.")
                        self._powerxrd_preview_data = None
                        self.update_plot(preserve_view=True)
                        return

                    x_arr = np.asarray(baseline_patterns[target_idx]["x"], dtype=np.float64)
                    y_arr = np.asarray(baseline_patterns[target_idx]["y"], dtype=np.float64)
                    xmin = float(peak_xmin_spin.value())
                    xmax = float(peak_xmax_spin.value())
                    wavelength = float(peak_wavelength_spin.value())
                    k_factor = float(peak_k_spin.value())
                    if x_arr.size < 3 or y_arr.size < 3 or x_arr.size != y_arr.size:
                        status_label.setText("Selected curve does not contain valid data.")
                        self._powerxrd_preview_data = None
                        self.update_plot(preserve_view=True)
                        return
                    if xmin >= xmax:
                        status_label.setText("Peak preview requires X min < X max.")
                        self._powerxrd_preview_data = None
                        self.update_plot(preserve_view=True)
                        return
                    if wavelength <= 0 or k_factor <= 0:
                        status_label.setText("Wavelength and Scherrer K must be > 0.")
                        self._powerxrd_preview_data = None
                        self.update_plot(preserve_view=True)
                        return

                    mask = (x_arr >= xmin) & (x_arr <= xmax)
                    if int(np.count_nonzero(mask)) < 3:
                        status_label.setText("Peak preview range has too few points.")
                        self._powerxrd_preview_data = None
                        self.update_plot(preserve_view=True)
                        return

                    x_seg = x_arr[mask]
                    y_seg = y_arr[mask]
                    peak_rel_idx = int(np.argmax(y_seg))
                    peak_x = float(x_seg[peak_rel_idx])
                    peak_y = float(y_seg[peak_rel_idx])
                    d_hkl = powerxrd_braggs(peak_x, wavelength=wavelength)
                    fwhm_deg, left_x, right_x, half_y = estimate_peak_fwhm(x_seg, y_seg, peak_rel_idx)
                    scherrer_ang = None
                    scherrer_nm = None
                    if fwhm_deg is not None and np.isfinite(fwhm_deg) and fwhm_deg > 0:
                        beta_rad = np.deg2rad(float(fwhm_deg))
                        theta_rad = np.deg2rad(peak_x / 2.0)
                        scherrer_ang = powerxrd_scherrer(k_factor, wavelength, beta_rad, theta_rad)
                        if np.isfinite(scherrer_ang):
                            scherrer_nm = scherrer_ang / 10.0

                    peak_lines = [
                        f"Curve: {_curve_label(target_idx)}",
                        f"2theta peak: {peak_x:.4f} deg",
                        f"Peak intensity: {peak_y:.6g}",
                        f"d-spacing: {float(d_hkl):.4f} Å",
                    ]
                    if fwhm_deg is None:
                        peak_lines.append("FWHM: unavailable")
                        peak_lines.append("Scherrer size: unavailable")
                    else:
                        peak_lines.append(f"FWHM: {fwhm_deg:.4f} deg")
                        if scherrer_nm is None:
                            peak_lines.append("Scherrer size: unavailable")
                        else:
                            peak_lines.append(f"Scherrer size (K={k_factor:g}): {scherrer_nm:.4f} nm")

                    self._powerxrd_preview_data = {
                        "tool": "peak",
                        "curve_idx": target_idx,
                        "title": f"Preview | Peak | {_curve_label(target_idx)}",
                        "orig_x": np.array(x_arr, copy=True),
                        "orig_y": np.array(y_arr, copy=True),
                        "new_x": np.array(x_arr, copy=True),
                        "new_y": np.array(y_arr, copy=True),
                        "peak": {
                            "peak_x": peak_x,
                            "left_x": left_x,
                            "right_x": right_x,
                            "half_y": half_y,
                        },
                    }
                    pending_apply = {
                        "tool": "peak",
                        "params": {
                            "target_idx": target_idx,
                            "xmin": xmin,
                            "xmax": xmax,
                            "wavelength": wavelength,
                            "k_factor": k_factor,
                        },
                    }
                    result = {"ok": True, "message": "\n".join(peak_lines)}

                self.update_plot(preserve_view=True)
                status_label.setText(result.get("message", "") if isinstance(result, dict) else "")
            except Exception as exc:
                pending_apply = None
                self._powerxrd_preview_data = None
                self.update_plot(preserve_view=True)
                status_label.setText(f"Preview failed: {exc}")
            finally:
                self._powerxrd_live_updating = False

        def _apply_changes():
            nonlocal baseline_patterns, pending_apply
            if not pending_apply:
                status_label.setText("No pending preview result to apply.")
                return

            if (dialog.pattern_objects != [id(p) for p in self.patterns]
                    or len(baseline_patterns) != len(self.patterns)
                    or any(not np.array_equal(old[key], current[key])
                           for old, current in zip(baseline_patterns, self.patterns)
                           for key in ("x", "y"))):
                pending_apply = None
                self._powerxrd_preview_data = None
                self.update_plot(preserve_view=True)
                status_label.setText("Curves changed. Reopen PXRD tools to create a new preview.")
                return

            tool = str(pending_apply.get("tool", ""))
            if tool == "peak":
                params = pending_apply.get("params", {})
                result = self.run_powerxrd_peak_analysis(
                    xmin=params.get("xmin"),
                    xmax=params.get("xmax"),
                    wavelength=params.get("wavelength"),
                    k_factor=params.get("k_factor", 0.9),
                    target_idx=params.get("target_idx"),
                    show_dialog=False,
                )
                if not result.get("ok", False):
                    status_label.setText(str(result.get("message", "Peak analysis failed.")))
                    return
                baseline_patterns = self._snapshot_patterns_data()
                pending_apply = None
                self._powerxrd_preview_data = None
                self.update_plot(preserve_view=True)
                status_label.setText("Applied peak analysis overlay to main plot.")
                return

            if tool not in ("smooth", "bgsub"):
                status_label.setText("Unsupported tool apply mode.")
                return

            transformed = pending_apply.get("transformed", {})
            if not transformed:
                status_label.setText("No pending transformed data to apply.")
                return

            suffix = "smooth" if tool == "smooth" else "BS"
            self._push_undo_snapshot()
            for idx, xy in transformed.items():
                if idx < 0 or idx >= len(self.patterns):
                    continue
                if not isinstance(xy, (tuple, list)) or len(xy) != 2:
                    continue
                x_new, y_new = xy
                self.patterns[idx]["x"] = np.array(x_new, copy=True)
                self.patterns[idx]["y"] = np.array(y_new, copy=True)
                self._mark_pattern_processed(idx, suffix)

            baseline_patterns = self._snapshot_patterns_data()
            apply_name = (
                str(pending_apply.get("smooth_method_name", "Smooth"))
                if tool == "smooth"
                else "Background subtraction"
            )
            pending_apply = None
            self._powerxrd_preview_data = None
            self.applied_peak_overlay = None
            self.update_plot(preserve_view=True)
            status_label.setText(f"Applied {apply_name} preview changes to main curves.")

        live_signals = [
            (tool_combo.currentIndexChanged, _apply_live),
            (smooth_window_spin.valueChanged, _apply_live),
            (smooth_sigma_spin.valueChanged, _apply_live),
            (bg_lambda_spin.valueChanged, _apply_live),
            (bg_iter_spin.valueChanged, _apply_live),
            (bg_conv_spin.valueChanged, _apply_live),
            (peak_curve_combo.currentIndexChanged, _apply_live),
            (peak_xmin_spin.valueChanged, _apply_live),
            (peak_xmax_spin.valueChanged, _apply_live),
            (peak_wavelength_spin.valueChanged, _apply_live),
            (peak_k_spin.valueChanged, _apply_live),
        ]
        tool_combo.currentIndexChanged.connect(_sync_tool_page_and_params)
        _sync_tool_page_and_params()
        for signal, handler in live_signals:
            signal.connect(handler)
        _apply_live()
        apply_btn.clicked.connect(_apply_changes)

        def _on_dialog_closed(_result):
            self.powerxrd_tools_dialog = None
            self._powerxrd_live_updating = False
            if self._powerxrd_preview_data is not None:
                self._powerxrd_preview_data = None
                if self.patterns:
                    self.update_plot(preserve_view=True)

        dialog.finished.connect(_on_dialog_closed)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def apply_powerxrd_moving_average(self, window=None, target_mode="auto", show_message=True, record_undo=True):
        if not self.patterns:
            msg = "There are no curves to smooth."
            if show_message:
                QMessageBox.information(self, "PXRD", msg)
            return {"ok": False, "message": msg}

        if window is None:
            window, ok = QInputDialog.getInt(
                self,
                "PXRD Smooth",
                "Savitzky-Golay window points (odd):",
                5,
                3,
                10001,
                2,
            )
            if not ok:
                return {"ok": False, "message": "Canceled."}
        window = int(window)
        if window < 3:
            msg = "Window size must be at least 3 points for Savitzky-Golay smoothing."
            if show_message:
                QMessageBox.warning(self, "PXRD Smooth", msg)
            return {"ok": False, "message": msg}

        targets = self._resolve_transform_targets(target_mode=target_mode)
        if not targets:
            if str(target_mode).lower() == "selected":
                msg = "Please select a curve first."
            else:
                msg = "No valid target curves."
            if show_message:
                QMessageBox.information(self, "PXRD Smooth", msg)
            return {"ok": False, "message": msg}

        valid_targets = []
        for idx in targets:
            p = self.patterns[idx]
            x_arr = np.asarray(p.get("x", []))
            y_arr = np.asarray(p.get("y", []))
            if x_arr.size == y_arr.size and x_arr.size >= 3:
                valid_targets.append(idx)

        if not valid_targets:
            msg = "None of the target curves have enough points for Savitzky-Golay smoothing."
            if show_message:
                QMessageBox.warning(self, "PXRD Smooth", msg)
            return {"ok": False, "message": msg}

        if record_undo:
            self._push_undo_snapshot()
        applied_count = 0
        effective_windows = set()
        for idx in valid_targets:
            p = self.patterns[idx]
            try:
                x_new, y_new, window_eff = powerxrd_moving_average(
                    p["x"], p["y"], window, return_effective_window=True
                )
            except Exception:
                continue
            p["x"] = x_new
            p["y"] = y_new
            self._mark_pattern_processed(idx, "smooth")
            applied_count += 1
            effective_windows.add(int(window_eff))

        if applied_count <= 0:
            msg = "Smoothing failed for all target curves."
            if show_message:
                QMessageBox.warning(self, "PXRD Smooth", msg)
            return {"ok": False, "message": msg}

        self.applied_peak_overlay = None
        self.update_plot(preserve_view=True)
        if len(effective_windows) == 1:
            n_text = str(next(iter(effective_windows)))
        elif effective_windows:
            n_text = ",".join(str(v) for v in sorted(effective_windows))
        else:
            n_text = str(window)
        msg = f"Applied Savitzky-Golay smoothing (n={n_text}) to {applied_count} curve(s)."
        if show_message:
            QMessageBox.information(self, "PXRD Smooth", msg)
        return {"ok": True, "message": msg}

    def apply_powerxrd_background_subtraction(
        self,
        lam=None,
        max_iter=None,
        conv=None,
        target_mode="auto",
        show_message=True,
        record_undo=True,
    ):
        if not self.patterns:
            msg = "There are no curves to process."
            if show_message:
                QMessageBox.information(self, "PXRD", msg)
            return {"ok": False, "message": msg}

        if lam is None:
            lam, ok = QInputDialog.getDouble(
                self,
                "PXRD Background subtraction",
                "Smoothing parameter (λ):",
                1e6,
                1.0,
                1e12,
                0,
            )
            if not ok:
                return {"ok": False, "message": "Canceled."}
        if max_iter is None:
            max_iter, ok = QInputDialog.getInt(
                self,
                "PXRD Background subtraction",
                "Max iterations:",
                30,
                1,
                200,
                1,
            )
            if not ok:
                return {"ok": False, "message": "Canceled."}
        if conv is None:
            conv, ok = QInputDialog.getDouble(
                self,
                "PXRD Background subtraction",
                "Convergence threshold:",
                1e-3,
                1e-6,
                1.0,
                6,
            )
            if not ok:
                return {"ok": False, "message": "Canceled."}

        lam = float(lam)
        max_iter = int(max_iter)
        conv = float(conv)
        if lam <= 0:
            msg = "Lambda must be > 0."
            if show_message:
                QMessageBox.warning(self, "PXRD Background subtraction", msg)
            return {"ok": False, "message": msg}
        if max_iter < 1:
            msg = "Max iterations must be >= 1."
            if show_message:
                QMessageBox.warning(self, "PXRD Background subtraction", msg)
            return {"ok": False, "message": msg}
        if conv <= 0:
            msg = "Convergence threshold must be > 0."
            if show_message:
                QMessageBox.warning(self, "PXRD Background subtraction", msg)
            return {"ok": False, "message": msg}

        targets = self._resolve_transform_targets(target_mode=target_mode)
        if not targets:
            if str(target_mode).lower() == "selected":
                msg = "Please select a curve first."
            else:
                msg = "No valid target curves."
            if show_message:
                QMessageBox.information(self, "PXRD Background subtraction", msg)
            return {"ok": False, "message": msg}

        valid_targets = []
        for idx in targets:
            p = self.patterns[idx]
            x_arr = np.asarray(p.get("x", []))
            y_arr = np.asarray(p.get("y", []))
            if x_arr.size == y_arr.size and x_arr.size >= 3:
                valid_targets.append(idx)

        if not valid_targets:
            msg = "None of the target curves have enough points for this operation."
            if show_message:
                QMessageBox.warning(self, "PXRD Background subtraction", msg)
            return {"ok": False, "message": msg}

        if record_undo:
            self._push_undo_snapshot()
        for idx in valid_targets:
            p = self.patterns[idx]
            try:
                x_new, y_new = powerxrd_background_subtraction(
                    p["x"],
                    p["y"],
                    lam=lam,
                    max_iter=max_iter,
                    conv=conv,
                )
            except Exception as exc:
                msg = f"Background subtraction failed: {exc}"
                if show_message:
                    QMessageBox.warning(self, "PXRD Background subtraction", msg)
                return {"ok": False, "message": msg}
            p["x"] = x_new
            p["y"] = y_new
            self._mark_pattern_processed(idx, "BS")

        self.applied_peak_overlay = None
        self.update_plot(preserve_view=True)
        msg = (
            "Applied arPLS background subtraction "
            f"(lambda={lam:g}, iter={max_iter}, conv={conv:g}) "
            f"to {len(valid_targets)} curve(s)."
        )
        if show_message:
            QMessageBox.information(self, "PXRD Background subtraction", msg)
        return {"ok": True, "message": msg}

    def run_powerxrd_peak_analysis(
        self,
        xmin=None,
        xmax=None,
        wavelength=None,
        k_factor=0.9,
        target_idx=None,
        show_dialog=True,
    ):
        idx = target_idx if target_idx is not None else self._resolve_single_target_index()
        if idx is None:
            return {"ok": False, "message": "No target curve selected."}
        if idx < 0 or idx >= len(self.patterns):
            msg = "Invalid target curve."
            if show_dialog:
                QMessageBox.warning(self, "PXRD Peak", msg)
            return {"ok": False, "message": msg}

        pattern = self.patterns[idx]
        x_arr = np.asarray(pattern.get("x", []), dtype=np.float64)
        y_arr = np.asarray(pattern.get("y", []), dtype=np.float64)
        if x_arr.size < 3 or y_arr.size < 3 or x_arr.size != y_arr.size:
            msg = "Selected curve does not contain valid data."
            if show_dialog:
                QMessageBox.warning(self, "PXRD Peak", msg)
            return {"ok": False, "message": msg}

        if xmin is None or xmax is None:
            default_xmin = float(self.xmin_spin.value()) if self.xmin_spin else float(np.min(x_arr))
            default_xmax = float(self.xmax_spin.value()) if self.xmax_spin else float(np.max(x_arr))
            range_text, ok = QInputDialog.getText(
                self,
                "PXRD Peak",
                "Peak search range (xmin, xmax):",
                text=f"{default_xmin:.3f}, {default_xmax:.3f}",
            )
            if not ok:
                return {"ok": False, "message": "Canceled."}

            try:
                parts = [p.strip() for p in range_text.replace(";", ",").split(",") if p.strip()]
                if len(parts) != 2:
                    raise ValueError("Need exactly two values.")
                xmin = float(parts[0])
                xmax = float(parts[1])
            except Exception as exc:
                msg = f"Invalid range input: {exc}"
                if show_dialog:
                    QMessageBox.warning(self, "PXRD Peak", msg)
                return {"ok": False, "message": msg}

        if wavelength is None:
            wavelength, ok = QInputDialog.getDouble(
                self,
                "PXRD Peak",
                "X-ray wavelength (Angstrom):",
                1.5406,
                0.01,
                10.0,
                5,
            )
            if not ok:
                return {"ok": False, "message": "Canceled."}

        xmin = float(xmin)
        xmax = float(xmax)
        wavelength = float(wavelength)
        k_factor = float(k_factor)
        if xmin >= xmax:
            msg = "X Min must be smaller than X Max."
            if show_dialog:
                QMessageBox.warning(self, "PXRD Peak", msg)
            return {"ok": False, "message": msg}
        if wavelength <= 0:
            msg = "Wavelength must be > 0."
            if show_dialog:
                QMessageBox.warning(self, "PXRD Peak", msg)
            return {"ok": False, "message": msg}
        if k_factor <= 0:
            msg = "Scherrer K must be > 0."
            if show_dialog:
                QMessageBox.warning(self, "PXRD Peak", msg)
            return {"ok": False, "message": msg}

        mask = (x_arr >= xmin) & (x_arr <= xmax)
        if int(np.count_nonzero(mask)) < 3:
            msg = "The selected range has too few data points."
            if show_dialog:
                QMessageBox.warning(self, "PXRD Peak", msg)
            return {"ok": False, "message": msg}

        x_seg = x_arr[mask]
        y_seg = y_arr[mask]
        peak_rel_idx = int(np.argmax(y_seg))
        peak_x = float(x_seg[peak_rel_idx])
        peak_y = float(y_seg[peak_rel_idx])

        d_hkl = powerxrd_braggs(peak_x, wavelength=wavelength)
        fwhm_deg, left_x, right_x, half_y = estimate_peak_fwhm(x_seg, y_seg, peak_rel_idx)

        scherrer_ang = None
        scherrer_nm = None
        if fwhm_deg is not None and np.isfinite(fwhm_deg) and fwhm_deg > 0:
            beta_rad = np.deg2rad(float(fwhm_deg))
            theta_rad = np.deg2rad(peak_x / 2.0)
            scherrer_ang = powerxrd_scherrer(k_factor, wavelength, beta_rad, theta_rad)
            if np.isfinite(scherrer_ang):
                scherrer_nm = scherrer_ang / 10.0

        self.applied_peak_overlay = {
            "curve_idx": int(idx),
            "curve_uid": int(pattern.get("uid", -1)),
            "peak_x": float(peak_x),
            "peak_y": float(peak_y),
            "left_x": float(left_x) if left_x is not None else None,
            "right_x": float(right_x) if right_x is not None else None,
            "half_y": float(half_y) if half_y is not None else None,
        }
        self._redraw_applied_peak_overlay()
        self.canvas.draw_idle()

        lines = [
            f"Curve: {pattern.get('label', pattern.get('filename', 'N/A'))}",
            f"2θ peak: {peak_x:.4f} deg",
            f"Peak intensity: {peak_y:.6g}",
            f"d-spacing (Bragg): {d_hkl:.4f} Å",
        ]
        if fwhm_deg is None:
            lines.append("FWHM: unavailable (half-max crossings not found).")
            lines.append("Scherrer size: unavailable.")
        else:
            lines.append(f"FWHM: {fwhm_deg:.4f} deg")
            if scherrer_nm is None:
                lines.append("Scherrer size: unavailable (invalid geometry).")
            else:
                lines.append(f"Scherrer size (K={k_factor:g}): {scherrer_nm:.4f} nm ({scherrer_ang:.2f} Å)")
        summary = "\n".join(lines)
        if show_dialog:
            QMessageBox.information(self, "PXRD Peak", summary)
        return {"ok": True, "message": summary}

    def populate_browser_tree(self):
        self._browser_search_timer.stop()
        self.browser_tree.clear()
        root = self.browser_root_dir
        if not os.path.isdir(root):
            item = QTreeWidgetItem([f"Directory not found: {root}"])
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self.browser_tree.addTopLevelItem(item)
            return

        try:
            root_entries = os.listdir(root)
        except PermissionError:
            item = QTreeWidgetItem([f"Access denied: {root}"])
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self.browser_tree.addTopLevelItem(item)
            return
        except OSError as e:
            item = QTreeWidgetItem([f"Cannot read directory: {e}"])
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self.browser_tree.addTopLevelItem(item)
            return

        search_text = self.browser_search_edit.text().strip() if self.browser_search_edit else ""
        if search_text:
            query = search_text.lower()
            matches = []
            for dirpath, _dirnames, filenames in os.walk(root, onerror=lambda _e: None):
                for fname in filenames:
                    ext = os.path.splitext(fname)[1].lower()
                    if ext not in self.supported_exts:
                        continue
                    full_path = os.path.join(dirpath, fname)
                    rel_path = os.path.relpath(full_path, root)
                    rel_display = rel_path.replace("\\", "/")
                    if query in rel_display.lower():
                        matches.append((full_path, rel_display))

            if self.sort_by_filename_cb and self.sort_by_filename_cb.isChecked():
                matches = sorted(
                    matches,
                    key=lambda pair: self._mtime_sort_key(pair[0]),
                    reverse=True,
                )
            else:
                matches = sorted(matches, key=lambda pair: self._natural_sort_key(pair[1]))

            if not matches:
                item = QTreeWidgetItem([f"No matches for: {search_text}"])
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                self.browser_tree.addTopLevelItem(item)
                return

            for full_path, rel_display in matches:
                item = QTreeWidgetItem([rel_display])
                item.setData(0, Qt.ItemDataRole.UserRole, full_path)
                item.setToolTip(0, full_path)
                self.browser_tree.addTopLevelItem(item)
            self.browser_tree.resizeColumnToContents(0)
            return

        root_files = [
            f for f in root_entries
            if os.path.isfile(os.path.join(root, f))
            and os.path.splitext(f)[1].lower() in self.supported_exts
        ]
        if self.sort_by_filename_cb and self.sort_by_filename_cb.isChecked():
            root_files = sorted(
                root_files,
                key=lambda f: self._mtime_sort_key(os.path.join(root, f)),
                reverse=True,
            )

        for fname in root_files:
            full_path = os.path.join(root, fname)
            item = QTreeWidgetItem([fname])
            item.setData(0, Qt.ItemDataRole.UserRole, full_path)
            item.setToolTip(0, full_path)
            self.browser_tree.addTopLevelItem(item)

        subdirs = [d for d in root_entries if os.path.isdir(os.path.join(root, d))]
        if self.sort_by_filename_cb and self.sort_by_filename_cb.isChecked():
            subdirs = sorted(subdirs, key=self._natural_sort_key)

        for folder_name in subdirs:
            folder_path = os.path.join(root, folder_name)
            try:
                folder_entries = os.listdir(folder_path)
            except (PermissionError, OSError):
                continue

            parent_item = QTreeWidgetItem([folder_name])
            parent_item.setFlags(parent_item.flags() & ~Qt.ItemFlag.ItemIsDragEnabled)
            parent_item.setToolTip(0, folder_path)
            self.browser_tree.addTopLevelItem(parent_item)

            files = [
                f for f in folder_entries
                if os.path.isfile(os.path.join(folder_path, f))
                and os.path.splitext(f)[1].lower() in self.supported_exts
            ]

            if self.sort_by_filename_cb and self.sort_by_filename_cb.isChecked():
                files = sorted(
                    files,
                    key=lambda f: self._mtime_sort_key(os.path.join(folder_path, f)),
                    reverse=True,
                )

            for fname in files:
                full_path = os.path.join(folder_path, fname)
                child = QTreeWidgetItem([fname])
                child.setData(0, Qt.ItemDataRole.UserRole, full_path)
                child.setToolTip(0, full_path)
                parent_item.addChild(child)

            if files:
                parent_item.setExpanded(False)
        self.browser_tree.resizeColumnToContents(0)
        self.browser_tree.collapseAll()

    def on_browser_item_double_clicked(self, item, _column):
        file_path = item.data(0, Qt.ItemDataRole.UserRole)
        if file_path and os.path.isfile(file_path):
            self.queue_patterns_from_paths([file_path])

    def add_patterns_dialog(self):
        start_dir = self.browser_root_dir if os.path.isdir(self.browser_root_dir) else ""
        files, _ = QFileDialog.getOpenFileNames(
            self, "Select PXRD Files", start_dir,
            PXRD_FILE_DIALOG_FILTER
        )
        if files:
            self.queue_patterns_from_paths(files)

    def on_browser_search_changed(self, _text):
        self._browser_search_timer.start()

    def on_browser_root_changed(self):
        new_root = self.browser_root_edit.text().strip() if self.browser_root_edit else ""
        if not new_root:
            QMessageBox.warning(self, "Warning", "Default folder cannot be empty.")
            return
        if not os.path.isdir(new_root):
            QMessageBox.warning(self, "Warning", f"Directory not found: {new_root}")
            return
        self.set_browser_root(new_root)

    def pick_browser_root_dir(self):
        start_dir = self.browser_root_dir if os.path.isdir(self.browser_root_dir) else ""
        path = QFileDialog.getExistingDirectory(self, "Select default folder", start_dir)
        if not path:
            return
        self.set_browser_root(path)

    def set_browser_root(self, path):
        self.browser_root_dir = os.path.abspath(path)
        self.settings.setValue("browser/root_dir", self.browser_root_dir)
        self.settings.sync()
        if self.browser_root_edit:
            self.browser_root_edit.setText(self.browser_root_dir)
        self.populate_browser_tree()

    def queue_patterns_from_paths(self, paths):
        self._import_pending.extend(os.path.abspath(path) for path in paths)
        if not self._import_active:
            self._start_import()

    def _start_import(self):
        existing = {os.path.normcase(p.get("source_path", "")) for p in self.patterns}
        paths = list(dict.fromkeys(p for p in self._import_pending if os.path.normcase(p) not in existing))
        self._import_pending.clear()
        if not paths:
            return
        self._import_active = True
        self.statusBar().showMessage(f"Loading {len(paths)} PXRD file(s)…")
        threading.Thread(target=_read_patterns_to_queue,
                         args=(paths, self._import_results), daemon=True).start()
        self._import_timer.start()

    def _poll_import(self):
        try:
            loaded, errors = self._import_results.get_nowait()
        except queue.Empty:
            return
        self._import_timer.stop()
        try:
            self._append_loaded_patterns(loaded, errors)
        finally:
            self._import_active = False
            self.statusBar().showMessage("PXRD import complete", 3000)
            self._start_import()

    def add_patterns_from_paths(self, paths):
        """Synchronous programmatic import; interactive entry points use the queue."""
        self._append_loaded_patterns(*read_pattern_batch(paths))

    def _append_loaded_patterns(self, loaded, errors):
        existing = {os.path.normcase(os.path.abspath(p["source_path"]))
                    for p in self.patterns if p.get("source_path")}
        pending = []
        for path, x, y in loaded:
            key = os.path.normcase(os.path.abspath(path))
            if key in existing:
                continue
            existing.add(key)
            pending.append({
                "uid": self._uid_counter + len(pending) + 1,
                "x": x, "y": y, "label": self._list_display_name_from_path(path),
                "filename": os.path.basename(path), "source_path": os.path.abspath(path),
                "color": self._color_for_index(len(self.patterns) + len(pending)),
                "scale": 1.0, "y_shift": 0.0, "label_dx": 0.0, "label_dy": 0.0,
            })
        if pending:
            self._push_undo_snapshot()
            self.patterns.extend(pending)
            self._uid_counter += len(pending)
            self.selected_idx = None
            self.update_plot(preserve_view=False)
        if errors:
            QMessageBox.critical(self, "Import errors", "\n\n".join(errors))

    def _paths_from_mime(self, mime):
        paths = []
        if not mime or not mime.hasUrls():
            return paths
        for url in mime.urls():
            local = url.toLocalFile()
            if local:
                paths.append(local)
        return paths

    def eventFilter(self, obj, event):
        canvas = getattr(self, "canvas", None)
        viewport = self.canvas_scroll.viewport() if self.canvas_scroll is not None else None

        if viewport is not None and obj == viewport:
            if event.type() == QEvent.Type.Resize:
                self._reposition_scale_overlay()
                return False

        if canvas is not None and obj == canvas:
            if event.type() == QEvent.Type.DragEnter:
                mime = event.mimeData()
                if mime and mime.hasUrls():
                    event.acceptProposedAction()
                    return True
            elif event.type() == QEvent.Type.Drop:
                paths = self._paths_from_mime(event.mimeData())
                if paths:
                    self.queue_patterns_from_paths(paths)
                    event.acceptProposedAction()
                    return True
            elif event.type() == QEvent.Type.Resize:
                # Keep figure display size locked to user-defined width/height.
                if self.fig and self.fig_w_spin and self.fig_h_spin:
                    scale = float(getattr(self, "canvas_view_scale", 1.0))
                    base_dpi = float(getattr(self, "base_fig_dpi", 100.0))
                    self.fig.set_dpi(base_dpi * scale)
                    self.fig.set_size_inches(self.fig_w_spin.value(), self.fig_h_spin.value(), forward=True)
                if self.plot_lines:
                    self._apply_horizontal_clip_only()
                return False
        return super().eventFilter(obj, event)

    def clear_patterns(self):
        if self.patterns:
            self._push_undo_snapshot()
        self.patterns = []
        self.selected_idx = None
        self.refresh_files_list()
        self.clear_plot()

    def set_display_range(self, show_warning=True):
        if self.xmin_spin is None or self.xmax_spin is None:
            return
        xmin = float(self.xmin_spin.value())
        xmax = float(self.xmax_spin.value())
        if xmin >= xmax:
            if show_warning:
                QMessageBox.warning(self, "Warning", "Display range requires X min < X max.")
            return
        if self.patterns:
            self._set_shared_xlim(xmin, xmax)
            self.reposition_labels()
            self.canvas.draw_idle()
            return
        ax = self._primary_ax()
        if ax:
            ax.set_xlim(xmin, xmax)
            self.canvas.draw_idle()

    def on_display_range_live_changed(self, _value):
        self.set_display_range(show_warning=False)

    def delete_selected_patterns(self):
        if self.del_idx_edit is None:
            return
        raw = self.del_idx_edit.text().strip()
        if not raw:
            return
        try:
            indices = [int(i) - 1 for i in raw.replace(",", " ").split()]
            indices = sorted(set(indices), reverse=True)
            valid_indices = [i for i in indices if 0 <= i < len(self.patterns)]
            if not valid_indices:
                return
            self._push_undo_snapshot()
            for i in valid_indices:
                if 0 <= i < len(self.patterns):
                    del self.patterns[i]

            if not self.patterns:
                self.selected_idx = None
            else:
                if self.selected_idx is None:
                    self.selected_idx = 0
                self.selected_idx = min(self.selected_idx, len(self.patterns) - 1)

            self.refresh_files_list()
            self.update_plot()
        except Exception:
            QMessageBox.warning(self, "Error", "Please enter valid numeric indices.")

    def delete_current_selected_curve(self):
        if not self.patterns:
            return

        # Avoid accidental deletion while typing in text fields.
        fw = QApplication.focusWidget()
        if isinstance(fw, QLineEdit):
            return

        idx = self.selected_idx
        if idx is None or idx < 0 or idx >= len(self.patterns):
            row = self.files_list.currentRow() if self.files_list else -1
            if 0 <= row < len(self.patterns):
                idx = row
            else:
                return

        self._push_undo_snapshot()
        del self.patterns[idx]

        if not self.patterns:
            self.selected_idx = None
            self.refresh_files_list()
            self.clear_plot()
            return

        self.selected_idx = min(idx, len(self.patterns) - 1)
        self.refresh_files_list()
        self.update_plot()

    # ---------------- Label Repositioning ---------------- #

    def reposition_labels(self):
        if not self.label_texts or not self.patterns:
            return

        frac_x = float(self.fixed_label_x_position)
        y_rel = float(self.fixed_label_y_position)
        y_scale = float(self.fixed_label_y_spacing)
        superimpose_mode = self._is_superimpose_mode()
        if superimpose_mode:
            ax = self._primary_ax()
            if ax is None:
                return

            entries = []
            for entry in self.label_texts:
                idx = int(entry.get("idx", -1))
                if 0 <= idx < len(self.patterns):
                    entries.append(entry)
            if not entries:
                return
            entries.sort(key=lambda e: int(e.get("idx", -1)))

            label_fs = int(self.curve_label_fs_spin.value()) if self.curve_label_fs_spin else 10
            max_chars = 1
            for entry in entries:
                idx = int(entry.get("idx", -1))
                if 0 <= idx < len(self.patterns):
                    max_chars = max(max_chars, len(str(self.patterns[idx].get("label", ""))))

            ax_w_px = 800.0
            ax_h_px = 500.0
            try:
                renderer = self.canvas.get_renderer() if self.canvas else None
                bbox = ax.get_window_extent(renderer=renderer)
                ax_w_px = max(1.0, float(bbox.width))
                ax_h_px = max(1.0, float(bbox.height))
            except Exception:
                pass

            line_len = max(0.04, 48.0 / ax_w_px)
            line_gap = max(0.01, 8.0 / ax_w_px)
            # Add more vertical separation between labels in superimpose mode.
            row_step = max(0.036, (float(label_fs) + 14.0) / ax_h_px)
            module_h = row_step * max(0, len(entries) - 1)
            text_w_frac = max(0.08, min(0.62, (float(max_chars) * float(label_fs) * 0.62 + 14.0) / ax_w_px))

            x_anchor = min(max(frac_x, 0.0), 1.0)
            y_anchor = min(max(y_rel, 0.0), 1.0)

            gdx, gdy = self.global_label_offset
            gdx_min = (0.01 + line_len + line_gap) - x_anchor
            gdx_max = (0.99 - text_w_frac) - x_anchor
            if gdx_min <= gdx_max:
                gdx = min(max(gdx, gdx_min), gdx_max)
            else:
                gdx = gdx_min

            y_top_nominal = y_anchor
            y_bottom_nominal = y_anchor - module_h
            gdy_min = 0.01 - y_bottom_nominal
            gdy_max = 0.99 - y_top_nominal
            if gdy_min <= gdy_max:
                gdy = min(max(gdy, gdy_min), gdy_max)
            else:
                gdy = gdy_min

            self.global_label_offset = (gdx, gdy)
            x_text = x_anchor + gdx
            y_top = y_anchor + gdy

            for row_i, entry in enumerate(entries):
                idx = int(entry.get("idx", -1))
                label_text = self.patterns[idx]["label"] if (0 <= idx < len(self.patterns)) else entry["artist"].get_text()
                y_row = y_top - row_i * row_step

                entry["artist"].set_text(self._format_chemical_plot(label_text))
                entry["artist"].set_transform(ax.transAxes)
                entry["artist"].set_position((x_text, y_row))

                tag_line = entry.get("line_artist", None)
                if tag_line is not None:
                    x_right = x_text - line_gap
                    x_left = x_right - line_len
                    if 0 <= idx < len(self.patterns):
                        color = self.patterns[idx].get("color", None)
                        if color:
                            tag_line.set_color(color)
                    tag_line.set_transform(ax.transAxes)
                    tag_line.set_xdata([x_left, x_right])
                    tag_line.set_ydata([y_row, y_row])
                    tag_line.set_visible(True)
            return

        gdx, gdy = self.global_label_offset
        x_off_min, x_off_max = -float("inf"), float("inf")
        y_off_min, y_off_max = -float("inf"), float("inf")
        placements = []

        for entry in self.label_texts:
            ax = entry.get("ax") or self._primary_ax()
            if ax is None:
                continue
            # Keep non-superimpose labels locked in axis coordinates so zoom/scale does not move them.
            x_anchor = float(frac_x)
            y_anchor = float(y_rel * y_scale)
            x_min, x_max = 0.015, 0.985
            y_min, y_max = 0.10, 0.97

            if x_min < x_max:
                x_off_min = max(x_off_min, x_min - x_anchor)
                x_off_max = min(x_off_max, x_max - x_anchor)
            if y_min < y_max:
                y_off_min = max(y_off_min, y_min - y_anchor)
                y_off_max = min(y_off_max, y_max - y_anchor)

            placements.append((entry, x_anchor, y_anchor, x_min, x_max, y_min, y_max))

        if x_off_min <= x_off_max:
            gdx = min(max(gdx, x_off_min), x_off_max)
        if y_off_min <= y_off_max:
            gdy = min(max(gdy, y_off_min), y_off_max)

        self.global_label_offset = (gdx, gdy)

        for entry, x_anchor, y_anchor, x_min, x_max, y_min, y_max in placements:
            x_pos = x_anchor + gdx
            y_pos = y_anchor + gdy
            if x_min < x_max:
                x_pos = min(max(x_pos, x_min), x_max)
            if y_min < y_max:
                y_pos = min(max(y_pos, y_min), y_max)
            ax = entry.get("ax") or self._primary_ax()
            if ax is not None:
                entry["artist"].set_transform(ax.transAxes)
            entry["artist"].set_position((x_pos, y_pos))
            tag_line = entry.get("line_artist", None)
            if tag_line is not None:
                tag_line.set_visible(False)

    def apply_top_labels(self, fontsize=None):
        self.show_top_labels = False
        self.fig.suptitle("")

    def on_toggle_curve_labels(self, checked):
        self.show_curve_labels = bool(checked)
        if self.curve_labels_btn:
            self.curve_labels_btn.setText("Curve labels: On" if self.show_curve_labels else "Curve labels: Off")
        self.update_plot(preserve_view=True)

    def on_superimpose_toggled(self, _state):
        self.update_plot(preserve_view=True)

    # ---------------- Plot Logic ---------------- #

    def update_plot(self, preserve_view=False):
        if not self.patterns:
            return
        try:
            gap = float(self.fixed_y_headroom)
            curve_label_fs = int(self.curve_label_fs_spin.value()) if self.curve_label_fs_spin else 10
            tick_fs = int(self.tick_fs_spin.value()) if self.tick_fs_spin else 10
            axis_label_fs = int(self.axis_label_fs_spin.value()) if self.axis_label_fs_spin else 10
            title_fs = int(self.title_fs_spin.value()) if self.title_fs_spin else 10
            curve_lw = float(self.curve_lw_spin.value()) if self.curve_lw_spin else 1.0
            frame_lw = float(self.frame_lw_spin.value()) if self.frame_lw_spin else 1.5
            curve_y_offset = float(self.curve_offset_spin.value()) if self.curve_offset_spin else 0.03
            self.fig.set_size_inches(self.fig_w_spin.value(), self.fig_h_spin.value(), forward=True)
        except Exception:
            return

        old_xlim = old_ylim = None
        old_ax = self._primary_ax()
        if preserve_view and old_ax:
            try:
                old_xlim = old_ax.get_xlim()
                old_ylim = old_ax.get_ylim()
            except Exception:
                old_xlim = old_ylim = None

        self.fig.clear()
        self.axes = []
        self.label_texts = []
        self.plot_lines = []
        self.cursor_vlines = []
        self.cursor_vline = None
        self.cursor_text = None
        self.zoom_rect = None
        self.zoom_start_vlines = []
        self.zoom_end_vlines = []
        self.analysis_artists = []
        self.press_event = None
        self.pan_event = None

        y_list = []
        if self.norm_cb.isChecked():
            for p in self.patterns:
                m = np.max(p["y"])
                y_list.append(p["y"] / m if m > 0 else p["y"])
        else:
            y_list = [p["y"] for p in self.patterns]

        scaled_y_list = []
        y_base_list = []
        min_curve_value = None
        max_curve_value = None
        for p, y_proc in zip(self.patterns, y_list):
            y_base = np.asarray(y_proc) * float(p.get("scale", 1.0)) + curve_y_offset
            y_shift = float(p.get("y_shift", 0.0))
            y_scaled = y_base + y_shift
            y_base_list.append(y_base)
            scaled_y_list.append(y_scaled)
            if y_scaled.size == 0:
                continue
            y_min = float(np.min(y_scaled))
            y_max = float(np.max(y_scaled))
            min_curve_value = y_min if min_curve_value is None else min(min_curve_value, y_min)
            max_curve_value = y_max if max_curve_value is None else max(max_curve_value, y_max)

        if min_curve_value is None or max_curve_value is None:
            min_curve_value, max_curve_value = 0.0, 1.0

        y_bottom = min(0.0, min_curve_value)
        y_top = max_curve_value
        if y_top <= y_bottom:
            y_top = y_bottom + 1.0
        span = y_top - y_bottom
        if gap > 0:
            y_top = y_bottom + span * gap
        if y_top <= y_bottom:
            y_top = y_bottom + max(span, 1.0)

        self._base_gap_current = float(y_top - y_bottom)

        axes = []
        n = len(self.patterns)
        superimpose_mode = self._is_superimpose_mode()
        preview_info = self._powerxrd_preview_data if isinstance(self._powerxrd_preview_data, dict) else None
        grid = None
        if preview_info:
            if superimpose_mode:
                grid = self.fig.add_gridspec(2, 1, height_ratios=[1.1, 1.6], hspace=0.03)
            else:
                main_row_weight = 1.6
                height_ratios = [1.1] + [main_row_weight] * n
                grid = self.fig.add_gridspec(n + 1, 1, height_ratios=height_ratios, hspace=0.03)
            self.preview_ax = self.fig.add_subplot(grid[0, 0])
        else:
            self.preview_ax = None

        plot_axes_for_pattern = []
        if superimpose_mode:
            if grid is None:
                main_ax = self.fig.add_subplot(111)
            else:
                main_ax = self.fig.add_subplot(grid[1, 0])
            # Keep axis background transparent so overflow curves are not hidden by neighboring subplot patches.
            main_ax.set_facecolor("none")
            main_ax.patch.set_visible(False)
            axes.append(main_ax)
            plot_axes_for_pattern = [main_ax] * n
        else:
            for i in range(n):
                shared_ax = axes[0] if axes else None
                if grid is None:
                    ax = self.fig.add_subplot(
                        n, 1, i + 1,
                        sharex=shared_ax,
                        sharey=shared_ax,
                    )
                else:
                    ax = self.fig.add_subplot(
                        grid[i + 1, 0],
                        sharex=shared_ax,
                        sharey=shared_ax,
                    )
                # Keep axis background transparent so overflow curves are not hidden by neighboring subplot patches.
                ax.set_facecolor("none")
                ax.patch.set_visible(False)
                axes.append(ax)
                plot_axes_for_pattern.append(ax)

        for i, (p, y_scaled, y_base, ax) in enumerate(zip(self.patterns, scaled_y_list, y_base_list, plot_axes_for_pattern)):
            color = p.get("color")
            line, = ax.plot(p["x"], y_scaled, color=color, linewidth=curve_lw, clip_on=False)
            self.plot_lines.append({
                "line": line,
                "x": p["x"],
                "y": y_scaled,
                "y_base": y_base,
                "label": p["label"],
                "ax": ax,
            })

            if self.show_curve_labels:
                if superimpose_mode:
                    txt = ax.text(
                        0,
                        0,
                        self._format_chemical_plot(p["label"]),
                        transform=ax.transAxes,
                        fontsize=curve_label_fs,
                        color="#1f1f1f",
                        va="center",
                        ha="left",
                        clip_on=False,
                        zorder=9,
                    )
                    tag_line, = ax.plot(
                        [0.0, 0.0],
                        [0.0, 0.0],
                        transform=ax.transAxes,
                        color=color if color else "#000000",
                        linewidth=max(2.0, curve_lw + 0.4),
                        solid_capstyle="round",
                        clip_on=False,
                        zorder=10,
                    )
                    self.label_texts.append({"artist": txt, "line_artist": tag_line, "idx": i, "ax": ax})
                else:
                    txt = ax.text(
                        0,
                        0,
                        self._format_chemical_plot(p["label"]),
                        transform=ax.transAxes,
                        fontsize=curve_label_fs,
                        va="center",
                        ha="left",
                        clip_on=False,
                        zorder=8,
                    )
                    self.label_texts.append({"artist": txt, "line_artist": None, "idx": i, "ax": ax})

        self.axes = axes
        self.ax = self._primary_ax()
        self._apply_figure_layout()

        if preview_info and self.preview_ax is not None:
            p_ax = self.preview_ax
            nx = np.asarray(preview_info.get("new_x", []), dtype=np.float64)
            ny = np.asarray(preview_info.get("new_y", []), dtype=np.float64)

            if self.norm_cb and self.norm_cb.isChecked():
                if ny.size > 0:
                    m1 = float(np.max(ny))
                    if m1 > 0:
                        ny = ny / m1

            p_ax.set_facecolor("#f7f9fc")
            p_ax.patch.set_alpha(1.0)
            if nx.size > 0 and ny.size == nx.size:
                p_ax.plot(nx, ny, color="#d32f2f", linewidth=1.2, alpha=0.95, label="Adjusted")

            peak_info = preview_info.get("peak")
            if isinstance(peak_info, dict):
                peak_x = peak_info.get("peak_x")
                left_x = peak_info.get("left_x")
                right_x = peak_info.get("right_x")
                if peak_x is not None and np.isfinite(float(peak_x)):
                    p_ax.axvline(float(peak_x), color="#d32f2f", linestyle="--", linewidth=1.0, alpha=0.9)
                if left_x is not None and right_x is not None:
                    lx = float(left_x)
                    rx = float(right_x)
                    if np.isfinite(lx) and np.isfinite(rx):
                        y_lo, y_hi = p_ax.get_ylim()
                        y_mid = y_lo + 0.6 * (y_hi - y_lo)
                        p_ax.hlines(y_mid, lx, rx, colors="#1565c0", linestyles="-", linewidth=1.1)

            title = str(preview_info.get("title", "Preview"))
            p_ax.set_title(title, fontsize=max(8, tick_fs - 1))
            p_ax.set_xticks([])
            p_ax.set_yticks([])
            p_ax.tick_params(
                axis="both",
                which="both",
                bottom=False,
                top=False,
                left=False,
                right=False,
                labelbottom=False,
                labelleft=False,
            )
            for spine in p_ax.spines.values():
                spine.set_visible(False)
            if p_ax.lines:
                p_ax.legend(loc="upper right", fontsize=max(7, tick_fs - 3), frameon=False)

        axes_for_ticks = self._plot_axes()
        for i, ax in enumerate(axes_for_ticks):
            ax.tick_params(axis="x", which="both", labelsize=tick_fs)
            ax.tick_params(axis="y", which="both", labelsize=tick_fs)
            ax.yaxis.set_ticks([])
            ax.set_ylim(y_bottom, y_top, emit=False)
            if (not superimpose_mode) and i < (len(axes_for_ticks) - 1):
                ax.tick_params(axis="x", which="both", labelbottom=False, bottom=False)

        self.title_artist = (
            self.ax.set_title(self._format_chemical_plot(self.title_text), fontsize=title_fs)
            if self.ax else None
        )
        axes_for_label = self._plot_axes()
        bottom_ax = axes_for_label[-1] if axes_for_label else None
        self.x_label_artist = (
            bottom_ax.set_xlabel(self._format_chemical_plot(self.x_axis_label_text), fontsize=axis_label_fs)
            if bottom_ax else None
        )
        if axes_for_label:
            top_pos = axes_for_label[0].get_position()
            bottom_pos = axes_for_label[-1].get_position()
            y_center = 0.5 * (top_pos.y1 + bottom_pos.y0)
            x_pos = max(0.005, top_pos.x0 - 0.028)
            self.y_label_artist = self.fig.text(
                x_pos,
                y_center,
                self._format_chemical_plot(self.y_axis_label_text),
                fontsize=axis_label_fs,
                rotation=90,
                ha="center",
                va="center",
            )
        else:
            self.y_label_artist = None
        self.apply_top_labels(fontsize=curve_label_fs)

        # Frame line width uses frame_lw only
        self.apply_frame_style(frame_lw)

        primary_ax = self._primary_ax()
        if primary_ax is not None:
            self.xlim_full, self.ylim_full = primary_ax.get_xlim(), primary_ax.get_ylim()

        if preserve_view and old_xlim is not None and old_ylim is not None:
            try:
                self._set_shared_xlim(old_xlim[0], old_xlim[1])
                if not self.live_norm_cb.isChecked():
                    self._set_shared_ylim(old_ylim[0], old_ylim[1])
            except Exception:
                pass
        else:
            xmin = float(self.xmin_spin.value()) if self.xmin_spin else 2.0
            xmax = float(self.xmax_spin.value()) if self.xmax_spin else 30.0
            if xmin < xmax:
                self._set_shared_xlim(xmin, xmax)

        self._apply_horizontal_clip_only()

        # Re-apply once for stability
        self.apply_frame_style(frame_lw)
        self._redraw_applied_peak_overlay()

        for ax in self._plot_axes():
            self.cursor_vlines.append(
                ax.axvline(2, color="#666666", linestyle="--", linewidth=0.8, alpha=0.8, visible=False)
            )
        self.cursor_vline = self.cursor_vlines[0] if self.cursor_vlines else None
        self.cursor_text = self.fig.text(
            0.012, 0.988, "X=2.00",
            transform=self.fig.transFigure,
            ha="left", va="top",
            fontsize=10, color="#444444",
            visible=False
        )

        self.reposition_labels()
        self.apply_selection_highlight()
        self.canvas.draw_idle()
        self.refresh_files_list()

    def clear_plot(self):
        self.xlim_full = self.ylim_full = None
        self._build_empty_plot()
        self.canvas.draw()

    # ---------------- Mouse Wheel: Ctrl zooms canvas view; plain wheel scales selected curve ---------------- #

    def on_scroll(self, event):
        step = getattr(event, "step", 0)
        if step == 0:
            return

        modifiers = QApplication.keyboardModifiers()
        if bool(modifiers & Qt.KeyboardModifier.ControlModifier):
            old_scale = float(getattr(self, "canvas_view_scale", 1.0))
            new_scale = old_scale * (1.08 ** float(step))
            self._set_canvas_view_scale(new_scale)
            return

        if not self._is_plot_axis(event.inaxes):
            return

        if not self.patterns:
            return

        self._push_undo_snapshot()
        factor = 1.10 if step > 0 else (1.0 / 1.10)
        if self.selected_idx is not None and 0 <= self.selected_idx < len(self.patterns):
            p = self.patterns[self.selected_idx]
            s0 = float(p.get("scale", 1.0))
            s1 = s0 * factor
            s1 = max(0.02, min(200.0, s1))
            p["scale"] = s1
        else:
            for p in self.patterns:
                s0 = float(p.get("scale", 1.0))
                s1 = s0 * factor
                p["scale"] = max(0.02, min(200.0, s1))

        self._update_scaled_curves()

    # ---------------- Interaction Events ---------------- #

    def _update_scaled_curves(self):
        """Scale existing artists without rebuilding axes or changing the view."""
        if len(self.plot_lines) != len(self.patterns):
            self.update_plot(preserve_view=True)
            return
        bounds = []
        for p, info in zip(self.patterns, self.plot_lines):
            y = np.asarray(p["y"])
            maximum = float(np.max(y)) if self.norm_cb.isChecked() else 1.0
            divisor = maximum if maximum > 0 else 1.0
            base = y / divisor * float(p.get("scale", 1.0)) + self.curve_offset_spin.value()
            drawn = base + float(p.get("y_shift", 0.0))
            info["y_base"], info["y"] = base, drawn
            info["line"].set_ydata(drawn)
            bounds.extend((float(np.min(drawn)), float(np.max(drawn))))
        bottom = min(0.0, min(bounds))
        top = max(bounds)
        span = top - bottom if top > bottom else 1.0
        gap = float(self.fixed_y_headroom)
        self.ylim_full = (bottom, bottom + span * (gap if gap > 0 else 1.0))
        self._base_gap_current = self.ylim_full[1] - bottom
        if self.live_norm_cb.isChecked():
            self._apply_live_normalization()
        else:
            self._redraw_applied_peak_overlay()
            self.reposition_labels()
        self.refresh_files_list()
        self.canvas.draw_idle()

    def canvas_draw_idle(self):
        self.canvas.draw_idle()

    def dragEnterEvent(self, event):
        if event.mimeData() and event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        files = self._paths_from_mime(event.mimeData())
        if files:
            self.queue_patterns_from_paths(files)
            event.acceptProposedAction()
        else:
            event.ignore()

    def on_mouse_press(self, event):
        # Allow double-click edits first; if the click lands on empty plot space, restore the full view.
        if event.button == 1 and event.dblclick:
            if self.handle_label_double_click(event):
                return
            if self.handle_double_click(event):
                return
            if self._is_plot_axis(event.inaxes):
                self._reset_full_view()
                return

        if not self._is_plot_axis(event.inaxes):
            return

        if event.button == 2:
            self._push_undo_snapshot()
            self.pan_event = event
            self.pan_xlim_start = event.inaxes.get_xlim()
            return

        if event.button == 1:

            for i, info in enumerate(self.plot_lines):
                if info["line"].contains(event)[0]:
                    if self._is_superimpose_mode() and event.ydata is not None:
                        self.dragging_curve_idx = i
                        self.drag_curve_axis = event.inaxes
                        self.drag_curve_start_ydata = float(event.ydata)
                        self.drag_curve_start_shift = float(self.patterns[i].get("y_shift", 0.0))
                        self.drag_curve_moved = False
                        self.selected_idx = i
                        if self.files_list and 0 <= i < self.files_list.count():
                            self.files_list.setCurrentRow(i)
                        self.apply_selection_highlight()
                        return

                    if self.selected_idx == i:
                        self.selected_idx = None
                        if self.files_list:
                            self.files_list.clearSelection()
                            self.files_list.setCurrentRow(-1)
                    else:
                        self.selected_idx = i
                        if self.files_list and 0 <= i < self.files_list.count():
                            self.files_list.setCurrentRow(i)
                    self.apply_selection_highlight()
                    return

            for entry in self.label_texts:
                hit_label = entry["artist"].contains(event)[0]
                tag_line = entry.get("line_artist", None)
                hit_tag_line = bool(tag_line is not None and tag_line.contains(event)[0])
                if hit_label or hit_tag_line:
                    ax_for_drag = entry.get("ax") or event.inaxes
                    if not self._is_plot_axis(ax_for_drag):
                        return
                    self._push_undo_snapshot()
                    self.dragging_label = True
                    self.drag_label_idx = int(entry.get("idx", -1))
                    self.drag_label_axis = ax_for_drag
                    if event.x is None or event.y is None:
                        self.dragging_label = False
                        return
                    try:
                        px, py = ax_for_drag.transAxes.inverted().transform((event.x, event.y))
                        self.drag_start_data = (float(px), float(py))
                    except Exception:
                        self.dragging_label = False
                        return
                    self.drag_start_global_offset = tuple(self.global_label_offset)
                    return

            if self.selected_idx is not None:
                self.selected_idx = None
                if self.files_list:
                    self.files_list.clearSelection()
                    self.files_list.setCurrentRow(-1)
                self.apply_selection_highlight()

            if event.xdata is None:
                return
            self.press_event = event
            self._show_zoom_guides(float(event.xdata), float(event.xdata))
            if self.cursor_vlines:
                for vline in self.cursor_vlines:
                    vline.set_visible(False)
            if self.cursor_text:
                self.cursor_text.set_visible(False)
            self.canvas.draw_idle()

    def on_mouse_move(self, event):
        if self.dragging_curve_idx is not None:
            if not self._is_superimpose_mode():
                self.dragging_curve_idx = None
                self.drag_curve_axis = None
                self.drag_curve_start_ydata = None
                self.drag_curve_start_shift = None
                self.drag_curve_moved = False
                return

            idx = int(self.dragging_curve_idx)
            if idx < 0 or idx >= len(self.patterns):
                self.dragging_curve_idx = None
                return
            if event.inaxes != self.drag_curve_axis or event.ydata is None:
                return
            if self.drag_curve_start_ydata is None or self.drag_curve_start_shift is None:
                return

            dy = float(event.ydata) - float(self.drag_curve_start_ydata)
            new_shift = float(self.drag_curve_start_shift) + dy
            old_shift = float(self.patterns[idx].get("y_shift", 0.0))
            if 0 <= idx < len(self.plot_lines):
                info = self.plot_lines[idx]
                y_base = np.asarray(info.get("y_base", []), dtype=np.float64)
                x_base = np.asarray(info.get("x", []), dtype=np.float64)
                ax = info.get("ax")
                if y_base.size and ax is not None:
                    y0, y1 = ax.get_ylim()
                    if np.isfinite(y0) and np.isfinite(y1):
                        lower_bound = min(y0, y1)
                        upper_bound = max(y0, y1)
                        # Use current visible X-range for drag limits; fall back to full curve if needed.
                        y_for_bounds = y_base
                        if x_base.size == y_base.size:
                            x0, x1 = ax.get_xlim()
                            if np.isfinite(x0) and np.isfinite(x1):
                                x_lo = min(x0, x1)
                                x_hi = max(x0, x1)
                                visible_mask = (
                                    np.isfinite(x_base)
                                    & np.isfinite(y_base)
                                    & (x_base >= x_lo)
                                    & (x_base <= x_hi)
                                )
                                if np.any(visible_mask):
                                    y_for_bounds = y_base[visible_mask]

                        y_for_bounds = y_for_bounds[np.isfinite(y_for_bounds)]
                        if y_for_bounds.size:
                            min_shift_allowed = lower_bound - float(np.min(y_for_bounds))
                            max_shift_allowed = upper_bound - float(np.max(y_for_bounds))
                            if min_shift_allowed <= max_shift_allowed:
                                new_shift = min(max(new_shift, min_shift_allowed), max_shift_allowed)
                            else:
                                new_shift = min_shift_allowed

            if not self.drag_curve_moved and abs(new_shift - old_shift) > 1e-12:
                self._push_undo_snapshot()
                self.drag_curve_moved = True
            self.patterns[idx]["y_shift"] = new_shift

            if 0 <= idx < len(self.plot_lines):
                info = self.plot_lines[idx]
                y_base = np.asarray(info.get("y_base", []), dtype=np.float64)
                if y_base.size:
                    y_draw = y_base + new_shift
                    info["y"] = y_draw
                    info["line"].set_ydata(y_draw)

            self.reposition_labels()
            self._redraw_applied_peak_overlay()
            self.canvas.draw_idle()
            return

        if self.press_event is not None:
            x0 = self.press_event.xdata
            if x0 is not None and event.xdata is not None:
                self._show_zoom_guides(float(x0), float(event.xdata))
            if self.cursor_vlines:
                for vline in self.cursor_vlines:
                    vline.set_visible(False)
            if self.cursor_text:
                self.cursor_text.set_visible(False)
            self.canvas.draw_idle()
            return

        if self.cursor_vlines and self.cursor_text:
            if self._is_plot_axis(event.inaxes) and event.xdata is not None:
                x_cur = float(event.xdata)
                for vline in self.cursor_vlines:
                    vline.set_xdata([x_cur, x_cur])
                    vline.set_visible(True)
                self.cursor_text.set_text(f"X={x_cur:.2f}")
                self.cursor_text.set_visible(True)
            else:
                for vline in self.cursor_vlines:
                    vline.set_visible(False)
                self.cursor_text.set_visible(False)

        if self.dragging_label:
            if event.inaxes != self.drag_label_axis:
                self.drag_start_data = None
                return

            if event.x is None or event.y is None:
                self.drag_start_data = None
                return
            try:
                px, py = self.drag_label_axis.transAxes.inverted().transform((event.x, event.y))
                x_now = float(px)
                y_now = float(py)
            except Exception:
                self.drag_start_data = None
                return

            if self.drag_start_data is None:
                self.drag_start_data = (x_now, y_now)
                return

            dx = x_now - self.drag_start_data[0]
            dy = y_now - self.drag_start_data[1]
            ox, oy = self.global_label_offset
            self.global_label_offset = (ox + dx, oy + dy)
            self.reposition_labels()

            # Use incremental dragging to avoid hidden offset accumulation near bounds.
            self.drag_start_data = (x_now, y_now)
            self.drag_start_global_offset = tuple(self.global_label_offset)

            self.canvas.draw_idle()
            return

        if self.pan_event and event.xdata is not None:
            dx = event.xdata - self.pan_event.xdata
            x0, x1 = self.pan_xlim_start
            self._set_shared_xlim(x0 - dx, x1 - dx)
            self.reposition_labels()
            self.canvas.draw_idle()
            return

        # Disable hover tooltip box while keeping cursor/zoom interactions.
        if self.hover_annotation:
            self.hover_annotation.set_visible(False)
        if not self._cursor_draw_timer.isActive():
            self._cursor_draw_timer.start()

    def on_mouse_release(self, event):
        self.dragging_curve_idx = None
        self.drag_curve_axis = None
        self.drag_curve_start_ydata = None
        self.drag_curve_start_shift = None
        self.drag_curve_moved = False

        self.dragging_label = False
        self.drag_label_idx = None
        self.drag_start_data = None
        self.drag_start_global_offset = None
        self.drag_label_axis = None

        self.pan_event = None
        if self.press_event:
            x0, x1 = self.press_event.xdata, event.xdata
            if x0 is not None and x1 is not None and abs(x1 - x0) > 0.01:
                self._push_undo_snapshot()
                self._set_shared_xlim(min(x0, x1), max(x0, x1))
            self._clear_zoom_guides()
            self.reposition_labels()
            self.canvas.draw_idle()
        self.press_event = None

    # ---------------- Double-Click: Edit Curve Label ---------------- #

    def handle_label_double_click(self, event):
        for entry in self.label_texts:
            art = entry["artist"]
            tag_line = entry.get("line_artist", None)
            hit_tag_line = bool(tag_line is not None and tag_line.contains(event)[0])
            if art.contains(event)[0] or hit_tag_line:
                idx = entry["idx"]
                old = self.patterns[idx]["label"] if 0 <= idx < len(self.patterns) else art.get_text()
                new_t, ok = QInputDialog.getText(self, "Edit label", "Label text:", text=old)
                if ok:
                    new_t = (new_t or "").strip()
                    if new_t == "":
                        new_t = ""
                    if new_t != old:
                        self._push_undo_snapshot()
                    art.set_text(self._format_chemical_plot(new_t))
                    self.patterns[idx]["label"] = new_t
                    if 0 <= idx < len(self.plot_lines):
                        self.plot_lines[idx]["label"] = new_t
                    self.refresh_files_list()
                    self.canvas.draw_idle()
                return True
        return False

    # ---------------- Double-Click: Title / Axis Labels / Color ---------------- #

    def _is_event_near_artist(self, event, artist, pad_px=8):
        if event is None or artist is None:
            return False
        if event.x is None or event.y is None:
            return False
        try:
            renderer = self.canvas.get_renderer()
            bbox = artist.get_window_extent(renderer=renderer).expanded(1.0, 1.0)
            return (
                (bbox.x0 - pad_px) <= event.x <= (bbox.x1 + pad_px)
                and (bbox.y0 - pad_px) <= event.y <= (bbox.y1 + pad_px)
            )
        except Exception:
            return False

    def handle_double_click(self, event):
        for art in [self.x_label_artist, self.y_label_artist, self.title_artist]:
            if art is None:
                continue
            if art.contains(event)[0] or self._is_event_near_artist(event, art, pad_px=10):
                old_t = art.get_text()
                if art == self.title_artist:
                    old_t = self.title_text
                elif art == self.x_label_artist:
                    old_t = self.x_axis_label_text
                elif art == self.y_label_artist:
                    old_t = self.y_axis_label_text
                new_t, ok = QInputDialog.getText(self, "Edit text", "Content:", text=old_t)
                if ok:
                    if new_t != old_t:
                        self._push_undo_snapshot()
                    art.set_text(self._format_chemical_plot(new_t))
                    if art == self.title_artist:
                        self.title_text = new_t
                    elif art == self.x_label_artist:
                        self.x_axis_label_text = new_t
                        if self.x_axis_label_edit is not None:
                            self.x_axis_label_edit.blockSignals(True)
                            self.x_axis_label_edit.setText(new_t)
                            self.x_axis_label_edit.blockSignals(False)
                    elif art == self.y_label_artist:
                        self.y_axis_label_text = new_t
                        if self.y_axis_label_edit is not None:
                            self.y_axis_label_edit.blockSignals(True)
                            self.y_axis_label_edit.setText(new_t)
                            self.y_axis_label_edit.blockSignals(False)

                self.canvas.draw_idle()
                return True

        for i, info in enumerate(self.plot_lines):
            if info["line"].contains(event)[0]:
                color = QColorDialog.getColor()
                if color.isValid():
                    c_hex = color.name()
                    if c_hex != self.patterns[i].get("color"):
                        self._push_undo_snapshot()
                    info["line"].set_color(c_hex)
                    self.patterns[i]["color"] = c_hex
                    for entry in self.label_texts:
                        if int(entry.get("idx", -1)) == i:
                            tag_line = entry.get("line_artist", None)
                            if tag_line is not None:
                                tag_line.set_color(c_hex)
                    self.refresh_files_list()
                    self.canvas.draw_idle()
                return True
        return False

    # ---------------- Export ---------------- #

    def export_csv(self):
        if not self.patterns:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export CSV", self.project_dir, "CSV (*.csv)")
        if not path:
            return
        try:
            # Export raw points per curve without interpolation.
            curves = []
            max_points = 0
            for idx, p in enumerate(self.patterns, start=1):
                x_arr = np.asarray(p.get("x", []), dtype=np.float64).reshape(-1)
                y_arr = np.asarray(p.get("y", []), dtype=np.float64).reshape(-1)
                n_points = int(min(x_arr.size, y_arr.size))
                if n_points <= 0:
                    continue
                if x_arr.size != n_points:
                    x_arr = x_arr[:n_points]
                if y_arr.size != n_points:
                    y_arr = y_arr[:n_points]

                label = str(p.get("label", f"curve_{idx}"))
                curves.append((idx, label, x_arr, y_arr))
                max_points = max(max_points, n_points)

            if not curves:
                QMessageBox.warning(self, "Export CSV", "No valid curve data to export.")
                return

            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                header = []
                for idx, label, _x_arr, _y_arr in curves:
                    base = f"{idx}:{label}"
                    header.extend([f"{base}:2theta", f"{base}:intensity"])
                writer.writerow(header)

                for i in range(max_points):
                    row = []
                    for _idx, _label, x_arr, y_arr in curves:
                        if i < x_arr.size:
                            row.extend([float(x_arr[i]), float(y_arr[i])])
                        else:
                            row.extend(["", ""])
                    writer.writerow(row)
            QMessageBox.information(self, "Success", "CSV exported (raw points per curve).")
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def export_svg(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export SVG", self.project_dir, "SVG (*.svg)")
        if not path:
            return
        try:
            bg_mode = self._current_svg_background()
            svg_bytes = self._make_svg_bytes(background_mode=bg_mode)
            with open(path, "wb") as f:
                f.write(svg_bytes)
            bg_text = "transparent" if bg_mode == "transparent" else "white"
            QMessageBox.information(self, "Success", f"SVG exported ({bg_text} background).")
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def export_png(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export PNG", self.project_dir, "PNG (*.png)")
        if not path:
            return
        if not path.lower().endswith(".png"):
            path += ".png"
        try:
            bg_mode = self._current_svg_background()
            png_bytes = self._make_png_bytes(background_mode=bg_mode)
            with open(path, "wb") as f:
                f.write(png_bytes)
            bg_text = "transparent" if bg_mode == "transparent" else "white"
            QMessageBox.information(self, "Success", f"PNG exported ({bg_text} background).")
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def closeEvent(self, event):
        self._import_timer.stop()
        self._browser_search_timer.stop()
        self._cursor_draw_timer.stop()
        # QtAgg queues a zero-delay draw; cancel it before Qt deletes the canvas.
        self.canvas._draw_pending = False
        self._cleanup_clipboard_temp_png()
        super().closeEvent(event)


if __name__ == "__main__":
    startup_paths = [os.path.abspath(path) for path in sys.argv[1:]]
    app = QApplication(sys.argv)
    file_service = FileOpenService()
    try:
        if not file_service.start_or_forward(startup_paths):
            sys.exit(0)
    except RuntimeError as exc:
        QMessageBox.critical(None, "xStack", str(exc))
        sys.exit(1)
    app.aboutToQuit.connect(file_service.close)
    app_icon_path = resolve_app_icon_path()
    if app_icon_path:
        app.setWindowIcon(QIcon(app_icon_path))

    splash = None
    splash_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "starting_fig.png")
    if not startup_paths and os.path.isfile(splash_path):
        splash_pixmap = QPixmap(splash_path)
        if not splash_pixmap.isNull():
            # The branded splash includes version and author information.
            splash_overlay_text = ""
            # Keep the full 3x artwork. Downsampling to logical pixels here
            # would force Qt to enlarge a low-resolution image on HiDPI screens.
            splash_pixmap.setDevicePixelRatio(3.0)
            splash = ClickableSplashScreen(
                splash_pixmap,
                "https://orcid.org/0000-0001-9846-8127",
                overlay_text=splash_overlay_text,
            )
            splash.show()
            app.processEvents()

    def _start_main_window():
        window = PXRDMultiCompareApp(startup_paths=startup_paths)
        window.show()
        if splash is not None:
            splash.finish(window)
        app.main_window = window
        file_service.attach_window(window)

    QTimer.singleShot(2000 if splash is not None else 0, _start_main_window)
    sys.exit(app.exec())

