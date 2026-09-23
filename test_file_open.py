"""Run with Python; uses isolated preferences and an offscreen Qt window."""
import importlib.util
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import time
import threading
import uuid
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication, QMessageBox, QFileDialog

spec = importlib.util.spec_from_file_location(
    "xstack", Path(__file__).with_name("xStack_v1.4.py")
)
xstack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(xstack)


class FileOpenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = QSettings(str(self.root / "prefs.ini"), QSettings.Format.IniFormat)
        self.settings.setValue("browser/root_dir", str(self.root))
        self.files = []
        for name in ("sample one.xy", "中文.xy"):
            path = self.root / name
            path.write_text("10 5\n11 8\n12 4\n", encoding="utf-8")
            self.files.append(str(path))

    def window(self, paths=None):
        window = xstack.PXRDMultiCompareApp(paths, settings=self.settings)
        self.addCleanup(window.close)
        return window

    def test_browser_remembers_explicit_folder(self):
        window = self.window()
        self.assertFalse(hasattr(window, "mode_combo"))
        folder = self.root / "selected"
        folder.mkdir()
        window.set_browser_root(str(folder))
        reopened = self.window()
        self.assertEqual(reopened.browser_root_dir, str(folder))
        folder.rmdir()
        unavailable = self.window()
        self.assertIn("Directory not found", unavailable.browser_tree.topLevelItem(0).text(0))

    def test_external_files_append_and_preserve_settings(self):
        window = self.window(self.files[:1])
        window.open_startup_files()
        first = window.patterns[0]
        window.norm_cb.setChecked(False)
        window.open_external_files(self.files)
        self.assertEqual(window.files_list.count(), 2)
        self.assertIs(window.patterns[0], first)
        self.assertFalse(window.norm_cb.isChecked())
        self.assertFalse(hasattr(window, "mode_combo"))
        self.assertEqual(self.settings.value("browser/root_dir"), str(self.root))

    def test_second_process_forwards_to_existing_window(self):
        service = xstack.FileOpenService("xstack-test-" + uuid.uuid4().hex)
        self.addCleanup(service.close)
        self.assertTrue(service.start_or_forward(self.files[:1]))
        code = """
import importlib.util, sys
from PyQt6.QtWidgets import QApplication
spec = importlib.util.spec_from_file_location('xstack', sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
app = QApplication([])
service = m.FileOpenService(sys.argv[2])
assert service.start_or_forward(sys.argv[3:]) is False
"""
        process = subprocess.Popen([sys.executable, "-c", code,
            str(Path(xstack.__file__).resolve()), service.name, self.files[1]],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 20
            while process.poll() is None and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            self.assertIsNotNone(process.poll(), "Forwarding timed out")
            output = process.communicate(timeout=2)
            self.assertEqual(process.returncode, 0, repr(output))
            window = self.window()
            service.attach_window(window)
            self.app.processEvents()
            deadline = time.monotonic() + 10
            while (window._import_active or window._import_pending) and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            self.assertEqual(window.files_list.count(), 2)
            service.close()
            replacement = xstack.FileOpenService(service.name)
            self.addCleanup(replacement.close)
            self.assertTrue(replacement.start_or_forward([]))
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_background_import_keeps_event_loop_responsive(self):
        window = self.window()
        started, release = threading.Event(), threading.Event()
        reader = xstack.load_pattern_file
        threads = []
        def delayed(path):
            threads.append(threading.get_ident())
            started.set()
            release.wait(3)
            return reader(path)
        with patch.object(xstack, "load_pattern_file", side_effect=delayed), patch.object(window, "update_plot", wraps=window.update_plot) as redraw:
            try:
                window.queue_patterns_from_paths(self.files)
                self.assertTrue(started.wait(1))
                from PyQt6.QtCore import QTimer
                ticks = []
                QTimer.singleShot(0, lambda: ticks.append(True))
                self.app.processEvents()
                self.assertEqual(ticks, [True])
                self.assertTrue(window._import_active)
                release.set()
                deadline = time.monotonic() + 10
                while window._import_active and time.monotonic() < deadline:
                    self.app.processEvents()
                    time.sleep(0.01)
                self.assertFalse(window._import_active)
                self.assertEqual(window.files_list.count(), 2)
                self.assertTrue(all(t != threading.get_ident() for t in threads))
                redraw.assert_called_once()
            finally:
                release.set()

    def test_invalid_files_do_not_block_valid_data(self):
        invalid = self.root / "broken.xy"
        invalid.write_text("invalid data", encoding="utf-8")
        window = self.window(self.files + [str(invalid), str(self.root / "missing.xy"), str(self.root / "prefs.ini")])
        self.settings.sync()
        with patch.object(QMessageBox, "warning") as warning, patch.object(QMessageBox, "critical") as critical:
            window.open_startup_files()
        self.assertEqual(window.files_list.count(), 2)
        warning.assert_called_once()
        critical.assert_called_once()

    def test_external_project_then_additional_data(self):
        window = self.window(self.files[:1])
        window.open_startup_files()
        project = str(self.root / "saved project.pxrdproj")
        with patch.object(QFileDialog, "getSaveFileName", return_value=(project, "")), patch.object(QMessageBox, "information"):
            window.save_project()
        self.assertTrue(Path(project).is_file())
        reopened = self.window([project, self.files[1]])
        with patch.object(QMessageBox, "information") as info, patch.object(QMessageBox, "critical") as error:
            reopened.open_startup_files()
        info.assert_not_called()
        error.assert_not_called()
        self.assertFalse(hasattr(reopened, "mode_combo"))
        self.assertEqual(reopened.files_list.count(), 2)


if __name__ == "__main__":
    unittest.main()
