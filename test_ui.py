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
        self.assertEqual(buttons, ["Add PXRD files", "Save project", "Open project", "Choose folder"])
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
        self.window._set_norm_mode("none")
        self.window.superimpose_cb.setChecked(True)
        self.window.save_plot_defaults_button.click()
        settings = QSettings(str(self.root / "prefs.ini"), QSettings.Format.IniFormat)
        other = xstack.PXRDMultiCompareApp(settings=settings)
        self.addCleanup(other.close)
        self.assertEqual(other.curve_lw_spin.value(), 2.8)
        self.assertEqual(other.fig_w_spin.value(), 8.5)
        self.assertEqual(other.xmin_spin.value(), 5)
        self.assertEqual(other.xmax_spin.value(), 45)
        self.assertEqual(other._norm_mode(), "none")
        self.assertTrue(other.superimpose_cb.isChecked())
        path = self.root / "defaults.xy"
        path.write_text("2 1\n10 8\n20 3\n50 1\n")
        other.open_external_files([str(path)])
        self.assertEqual(other.plot_lines[0]["line"].get_linewidth(), 2.8)
        self.assertEqual(tuple(other.axes[0].get_xlim()), (5, 45))
        other._set_norm_mode("full")
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
        self.window._set_norm_mode("none")
        self.window._set_norm_mode("visible")
        self.window.superimpose_cb.setChecked(True)
        self.window.color_mode_combo.setCurrentIndex(self.window.color_mode_combo.findData("colorful"))
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

    def test_legacy_normalization_settings_migrate(self):
        legacy = {"norm_cb": False, "live_norm_cb": True}
        self.window._apply_plot_defaults(legacy)
        self.assertEqual(self.window._norm_mode(), "visible")
        self.window._apply_plot_defaults({"norm_cb": False, "live_norm_cb": False})
        self.assertEqual(self.window._norm_mode(), "none")
        state = self.window._norm_mode_from_state
        self.assertEqual(state({"norm": True, "live_norm": False}), "full")
        self.assertEqual(state({"norm": False, "live_norm": True}), "visible")
        self.assertEqual(state({"norm_mode": "none", "norm": True}), "none")
        ui = self.window._collect_ui_state()
        self.assertEqual((ui["norm"], ui["live_norm"]), (False, False))

    def test_undo_snapshots_share_read_only_arrays(self):
        path = self.root / "shared.xy"
        path.write_text("2 1\n10 8\n20 3\n30 1\n", encoding="utf-8")
        self.window.open_external_files([str(path)])
        snapshot = self.window._capture_undo_snapshot()
        self.assertIs(snapshot["patterns"][0]["y"], self.window.patterns[0]["y"])
        self.assertFalse(self.window.patterns[0]["y"].flags.writeable)

    def test_undo_restores_canvas_size(self):
        self.window.fig_w_spin.setValue(7)
        path = self.root / "size.xy"
        path.write_text("2 1\n10 8\n20 3\n30 1\n", encoding="utf-8")
        self.window.open_external_files([str(path)])
        width = self.window.canvas.maximumWidth()
        self.window._push_undo_snapshot()
        self.window.fig_w_spin.setValue(10)
        self.assertGreater(self.window.canvas.maximumWidth(), width)
        self.window.undo_last_action()
        self.assertEqual(self.window.canvas.maximumWidth(), width)

    def test_browser_sorts_by_name_and_hides_empty_folders(self):
        for name in ("s10.xy", "s2.xy", "s1.xy"):
            (self.root / name).write_text("1 1\n2 2\n", encoding="utf-8")
        for folder in ("b10", "b2", "empty"):
            (self.root / folder).mkdir()
        for folder in ("b10", "b2"):
            (self.root / folder / "x.xy").write_text("1 1\n2 2\n", encoding="utf-8")
        self.window.sort_by_mtime_cb.setChecked(False)
        tree = self.window.browser_tree
        names = [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())]
        self.assertEqual(names, ["s1.xy", "s2.xy", "s10.xy", "b2", "b10"])

    def test_browser_search_runs_in_background(self):
        (self.root / "deep" / "deeper").mkdir(parents=True)
        (self.root / "deep" / "deeper" / "target_1.xy").write_text("1 1\n2 2\n", encoding="utf-8")
        (self.root / "other.xy").write_text("1 1\n2 2\n", encoding="utf-8")
        self.window.browser_search_edit.setText("target")
        self.window.populate_browser_tree()
        tree = self.window.browser_tree
        self.assertEqual(tree.topLevelItem(0).text(0), "Searching…")
        import time
        deadline = time.monotonic() + 5
        while self.window._browser_search_poll.isActive() and time.monotonic() < deadline:
            self.app.processEvents()
        names = [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())]
        self.assertEqual(names, ["deep/deeper/target_1.xy"])

    def test_axis_text_fits_small_canvas(self):
        path = self.root / "small.xy"
        path.write_text("2 1\n10 8\n20 3\n30 1\n", encoding="utf-8")
        self.window.open_external_files([str(path)])
        default_position = self.window.ax.get_position().bounds
        for width, height in ((3.3, 2.6), (2.0, 1.6), (5.3, 5.2)):
            with self.subTest(width=width, height=height):
                self.window.fig_w_spin.setValue(width)
                self.window.fig_h_spin.setValue(height)
                self.window.tick_fs_spin.setValue(12)
                self.window.axis_label_fs_spin.setValue(12)
                fig = self.window.fig
                fig.canvas.draw()
                renderer = fig.canvas.get_renderer()
                figure_box = fig.bbox
                artists = [self.window.x_label_artist, self.window.y_label_artist]
                artists += self.window.ax.get_xticklabels()
                for artist in artists:
                    if not artist.get_visible() or not artist.get_text():
                        continue
                    box = artist.get_window_extent(renderer)
                    self.assertGreaterEqual(box.y0, figure_box.y0 - 0.5, artist.get_text())
                    self.assertGreaterEqual(box.x0, figure_box.x0 - 0.5, artist.get_text())
                    self.assertLessEqual(box.x1, figure_box.x1 + 0.5, artist.get_text())
        self.window.fig_w_spin.setValue(7)
        self.window.fig_h_spin.setValue(6)
        self.window.tick_fs_spin.setValue(12)
        self.window.axis_label_fs_spin.setValue(12)
        # Large canvases keep the historical fractional layout.
        self.assertEqual(self.window.ax.get_position().bounds, default_position)

    def test_every_palette_is_selectable_and_valid(self):
        from matplotlib.colors import is_color_like
        from xstack_palettes import PALETTES, palette_colors
        combo = self.window.color_mode_combo
        keys = [combo.itemData(i) for i in range(combo.count()) if combo.itemData(i)]
        self.assertEqual(keys, ["black"] + list(PALETTES))
        for key in PALETTES:
            colors = palette_colors(key, 12)
            self.assertEqual(len(colors), 12)
            self.assertTrue(all(is_color_like(c) for c in colors), key)

    def test_gradient_follows_list_order(self):
        for name in ("g1", "g2", "g3"):
            (self.root / f"{name}.xy").write_text("2 1\n10 8\n20 3\n30 1\n", encoding="utf-8")
        self.window.color_mode_combo.setCurrentIndex(self.window.color_mode_combo.findData("viridis"))
        self.window.open_external_files([str(self.root / "g1.xy"), str(self.root / "g2.xy")])
        self.window.open_external_files([str(self.root / "g3.xy")])
        from xstack_palettes import palette_colors
        expected = palette_colors("viridis", 3)
        self.assertEqual([p["color"] for p in self.window.patterns], expected)
        self.window.files_list.insertItem(2, self.window.files_list.takeItem(0))
        self.window.on_files_order_changed()
        self.assertEqual([p["color"] for p in self.window.patterns], expected)
        self.assertEqual(self.window.plot_lines[0]["line"].get_color(), expected[0])
        self.window.undo_last_action()
        self.assertEqual([p["color"] for p in self.window.patterns], expected)

    def test_categorical_palette_and_legacy_key(self):
        path = self.root / "c.xy"
        path.write_text("2 1\n10 8\n20 3\n30 1\n", encoding="utf-8")
        self.window.open_external_files([str(path)])
        self.window.color_mode_combo.setCurrentIndex(self.window.color_mode_combo.findData("npg"))
        self.assertEqual(self.window.patterns[0]["color"], "#E64B35")
        self.window._apply_ui_state({"color_mode": "colorful"})
        self.assertEqual(self.window._current_color_mode(), "colorful")
        self.window._apply_ui_state({"default_black": False})
        self.assertEqual(self.window._current_color_mode(), "colorful")

    def test_color_dialog_shades_follow_selected_palette(self):
        from PyQt6.QtGui import QColor
        from PyQt6.QtWidgets import QColorDialog
        from xstack_palettes import PALETTES, shade_grid
        dialog = xstack.CurveColorDialog(QColor("#E64B35"), "npg", self.window)
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.palette_key(), "npg")
        self.assertEqual(dialog.currentColor().name(), "#e64b35")
        for key in ("npg", "tol_bright", "viridis"):
            dialog.palette_combo.setCurrentIndex(dialog.palette_combo.findData(key))
            grid = shade_grid(key)
            self.assertEqual([len(row) for row in grid], [8] * 6)
            for row, colors in enumerate(grid):
                for col, color in enumerate(colors):
                    self.assertEqual(QColorDialog.standardColor(col * 6 + row).name(), color)
        # The unshaded row is the palette itself, so its colors stay pickable exactly.
        self.assertEqual(shade_grid("npg")[2], [c.lower() for c in PALETTES["npg"].colors[:8]])
        self.assertEqual(shade_grid("tol_bright")[2][7], "#7f7f7f")

    def test_folder_field_elides_but_keeps_full_path(self):
        edit = self.window.browser_root_edit
        long_path = str(self.root / ("very_long_folder_name_" * 6))
        edit.resize(160, edit.height())
        edit.setPath(long_path)
        self.assertEqual(edit.path(), long_path)
        self.assertTrue(edit.text().startswith("…"))
        self.assertTrue(long_path.endswith(edit.text()[1:]))
        self.assertEqual(edit.toolTip(), long_path)


if __name__ == "__main__":
    unittest.main()

