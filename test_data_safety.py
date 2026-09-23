"""Regression coverage for imports, project recovery, and stale tool previews."""
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
from test_file_open import xstack
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication, QFileDialog, QMessageBox, QPushButton


class DataSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        settings = QSettings(str(self.root / 'prefs.ini'), QSettings.Format.IniFormat)
        settings.setValue('browser/root_dir', str(self.root))
        self.window = xstack.PXRDMultiCompareApp(settings=settings)
        self.addCleanup(self.window.close)
        for name, values in [('a', [1, 5, 10, 5, 1]), ('b', [100, 200, 300, 400, 500])]:
            path = self.root / (name + '.xy')
            path.write_text(''.join(f'{10+i} {v}\n' for i, v in enumerate(values)))
            self.window.add_patterns_from_paths([str(path)])

    def save(self):
        path = str(self.root / 'saved.pxrdproj')
        with patch.object(QFileDialog, 'getSaveFileName', return_value=(path, '')), patch.object(QMessageBox, 'information'):
            self.window.save_project()
        return path

    def open_tools(self):
        self.window.files_list.setCurrentRow(0)
        self.window.selected_idx = 0
        self.window.open_powerxrd_tools_dialog()
        dialog = self.window.powerxrd_tools_dialog
        self.addCleanup(dialog.close)
        return dialog

    def test_bad_rows_keep_coordinate_pairs(self):
        path = self.root / 'bad.xy'
        path.write_text('10 5\n11 invalid\n12 7\n13 nan\ninf 8\n')
        x, y = xstack.load_two_column_file(str(path))
        np.testing.assert_array_equal(x, [10, 12])
        np.testing.assert_array_equal(y, [5, 7])
        self.window.add_patterns_from_paths([str(path)])
        self.assertEqual(len(self.window.plot_lines), 3)

    def test_missing_project_data_preserves_session(self):
        patterns = list(self.window.patterns)
        path = self.root / 'broken.pxrdproj'
        with zipfile.ZipFile(path, 'w') as z:
            z.writestr('project.json', json.dumps({'ui': {}, 'patterns': [{'uid': 1}]}))
        with patch.object(QMessageBox, 'critical') as error:
            self.window.load_project_path(str(path), False)
        error.assert_called_once()
        self.assertEqual([id(p) for p in self.window.patterns], [id(p) for p in patterns])
        self.assertEqual(self.window.files_list.count(), 2)

    def test_invalid_ui_rolls_back_session(self):
        path = self.save()
        with zipfile.ZipFile(path) as z:
            entries = {name: z.read(name) for name in z.namelist()}
        state = json.loads(entries['project.json'])
        state['ui']['curve_line_width'] = 'invalid'
        entries['project.json'] = json.dumps(state).encode()
        with zipfile.ZipFile(path, 'w') as z:
            for name, data in entries.items():
                z.writestr(name, data)
        before = self.window._capture_undo_snapshot()
        with patch.object(QMessageBox, 'critical') as error:
            self.window.load_project_path(path, False)
        error.assert_called_once()
        self.assertEqual(len(self.window.plot_lines), 2)
        self.assertEqual(self.window._capture_undo_snapshot()['ui'], before['ui'])
        for current, old in zip(self.window.patterns, before['patterns']):
            np.testing.assert_array_equal(current['y'], old['y'])

    def test_reorder_closes_stale_tools(self):
        dialog = self.open_tools()
        self.window.patterns.reverse()
        self.window.refresh_files_list()
        self.assertIsNone(self.window.powerxrd_tools_dialog)
        self.assertFalse(dialog.isVisible())
        np.testing.assert_array_equal(self.window.patterns[0]['y'], [100, 200, 300, 400, 500])

    def test_changed_data_rejects_apply(self):
        dialog = self.open_tools()
        self.window.patterns[0]['y'][:] = 42
        button = next(b for b in dialog.findChildren(QPushButton) if b.text() == 'Apply')
        button.click()
        np.testing.assert_array_equal(self.window.patterns[0]['y'], [42]*5)
        self.assertEqual(self.window.patterns[0]['label'], 'a')

    def test_unchanged_preview_can_apply(self):
        dialog = self.open_tools()
        next(b for b in dialog.findChildren(QPushButton) if b.text() == 'Apply').click()
        self.assertEqual(self.window.patterns[0]['label'], 'a_smooth')
        np.testing.assert_array_equal(self.window.patterns[1]['y'], [100, 200, 300, 400, 500])

    def test_project_restores_peak_and_resets_undo(self):
        self.window.run_powerxrd_peak_analysis(xmin=10, xmax=14, wavelength=1.5406, target_idx=0, show_dialog=False)
        overlay = dict(self.window.applied_peak_overlay)
        path = self.save()
        self.window.applied_peak_overlay = None
        self.window.load_project_path(path, False)
        self.assertEqual(self.window.applied_peak_overlay, overlay)
        self.assertTrue(self.window.analysis_artists)
        self.assertFalse(self.window.undo_stack)
        self.window.undo_last_action()
        self.assertEqual(len(self.window.patterns), 2)


if __name__ == '__main__':
    unittest.main()
