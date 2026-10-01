"""PXRD tools dialog and the curve transforms it applies to the main window."""
import logging

import numpy as np
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QInputDialog,
    QLabel, QMessageBox, QSpinBox, QStackedWidget, QVBoxLayout, QWidget,
)

from xstack_io import readonly_array
from xstack_richtext import to_plain
from xstack_processing import (
    estimate_peak_fwhm, powerxrd_background_subtraction, powerxrd_braggs,
    powerxrd_gaussian_smooth, powerxrd_moving_average_filter,
    powerxrd_savgol_smooth, powerxrd_scherrer,
)

logger = logging.getLogger("xstack")


class PxrdToolsMixin:
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
                logger.debug("Suppressed error", exc_info=True)
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
                logger.debug("Suppressed error", exc_info=True)
                return int(fallback)

        def _curve_label(idx):
            if idx < 0 or idx >= len(baseline_patterns):
                return "N/A"
            p = baseline_patterns[idx]
            return to_plain(p.get("label", p.get("filename", f"Curve {idx + 1}")))

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
            label = to_plain(pattern.get("label", pattern.get("filename", f"Curve {i + 1}")))
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
                self.patterns[idx]["x"] = readonly_array(x_new)
                self.patterns[idx]["y"] = readonly_array(y_new)
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
