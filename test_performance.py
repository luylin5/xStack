"""Behavior checks for incremental plotting and coalesced input."""
import types
import unittest
from unittest.mock import patch

import numpy as np
import test_data_safety


class IncrementalPlotTests(unittest.TestCase):
    setUpClass = classmethod(test_data_safety.DataSafetyTests.setUpClass.__func__)
    setUp = test_data_safety.DataSafetyTests.setUp

    def test_wheel_matches_full_rebuild_and_undo(self):
        w = self.window
        for overlay, live, normalized in [(False, False, True), (True, False, False),
                                           (False, True, True), (True, True, False)]:
            with self.subTest(overlay=overlay, live=live, normalized=normalized):
                w.superimpose_cb.setChecked(overlay)
                w.norm_cb.setChecked(normalized)
                w.live_norm_cb.setChecked(live)
                w.update_plot()
                w._set_shared_xlim(10, 12)
                w.selected_idx = 0
                original = [p['y'].copy() for p in w.patterns]
                scales = [p['scale'] for p in w.patterns]
                axes = list(w.axes)
                lines = [info['line'] for info in w.plot_lines]
                w.on_scroll(types.SimpleNamespace(step=1, inaxes=w._primary_ax()))
                self.assertEqual(w.axes, axes)
                self.assertEqual([info['line'] for info in w.plot_lines], lines)
                self.assertAlmostEqual(w.patterns[0]['scale'], scales[0] * 1.1)
                self.assertEqual(w.patterns[1]['scale'], scales[1])
                drawn = [info['y'].copy() for info in w.plot_lines]
                limits = (w.ax.get_xlim(), w.ax.get_ylim())
                full_ylim = w.ylim_full
                w.update_plot(preserve_view=True)
                for info, expected, p, raw in zip(w.plot_lines, drawn, w.patterns, original):
                    np.testing.assert_allclose(info['y'], expected)
                    np.testing.assert_array_equal(p['y'], raw)
                np.testing.assert_allclose(w.ax.get_xlim(), limits[0])
                np.testing.assert_allclose(w.ax.get_ylim(), limits[1])
                np.testing.assert_allclose(w.ylim_full, full_ylim)
                w.undo_last_action()
                self.assertEqual([p['scale'] for p in w.patterns], scales)

    def test_search_waits_for_typing_pause(self):
        w = self.window
        with patch.object(w, 'populate_browser_tree') as scan:
            for text in ('a', 'ab', 'abc'):
                w.on_browser_search_changed(text)
            scan.assert_not_called()
            self.assertTrue(w._browser_search_timer.isActive())
        w._browser_search_timer.stop()


if __name__ == '__main__':
    import unittest
    unittest.main()
