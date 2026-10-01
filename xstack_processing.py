"""Smoothing, background subtraction and peak geometry for PXRD curves."""
import numpy as np

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
