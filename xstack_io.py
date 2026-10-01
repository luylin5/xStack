"""PXRD file readers. Pure data code: no Qt or matplotlib imports."""
import math
import os
import struct
import xml.etree.ElementTree as ET

import numpy as np


TEXT_PXRD_EXTS = (
    ".txt", ".dat", ".xy", ".xye", ".chi",
    ".asc", ".uxd", ".ras", ".csv",
)
BINARY_PXRD_EXTS = (".raw",)
SUPPORTED_PXRD_EXTS = set(TEXT_PXRD_EXTS + BINARY_PXRD_EXTS + (".xrdml",))
PXRD_FILE_DIALOG_FILTER = (
    f"PXRD ({' '.join(f'*{ext}' for ext in (TEXT_PXRD_EXTS + BINARY_PXRD_EXTS))} *.xrdml);;All (*)"
)


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


def readonly_array(values):
    """Curve arrays are replaced, never edited in place, so undo snapshots can share them."""
    if isinstance(values, np.ndarray) and not values.flags.writeable:
        return values
    arr = np.array(values, copy=True)
    arr.flags.writeable = False
    return arr


def search_pattern_files(root, query, exts, cancel=None, limit=2000):
    """Walk *root* for PXRD files whose relative path contains *query*.

    Runs in a worker thread. Returns ``(matches, truncated)`` where each match is
    ``(full_path, rel_display, mtime)``; stops early when *cancel* is set.
    """
    query = query.lower()
    matches = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _e: None):
        if cancel is not None and cancel.is_set():
            return [], False
        dirnames[:] = [d for d in dirnames if not d.startswith((".", "$"))]
        for fname in filenames:
            if os.path.splitext(fname)[1].lower() not in exts:
                continue
            full_path = os.path.join(dirpath, fname)
            rel_display = os.path.relpath(full_path, root).replace("\\", "/")
            if query not in rel_display.lower():
                continue
            try:
                mtime = os.path.getmtime(full_path)
            except OSError:
                mtime = 0.0
            matches.append((full_path, rel_display, mtime))
            if len(matches) >= limit:
                return matches, True
    return matches, False
