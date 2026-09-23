"""Visible-range normalization must affect display only."""
import unittest
import test_data_safety
import numpy as np


class LiveNormalizationTests(unittest.TestCase):
    setUpClass = classmethod(test_data_safety.DataSafetyTests.setUpClass.__func__)
    setUp = test_data_safety.DataSafetyTests.setUp
    save = test_data_safety.DataSafetyTests.save

    def test_zoom_pan_reset_and_disable(self):
        w = self.window
        originals = [p['y'].copy() for p in w.patterns]
        self.assertFalse(w.live_norm_cb.isChecked())
        w.live_norm_cb.setChecked(True)
        for limits in [(10, 11), (13, 14), (11, 10), (50, 60)]:
            w._set_shared_xlim(*limits)
            lo, hi = sorted(limits)
            for p, line, original in zip(w.patterns, w.plot_lines, originals):
                mask = (p['x'] >= lo) & (p['x'] <= hi)
                maximum = original[mask].max() if mask.any() else original.max()
                np.testing.assert_allclose(line['line'].get_ydata(), original / maximum + w.curve_offset_spin.value())
                np.testing.assert_array_equal(p['y'], original)
            self.assertTrue(np.all(np.isfinite(w.ax.get_ylim())))
        w._reset_full_view()
        w.live_norm_cb.setChecked(False)
        for line, original in zip(w.plot_lines, originals):
            np.testing.assert_allclose(line['y'], original / original.max() + w.curve_offset_spin.value())

    def test_raw_mode_overlay_and_project_roundtrip(self):
        w = self.window
        w.norm_cb.setChecked(False)
        w.live_norm_cb.setChecked(True)
        w.superimpose_cb.setChecked(True)
        w.run_powerxrd_peak_analysis(xmin=10, xmax=14, wavelength=1.5406, target_idx=0, show_dialog=False)
        w._set_shared_xlim(10, 11)
        np.testing.assert_allclose(w.analysis_artists[0].get_ydata(), [2 + w.curve_offset_spin.value()])
        path = self.save()
        w.live_norm_cb.setChecked(False)
        self.assertGreater(w.ax.get_ylim()[1], 100)
        w.load_project_path(path, False)
        self.assertTrue(w.live_norm_cb.isChecked())
        np.testing.assert_allclose(w.ax.get_xlim(), [10, 11])
        np.testing.assert_allclose(w.plot_lines[1]['y'][:2], [.5 + w.curve_offset_spin.value(), 1 + w.curve_offset_spin.value()])
        snapshot = w._capture_undo_snapshot()
        w.live_norm_cb.setChecked(False)
        w._restore_undo_snapshot(snapshot)
        self.assertTrue(w.live_norm_cb.isChecked())
        np.testing.assert_allclose(w.ax.get_xlim(), [10, 11])

    def test_zero_negative_and_empty_visible_ranges(self):
        w = self.window
        w.patterns[0]['y'][:] = 0
        w.patterns[1]['y'][:] = -2
        w.live_norm_cb.setChecked(True)
        w._set_shared_xlim(10, 11)
        for line in w.plot_lines:
            self.assertTrue(np.all(np.isfinite(line['y'])))
        self.assertTrue(np.all(np.isfinite(w.ax.get_ylim())))
