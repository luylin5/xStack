"""Label markup: conversion, the formatting dialog, and use in plots and exports."""
import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_file_open import xstack
import xstack_richtext as rt
import xstack_ui
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication, QFileDialog, QMessageBox


class MarkupTests(unittest.TestCase):
    def test_conversions(self):
        text = "Cu<sub>3</sub>(BTC)<sub>2</sub> 2<i>θ</i>"
        self.assertEqual(rt.to_plain(text), "Cu3(BTC)2 2θ")
        self.assertEqual(rt.to_html("a<b>&</b>"), "a<b>&amp;</b>")
        self.assertEqual(rt.to_mathtext("plain_name"), "plain_name")
        math = rt.to_mathtext(text)
        self.assertIn(r"$_{\mathrm{3}}$", math)
        self.assertIn(r"$\mathsf{θ}$", math)

    def test_adjacent_scripts_share_one_block_side_by_side(self):
        self.assertEqual(rt.to_mathtext("SO<sub>4</sub><sup>2-</sup>"),
                         r"SO$_{\mathrm{4}}{}^{\mathrm{2{-}}}$")

    def test_scripts_never_stack(self):
        self.assertEqual(rt.to_mathtext("202<sup>6</sup><sub>0</sub>6"),
                         r"202$^{\mathrm{6}}{}_{\mathrm{0}}$6")
        self.assertEqual(rt.to_mathtext("x<sup>2</sup><sup><b>+</b></sup>"),
                         r"x$^{\mathrm{2}}{}^{\mathbf{{+}}}$")
        # Identical neighbours merge into one run.
        self.assertEqual(rt.to_mathtext("a<sub>1</sub><sub>2</sub>"), r"a$_{\mathrm{12}}$")
        self.assertEqual(rt.to_mathtext("x<sub>i</sub><sup>2</sup><sub>j</sub>"),
                         r"x$_{\mathrm{i}}{}^{\mathrm{2}}{}_{\mathrm{j}}$")
        # A formatted non-script run in between needs no separator.
        self.assertEqual(rt.to_mathtext("<sup>1</sup><b>B</b><sub>2</sub>"),
                         r"$^{\mathrm{1}}\mathbf{B}_{\mathrm{2}}$")

    def test_script_offsets_do_not_depend_on_neighbours(self):
        """H<sub>2</sub> and SO<sub>4</sub><sup>+</sup>: every subscript at one depth, every superscript at one height."""
        import matplotlib
        from matplotlib.font_manager import FontProperties
        from matplotlib.mathtext import MathTextParser
        rt.configure_math_fonts(matplotlib.rcParams)
        parser = MathTextParser("path")
        prop = FontProperties(family="Arial", size=100)

        def extent(label):
            blocks = rt.to_mathtext(label).split("$")[1::2]
            self.assertEqual(len(blocks), 1, label)
            width, height, depth, _glyphs, _rects = parser.parse("$" + blocks[0] + "$", dpi=72, prop=prop)
            return height - depth, depth

        sub_depth = extent("<sub>4</sub>")[1]
        sup_height = extent("<sup>+</sup>")[0]
        for label in ("<sub>4</sub><sup>+</sup>", "<sup>+</sup><sub>4</sub>",
                      "<sub><b>4</b></sub><sup>+</sup>", "<sup>+</sup><sub>4</sub><sup>+</sup>"):
            height, depth = extent(label)
            self.assertAlmostEqual(depth, sub_depth, places=3, msg=label)
            self.assertAlmostEqual(height, sup_height, places=3, msg=label)

    def test_every_case_renders_as_math(self):
        from matplotlib.mathtext import MathTextParser
        parser = MathTextParser("path")
        for case in ("h_202<sup>6</sup><sub>0</sub>6<b>04T18</b>", "x<sup>2</sup><sup><b>+</b></sup>",
                     "a<sub>1</sub><sub>2</sub>", "SO<sub>4</sub><sup>2-</sup>"):
            text = rt.to_mathtext(case)
            self.assertIn("$", text, case)  # did not fall back to plain text
            for block in text.split("$")[1::2]:
                parser.parse("$" + block + "$")

    def test_script_metrics_calibrated(self):
        self.assertIsNone(rt.script_metrics_error)

    def test_scripts_use_word_processor_offsets(self):
        import matplotlib
        from matplotlib.font_manager import FontProperties
        from matplotlib.mathtext import MathTextParser
        rt.configure_math_fonts(matplotlib.rcParams)
        prop = FontProperties(family="Arial", size=100)
        _w, _h, _d, glyphs, _r = MathTextParser("path").parse(
            rt.to_mathtext("H<sub>2</sub>SO<sub>4</sub><sup>+</sup>"), dpi=72, prop=prop)
        y = {chr(g[2]): g[4] for g in glyphs}
        x = {chr(g[2]): g[3] for g in glyphs}
        self.assertAlmostEqual((y["H"] - y["2"]) / 100, rt.SUBSCRIPT_DROP, places=2)
        self.assertAlmostEqual(y["2"], y["4"], places=3)
        self.assertAlmostEqual((y["+"] - y["H"]) / 100, rt.SUPERSCRIPT_RAISE, places=2)
        # No extra gap: "+" starts where the subscript 4 ends.
        script_size = [g[1] for g in glyphs if chr(g[2]) == "4"][0]
        digit_advance = 0.556 * script_size  # Arial digits are 1139/2048 em wide
        self.assertAlmostEqual(x["+"], x["4"] + digit_advance, delta=0.5)

    def test_hyphen_and_specials_inside_formatting(self):
        self.assertEqual(rt.to_mathtext("<b>MOF-5</b>"), "$\\mathbf{MOF­5}$")
        self.assertIn(r"x\_y\ \{z\}", rt.to_mathtext("<i>x_y {z}</i>"))
        self.assertEqual(rt.to_mathtext("<b>$5</b> $"), r"$\mathbf{\$5}$ \$")

    def test_unbalanced_tags_stay_literal(self):
        self.assertEqual(rt.to_plain("bad <i>x"), "bad <i>x")
        self.assertEqual(rt.to_html("a </b>"), "a &lt;/b&gt;")
        self.assertEqual(rt.remove_tags("<b>a<i>b</b>c</i>"), "abc")


class DialogAndPlotTests(unittest.TestCase):
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

    def dialog(self, text):
        dialog = xstack.FormattedTextDialog("Edit label", "Label text:", text, self.window)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_buttons_wrap_toggle_combine_and_clear(self):
        d = self.dialog("SO42-")
        d.edit.setSelection(2, 1)
        d.format_buttons["sub"].click()
        self.assertEqual(d.text(), "SO<sub>4</sub>2-")
        self.assertEqual(d.edit.selectedText(), "4")
        d.format_buttons["b"].click()
        self.assertEqual(d.text(), "SO<sub><b>4</b></sub>2-")
        d.format_buttons["b"].click()
        self.assertEqual(d.text(), "SO<sub>4</sub>2-")
        d.edit.setSelection(len("SO<sub>4</sub>"), 2)
        d.format_buttons["sup"].click()
        self.assertEqual(d.text(), "SO<sub>4</sub><sup>2-</sup>")
        preview = d.preview.pixmap()
        self.assertFalse(preview.isNull())
        # The preview is drawn by matplotlib from the same mathtext as the plot.
        with patch.object(xstack_ui, "to_mathtext", wraps=xstack_ui.to_mathtext) as convert:
            d._update_preview()
        convert.assert_called_once_with(d.text())
        d.edit.deselect()
        d.edit.setCursorPosition(0)
        d.format_buttons["i"].click()
        self.assertTrue(d.text().startswith("<i></i>"))
        self.assertEqual(d.edit.cursorPosition(), 3)
        d.edit.deselect()
        d.clear_format()
        self.assertEqual(d.text(), "SO42-")

    def test_formatted_label_reaches_plot_list_and_csv(self):
        path = self.root / "a.xy"
        path.write_text("2 1\n10 8\n20 3\n30 1\n", encoding="utf-8")
        self.window.open_external_files([str(path)])
        self.window.update_plot()
        self.window.canvas.draw()
        entry = self.window.label_texts[0]
        bbox = entry["artist"].get_window_extent(self.window.canvas.get_renderer())
        event = type("Event", (), {})()
        with patch.object(entry["artist"], "contains", return_value=(True, {})), \
                patch.object(xstack.FormattedTextDialog, "get_text", return_value=("Fe<sup>3+</sup>", True)):
            self.assertTrue(self.window.handle_label_double_click(event))
        self.assertEqual(self.window.patterns[0]["label"], "Fe<sup>3+</sup>")
        self.window.canvas.draw()  # mathtext must render without errors
        texts = [e["artist"].get_text() for e in self.window.label_texts]
        self.assertIn(r"Fe$^{\mathrm{3{+}}}$", texts)
        self.assertNotIn("<sup>", self.window.files_list.item(0).toolTip())
        self.assertGreater(bbox.width, 0)
        out = self.root / "out.csv"
        with patch.object(QFileDialog, "getSaveFileName", return_value=(str(out), "")), \
                patch.object(QMessageBox, "information"):
            self.window.export_csv()
        with open(out, newline="", encoding="utf-8-sig") as handle:
            header = " ".join(next(csv.reader(handle)))
        self.assertIn("Fe3+", header)
        self.assertNotIn("<sup>", header)


if __name__ == "__main__":
    unittest.main()
