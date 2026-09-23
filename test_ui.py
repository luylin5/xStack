"""Guard the established control positions and plotting behavior during UI work."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_file_open import xstack
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication, QPushButton, QFileDialog, QMessageBox


class UiCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        settings = QSettings(str(self.root / "prefs.ini"), QSettings.Format.IniFormat)
        settings.setValue("browser/root_dir", str(self.root))
        self.window = xstack.PXRDMultiCompareApp(settings=settings)
        self.addCleanup(self.window.close)

    def test_existing_button_positions(self):
        toolbar = self.window.centralWidget().layout().itemAt(0).widget().layout()
        buttons = [toolbar.itemAt(i).widget().text() for i in range(toolbar.count())
                   if isinstance(toolbar.itemAt(i).widget(), QPushButton)]
        self.assertEqual(buttons, ["Add PXRD files", "Save project", "Open project",
                                   "Curve labels: On", "Choose folder"])
        self.assertEqual(list(self.window.inspector_sections),
                         ["Display", "Curves & text", "Canvas & range", "Export"])
        export_buttons = {b.accessibleName() for b in self.window.inspector_scroll.findChildren(QPushButton)
                          if b.accessibleName()}
        self.assertEqual(export_buttons, {"Export CSV", "Export PNG", "Copy PNG", "Export SVG", "Copy SVG"})

    def test_fixed_sections_and_segmented_mode_preserve_values(self):
        self.window.curve_lw_spin.setValue(2.7)
        section = self.window.inspector_sections["Curves & text"]
        self.assertFalse(hasattr(section, "toggle"))
        self.assertFalse(section.content.isHidden())
        self.assertEqual(self.window.curve_lw_spin.value(), 2.7)
        switch = self.window.superimpose_cb
        switch.overlay_button.click()
        self.assertTrue(self.window._is_superimpose_mode())
        switch.blockSignals(True)
        switch.setChecked(False)
        switch.blockSignals(False)
        self.assertTrue(switch.stacked_button.isChecked())
        self.assertFalse(switch.overlay_button.isChecked())

    def test_loaded_list_keeps_identifiers_and_details(self):
        for index in range(2):
            path = self.root / f"sample_{index}.xy"
            path.write_text("2 1\n10 8\n20 3\n30 1\n", encoding="utf-8")
            self.window.open_external_files([str(path)])
        self.assertEqual(self.window.loaded_count_label.text(), "2")
        item = self.window.files_list.item(0)
        self.assertNotIn("orig:", item.text())
        self.assertFalse(item.icon().isNull())
        self.assertIn(str(self.root), item.toolTip())
        uid = self.window.patterns[0]["uid"]
        self.window.files_list.insertItem(1, self.window.files_list.takeItem(0))
        self.window.on_files_order_changed()
        self.assertEqual(self.window.patterns[1]["uid"], uid)
        self.window.undo_last_action()
        self.assertEqual(self.window.patterns[0]["uid"], uid)

    def test_live_plot_controls_and_exports(self):
        path = self.root / "sample.xy"
        path.write_text("2 1\n10 8\n20 3\n30 1\n", encoding="utf-8")
        self.window.open_external_files([str(path)])
        self.window.curve_lw_spin.setValue(2.5)
        self.assertEqual(self.window.plot_lines[0]["line"].get_linewidth(), 2.5)
        self.window.curve_labels_btn.click()
        self.assertFalse(self.window.show_curve_labels)
        self.window.superimpose_cb.setChecked(True)
        self.assertEqual(len(self.window.axes), 1)
        self.assertTrue(self.window._make_png_bytes(background_mode="white").startswith(b"\x89PNG"))
        self.assertIn(b"<svg", self.window._make_svg_bytes(background_mode="transparent"))

    def test_plot_defaults_survive_new_window(self):
        self.window.curve_lw_spin.setValue(2.8)
        self.window.fig_w_spin.setValue(8.5)
        self.window.xmin_spin.setValue(5)
        self.window.xmax_spin.setValue(45)
        self.window.norm_cb.setChecked(False)
        self.window.superimpose_cb.setChecked(True)
        self.window.save_plot_defaults_button.click()
        settings = QSettings(str(self.root / "prefs.ini"), QSettings.Format.IniFormat)
        other = xstack.PXRDMultiCompareApp(settings=settings)
        self.addCleanup(other.close)
        self.assertEqual(other.curve_lw_spin.value(), 2.8)
        self.assertEqual(other.fig_w_spin.value(), 8.5)
        self.assertEqual(other.xmin_spin.value(), 5)
        self.assertEqual(other.xmax_spin.value(), 45)
        self.assertFalse(other.norm_cb.isChecked())
        self.assertTrue(other.superimpose_cb.isChecked())
        path = self.root / "defaults.xy"
        path.write_text("2 1\n10 8\n20 3\n50 1\n")
        other.open_external_files([str(path)])
        self.assertEqual(other.plot_lines[0]["line"].get_linewidth(), 2.8)
        self.assertEqual(tuple(other.axes[0].get_xlim()), (5, 45))
        other.norm_cb.setChecked(True)
        self.assertAlmostEqual(max(other.plot_lines[0]["line"].get_ydata()), 1.0 + other.curve_offset_spin.value())

    def test_restore_factory_defaults_updates_plot_without_overwriting_saved_defaults(self):
        path = self.root / "reset.xy"
        path.write_text("2 1\n10 8\n20 3\n50 1\n")
        self.window.open_external_files([str(path)])
        original_x = self.window.patterns[0]["x"].copy()
        original_y = self.window.patterns[0]["y"].copy()
        self.window.curve_lw_spin.setValue(3.5)
        self.window.fig_w_spin.setValue(9)
        self.window.xmin_spin.setValue(6)
        self.window.xmax_spin.setValue(40)
        self.window.norm_cb.setChecked(False)
        self.window.live_norm_cb.setChecked(True)
        self.window.superimpose_cb.setChecked(True)
        self.window.color_mode_combo.setCurrentIndex(1)
        self.window.curve_labels_btn.setChecked(False)
        self.window.save_plot_defaults_button.click()
        saved = self.window.settings.value("plot/defaults")
        self.window.reset_plot_defaults_button.click()
        self.assertEqual(self.window._collect_plot_defaults(), self.window._factory_plot_defaults)
        self.assertEqual(self.window.settings.value("plot/defaults"), saved)
        self.assertEqual(len(self.window.patterns), 1)
        self.assertTrue((self.window.patterns[0]["x"] == original_x).all())
        self.assertTrue((self.window.patterns[0]["y"] == original_y).all())
        self.assertEqual(self.window.plot_lines[0]["line"].get_linewidth(), 1.5)
        self.assertEqual(self.window.plot_lines[0]["line"].get_color(), "#000000")
        self.assertEqual(tuple(self.window.axes[0].get_xlim()), (2, 30))
        self.assertEqual(tuple(self.window.fig.get_size_inches()), (7, 6))
        self.assertTrue(self.window.show_curve_labels)
        self.window.save_plot_defaults_button.click()
        other = xstack.PXRDMultiCompareApp(settings=self.window.settings)
        self.addCleanup(other.close)
        self.assertEqual(other._collect_plot_defaults(), self.window._factory_plot_defaults)

    def test_resizable_list_and_clear_button(self):
        self.window.show()
        self.app.processEvents()
        splitter = self.window.controls_splitter
        splitter.setSizes([150, 450])
        self.app.processEvents()
        before = self.window.files_list.height()
        splitter.setSizes([280, 320])
        self.app.processEvents()
        self.assertGreater(self.window.files_list.height(), before)
        self.assertEqual(self.window.clear_all_button.parent(), self.window.files_list.parent())
        texts = [b.text() for b in self.window.findChildren(QPushButton)]
        self.assertNotIn("Set display range", texts)
        self.assertNotIn("Update plot", texts)
        self.assertNotIn("Clear all curves", texts)
        path = self.root / "clear.xy"
        path.write_text("2 1\n10 8\n20 3\n30 1\n")
        self.window.open_external_files([str(path)])
        self.window.clear_all_button.click()
        self.assertEqual(self.window.patterns, [])
        self.assertEqual(self.window.loaded_count_label.text(), "0")

    def test_project_restores_segmented_mode(self):
        path = self.root / "sample.xy"
        path.write_text("2 1\n10 8\n20 3\n30 1\n", encoding="utf-8")
        self.window.open_external_files([str(path)])
        self.window.superimpose_cb.overlay_button.click()
        project = str(self.root / "overlay.pxrdproj")
        with patch.object(QFileDialog, "getSaveFileName", return_value=(project, "")), patch.object(QMessageBox, "information"):
            self.window.save_project()
        self.window.superimpose_cb.stacked_button.click()
        self.window.load_project_path(project, show_success=False)
        self.assertTrue(self.window.superimpose_cb.isChecked())
        self.assertTrue(self.window.superimpose_cb.overlay_button.isChecked())
        self.assertFalse(self.window.superimpose_cb.stacked_button.isChecked())


if __name__ == "__main__":
    unittest.main()

