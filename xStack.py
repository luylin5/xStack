import sys
import queue
import threading
import hashlib
import logging
import os
import json
import time
import zipfile
import io
import re
import html
import tempfile
import numpy as np
import csv

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QLineEdit, QSpinBox, QFileDialog, QMessageBox,
    QListWidget, QListWidgetItem, QAbstractItemView, QTreeWidget, QTreeWidgetItem,
    QStyledItemDelegate, QStyleOptionViewItem, QStyle, QSplashScreen,
)
from PyQt6.QtCore import (
    Qt, pyqtSignal, QByteArray, QMimeData, QUrl, QEvent, QSize, QRectF, QTimer, QSettings, QObject,
    QLockFile, QStandardPaths,
)
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtGui import QDrag, QTextDocument, QPalette, QPixmap, QImage, QDesktopServices, QIcon, QColor

import matplotlib
matplotlib.use("QtAgg")
matplotlib.rcParams["font.family"] = "Arial"
from matplotlib.patches import Rectangle

from xstack_ui import CurveColorDialog, FormattedTextDialog, MainWindowUiMixin, pattern_icon
import xstack_richtext
from xstack_richtext import configure_math_fonts, to_html, to_mathtext, to_plain
from xstack_tools import PxrdToolsMixin
from xstack_palettes import PALETTES, is_gradient, palette_colors
from xstack_io import (  # noqa: F401 - loaders are re-exported for tests and scripts
    PXRD_FILE_DIALOG_FILTER, SUPPORTED_PXRD_EXTS, load_pattern_file, load_two_column_file,
    read_pattern_batch, readonly_array, search_pattern_files, _read_patterns_to_queue,
)

configure_math_fonts(matplotlib.rcParams)
logger = logging.getLogger("xstack")

NORM_MODES = ("none", "full", "visible")
BROWSER_SEARCH_LIMIT = 2000


def resolve_app_icon_path():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = ["xStack.ico", "xStack.png"]
    for name in candidates:
        path = os.path.join(base_dir, name)
        if os.path.isfile(path):
            return path
    return ""


# ---------------- Reorderable List Widget ---------------- #

class ReorderListWidget(QListWidget):
    orderChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)

    def dropEvent(self, event):
        super().dropEvent(event)
        self.orderChanged.emit()


class PxrdFileBrowserTree(QTreeWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setColumnCount(1)
        self.setHeaderLabel("Files")
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAlternatingRowColors(True)
        self.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

    def startDrag(self, supported_actions):
        selected_items = self.selectedItems()
        file_paths = []
        for item in selected_items:
            p = item.data(0, Qt.ItemDataRole.UserRole)
            if p and os.path.isfile(p):
                file_paths.append(p)

        if not file_paths:
            return

        # Keep stable ordering for multi-file drag to avoid nondeterministic order after drop.
        file_paths = sorted(file_paths, key=lambda p: p.lower())
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(path) for path in file_paths])
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)


class ChemicalTextDelegate(QStyledItemDelegate):
    def __init__(self, html_formatter=None, parent=None):
        super().__init__(parent)
        self._html_formatter = html_formatter or (lambda text: html.escape("" if text is None else str(text)))

    def _document(self, text, option):
        role = QPalette.ColorRole.HighlightedText if option.state & QStyle.StateFlag.State_Selected else QPalette.ColorRole.Text
        color = option.palette.color(role).name()
        doc = QTextDocument()
        doc.setDefaultFont(option.font)
        doc.setDocumentMargin(0)
        doc.setHtml(f'<span style="color: {color};">{self._html_formatter(text)}</span>')
        return doc

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        widget = opt.widget
        style = widget.style() if widget else QApplication.style()
        text = opt.text
        opt.text = ""
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)
        text_rect = style.subElementRect(QStyle.SubElement.SE_ItemViewItemText, opt, widget)
        doc = self._document(text, opt)
        doc.setTextWidth(-1)
        text_h = doc.size().height()
        y_offset = max(0.0, (float(text_rect.height()) - text_h) / 2.0)
        painter.save()
        painter.translate(text_rect.left(), text_rect.top() + y_offset)
        painter.setClipRect(QRectF(0, 0, text_rect.width(), text_rect.height()))
        doc.drawContents(painter, QRectF(0, 0, text_rect.width(), text_rect.height()))
        painter.restore()

    def sizeHint(self, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        doc = self._document(opt.text, opt)
        doc.setTextWidth(-1)
        return QSize(
            max(int(doc.idealWidth()) + 6, opt.fontMetrics.horizontalAdvance(opt.text) + 6),
            opt.fontMetrics.height() + 6,
        )


# ---------------- Main UI ---------------- #

class ClickableSplashScreen(QSplashScreen):
    """Splash artwork (version and author are part of the image); a click opens the URL."""
    def __init__(self, pixmap, url):
        super().__init__(pixmap, Qt.WindowType.WindowStaysOnTopHint)
        self._url = QUrl(url)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(url)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._url.isValid():
            QDesktopServices.openUrl(self._url)
        event.accept()


class FileOpenService(QObject):
    """One window per user; subsequent launches queue files in that window."""

    def __init__(self, name=None):
        super().__init__()
        identity = hashlib.sha256(os.path.expanduser("~").encode("utf-8")).hexdigest()[:20]
        self.name = name or ("xStack-files-" + identity)
        self.lock = QLockFile(os.path.join(tempfile.gettempdir(), self.name + ".lock"))
        self.lock.setStaleLockTime(0)
        self.server = QLocalServer(self)
        self.server.newConnection.connect(self._accept)
        self.window = None
        self.pending = []
        self.dispatching = False
        self.clients = {}
        self.owns_lock = False

    def start_or_forward(self, paths):
        if self.lock.tryLock(0):
            self.owns_lock = True
            QLocalServer.removeServer(self.name)
            if not self.server.listen(self.name):
                self.close()
                raise RuntimeError("Cannot start file-open service: " + self.server.errorString())
            self.pending.append(paths)
            return True
        socket = QLocalSocket(self)
        for _ in range(5):
            socket.connectToServer(self.name)
            if socket.waitForConnected(1000):
                break
            socket.abort()
            # Another process may still be starting its local server.
            time.sleep(0.1)
        else:
            raise RuntimeError("The existing xStack window is not responding. Close it and try again.")
        payload = json.dumps(paths, ensure_ascii=False).encode("utf-8") + b"\n"
        socket.write(payload)
        if socket.bytesToWrite() and not socket.waitForBytesWritten(5000):
            raise RuntimeError("Could not send files to the existing xStack window.")
        if not socket.bytesAvailable() and not socket.waitForReadyRead(10000):
            raise RuntimeError("xStack did not confirm receipt. Check the existing window before trying again.")
        if bytes(socket.readAll()) != b"OK\n":
            raise RuntimeError("xStack rejected the file-open request.")
        socket.disconnectFromServer()
        return False

    def _accept(self):
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            self.clients[socket] = bytearray()
            socket.readyRead.connect(lambda sock=socket: self._receive(sock))
            socket.disconnected.connect(lambda sock=socket: self._discard(sock))
            if socket.bytesAvailable():
                self._receive(socket)

    def _discard(self, socket):
        self.clients.pop(socket, None)
        socket.deleteLater()

    def _receive(self, socket):
        buffer = self.clients.get(socket)
        if buffer is None:
            return
        buffer.extend(bytes(socket.readAll()))
        if len(buffer) > 1024 * 1024:
            socket.disconnectFromServer()
            return
        if b"\n" not in buffer:
            return
        try:
            paths = json.loads(bytes(buffer).decode("utf-8"))
            if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
                raise ValueError("Invalid file list")
        except (ValueError, UnicodeError):
            socket.disconnectFromServer()
            return
        self.pending.append(paths)
        self.clients.pop(socket, None)
        socket.write(b"OK\n")
        socket.flush()
        socket.disconnectFromServer()
        QTimer.singleShot(0, self._dispatch)

    def attach_window(self, window):
        self.window = window
        QTimer.singleShot(0, self._dispatch)

    def _dispatch(self):
        if self.window is None or self.dispatching:
            return
        self.dispatching = True
        try:
            while self.pending:
                paths = self.pending.pop(0)
                if paths:
                    self.window.open_external_files(paths, background=True)
                if self.window.isMinimized():
                    self.window.showNormal()
                self.window.raise_()
                self.window.activateWindow()
        finally:
            self.dispatching = False

    def close(self):
        if self.owns_lock:
            self.server.close()
            self.lock.unlock()
            self.owns_lock = False


class PXRDMultiCompareApp(MainWindowUiMixin, PxrdToolsMixin, QMainWindow):
    browser_tree_type = PxrdFileBrowserTree
    file_list_type = ReorderListWidget
    text_delegate_type = ChemicalTextDelegate

    # Fixed layout constants (no longer user-adjustable).
    fixed_y_headroom = 1.2
    fixed_label_y_spacing = 1.0
    fixed_label_x_position = 0.04
    fixed_label_y_position = 0.90
    title_font_size = 10

    def __init__(self, settings=None):
        super().__init__()
        self.settings = settings if settings is not None else QSettings("xStack", "xStack")
        self.setWindowTitle("xStack")
        icon_path = resolve_app_icon_path()
        if icon_path:
            self.setWindowIcon(QIcon(icon_path))
        self.resize(1200, 850)
        self.setMinimumSize(980, 640)
        self.setAcceptDrops(True)

        # Open/save dialogs start in the last folder used, falling back to Documents.
        home_dir = os.path.expanduser("~")
        documents_dir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
        default_project_dir = documents_dir if os.path.isdir(documents_dir) else home_dir
        self.project_dir = str(self.settings.value("project/last_dir", default_project_dir))
        if not os.path.isdir(self.project_dir):
            self.project_dir = default_project_dir
        self.browser_root_dir = str(self.settings.value("browser/root_dir", home_dir))
        self.supported_exts = set(SUPPORTED_PXRD_EXTS)

        # Data and state
        self.patterns = []
        self.axes = []
        self.plot_lines = []
        self.label_texts = []
        self.cursor_vlines = []
        self.cursor_text = None
        self.ax = None
        self.title_artist = None
        self.x_label_artist = None
        self.y_label_artist = None
        self.zoom_start_vlines = []
        self.zoom_end_vlines = []
        self.analysis_artists = []
        self.applied_peak_overlay = None
        self.preview_ax = None
        self.press_event = None
        self.pan_event = None

        # Label group drag (global offset in data coordinates)
        self.dragging_label = False
        self.drag_start_data = None
        self.drag_label_axis = None
        self.dragging_curve_idx = None
        self.drag_curve_axis = None
        self.drag_curve_start_ydata = None
        self.drag_curve_start_shift = None
        self.drag_curve_moved = False
        self.global_label_offset = (0.0, 0.0)

        self.xlim_full = self.ylim_full = None
        self.base_fig_dpi = 100.0
        self.canvas_view_scale = 1.3
        self.canvas_view_scale_min = 0.25
        self.canvas_view_scale_max = 4.0

        # Axis label/title text
        self.title_text = ""
        self.x_axis_label_text = "2θ (deg)"
        self.y_axis_label_text = "Intensity (a.u.)"
        self.show_curve_labels = True

        # Unique id for each pattern
        self._uid_counter = 0

        # Selected curve
        self.selected_idx = None

        # Undo stack
        self.undo_stack = []
        self.undo_limit = 30
        self._restoring_undo = False

        # Widgets
        self.color_mode_combo = None
        self.svg_bg_combo = None
        self.sort_by_mtime_cb = None
        self.superimpose_cb = None
        self.curve_lw_spin = None     # Curve line width
        self.frame_lw_spin = None     # Frame line width (including ticks)
        self.curve_offset_spin = None # Global vertical offset for all curves
        self.curve_label_fs_spin = None
        self.tick_fs_spin = None
        self.axis_label_fs_spin = None
        self.xmin_spin = None
        self.xmax_spin = None
        self.curve_labels_btn = None
        self.browser_root_edit = None
        self.browser_search_edit = None
        self.powerxrd_tools_dialog = None
        self._powerxrd_live_updating = False
        self._powerxrd_preview_data = None
        self.preview_ax = None
        self._clipboard_temp_png = None
        self.scale_overlay = None
        self.canvas_scroll = None
        self._screen_change_connected = False

        self._import_pending = []
        self._import_active = False
        self._import_results = queue.Queue()
        self._import_timer = QTimer(self)
        self._import_timer.setInterval(40)
        self._import_timer.timeout.connect(self._poll_import)
        self._browser_search_timer = QTimer(self)
        self._browser_search_timer.setSingleShot(True)
        self._browser_search_timer.setInterval(300)
        self._browser_search_timer.timeout.connect(self.populate_browser_tree)
        self._browser_search_cancel = None
        self._browser_search_text = ""
        self._browser_search_results = queue.Queue()
        self._browser_search_poll = QTimer(self)
        self._browser_search_poll.setInterval(50)
        self._browser_search_poll.timeout.connect(self._poll_browser_search)
        self._cursor_draw_timer = QTimer(self)
        self._cursor_draw_timer.setSingleShot(True)
        self._cursor_draw_timer.setInterval(33)
        self._cursor_draw_timer.timeout.connect(self.canvas_draw_idle)
        self._init_ui()
        self._factory_plot_defaults = self._collect_plot_defaults()
        self.restore_plot_defaults()

    # ---------------- UI ---------------- #

    _plot_default_numbers = (
        "curve_label_fs_spin", "curve_lw_spin", "tick_fs_spin", "frame_lw_spin",
        "axis_label_fs_spin", "curve_offset_spin", "fig_w_spin", "fig_h_spin",
        "xmin_spin", "xmax_spin",
    )
    _plot_default_checks = ("superimpose_cb", "curve_labels_btn")

    def _collect_plot_defaults(self):
        values = {name: getattr(self, name).value() for name in self._plot_default_numbers}
        values.update({name: getattr(self, name).isChecked() for name in self._plot_default_checks})
        values["color_mode"] = self.color_mode_combo.currentData()
        values["norm_mode"] = self._norm_mode()
        return values

    def reset_plot_defaults(self):
        """Restore built-in parameters; persistence remains an explicit Save action."""
        if self.patterns:
            self._push_undo_snapshot()
        self._apply_plot_defaults(self._factory_plot_defaults, reset_colors=True)
        self.statusBar().showMessage("Factory plot parameters restored. Use Save as default to keep them for future sessions.", 7000)

    def save_plot_defaults(self):
        if self.xmin_spin.value() >= self.xmax_spin.value():
            QMessageBox.warning(self, "Plot defaults", "Display range requires X min < X max.")
            return
        values = self._collect_plot_defaults()
        self.settings.setValue("plot/defaults", json.dumps(values))
        self.settings.sync()
        if self.settings.status() != QSettings.Status.NoError:
            QMessageBox.warning(self, "Plot defaults", "Could not save plotting defaults.")
            return
        self.statusBar().showMessage("Plot defaults saved for future sessions", 5000)

    def restore_plot_defaults(self):
        try:
            values = json.loads(str(self.settings.value("plot/defaults", "{}")))
        except (ValueError, TypeError):
            return
        self._apply_plot_defaults(values)

    def _apply_plot_defaults(self, values, reset_colors=False):
        if not isinstance(values, dict):
            return
        for name in self._plot_default_numbers + self._plot_default_checks:
            if name not in values:
                continue
            control = getattr(self, name)
            previous = control.blockSignals(True)
            try:
                value = values[name]
                if name in self._plot_default_checks:
                    if isinstance(value, bool):
                        control.setChecked(value)
                elif isinstance(value, (int, float)) and np.isfinite(value):
                    if isinstance(control, QSpinBox):
                        value = int(value)
                    control.setValue(value)
            finally:
                control.blockSignals(previous)
        if self.xmin_spin.value() >= self.xmax_spin.value():
            for control, value in ((self.xmin_spin, 2.0), (self.xmax_spin, 30.0)):
                previous = control.blockSignals(True)
                control.setValue(value)
                control.blockSignals(previous)
        index = self.color_mode_combo.findData(values.get("color_mode", "black"))
        self._set_silently(self.color_mode_combo, self.color_mode_combo.setCurrentIndex, max(0, index))
        # Defaults saved by 1.5 and earlier use the old checkbox names.
        legacy = {"norm": values.get("norm_cb", True), "live_norm": values.get("live_norm_cb", False)}
        mode = self._norm_mode_from_state({"norm_mode": values.get("norm_mode"), **legacy})
        self._set_silently(self.norm_mode_combo, self._set_norm_mode, mode)
        self.show_curve_labels = self.curve_labels_btn.isChecked()
        if reset_colors:
            self._recolor_patterns()
        self._on_fig_size_changed(0)

    def _get_system_display_scale(self):
        screen = None
        try:
            win = self.windowHandle()
            if win is not None:
                screen = win.screen()
        except Exception:
            logger.debug("Suppressed error", exc_info=True)
            screen = None
        if screen is None:
            try:
                screen = QApplication.primaryScreen()
            except Exception:
                logger.debug("Suppressed error", exc_info=True)
                screen = None
        if screen is None:
            return 1.0

        vals = []
        try:
            dpr = float(screen.devicePixelRatio())
            if np.isfinite(dpr) and dpr > 0:
                vals.append(dpr)
        except Exception:
            logger.debug("Suppressed error", exc_info=True)
        try:
            logical_dpi = float(screen.logicalDotsPerInch())
            dpi_scale = logical_dpi / 96.0
            if np.isfinite(dpi_scale) and dpi_scale > 0:
                vals.append(dpi_scale)
        except Exception:
            logger.debug("Suppressed error", exc_info=True)
        if not vals:
            return 1.0
        return float(max(vals))

    def _apply_canvas_size_lock(self):
        if not hasattr(self, "canvas") or self.canvas is None:
            return
        if not hasattr(self, "fig") or self.fig is None:
            return
        if self.fig_w_spin is None or self.fig_h_spin is None:
            return
        scale = float(getattr(self, "canvas_view_scale", 1.0))
        if not np.isfinite(scale):
            scale = 1.0
        scale = min(
            float(getattr(self, "canvas_view_scale_max", 4.0)),
            max(float(getattr(self, "canvas_view_scale_min", 0.25)), scale),
        )
        self.canvas_view_scale = scale

        base_dpi = float(getattr(self, "base_fig_dpi", 100.0))
        view_dpi = base_dpi * scale
        system_scale = float(self._get_system_display_scale())
        if not np.isfinite(system_scale) or system_scale <= 0:
            system_scale = 1.0
        self.fig.set_dpi(view_dpi)

        # Compensate OS display scaling so canvas visual size stays consistent.
        logical_dpi = view_dpi / system_scale
        w_px = max(1, int(round(float(self.fig_w_spin.value()) * logical_dpi)))
        h_px = max(1, int(round(float(self.fig_h_spin.value()) * logical_dpi)))
        self.canvas.setMinimumSize(w_px, h_px)
        self.canvas.setMaximumSize(w_px, h_px)
        self._update_scale_overlay()

    def _set_canvas_view_scale(self, new_scale):
        if not np.isfinite(new_scale):
            return
        scale_min = float(getattr(self, "canvas_view_scale_min", 0.25))
        scale_max = float(getattr(self, "canvas_view_scale_max", 4.0))
        scale = min(scale_max, max(scale_min, float(new_scale)))
        if abs(scale - float(getattr(self, "canvas_view_scale", 1.0))) < 1e-12:
            self._update_scale_overlay()
            return
        self.canvas_view_scale = scale
        self._apply_canvas_size_lock()
        if self.fig_w_spin is not None and self.fig_h_spin is not None:
            self.fig.set_size_inches(self.fig_w_spin.value(), self.fig_h_spin.value(), forward=True)
        if self.plot_lines:
            self._apply_horizontal_clip_only()
        if self.canvas:
            self.canvas.draw_idle()

    def _reposition_scale_overlay(self):
        if self.scale_overlay is None:
            return
        if self.canvas_scroll is None:
            return
        viewport = self.canvas_scroll.viewport()
        if viewport is None:
            return
        margin = 10
        x = max(margin, viewport.width() - self.scale_overlay.width() - margin)
        self.scale_overlay.move(x, margin)
        self.scale_overlay.raise_()

    def _update_scale_overlay(self):
        if self.scale_overlay is None:
            return
        scale = float(getattr(self, "canvas_view_scale", 1.0))
        if not np.isfinite(scale):
            scale = 1.0
        self.scale_overlay.setText(f"Zoom: {scale:.0%}")
        self.scale_overlay.adjustSize()
        self._reposition_scale_overlay()

    def _on_screen_changed(self, _screen):
        self._apply_canvas_size_lock()
        if self.canvas:
            self.canvas.draw_idle()

    def showEvent(self, event):
        super().showEvent(event)
        if not bool(getattr(self, "_screen_change_connected", False)):
            win = self.windowHandle()
            if win is not None:
                try:
                    win.screenChanged.connect(self._on_screen_changed)
                    self._screen_change_connected = True
                except Exception:
                    logger.debug("Suppressed error", exc_info=True)
        self._apply_canvas_size_lock()

    def _on_fig_size_changed(self, _value):
        self._apply_canvas_size_lock()
        self.update_plot()

    def open_external_files(self, paths, background=False):
        projects, patterns, errors = [], [], []
        for path in paths:
            ext = os.path.splitext(path)[1].lower()
            if not background and not os.path.isfile(path):
                errors.append(f"File not found: {path}")
            elif ext == ".pxrdproj":
                projects.append(path)
            elif ext in self.supported_exts:
                patterns.append(path)
            else:
                errors.append(f"Unsupported file type: {path}")
        if len(projects) == 1:
            if self.patterns:
                errors.append("A project cannot replace the current curves through an external open. Use Open project to load it explicitly.")
            else:
                self.load_project_path(projects[0], show_success=False)
        elif projects:
            errors.append("Please open each PXRD project in a separate window.")
        if patterns:
            (self.queue_patterns_from_paths if background else self.add_patterns_from_paths)(patterns)
        if errors:
            QMessageBox.warning(self, "Open files", "\n".join(errors))

    # ---------------- Frame Line Width ---------------- #

    def _plot_axes(self):
        axes = [ax for ax in getattr(self, "axes", []) if ax is not None]
        if axes:
            return axes
        ax = getattr(self, "ax", None)
        return [ax] if ax is not None else []

    def _primary_ax(self):
        axes = self._plot_axes()
        return axes[0] if axes else None

    def _is_plot_axis(self, ax):
        return ax is not None and ax in self._plot_axes()

    def _is_superimpose_mode(self):
        return bool(self.superimpose_cb and self.superimpose_cb.isChecked())

    def _set_shared_xlim(self, x0, x1):
        for ax in self._plot_axes():
            ax.set_xlim(x0, x1, emit=False)
        if getattr(self, "preview_ax", None) is not None:
            try:
                self.preview_ax.set_xlim(x0, x1)
            except Exception:
                logger.debug("Suppressed error", exc_info=True)
        self._apply_live_normalization()
        self._apply_horizontal_clip_only()

    def _normalization_divisor(self, x, y, xlim=None):
        values = np.asarray(y, dtype=float)
        finite = np.isfinite(values)
        mode = self._norm_mode()
        if mode == "visible" and xlim is not None:
            lo, hi = sorted(xlim)
            visible = finite & (np.asarray(x) >= lo) & (np.asarray(x) <= hi)
            # Empty windows use the full curve; zero/negative maxima are not divided.
            if np.any(visible):
                finite = visible
        elif mode == "none":
            return 1.0
        maximum = float(np.max(values[finite])) if np.any(finite) else 0.0
        return maximum if maximum > 0 else 1.0

    def on_norm_mode_changed(self, _index):
        """Renormalize while keeping the visible X range."""
        ax = self._primary_ax()
        xlim = ax.get_xlim() if ax is not None else None
        self.update_plot(preserve_view=False)
        if xlim is not None and self.patterns:
            self._set_shared_xlim(*xlim)
            self.reposition_labels()
            self.canvas.draw_idle()

    def _apply_live_normalization(self):
        if self._norm_mode() != "visible" or not self.plot_lines:
            return
        xlim = self._primary_ax().get_xlim()
        lo, hi = sorted(xlim)
        bounds = []
        for pattern, info in zip(self.patterns, self.plot_lines):
            x, y = np.asarray(pattern["x"]), np.asarray(pattern["y"])
            divisor = self._normalization_divisor(x, y, xlim)
            base = y / divisor * float(pattern.get("scale", 1.0)) + self.curve_offset_spin.value()
            drawn = base + float(pattern.get("y_shift", 0.0))
            info["y_base"], info["y"] = base, drawn
            info["line"].set_ydata(drawn)
            visible = drawn[(x >= lo) & (x <= hi) & np.isfinite(drawn)]
            if visible.size:
                bounds.extend((float(np.min(visible)), float(np.max(visible))))
        bottom = min(0.0, min(bounds)) if bounds else 0.0
        top = max(bounds) if bounds else 1.0
        span = max(top - bottom, 1e-12)
        top = bottom + span * max(1.0, float(self.fixed_y_headroom))
        if top <= bottom + 1e-12:
            top = bottom + 1.0
        self._set_shared_ylim(bottom, top)
        preview = self._powerxrd_preview_data
        if isinstance(preview, dict) and self.preview_ax is not None:
            nx, ny = np.asarray(preview["new_x"]), np.asarray(preview["new_y"])
            adjusted = ny / self._normalization_divisor(nx, ny, xlim)
            for line in self.preview_ax.lines:
                if line.get_label() == "Adjusted":
                    line.set_ydata(adjusted)
            visible = adjusted[(nx >= lo) & (nx <= hi) & np.isfinite(adjusted)]
            if visible.size:
                low, high = min(0.0, float(visible.min())), float(visible.max())
                self.preview_ax.set_ylim(low, low + max(high - low, 1e-6) * 1.2)
        self._redraw_applied_peak_overlay()
        self.reposition_labels()
        self.canvas.draw_idle()

    def _set_shared_ylim(self, y0, y1):
        for ax in self._plot_axes():
            ax.set_ylim(y0, y1, emit=False)
        self._apply_horizontal_clip_only()

    def _y_label_offset_pt(self):
        """Distance from the left spine to the centre of the rotated Y label."""
        width_pt = float(self.fig.get_size_inches()[0]) * 72.0
        return max(0.028 * width_pt, 0.75 * float(self.axis_label_fs_spin.value()) + 4.0)

    def _apply_figure_layout(self):
        """Keep the historical fractional margins, but never less than the text needs.

        Tick and axis labels have fixed point sizes, so on a small canvas a purely
        fractional margin clips them (e.g. the X label at the bottom).
        """
        width_pt, height_pt = (float(v) * 72.0 for v in self.fig.get_size_inches())
        tick_fs = float(self.tick_fs_spin.value())
        axis_fs = float(self.axis_label_fs_spin.value())
        tick_len = max(2.0, 3.5 * float(self.frame_lw_spin.value()))
        line = 1.3  # text height including descenders, as a multiple of the font size
        outer = 4.0
        bottom_pt = (tick_len + matplotlib.rcParams["xtick.major.pad"] + tick_fs * line
                     + matplotlib.rcParams["axes.labelpad"] + axis_fs * line + outer)
        left_pt = self._y_label_offset_pt() + 0.5 * axis_fs * line + outer
        right_pt = 1.2 * tick_fs + outer  # half of the last tick label, e.g. "30"
        top_pt = self.title_font_size * 1.8 + outer if self.title_text else 0.6 * tick_fs + outer
        left = max(0.125, left_pt / width_pt)
        right = min(0.90, 1.0 - right_pt / width_pt)
        bottom = max(0.14, bottom_pt / height_pt)
        top = min(0.90, 1.0 - top_pt / height_pt)
        # On an extremely small canvas keep a sliver of plot area rather than failing.
        if right - left < 0.1:
            left, right = 0.45, 0.55
        if top - bottom < 0.1:
            bottom, top = 0.45, 0.55
        self.fig.subplots_adjust(left=left, right=right, top=top, bottom=bottom, hspace=0.0)

    def _apply_horizontal_clip_only(self):
        # Clip each curve only in X via data coordinates so display/export match.
        if not self.plot_lines:
            return
        for info in self.plot_lines:
            line = info.get("line")
            ax = info.get("ax")
            if line is None or ax is None:
                continue
            x0, x1 = ax.get_xlim()
            if not (np.isfinite(x0) and np.isfinite(x1)):
                line.set_clip_path(None)
                continue
            left, right = (x0, x1) if x0 <= x1 else (x1, x0)
            if abs(right - left) < 1e-12:
                line.set_clip_path(None)
                line.set_clip_on(False)
                continue

            y0, y1 = ax.get_ylim()
            if not (np.isfinite(y0) and np.isfinite(y1)):
                y0, y1 = -1.0, 1.0
            bottom, top = (y0, y1) if y0 <= y1 else (y1, y0)
            span = max(top - bottom, 1.0)
            pad = span * 20.0
            y_lo = bottom - pad
            y_hi = top + pad
            clip_rect = Rectangle(
                (left, y_lo),
                right - left,
                y_hi - y_lo,
                transform=ax.transData,
            )
            line.set_clip_path(clip_rect)
            line.set_clip_on(True)

    def _reset_full_view(self):
        if not (self.xlim_full and self.ylim_full):
            return False
        self._set_shared_xlim(self.xlim_full[0], self.xlim_full[1])
        if self._norm_mode() != "visible":
            self._set_shared_ylim(self.ylim_full[0], self.ylim_full[1])
        self.reposition_labels()
        self.canvas.draw_idle()
        return True

    def _clear_zoom_guides(self):
        for line in self.zoom_start_vlines:
            try:
                line.remove()
            except Exception:
                logger.debug("Suppressed error", exc_info=True)
        for line in self.zoom_end_vlines:
            try:
                line.remove()
            except Exception:
                logger.debug("Suppressed error", exc_info=True)
        self.zoom_start_vlines = []
        self.zoom_end_vlines = []

    def _show_zoom_guides(self, x_start, x_end):
        axes = self._plot_axes()
        if not axes:
            return
        if len(self.zoom_start_vlines) != len(axes) or len(self.zoom_end_vlines) != len(axes):
            self._clear_zoom_guides()
            for ax in axes:
                self.zoom_start_vlines.append(
                    ax.axvline(x_start, color="#111111", linestyle="--", linewidth=1.0, alpha=0.9, visible=True)
                )
                self.zoom_end_vlines.append(
                    ax.axvline(x_end, color="#111111", linestyle="--", linewidth=1.0, alpha=0.9, visible=True)
                )
            return
        for line in self.zoom_start_vlines:
            line.set_xdata([x_start, x_start])
            line.set_visible(True)
        for line in self.zoom_end_vlines:
            line.set_xdata([x_end, x_end])
            line.set_visible(True)

    def _clear_analysis_artists(self):
        for artist in getattr(self, "analysis_artists", []):
            try:
                artist.remove()
            except Exception:
                logger.debug("Suppressed error", exc_info=True)
        self.analysis_artists = []

    def _redraw_applied_peak_overlay(self):
        self._clear_analysis_artists()
        info = self.applied_peak_overlay if isinstance(self.applied_peak_overlay, dict) else None
        if not info:
            return
        if not self.patterns or not self._plot_axes():
            return

        idx = -1
        uid = info.get("curve_uid", None)
        if uid is not None:
            try:
                uid_i = int(uid)
                for i, p in enumerate(self.patterns):
                    if int(p.get("uid", -1)) == uid_i:
                        idx = i
                        break
            except Exception:
                logger.debug("Suppressed error", exc_info=True)
                idx = -1
        if idx < 0:
            try:
                idx = int(info.get("curve_idx", -1))
            except Exception:
                logger.debug("Suppressed error", exc_info=True)
                self.applied_peak_overlay = None
                return
        if idx < 0 or idx >= len(self.patterns):
            self.applied_peak_overlay = None
            return

        peak_x = info.get("peak_x", None)
        peak_y = info.get("peak_y", None)
        left_x = info.get("left_x", None)
        right_x = info.get("right_x", None)
        half_y = info.get("half_y", None)

        try:
            peak_x = float(peak_x)
            peak_y = float(peak_y)
        except Exception:
            logger.debug("Suppressed error", exc_info=True)
            self.applied_peak_overlay = None
            return
        if not (np.isfinite(peak_x) and np.isfinite(peak_y)):
            self.applied_peak_overlay = None
            return

        pattern = self.patterns[idx]
        x_arr = np.asarray(pattern.get("x", []), dtype=np.float64)
        y_arr = np.asarray(pattern.get("y", []), dtype=np.float64)
        if x_arr.size < 2 or y_arr.size < 2 or x_arr.size != y_arr.size:
            return

        ax = None
        if 0 <= idx < len(self.plot_lines):
            ax = self.plot_lines[idx].get("ax")
        if ax is None:
            ax = self._primary_ax()
        if ax is None:
            self.applied_peak_overlay = None
            return
        divisor = self._normalization_divisor(x_arr, y_arr, ax.get_xlim())
        scale = float(pattern.get("scale", 1.0))
        curve_offset = float(self.curve_offset_spin.value()) if self.curve_offset_spin else 0.03
        curve_offset += float(pattern.get("y_shift", 0.0))
        amp = scale / divisor

        peak_y_draw = peak_y * amp + curve_offset
        self.analysis_artists.append(
            ax.plot([peak_x], [peak_y_draw], marker="o", color="#d32f2f", markersize=5, zorder=10)[0]
        )
        self.analysis_artists.append(
            ax.axvline(peak_x, color="#d32f2f", linestyle="--", linewidth=1.0, alpha=0.9)
        )

        try:
            lx = float(left_x) if left_x is not None else None
            rx = float(right_x) if right_x is not None else None
            hy = float(half_y) if half_y is not None else None
        except Exception:
            logger.debug("Suppressed error", exc_info=True)
            lx = rx = hy = None

        if (
            lx is not None and rx is not None and hy is not None
            and np.isfinite(lx) and np.isfinite(rx) and np.isfinite(hy)
        ):
            half_y_draw = hy * amp + curve_offset
            self.analysis_artists.append(
                ax.hlines(half_y_draw, lx, rx, colors="#1565c0", linestyles="-", linewidth=1.2)
            )

    def _build_empty_plot(self):
        self.fig.clear()
        self._apply_figure_layout()

        self.plot_lines = []
        self.label_texts = []
        self.cursor_vlines = []
        self.cursor_text = None
        self.zoom_start_vlines = []
        self.zoom_end_vlines = []
        self.analysis_artists = []
        self.applied_peak_overlay = None
        self.preview_ax = None
        self.press_event = None
        self.pan_event = None
        self.dragging_curve_idx = None
        self.drag_curve_axis = None
        self.drag_curve_start_ydata = None
        self.drag_curve_start_shift = None
        self.drag_curve_moved = False
        self.ax = None
        self.axes = []
        self.title_artist = None
        self.x_label_artist = None
        self.y_label_artist = None
        self.fig.suptitle("")

    def apply_frame_style(self, frame_lw: float):
        axes = self._plot_axes()
        if not axes:
            return
        tick_len = max(2.0, 3.5 * frame_lw)
        last_idx = len(axes) - 1
        for i, ax in enumerate(axes):
            for name, spine in ax.spines.items():
                spine.set_linewidth(frame_lw)
                spine.set_zorder(0.5)
                if name == "top":
                    spine.set_visible(i == 0)
                elif name == "bottom":
                    spine.set_visible(i == last_idx)
                else:
                    spine.set_visible(True)
            ax.tick_params(axis="both", which="both", width=frame_lw, length=tick_len)

    # ---------------- Curve Color Mode ---------------- #

    def _current_color_mode(self):
        if not self.color_mode_combo:
            return "black"
        mode = self.color_mode_combo.currentData()
        mode = str(mode) if mode is not None else "black"
        return mode if mode in PALETTES else "black"

    def _current_svg_background(self):
        if not self.svg_bg_combo:
            return "transparent"
        mode = self.svg_bg_combo.currentData()
        mode = str(mode) if mode is not None else "transparent"
        return mode if mode in ("transparent", "white") else "transparent"

    def _color_for_index(self, idx, count=None):
        """Color of curve *idx*; gradient palettes spread over *count* curves."""
        count = max(int(idx) + 1, len(self.patterns) if count is None else int(count))
        return palette_colors(self._current_color_mode(), count)[int(idx)]

    def _pick_curve_color(self, current):
        """Color dialog whose basic colors are shades of a chosen palette; None if cancelled."""
        key = str(self.settings.value("color/dialog_palette", ""))
        if key not in PALETTES:
            mode = self._current_color_mode()
            key = mode if mode in PALETTES else "okabe_ito"
        dialog = CurveColorDialog(QColor(current), key, self)
        accepted = dialog.exec()
        self.settings.setValue("color/dialog_palette", dialog.palette_key())
        if not accepted or not dialog.selectedColor().isValid():
            return None
        return dialog.selectedColor()

    def _recolor_patterns(self):
        colors = palette_colors(self._current_color_mode(), len(self.patterns))
        for pattern, color in zip(self.patterns, colors):
            pattern["color"] = color

    def _apply_color_mode_to_patterns(self):
        if not self.patterns:
            return
        self._recolor_patterns()
        self.update_plot(preserve_view=True)
        self.refresh_files_list()

    def on_color_mode_changed(self, _idx):
        if self.patterns and not self._restoring_undo:
            self._push_undo_snapshot()
        self._apply_color_mode_to_patterns()

    # ---------------- SVG/PNG: Generate in Memory + Copy ---------------- #

    def _make_svg_bytes(self, background_mode=None):
        # Export exactly what is currently on canvas: no style rewrite, no auto-cropping.
        if self.canvas:
            self.canvas.draw()

        # Keep X-only clipping aligned with the current renderer bbox.
        self._apply_horizontal_clip_only()
        if self.canvas:
            self.canvas.draw()

        bg_mode = str(background_mode) if background_mode is not None else self._current_svg_background()
        if bg_mode not in ("transparent", "white"):
            bg_mode = "transparent"

        buf = io.BytesIO()
        save_kwargs = {
            "format": "svg",
            "bbox_inches": None,
            "pad_inches": 0.0,
        }
        if bg_mode == "transparent":
            # Must keep face/edge as "none"; otherwise an explicit white facecolor can override transparency.
            save_kwargs.update({
                "transparent": True,
                "facecolor": "none",
                "edgecolor": "none",
            })
        else:
            save_kwargs.update({
                "transparent": False,
                "facecolor": "white",
                "edgecolor": "white",
            })
        self.fig.savefig(buf, **save_kwargs)
        return buf.getvalue()

    def _make_png_bytes(self, background_mode=None):
        # Export exactly what is currently on canvas as raster PNG.
        if self.canvas:
            self.canvas.draw()

        # Keep X-only clipping aligned with the current renderer bbox.
        self._apply_horizontal_clip_only()
        if self.canvas:
            self.canvas.draw()

        bg_mode = str(background_mode) if background_mode is not None else self._current_svg_background()
        if bg_mode not in ("transparent", "white"):
            bg_mode = "transparent"

        buf = io.BytesIO()
        save_kwargs = {
            "format": "png",
            "bbox_inches": None,
            "pad_inches": 0.0,
            "dpi": float(getattr(self, "base_fig_dpi", 100.0)),
        }
        if bg_mode == "transparent":
            save_kwargs.update({
                "transparent": True,
                # Keep alpha=0 but use white RGB to avoid black appearance
                # in viewers that do not handle premultiplied alpha correctly.
                "facecolor": (1.0, 1.0, 1.0, 0.0),
                "edgecolor": (1.0, 1.0, 1.0, 0.0),
            })
        else:
            save_kwargs.update({
                "transparent": False,
                "facecolor": "white",
                "edgecolor": "white",
            })
        self.fig.savefig(buf, **save_kwargs)
        return buf.getvalue()

    def copy_svg_to_clipboard(self):
        if not self.patterns:
            QMessageBox.information(self, "Info", "There is no plot to copy.")
            return
        try:
            svg_bytes = self._make_svg_bytes()
            mime = QMimeData()
            mime.setData("image/svg+xml", QByteArray(svg_bytes))
            try:
                mime.setText(svg_bytes.decode("utf-8"))
            except Exception:
                logger.debug("Suppressed error", exc_info=True)
            QApplication.clipboard().setMimeData(mime)
            QMessageBox.information(self, "Success", "SVG copied to clipboard.")
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def copy_png_to_clipboard(self):
        if not self.patterns:
            QMessageBox.information(self, "Info", "There is no plot to copy.")
            return
        try:
            png_bytes = self._make_png_bytes()
            image = QImage.fromData(png_bytes, "PNG")

            # Remove previous temp file (if any) before creating a new one.
            self._cleanup_clipboard_temp_png()
            with tempfile.NamedTemporaryFile(
                mode="wb",
                suffix=".png",
                prefix="xstack_clip_",
                delete=False,
            ) as tmp:
                tmp.write(png_bytes)
                temp_path = tmp.name

            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(temp_path)])
            if not image.isNull():
                mime.setImageData(image)
            QApplication.clipboard().setMimeData(mime)
            self._clipboard_temp_png = temp_path
            QMessageBox.information(self, "Success", "PNG copied to clipboard.")
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def _cleanup_clipboard_temp_png(self):
        path = getattr(self, "_clipboard_temp_png", None)
        self._clipboard_temp_png = None
        if not path:
            return
        try:
            if os.path.isfile(path):
                os.remove(path)
        except Exception:
            logger.debug("Suppressed error", exc_info=True)

    # ---------------- Shared UI State (projects + undo) ---------------- #

    # (widget attribute, state key, default); None means "use the legacy single font size".
    _ui_spin_fields = (
        ("curve_lw_spin", "curve_line_width", 1.5),
        ("frame_lw_spin", "frame_line_width", 1.5),
        ("curve_offset_spin", "curve_y_offset", 0.03),
        ("fig_w_spin", "fig_w", 7.0),
        ("fig_h_spin", "fig_h", 6.0),
        ("curve_label_fs_spin", "curve_label_fs", None),
        ("tick_fs_spin", "tick_fs", None),
        ("axis_label_fs_spin", "axis_label_fs", None),
        ("xmin_spin", "x_display_min", 2.0),
        ("xmax_spin", "x_display_max", 30.0),
    )

    @staticmethod
    def _set_silently(widget, setter, value):
        previous = widget.blockSignals(True)
        try:
            setter(value)
        finally:
            widget.blockSignals(previous)

    def _norm_mode(self):
        return self.norm_mode_combo.currentData() if self.norm_mode_combo else "full"

    def _set_norm_mode(self, mode):
        self.norm_mode_combo.setCurrentIndex(max(0, self.norm_mode_combo.findData(mode)))

    @staticmethod
    def _norm_mode_from_state(ui):
        mode = ui.get("norm_mode")
        if mode in NORM_MODES:
            return mode
        # Projects and saved defaults from 1.5 and earlier store two booleans.
        if ui.get("live_norm"):
            return "visible"
        return "full" if ui.get("norm", True) else "none"

    def _set_curve_labels_visible(self, visible):
        self.show_curve_labels = bool(visible)
        if self.curve_labels_btn:
            self._set_silently(self.curve_labels_btn, self.curve_labels_btn.setChecked, self.show_curve_labels)

    def _collect_ui_state(self):
        mode = self._norm_mode()
        state = {
            "norm_mode": mode,
            # Legacy flags keep projects readable by older xStack releases.
            "norm": mode != "none",
            "live_norm": mode == "visible",
            "superimpose": bool(self._is_superimpose_mode()),
            "default_black": self._current_color_mode() == "black",
            "color_mode": self._current_color_mode(),
            "svg_background": self._current_svg_background(),
        }
        for name, key, _default in self._ui_spin_fields:
            control = getattr(self, name)
            state[key] = int(control.value()) if isinstance(control, QSpinBox) else float(control.value())
        return state

    def _apply_ui_state(self, ui):
        """Set controls without emitting signals; the caller redraws once afterwards."""
        self._set_silently(self.norm_mode_combo, self._set_norm_mode, self._norm_mode_from_state(ui))
        self._set_silently(self.superimpose_cb, self.superimpose_cb.setChecked, bool(ui.get("superimpose", False)))
        mode = str(ui.get("color_mode", "black" if bool(ui.get("default_black", True)) else "colorful"))
        index = self.color_mode_combo.findData(mode)
        self._set_silently(self.color_mode_combo, self.color_mode_combo.setCurrentIndex, max(0, index))
        index = self.svg_bg_combo.findData(str(ui.get("svg_background", "transparent")))
        self._set_silently(self.svg_bg_combo, self.svg_bg_combo.setCurrentIndex, max(0, index))
        legacy_fs = int(ui.get("fs", 10))
        for name, key, default in self._ui_spin_fields:
            control = getattr(self, name)
            value = ui.get(key, legacy_fs if default is None else default)
            value = int(value) if isinstance(control, QSpinBox) else float(value)
            self._set_silently(control, control.setValue, value)
        self._apply_canvas_size_lock()

    @staticmethod
    def _copy_pattern(p):
        """Copy a curve record; x/y arrays are read-only and shared rather than duplicated."""
        return {
            "uid": int(p.get("uid", 0)),
            "x": readonly_array(p.get("x", [])),
            "y": readonly_array(p.get("y", [])),
            "label": str(p.get("label", "")),
            "filename": str(p.get("filename", "")),
            "source_path": str(p.get("source_path", "")),
            "color": p.get("color", None),
            "scale": float(p.get("scale", 1.0)),
            "y_shift": float(p.get("y_shift", 0.0)),
            "label_dx": float(p.get("label_dx", 0.0)),
            "label_dy": float(p.get("label_dy", 0.0)),
        }

    def _restore_view(self, xlim, ylim):
        if not (xlim and ylim):
            return
        try:
            self._set_shared_xlim(float(xlim[0]), float(xlim[1]))
            if self._norm_mode() != "visible":
                self._set_shared_ylim(float(ylim[0]), float(ylim[1]))
        except Exception:
            logger.debug("Could not restore view limits", exc_info=True)

    # ---------------- Project File: Save/Load ---------------- #

    def _collect_project_state(self):
        ax = self._primary_ax()
        state = {
            "version": 1,
            "applied_peak_overlay": self.applied_peak_overlay,
            "title_text": self.title_text,
            "x_axis_label_text": self.x_axis_label_text,
            "y_axis_label_text": self.y_axis_label_text,
            "show_curve_labels": bool(self.show_curve_labels),
            "browser_root_dir": self.browser_root_dir,
            "global_label_offset": [float(self.global_label_offset[0]), float(self.global_label_offset[1])],
            "selected_idx": int(self.selected_idx) if self.selected_idx is not None else -1,
            "ui": self._collect_ui_state(),
            "view": {
                "xlim": list(ax.get_xlim()) if ax else None,
                "ylim": list(ax.get_ylim()) if ax else None,
            },
            "patterns": [],
        }
        for p in self.patterns:
            meta = self._copy_pattern(p)
            del meta["x"], meta["y"]
            state["patterns"].append(meta)
        return state

    # ---------------- Undo ---------------- #

    def _capture_undo_snapshot(self):
        ax = self._primary_ax()
        view_xlim = view_ylim = None
        if ax is not None:
            view_xlim = tuple(float(v) for v in ax.get_xlim())
            view_ylim = tuple(float(v) for v in ax.get_ylim())
        return {
            "patterns": [self._copy_pattern(p) for p in self.patterns],
            "applied_peak_overlay": dict(self.applied_peak_overlay) if self.applied_peak_overlay else None,
            "uid_counter": int(self._uid_counter),
            "selected_idx": int(self.selected_idx) if self.selected_idx is not None else None,
            "global_label_offset": (
                float(self.global_label_offset[0]),
                float(self.global_label_offset[1]),
            ),
            "title_text": str(self.title_text),
            "x_axis_label_text": str(self.x_axis_label_text),
            "y_axis_label_text": str(self.y_axis_label_text),
            "show_curve_labels": bool(self.show_curve_labels),
            "xlim_full": tuple(self.xlim_full) if self.xlim_full is not None else None,
            "ylim_full": tuple(self.ylim_full) if self.ylim_full is not None else None,
            "view_xlim": view_xlim,
            "view_ylim": view_ylim,
            "ui": self._collect_ui_state(),
        }

    def _push_undo_snapshot(self):
        if self._restoring_undo:
            return
        self.undo_stack.append(self._capture_undo_snapshot())
        if len(self.undo_stack) > self.undo_limit:
            del self.undo_stack[0]

    def _restore_undo_snapshot(self, snapshot):
        self._restoring_undo = True
        try:
            self.patterns = [self._copy_pattern(p) for p in snapshot.get("patterns", [])]
            self.applied_peak_overlay = snapshot.get("applied_peak_overlay")

            max_uid = max((int(p.get("uid", 0)) for p in self.patterns), default=0)
            self._uid_counter = max(int(snapshot.get("uid_counter", 0)), max_uid)
            self.selected_idx = snapshot.get("selected_idx", None)
            off = snapshot.get("global_label_offset", (0.0, 0.0))
            self.global_label_offset = (float(off[0]), float(off[1]))

            self.title_text = str(snapshot.get("title_text", self.title_text))
            self.x_axis_label_text = str(snapshot.get("x_axis_label_text", self.x_axis_label_text))
            self.y_axis_label_text = str(snapshot.get("y_axis_label_text", self.y_axis_label_text))
            self._set_curve_labels_visible(snapshot.get("show_curve_labels", self.show_curve_labels))
            self._apply_ui_state(snapshot.get("ui", {}))

            if self.patterns:
                self.update_plot(preserve_view=False)
                self._restore_view(snapshot.get("view_xlim"), snapshot.get("view_ylim"))
                self.reposition_labels()
                self.canvas.draw_idle()
            else:
                self.clear_plot()

            self.xlim_full = tuple(snapshot.get("xlim_full")) if snapshot.get("xlim_full") else None
            self.ylim_full = tuple(snapshot.get("ylim_full")) if snapshot.get("ylim_full") else None

            self.refresh_files_list()
            if self.selected_idx is not None and self.files_list:
                if 0 <= self.selected_idx < self.files_list.count():
                    self.files_list.setCurrentRow(self.selected_idx)
                else:
                    self.selected_idx = None
        finally:
            self._restoring_undo = False

    def undo_last_action(self):
        if not self.undo_stack:
            return
        snapshot = self.undo_stack.pop()
        self._restore_undo_snapshot(snapshot)

    def _remember_project_dir(self, path):
        self.project_dir = os.path.dirname(os.path.abspath(path))
        self.settings.setValue("project/last_dir", self.project_dir)

    def save_project(self):
        if not self.patterns:
            QMessageBox.information(self, "Info", "There are no curves to save.")
            return

        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save project",
            self.project_dir,
            "PXRD Project (*.pxrdproj)"
        )
        if not path:
            return
        if not path.lower().endswith(".pxrdproj"):
            path += ".pxrdproj"
        self._remember_project_dir(path)

        try:
            state = self._collect_project_state()

            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                project_json = json.dumps(state, ensure_ascii=False, indent=2)
                zf.writestr("project.json", project_json)

                for p in self.patterns:
                    uid = int(p["uid"])
                    x = np.asarray(p["x"])
                    y = np.asarray(p["y"])
                    buf = io.BytesIO()
                    np.savez_compressed(buf, x=x, y=y)
                    zf.writestr(f"data/uid_{uid}.npz", buf.getvalue())

            QMessageBox.information(self, "Success", "Project file saved.")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save:\n{e}")

    def load_project(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open project",
            self.project_dir,
            "PXRD Project (*.pxrdproj)"
        )
        if not path:
            return
        self._remember_project_dir(path)
        self.load_project_path(path)

    def load_project_path(self, path, show_success=True):
        previous = self._capture_undo_snapshot()
        previous_browser_root = self.browser_root_dir
        changing_state = False
        try:
            with zipfile.ZipFile(path, "r") as zf:
                raw = zf.read("project.json").decode("utf-8")
                state = json.loads(raw)

                if "patterns" not in state or "ui" not in state:
                    raise ValueError("Project file is missing required fields.")

                loaded_patterns = []
                loaded_uids = set()
                loaded_uid_counter = 0
                for meta in state["patterns"]:
                    uid = int(meta["uid"])
                    npz_bytes = zf.read(f"data/uid_{uid}.npz")
                    buf = io.BytesIO(npz_bytes)
                    with np.load(buf, allow_pickle=False) as arr:
                        x = np.asarray(arr["x"], dtype=float)
                        y = np.asarray(arr["y"], dtype=float)
                    if (x.ndim != 1 or y.ndim != 1 or not x.size or x.size != y.size
                            or not np.all(np.isfinite(x)) or not np.all(np.isfinite(y))):
                        raise ValueError("Invalid curve coordinates in project.")
                    if uid in loaded_uids:
                        raise ValueError("Duplicate curve UID in project.")
                    loaded_uids.add(uid)
                    loaded_uid_counter = max(loaded_uid_counter, uid)

                    label = meta.get("label", f"uid_{uid}")
                    loaded_patterns.append(self._copy_pattern({
                        **meta,
                        "uid": uid, "x": x, "y": y, "label": label,
                        "filename": meta.get("filename", label),
                    }))

                changing_state = True
                if self.powerxrd_tools_dialog is not None:
                    self.powerxrd_tools_dialog.close()
                self.patterns = []
                self.selected_idx = None

                self._apply_ui_state(state.get("ui", {}))
                self.title_text = str(state.get("title_text", ""))
                self.x_axis_label_text = str(state.get("x_axis_label_text", "2θ (deg)"))
                self.y_axis_label_text = str(state.get("y_axis_label_text", "Intensity (a.u.)"))
                self.browser_root_dir = str(state.get("browser_root_dir", self.browser_root_dir))
                if self.browser_root_edit:
                    self.browser_root_edit.setPath(self.browser_root_dir)
                self._set_curve_labels_visible(state.get("show_curve_labels", True))

                off = state.get("global_label_offset", [0.0, 0.0])
                self.global_label_offset = (float(off[0]), float(off[1]))

            self.patterns = loaded_patterns
            self._uid_counter = loaded_uid_counter
            self.applied_peak_overlay = state.get("applied_peak_overlay")
            sel = int(state.get("selected_idx", -1))
            if 0 <= sel < len(self.patterns):
                self.selected_idx = sel
            else:
                self.selected_idx = None

            self.populate_browser_tree()
            self.refresh_files_list()
            self.update_plot(preserve_view=False)

            view = state.get("view", {})
            if view.get("xlim") and view.get("ylim"):
                self._restore_view(view["xlim"], view["ylim"])
                self.reposition_labels()
                self.canvas.draw_idle()

            if not self.patterns:
                self.clear_plot()
            self.undo_stack.clear()
            if show_success:
                QMessageBox.information(self, "Success", "Project file loaded.")
        except Exception as e:
            if changing_state:
                self.browser_root_dir = previous_browser_root
                self.browser_root_edit.setPath(previous_browser_root)
                self._restore_undo_snapshot(previous)
                self.populate_browser_tree()
            QMessageBox.critical(self, "Error", f"Failed to load:\n{e}")

    # ---------------- List Refresh/Sorting ---------------- #

    def refresh_files_list(self):
        dialog = self.powerxrd_tools_dialog
        if dialog is not None and getattr(dialog, "pattern_objects", []) != [id(p) for p in self.patterns]:
            dialog.close()
        self.files_list.blockSignals(True)
        self.files_list.clear()
        for p in self.patterns:
            scale = p.get("scale", 1.0)
            suffix = f"  ×{scale:.2f}" if abs(scale - 1.0) > 1e-6 else ""
            item = QListWidgetItem(f"{p['label']}{suffix}")
            item.setIcon(pattern_icon(p.get("color") or "#000000"))
            item.setSizeHint(QSize(0, 23))
            item.setData(Qt.ItemDataRole.UserRole, p["uid"])
            item.setToolTip(
                f"{p['filename']}\n{p.get('source_path', '')}\n"
                f"Label: {to_plain(p['label'])}\nColor: {p.get('color', '')}\nScale: {scale:g}"
            )
            self.files_list.addItem(item)
        self.loaded_count_label.setText(str(len(self.patterns)))

        if self.selected_idx is not None and 0 <= self.selected_idx < self.files_list.count():
            self.files_list.setCurrentRow(self.selected_idx)

        self.files_list.blockSignals(False)

    def on_files_order_changed(self):
        uid_order = []
        for i in range(self.files_list.count()):
            uid_order.append(self.files_list.item(i).data(Qt.ItemDataRole.UserRole))
        old_uid_order = [p["uid"] for p in self.patterns]
        if uid_order != old_uid_order:
            self._push_undo_snapshot()

        uid_to_pattern = {p["uid"]: p for p in self.patterns}
        new_patterns = [uid_to_pattern[uid] for uid in uid_order if uid in uid_to_pattern]

        if len(new_patterns) != len(self.patterns):
            remain = [p for p in self.patterns if p["uid"] not in set(uid_order)]
            new_patterns.extend(remain)

        old_uid = None
        if self.selected_idx is not None and 0 <= self.selected_idx < len(self.patterns):
            old_uid = self.patterns[self.selected_idx]["uid"]

        self.patterns = new_patterns
        if is_gradient(self._current_color_mode()):
            self._recolor_patterns()

        if old_uid is not None:
            for i, p in enumerate(self.patterns):
                if p["uid"] == old_uid:
                    self.selected_idx = i
                    break

        self.refresh_files_list()
        self.update_plot()

    def on_list_selection_changed(self, row):
        if row is None or row < 0 or row >= len(self.patterns):
            self.selected_idx = None
            self.apply_selection_highlight()
            return
        self.selected_idx = row
        self.apply_selection_highlight()

    def apply_selection_highlight(self):
        if not self.plot_lines:
            return
        base_lw = float(self.curve_lw_spin.value()) if self.curve_lw_spin else 1.0
        for i, info in enumerate(self.plot_lines):
            line = info.get("line")
            if line is None:
                continue
            if self.selected_idx is not None and i == self.selected_idx:
                line.set_linewidth(base_lw * 2.0)
                line.set_alpha(1.0)
                line.set_zorder(5)
            else:
                line.set_linewidth(base_lw)
                line.set_alpha(0.45 if self.selected_idx is not None else 1.0)
                line.set_zorder(2)
        if self.canvas:
            self.canvas.draw_idle()

    # ---------------- Axis Label Text ---------------- #

    # ---------------- Core Features ---------------- #

    def _format_chemical_html(self, text):
        return to_html(text)

    def _format_chemical_plot(self, text):
        return to_mathtext(text)

    def _natural_sort_key(self, text):
        parts = re.split(r"(\d+)", text.lower())
        return [int(p) if p.isdigit() else p for p in parts]

    def _mtime_sort_key(self, path):
        try:
            return os.path.getmtime(path)
        except Exception:
            logger.debug("Suppressed error", exc_info=True)
            return 0.0

    def _list_display_name_from_path(self, path):
        # Generic display name: file stem only.
        return os.path.splitext(os.path.basename(path))[0]

    def _append_suffix(self, text, suffix):
        raw = str(text or "").strip()
        if not raw:
            return raw
        tag = str(suffix or "").strip()
        if not tag:
            return raw
        suffix_token = f"_{tag}"
        if raw.lower().endswith(suffix_token.lower()):
            return raw
        return f"{raw}{suffix_token}"

    def _append_suffix_to_filename(self, filename, suffix):
        raw = str(filename or "").strip()
        if not raw:
            return raw
        stem, ext = os.path.splitext(raw)
        suffix_token = f"_{str(suffix or '').strip()}"
        if not suffix_token.strip("_"):
            return raw
        if stem.lower().endswith(suffix_token.lower()):
            return raw
        return f"{stem}{suffix_token}{ext}"

    def _mark_pattern_processed(self, idx, suffix):
        if idx < 0 or idx >= len(self.patterns):
            return
        p = self.patterns[idx]
        p["label"] = self._append_suffix(p.get("label", ""), suffix)
        p["filename"] = self._append_suffix_to_filename(p.get("filename", ""), suffix)

    def _resolve_single_target_index(self, show_message=True):
        if not self.patterns:
            return None
        if self.selected_idx is not None and 0 <= self.selected_idx < len(self.patterns):
            return self.selected_idx
        row = self.files_list.currentRow() if self.files_list else -1
        if 0 <= row < len(self.patterns):
            return row
        if len(self.patterns) == 1:
            return 0
        if show_message:
            QMessageBox.information(self, "PXRD", "Please select one curve first.")
        return None

    def _resolve_explicit_selected_index(self, show_message=True):
        if not self.patterns:
            return None
        if self.selected_idx is not None and 0 <= self.selected_idx < len(self.patterns):
            return self.selected_idx
        row = self.files_list.currentRow() if self.files_list else -1
        if 0 <= row < len(self.patterns):
            return row
        if show_message:
            QMessageBox.information(self, "PXRD", "Please select one curve first.")
        return None

    def _snapshot_patterns_data(self):
        return [self._copy_pattern(p) for p in self.patterns]

    def _add_browser_message(self, text):
        item = QTreeWidgetItem([text])
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable & ~Qt.ItemFlag.ItemIsDragEnabled)
        item.setForeground(0, QColor("#657282"))
        self.browser_tree.addTopLevelItem(item)
        return item

    def _make_browser_file_item(self, text, full_path):
        item = QTreeWidgetItem([text])
        item.setData(0, Qt.ItemDataRole.UserRole, full_path)
        item.setToolTip(0, full_path)
        return item

    def _sort_by_mtime(self):
        return bool(self.sort_by_mtime_cb and self.sort_by_mtime_cb.isChecked())

    def _sorted_file_names(self, names, folder):
        """Newest first when the modified-time option is on, otherwise natural name order."""
        if self._sort_by_mtime():
            return sorted(names, key=lambda f: self._mtime_sort_key(os.path.join(folder, f)), reverse=True)
        return sorted(names, key=self._natural_sort_key)

    def _cancel_browser_search(self):
        if self._browser_search_cancel is not None:
            self._browser_search_cancel.set()
            self._browser_search_cancel = None
        self._browser_search_poll.stop()

    def populate_browser_tree(self):
        self._browser_search_timer.stop()
        self._cancel_browser_search()
        self.browser_tree.clear()
        root = self.browser_root_dir
        if not os.path.isdir(root):
            self._add_browser_message(f"Directory not found: {root}")
            return

        try:
            root_entries = os.listdir(root)
        except PermissionError:
            self._add_browser_message(f"Access denied: {root}")
            return
        except OSError as e:
            self._add_browser_message(f"Cannot read directory: {e}")
            return

        search_text = self.browser_search_edit.text().strip() if self.browser_search_edit else ""
        if search_text:
            self._start_browser_search(root, search_text)
            return

        def is_pattern(folder, name):
            return (os.path.splitext(name)[1].lower() in self.supported_exts
                    and os.path.isfile(os.path.join(folder, name)))

        root_files = [f for f in root_entries if is_pattern(root, f)]
        for fname in self._sorted_file_names(root_files, root):
            self.browser_tree.addTopLevelItem(self._make_browser_file_item(fname, os.path.join(root, fname)))

        subdirs = sorted((d for d in root_entries if os.path.isdir(os.path.join(root, d))),
                         key=self._natural_sort_key)
        shown_folders = 0
        for folder_name in subdirs:
            folder_path = os.path.join(root, folder_name)
            try:
                files = [f for f in os.listdir(folder_path) if is_pattern(folder_path, f)]
            except OSError:
                continue
            if not files:
                continue
            parent_item = QTreeWidgetItem([folder_name])
            parent_item.setFlags(parent_item.flags() & ~Qt.ItemFlag.ItemIsDragEnabled)
            parent_item.setToolTip(0, folder_path)
            self.browser_tree.addTopLevelItem(parent_item)
            for fname in self._sorted_file_names(files, folder_path):
                parent_item.addChild(self._make_browser_file_item(fname, os.path.join(folder_path, fname)))
            shown_folders += 1

        if not root_files and not shown_folders:
            exts = " ".join(sorted(self.supported_exts))
            item = self._add_browser_message("No PXRD files in this folder")
            item.setToolTip(0, f"Supported types: {exts}\nOnly the folder and its direct subfolders are listed; use search to look deeper.")
        self.browser_tree.resizeColumnToContents(0)
        self.browser_tree.collapseAll()

    def _start_browser_search(self, root, search_text):
        """Walk the folder tree in a worker so a large root never freezes the window."""
        cancel = threading.Event()
        self._browser_search_cancel = cancel
        self._browser_search_text = search_text
        self._add_browser_message("Searching…")
        results = self._browser_search_results = queue.Queue()
        exts = frozenset(self.supported_exts)
        threading.Thread(
            target=lambda: results.put(search_pattern_files(
                root, search_text, exts, cancel, BROWSER_SEARCH_LIMIT)),
            daemon=True,
        ).start()
        self._browser_search_poll.start()

    def _poll_browser_search(self):
        try:
            matches, truncated = self._browser_search_results.get_nowait()
        except queue.Empty:
            return
        self._browser_search_poll.stop()
        if self._browser_search_cancel is None or self._browser_search_cancel.is_set():
            return
        self._browser_search_cancel = None
        self.browser_tree.clear()
        if not matches:
            self._add_browser_message(f"No matches for: {self._browser_search_text}")
            return
        if self._sort_by_mtime():
            matches.sort(key=lambda match: match[2], reverse=True)
        else:
            matches.sort(key=lambda match: self._natural_sort_key(match[1]))
        for full_path, rel_display, _mtime in matches:
            self.browser_tree.addTopLevelItem(self._make_browser_file_item(rel_display, full_path))
        if truncated:
            self._add_browser_message(f"Showing the first {BROWSER_SEARCH_LIMIT} matches; refine the search")
        self.browser_tree.resizeColumnToContents(0)

    def on_browser_item_double_clicked(self, item, _column):
        file_path = item.data(0, Qt.ItemDataRole.UserRole)
        if file_path and os.path.isfile(file_path):
            self.queue_patterns_from_paths([file_path])

    def add_patterns_dialog(self):
        start_dir = self.browser_root_dir if os.path.isdir(self.browser_root_dir) else ""
        files, _ = QFileDialog.getOpenFileNames(
            self, "Select PXRD Files", start_dir,
            PXRD_FILE_DIALOG_FILTER
        )
        if files:
            self.queue_patterns_from_paths(files)

    def on_browser_search_changed(self, _text):
        self._browser_search_timer.start()

    def on_browser_root_changed(self):
        new_root = self.browser_root_edit.text().strip() if self.browser_root_edit else ""
        if not new_root:
            QMessageBox.warning(self, "Warning", "Default folder cannot be empty.")
            return
        if not os.path.isdir(new_root):
            QMessageBox.warning(self, "Warning", f"Directory not found: {new_root}")
            return
        self.set_browser_root(new_root)

    def pick_browser_root_dir(self):
        start_dir = self.browser_root_dir if os.path.isdir(self.browser_root_dir) else ""
        path = QFileDialog.getExistingDirectory(self, "Select default folder", start_dir)
        if not path:
            return
        self.set_browser_root(path)

    def set_browser_root(self, path):
        self.browser_root_dir = os.path.abspath(path)
        self.settings.setValue("browser/root_dir", self.browser_root_dir)
        self.settings.sync()
        if self.browser_root_edit:
            self.browser_root_edit.setPath(self.browser_root_dir)
        self.populate_browser_tree()

    def queue_patterns_from_paths(self, paths):
        self._import_pending.extend(os.path.abspath(path) for path in paths)
        if not self._import_active:
            self._start_import()

    def _start_import(self):
        existing = {os.path.normcase(p.get("source_path", "")) for p in self.patterns}
        paths = list(dict.fromkeys(p for p in self._import_pending if os.path.normcase(p) not in existing))
        self._import_pending.clear()
        if not paths:
            return
        self._import_active = True
        self.statusBar().showMessage(f"Loading {len(paths)} PXRD file(s)…")
        threading.Thread(target=_read_patterns_to_queue,
                         args=(paths, self._import_results), daemon=True).start()
        self._import_timer.start()

    def _poll_import(self):
        try:
            loaded, errors = self._import_results.get_nowait()
        except queue.Empty:
            return
        self._import_timer.stop()
        try:
            self._append_loaded_patterns(loaded, errors)
        finally:
            self._import_active = False
            self.statusBar().showMessage("PXRD import complete", 3000)
            self._start_import()

    def add_patterns_from_paths(self, paths):
        """Synchronous programmatic import; interactive entry points use the queue."""
        self._append_loaded_patterns(*read_pattern_batch(paths))

    def _append_loaded_patterns(self, loaded, errors):
        existing = {os.path.normcase(os.path.abspath(p["source_path"]))
                    for p in self.patterns if p.get("source_path")}
        pending = []
        for path, x, y in loaded:
            key = os.path.normcase(os.path.abspath(path))
            if key in existing:
                continue
            existing.add(key)
            pending.append({
                "uid": self._uid_counter + len(pending) + 1,
                "x": readonly_array(x), "y": readonly_array(y), "label": self._list_display_name_from_path(path),
                "filename": os.path.basename(path), "source_path": os.path.abspath(path),
                "color": self._color_for_index(len(self.patterns) + len(pending)),
                "scale": 1.0, "y_shift": 0.0, "label_dx": 0.0, "label_dy": 0.0,
            })
        if pending:
            self._push_undo_snapshot()
            self.patterns.extend(pending)
            if is_gradient(self._current_color_mode()):
                # A gradient spans every curve, so new curves shift the existing colors.
                self._recolor_patterns()
            self._uid_counter += len(pending)
            self.selected_idx = None
            self.update_plot(preserve_view=False)
        if errors:
            QMessageBox.critical(self, "Import errors", "\n\n".join(errors))

    def _paths_from_mime(self, mime):
        paths = []
        if not mime or not mime.hasUrls():
            return paths
        for url in mime.urls():
            local = url.toLocalFile()
            if local:
                paths.append(local)
        return paths

    def eventFilter(self, obj, event):
        canvas = getattr(self, "canvas", None)
        viewport = self.canvas_scroll.viewport() if self.canvas_scroll is not None else None

        if viewport is not None and obj == viewport:
            if event.type() == QEvent.Type.Resize:
                self._reposition_scale_overlay()
                return False

        if canvas is not None and obj == canvas:
            if event.type() == QEvent.Type.DragEnter:
                mime = event.mimeData()
                if mime and mime.hasUrls():
                    event.acceptProposedAction()
                    return True
            elif event.type() == QEvent.Type.Drop:
                paths = self._paths_from_mime(event.mimeData())
                if paths:
                    self.queue_patterns_from_paths(paths)
                    event.acceptProposedAction()
                    return True
            elif event.type() == QEvent.Type.Resize:
                # Keep figure display size locked to user-defined width/height.
                if self.fig and self.fig_w_spin and self.fig_h_spin:
                    scale = float(getattr(self, "canvas_view_scale", 1.0))
                    base_dpi = float(getattr(self, "base_fig_dpi", 100.0))
                    self.fig.set_dpi(base_dpi * scale)
                    self.fig.set_size_inches(self.fig_w_spin.value(), self.fig_h_spin.value(), forward=True)
                if self.plot_lines:
                    self._apply_horizontal_clip_only()
                return False
        return super().eventFilter(obj, event)

    def clear_patterns(self):
        if self.patterns:
            self._push_undo_snapshot()
        self.patterns = []
        self.selected_idx = None
        self.refresh_files_list()
        self.clear_plot()

    def set_display_range(self, show_warning=True):
        if self.xmin_spin is None or self.xmax_spin is None:
            return
        xmin = float(self.xmin_spin.value())
        xmax = float(self.xmax_spin.value())
        if xmin >= xmax:
            if show_warning:
                QMessageBox.warning(self, "Warning", "Display range requires X min < X max.")
            return
        if self.patterns:
            self._set_shared_xlim(xmin, xmax)
            self.reposition_labels()
            self.canvas.draw_idle()
            return
        ax = self._primary_ax()
        if ax:
            ax.set_xlim(xmin, xmax)
            self.canvas.draw_idle()

    def on_display_range_live_changed(self, _value):
        self.set_display_range(show_warning=False)

    def delete_current_selected_curve(self):
        if not self.patterns:
            return

        # Avoid accidental deletion while typing in text fields.
        fw = QApplication.focusWidget()
        if isinstance(fw, QLineEdit):
            return

        idx = self.selected_idx
        if idx is None or idx < 0 or idx >= len(self.patterns):
            row = self.files_list.currentRow() if self.files_list else -1
            if 0 <= row < len(self.patterns):
                idx = row
            else:
                return

        self._push_undo_snapshot()
        del self.patterns[idx]

        if not self.patterns:
            self.selected_idx = None
            self.refresh_files_list()
            self.clear_plot()
            return

        self.selected_idx = min(idx, len(self.patterns) - 1)
        if is_gradient(self._current_color_mode()):
            self._recolor_patterns()
        self.refresh_files_list()
        self.update_plot()

    # ---------------- Label Repositioning ---------------- #

    def reposition_labels(self):
        if not self.label_texts or not self.patterns:
            return

        frac_x = float(self.fixed_label_x_position)
        y_rel = float(self.fixed_label_y_position)
        y_scale = float(self.fixed_label_y_spacing)
        superimpose_mode = self._is_superimpose_mode()
        if superimpose_mode:
            ax = self._primary_ax()
            if ax is None:
                return

            entries = []
            for entry in self.label_texts:
                idx = int(entry.get("idx", -1))
                if 0 <= idx < len(self.patterns):
                    entries.append(entry)
            if not entries:
                return
            entries.sort(key=lambda e: int(e.get("idx", -1)))

            label_fs = int(self.curve_label_fs_spin.value()) if self.curve_label_fs_spin else 10
            max_chars = 1
            for entry in entries:
                idx = int(entry.get("idx", -1))
                if 0 <= idx < len(self.patterns):
                    max_chars = max(max_chars, len(to_plain(self.patterns[idx].get("label", ""))))

            ax_w_px = 800.0
            ax_h_px = 500.0
            try:
                renderer = self.canvas.get_renderer() if self.canvas else None
                bbox = ax.get_window_extent(renderer=renderer)
                ax_w_px = max(1.0, float(bbox.width))
                ax_h_px = max(1.0, float(bbox.height))
            except Exception:
                logger.debug("Suppressed error", exc_info=True)

            line_len = max(0.04, 48.0 / ax_w_px)
            line_gap = max(0.01, 8.0 / ax_w_px)
            # Add more vertical separation between labels in superimpose mode.
            row_step = max(0.036, (float(label_fs) + 14.0) / ax_h_px)
            module_h = row_step * max(0, len(entries) - 1)
            text_w_frac = max(0.08, min(0.62, (float(max_chars) * float(label_fs) * 0.62 + 14.0) / ax_w_px))

            x_anchor = min(max(frac_x, 0.0), 1.0)
            y_anchor = min(max(y_rel, 0.0), 1.0)

            gdx, gdy = self.global_label_offset
            gdx_min = (0.01 + line_len + line_gap) - x_anchor
            gdx_max = (0.99 - text_w_frac) - x_anchor
            if gdx_min <= gdx_max:
                gdx = min(max(gdx, gdx_min), gdx_max)
            else:
                gdx = gdx_min

            y_top_nominal = y_anchor
            y_bottom_nominal = y_anchor - module_h
            gdy_min = 0.01 - y_bottom_nominal
            gdy_max = 0.99 - y_top_nominal
            if gdy_min <= gdy_max:
                gdy = min(max(gdy, gdy_min), gdy_max)
            else:
                gdy = gdy_min

            self.global_label_offset = (gdx, gdy)
            x_text = x_anchor + gdx
            y_top = y_anchor + gdy

            for row_i, entry in enumerate(entries):
                idx = int(entry.get("idx", -1))
                label_text = self.patterns[idx]["label"] if (0 <= idx < len(self.patterns)) else entry["artist"].get_text()
                y_row = y_top - row_i * row_step

                entry["artist"].set_text(self._format_chemical_plot(label_text))
                entry["artist"].set_transform(ax.transAxes)
                entry["artist"].set_position((x_text, y_row))

                tag_line = entry.get("line_artist", None)
                if tag_line is not None:
                    x_right = x_text - line_gap
                    x_left = x_right - line_len
                    if 0 <= idx < len(self.patterns):
                        color = self.patterns[idx].get("color", None)
                        if color:
                            tag_line.set_color(color)
                    tag_line.set_transform(ax.transAxes)
                    tag_line.set_xdata([x_left, x_right])
                    tag_line.set_ydata([y_row, y_row])
                    tag_line.set_visible(True)
            return

        gdx, gdy = self.global_label_offset
        x_off_min, x_off_max = -float("inf"), float("inf")
        y_off_min, y_off_max = -float("inf"), float("inf")
        placements = []

        for entry in self.label_texts:
            ax = entry.get("ax") or self._primary_ax()
            if ax is None:
                continue
            # Keep non-superimpose labels locked in axis coordinates so zoom/scale does not move them.
            x_anchor = float(frac_x)
            y_anchor = float(y_rel * y_scale)
            x_min, x_max = 0.015, 0.985
            y_min, y_max = 0.10, 0.97

            if x_min < x_max:
                x_off_min = max(x_off_min, x_min - x_anchor)
                x_off_max = min(x_off_max, x_max - x_anchor)
            if y_min < y_max:
                y_off_min = max(y_off_min, y_min - y_anchor)
                y_off_max = min(y_off_max, y_max - y_anchor)

            placements.append((entry, x_anchor, y_anchor, x_min, x_max, y_min, y_max))

        if x_off_min <= x_off_max:
            gdx = min(max(gdx, x_off_min), x_off_max)
        if y_off_min <= y_off_max:
            gdy = min(max(gdy, y_off_min), y_off_max)

        self.global_label_offset = (gdx, gdy)

        for entry, x_anchor, y_anchor, x_min, x_max, y_min, y_max in placements:
            x_pos = x_anchor + gdx
            y_pos = y_anchor + gdy
            if x_min < x_max:
                x_pos = min(max(x_pos, x_min), x_max)
            if y_min < y_max:
                y_pos = min(max(y_pos, y_min), y_max)
            ax = entry.get("ax") or self._primary_ax()
            if ax is not None:
                entry["artist"].set_transform(ax.transAxes)
            entry["artist"].set_position((x_pos, y_pos))
            tag_line = entry.get("line_artist", None)
            if tag_line is not None:
                tag_line.set_visible(False)

    def on_toggle_curve_labels(self, checked):
        self.show_curve_labels = bool(checked)
        self.update_plot(preserve_view=True)

    def on_superimpose_toggled(self, _state):
        self.update_plot(preserve_view=True)

    # ---------------- Plot Logic ---------------- #

    def update_plot(self, preserve_view=False):
        if not self.patterns:
            return
        gap = float(self.fixed_y_headroom)
        curve_label_fs = int(self.curve_label_fs_spin.value())
        tick_fs = int(self.tick_fs_spin.value())
        axis_label_fs = int(self.axis_label_fs_spin.value())
        title_fs = self.title_font_size
        curve_lw = float(self.curve_lw_spin.value())
        frame_lw = float(self.frame_lw_spin.value())
        curve_y_offset = float(self.curve_offset_spin.value())
        self.fig.set_size_inches(self.fig_w_spin.value(), self.fig_h_spin.value(), forward=True)

        old_xlim = old_ylim = None
        old_ax = self._primary_ax()
        if preserve_view and old_ax:
            try:
                old_xlim = old_ax.get_xlim()
                old_ylim = old_ax.get_ylim()
            except Exception:
                logger.debug("Suppressed error", exc_info=True)
                old_xlim = old_ylim = None

        self.fig.clear()
        self.axes = []
        self.label_texts = []
        self.plot_lines = []
        self.cursor_vlines = []
        self.cursor_text = None
        self.zoom_start_vlines = []
        self.zoom_end_vlines = []
        self.analysis_artists = []
        self.press_event = None
        self.pan_event = None

        y_list = []
        if self._norm_mode() != "none":
            for p in self.patterns:
                m = np.max(p["y"])
                y_list.append(p["y"] / m if m > 0 else p["y"])
        else:
            y_list = [p["y"] for p in self.patterns]

        scaled_y_list = []
        y_base_list = []
        min_curve_value = None
        max_curve_value = None
        for p, y_proc in zip(self.patterns, y_list):
            y_base = np.asarray(y_proc) * float(p.get("scale", 1.0)) + curve_y_offset
            y_shift = float(p.get("y_shift", 0.0))
            y_scaled = y_base + y_shift
            y_base_list.append(y_base)
            scaled_y_list.append(y_scaled)
            if y_scaled.size == 0:
                continue
            y_min = float(np.min(y_scaled))
            y_max = float(np.max(y_scaled))
            min_curve_value = y_min if min_curve_value is None else min(min_curve_value, y_min)
            max_curve_value = y_max if max_curve_value is None else max(max_curve_value, y_max)

        if min_curve_value is None or max_curve_value is None:
            min_curve_value, max_curve_value = 0.0, 1.0

        y_bottom = min(0.0, min_curve_value)
        y_top = max_curve_value
        if y_top <= y_bottom:
            y_top = y_bottom + 1.0
        span = y_top - y_bottom
        if gap > 0:
            y_top = y_bottom + span * gap
        if y_top <= y_bottom:
            y_top = y_bottom + max(span, 1.0)


        axes = []
        n = len(self.patterns)
        superimpose_mode = self._is_superimpose_mode()
        preview_info = self._powerxrd_preview_data if isinstance(self._powerxrd_preview_data, dict) else None
        grid = None
        if preview_info:
            if superimpose_mode:
                grid = self.fig.add_gridspec(2, 1, height_ratios=[1.1, 1.6], hspace=0.03)
            else:
                main_row_weight = 1.6
                height_ratios = [1.1] + [main_row_weight] * n
                grid = self.fig.add_gridspec(n + 1, 1, height_ratios=height_ratios, hspace=0.03)
            self.preview_ax = self.fig.add_subplot(grid[0, 0])
        else:
            self.preview_ax = None

        plot_axes_for_pattern = []
        if superimpose_mode:
            if grid is None:
                main_ax = self.fig.add_subplot(111)
            else:
                main_ax = self.fig.add_subplot(grid[1, 0])
            # Keep axis background transparent so overflow curves are not hidden by neighboring subplot patches.
            main_ax.set_facecolor("none")
            main_ax.patch.set_visible(False)
            axes.append(main_ax)
            plot_axes_for_pattern = [main_ax] * n
        else:
            for i in range(n):
                shared_ax = axes[0] if axes else None
                if grid is None:
                    ax = self.fig.add_subplot(
                        n, 1, i + 1,
                        sharex=shared_ax,
                        sharey=shared_ax,
                    )
                else:
                    ax = self.fig.add_subplot(
                        grid[i + 1, 0],
                        sharex=shared_ax,
                        sharey=shared_ax,
                    )
                # Keep axis background transparent so overflow curves are not hidden by neighboring subplot patches.
                ax.set_facecolor("none")
                ax.patch.set_visible(False)
                axes.append(ax)
                plot_axes_for_pattern.append(ax)

        for i, (p, y_scaled, y_base, ax) in enumerate(zip(self.patterns, scaled_y_list, y_base_list, plot_axes_for_pattern)):
            color = p.get("color")
            line, = ax.plot(p["x"], y_scaled, color=color, linewidth=curve_lw, clip_on=False)
            self.plot_lines.append({
                "line": line,
                "x": p["x"],
                "y": y_scaled,
                "y_base": y_base,
                "label": p["label"],
                "ax": ax,
            })

            if self.show_curve_labels:
                if superimpose_mode:
                    txt = ax.text(
                        0,
                        0,
                        self._format_chemical_plot(p["label"]),
                        transform=ax.transAxes,
                        fontsize=curve_label_fs,
                        color="#1f1f1f",
                        va="center",
                        ha="left",
                        clip_on=False,
                        zorder=9,
                    )
                    tag_line, = ax.plot(
                        [0.0, 0.0],
                        [0.0, 0.0],
                        transform=ax.transAxes,
                        color=color if color else "#000000",
                        linewidth=max(2.0, curve_lw + 0.4),
                        solid_capstyle="round",
                        clip_on=False,
                        zorder=10,
                    )
                    self.label_texts.append({"artist": txt, "line_artist": tag_line, "idx": i, "ax": ax})
                else:
                    txt = ax.text(
                        0,
                        0,
                        self._format_chemical_plot(p["label"]),
                        transform=ax.transAxes,
                        fontsize=curve_label_fs,
                        va="center",
                        ha="left",
                        clip_on=False,
                        zorder=8,
                    )
                    self.label_texts.append({"artist": txt, "line_artist": None, "idx": i, "ax": ax})

        self.axes = axes
        self.ax = self._primary_ax()
        self._apply_figure_layout()

        if preview_info and self.preview_ax is not None:
            p_ax = self.preview_ax
            nx = np.asarray(preview_info.get("new_x", []), dtype=np.float64)
            ny = np.asarray(preview_info.get("new_y", []), dtype=np.float64)

            if self._norm_mode() != "none":
                if ny.size > 0:
                    m1 = float(np.max(ny))
                    if m1 > 0:
                        ny = ny / m1

            p_ax.set_facecolor("#f7f9fc")
            p_ax.patch.set_alpha(1.0)
            if nx.size > 0 and ny.size == nx.size:
                p_ax.plot(nx, ny, color="#d32f2f", linewidth=1.2, alpha=0.95, label="Adjusted")

            peak_info = preview_info.get("peak")
            if isinstance(peak_info, dict):
                peak_x = peak_info.get("peak_x")
                left_x = peak_info.get("left_x")
                right_x = peak_info.get("right_x")
                if peak_x is not None and np.isfinite(float(peak_x)):
                    p_ax.axvline(float(peak_x), color="#d32f2f", linestyle="--", linewidth=1.0, alpha=0.9)
                if left_x is not None and right_x is not None:
                    lx = float(left_x)
                    rx = float(right_x)
                    if np.isfinite(lx) and np.isfinite(rx):
                        y_lo, y_hi = p_ax.get_ylim()
                        y_mid = y_lo + 0.6 * (y_hi - y_lo)
                        p_ax.hlines(y_mid, lx, rx, colors="#1565c0", linestyles="-", linewidth=1.1)

            title = str(preview_info.get("title", "Preview"))
            p_ax.set_title(title, fontsize=max(8, tick_fs - 1))
            p_ax.set_xticks([])
            p_ax.set_yticks([])
            p_ax.tick_params(
                axis="both",
                which="both",
                bottom=False,
                top=False,
                left=False,
                right=False,
                labelbottom=False,
                labelleft=False,
            )
            for spine in p_ax.spines.values():
                spine.set_visible(False)
            if p_ax.lines:
                p_ax.legend(loc="upper right", fontsize=max(7, tick_fs - 3), frameon=False)

        axes_for_ticks = self._plot_axes()
        for i, ax in enumerate(axes_for_ticks):
            ax.tick_params(axis="x", which="both", labelsize=tick_fs)
            ax.tick_params(axis="y", which="both", labelsize=tick_fs)
            ax.yaxis.set_ticks([])
            ax.set_ylim(y_bottom, y_top, emit=False)
            if (not superimpose_mode) and i < (len(axes_for_ticks) - 1):
                ax.tick_params(axis="x", which="both", labelbottom=False, bottom=False)

        self.title_artist = (
            self.ax.set_title(self._format_chemical_plot(self.title_text), fontsize=title_fs)
            if self.ax else None
        )
        axes_for_label = self._plot_axes()
        bottom_ax = axes_for_label[-1] if axes_for_label else None
        self.x_label_artist = (
            bottom_ax.set_xlabel(self._format_chemical_plot(self.x_axis_label_text), fontsize=axis_label_fs)
            if bottom_ax else None
        )
        if axes_for_label:
            top_pos = axes_for_label[0].get_position()
            bottom_pos = axes_for_label[-1].get_position()
            y_center = 0.5 * (top_pos.y1 + bottom_pos.y0)
            width_pt = float(self.fig.get_size_inches()[0]) * 72.0
            x_pos = max(0.005, top_pos.x0 - self._y_label_offset_pt() / width_pt)
            self.y_label_artist = self.fig.text(
                x_pos,
                y_center,
                self._format_chemical_plot(self.y_axis_label_text),
                fontsize=axis_label_fs,
                rotation=90,
                ha="center",
                va="center",
            )
        else:
            self.y_label_artist = None

        # Frame line width uses frame_lw only
        self.apply_frame_style(frame_lw)

        primary_ax = self._primary_ax()
        if primary_ax is not None:
            self.xlim_full, self.ylim_full = primary_ax.get_xlim(), primary_ax.get_ylim()

        if preserve_view and old_xlim is not None and old_ylim is not None:
            try:
                self._set_shared_xlim(old_xlim[0], old_xlim[1])
                if self._norm_mode() != "visible":
                    self._set_shared_ylim(old_ylim[0], old_ylim[1])
            except Exception:
                logger.debug("Suppressed error", exc_info=True)
        else:
            xmin = float(self.xmin_spin.value()) if self.xmin_spin else 2.0
            xmax = float(self.xmax_spin.value()) if self.xmax_spin else 30.0
            if xmin < xmax:
                self._set_shared_xlim(xmin, xmax)

        self._apply_horizontal_clip_only()

        # Re-apply once for stability
        self.apply_frame_style(frame_lw)
        self._redraw_applied_peak_overlay()

        for ax in self._plot_axes():
            self.cursor_vlines.append(
                ax.axvline(2, color="#666666", linestyle="--", linewidth=0.8, alpha=0.8, visible=False)
            )
        self.cursor_text = self.fig.text(
            0.012, 0.988, "X=2.00",
            transform=self.fig.transFigure,
            ha="left", va="top",
            fontsize=10, color="#444444",
            visible=False
        )

        self.reposition_labels()
        self.apply_selection_highlight()
        self.canvas.draw_idle()
        self.refresh_files_list()

    def clear_plot(self):
        self.xlim_full = self.ylim_full = None
        self._build_empty_plot()
        self.canvas.draw()

    # ---------------- Mouse Wheel: Ctrl zooms canvas view; plain wheel scales selected curve ---------------- #

    def on_scroll(self, event):
        step = getattr(event, "step", 0)
        if step == 0:
            return

        modifiers = QApplication.keyboardModifiers()
        if bool(modifiers & Qt.KeyboardModifier.ControlModifier):
            old_scale = float(getattr(self, "canvas_view_scale", 1.0))
            new_scale = old_scale * (1.08 ** float(step))
            self._set_canvas_view_scale(new_scale)
            return

        if not self._is_plot_axis(event.inaxes):
            return

        if not self.patterns:
            return

        self._push_undo_snapshot()
        factor = 1.10 if step > 0 else (1.0 / 1.10)
        if self.selected_idx is not None and 0 <= self.selected_idx < len(self.patterns):
            p = self.patterns[self.selected_idx]
            s0 = float(p.get("scale", 1.0))
            s1 = s0 * factor
            s1 = max(0.02, min(200.0, s1))
            p["scale"] = s1
        else:
            for p in self.patterns:
                s0 = float(p.get("scale", 1.0))
                s1 = s0 * factor
                p["scale"] = max(0.02, min(200.0, s1))

        self._update_scaled_curves()

    # ---------------- Interaction Events ---------------- #

    def _update_scaled_curves(self):
        """Scale existing artists without rebuilding axes or changing the view."""
        if len(self.plot_lines) != len(self.patterns):
            self.update_plot(preserve_view=True)
            return
        bounds = []
        for p, info in zip(self.patterns, self.plot_lines):
            y = np.asarray(p["y"])
            maximum = float(np.max(y)) if self._norm_mode() != "none" else 1.0
            divisor = maximum if maximum > 0 else 1.0
            base = y / divisor * float(p.get("scale", 1.0)) + self.curve_offset_spin.value()
            drawn = base + float(p.get("y_shift", 0.0))
            info["y_base"], info["y"] = base, drawn
            info["line"].set_ydata(drawn)
            bounds.extend((float(np.min(drawn)), float(np.max(drawn))))
        bottom = min(0.0, min(bounds))
        top = max(bounds)
        span = top - bottom if top > bottom else 1.0
        gap = float(self.fixed_y_headroom)
        self.ylim_full = (bottom, bottom + span * (gap if gap > 0 else 1.0))
        if self._norm_mode() == "visible":
            self._apply_live_normalization()
        else:
            self._redraw_applied_peak_overlay()
            self.reposition_labels()
        self.refresh_files_list()
        self.canvas.draw_idle()

    def canvas_draw_idle(self):
        self.canvas.draw_idle()

    def dragEnterEvent(self, event):
        if event.mimeData() and event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        files = self._paths_from_mime(event.mimeData())
        if files:
            self.queue_patterns_from_paths(files)
            event.acceptProposedAction()
        else:
            event.ignore()

    def on_mouse_press(self, event):
        # Allow double-click edits first; if the click lands on empty plot space, restore the full view.
        if event.button == 1 and event.dblclick:
            if self.handle_label_double_click(event):
                return
            if self.handle_double_click(event):
                return
            if self._is_plot_axis(event.inaxes):
                self._reset_full_view()
                return

        if not self._is_plot_axis(event.inaxes):
            return

        if event.button == 2:
            self._push_undo_snapshot()
            self.pan_event = event
            self.pan_xlim_start = event.inaxes.get_xlim()
            return

        if event.button == 1:

            for i, info in enumerate(self.plot_lines):
                if info["line"].contains(event)[0]:
                    if self._is_superimpose_mode() and event.ydata is not None:
                        self.dragging_curve_idx = i
                        self.drag_curve_axis = event.inaxes
                        self.drag_curve_start_ydata = float(event.ydata)
                        self.drag_curve_start_shift = float(self.patterns[i].get("y_shift", 0.0))
                        self.drag_curve_moved = False
                        self.selected_idx = i
                        if self.files_list and 0 <= i < self.files_list.count():
                            self.files_list.setCurrentRow(i)
                        self.apply_selection_highlight()
                        return

                    if self.selected_idx == i:
                        self.selected_idx = None
                        if self.files_list:
                            self.files_list.clearSelection()
                            self.files_list.setCurrentRow(-1)
                    else:
                        self.selected_idx = i
                        if self.files_list and 0 <= i < self.files_list.count():
                            self.files_list.setCurrentRow(i)
                    self.apply_selection_highlight()
                    return

            for entry in self.label_texts:
                hit_label = entry["artist"].contains(event)[0]
                tag_line = entry.get("line_artist", None)
                hit_tag_line = bool(tag_line is not None and tag_line.contains(event)[0])
                if hit_label or hit_tag_line:
                    ax_for_drag = entry.get("ax") or event.inaxes
                    if not self._is_plot_axis(ax_for_drag):
                        return
                    self._push_undo_snapshot()
                    self.dragging_label = True
                    self.drag_label_axis = ax_for_drag
                    if event.x is None or event.y is None:
                        self.dragging_label = False
                        return
                    try:
                        px, py = ax_for_drag.transAxes.inverted().transform((event.x, event.y))
                        self.drag_start_data = (float(px), float(py))
                    except Exception:
                        logger.debug("Suppressed error", exc_info=True)
                        self.dragging_label = False
                        return
                    return

            if self.selected_idx is not None:
                self.selected_idx = None
                if self.files_list:
                    self.files_list.clearSelection()
                    self.files_list.setCurrentRow(-1)
                self.apply_selection_highlight()

            if event.xdata is None:
                return
            self.press_event = event
            self._show_zoom_guides(float(event.xdata), float(event.xdata))
            if self.cursor_vlines:
                for vline in self.cursor_vlines:
                    vline.set_visible(False)
            if self.cursor_text:
                self.cursor_text.set_visible(False)
            self.canvas.draw_idle()

    def on_mouse_move(self, event):
        if self.dragging_curve_idx is not None:
            if not self._is_superimpose_mode():
                self.dragging_curve_idx = None
                self.drag_curve_axis = None
                self.drag_curve_start_ydata = None
                self.drag_curve_start_shift = None
                self.drag_curve_moved = False
                return

            idx = int(self.dragging_curve_idx)
            if idx < 0 or idx >= len(self.patterns):
                self.dragging_curve_idx = None
                return
            if event.inaxes != self.drag_curve_axis or event.ydata is None:
                return
            if self.drag_curve_start_ydata is None or self.drag_curve_start_shift is None:
                return

            dy = float(event.ydata) - float(self.drag_curve_start_ydata)
            new_shift = float(self.drag_curve_start_shift) + dy
            old_shift = float(self.patterns[idx].get("y_shift", 0.0))
            if 0 <= idx < len(self.plot_lines):
                info = self.plot_lines[idx]
                y_base = np.asarray(info.get("y_base", []), dtype=np.float64)
                x_base = np.asarray(info.get("x", []), dtype=np.float64)
                ax = info.get("ax")
                if y_base.size and ax is not None:
                    y0, y1 = ax.get_ylim()
                    if np.isfinite(y0) and np.isfinite(y1):
                        lower_bound = min(y0, y1)
                        upper_bound = max(y0, y1)
                        # Use current visible X-range for drag limits; fall back to full curve if needed.
                        y_for_bounds = y_base
                        if x_base.size == y_base.size:
                            x0, x1 = ax.get_xlim()
                            if np.isfinite(x0) and np.isfinite(x1):
                                x_lo = min(x0, x1)
                                x_hi = max(x0, x1)
                                visible_mask = (
                                    np.isfinite(x_base)
                                    & np.isfinite(y_base)
                                    & (x_base >= x_lo)
                                    & (x_base <= x_hi)
                                )
                                if np.any(visible_mask):
                                    y_for_bounds = y_base[visible_mask]

                        y_for_bounds = y_for_bounds[np.isfinite(y_for_bounds)]
                        if y_for_bounds.size:
                            min_shift_allowed = lower_bound - float(np.min(y_for_bounds))
                            max_shift_allowed = upper_bound - float(np.max(y_for_bounds))
                            if min_shift_allowed <= max_shift_allowed:
                                new_shift = min(max(new_shift, min_shift_allowed), max_shift_allowed)
                            else:
                                new_shift = min_shift_allowed

            if not self.drag_curve_moved and abs(new_shift - old_shift) > 1e-12:
                self._push_undo_snapshot()
                self.drag_curve_moved = True
            self.patterns[idx]["y_shift"] = new_shift

            if 0 <= idx < len(self.plot_lines):
                info = self.plot_lines[idx]
                y_base = np.asarray(info.get("y_base", []), dtype=np.float64)
                if y_base.size:
                    y_draw = y_base + new_shift
                    info["y"] = y_draw
                    info["line"].set_ydata(y_draw)

            self.reposition_labels()
            self._redraw_applied_peak_overlay()
            self.canvas.draw_idle()
            return

        if self.press_event is not None:
            x0 = self.press_event.xdata
            if x0 is not None and event.xdata is not None:
                self._show_zoom_guides(float(x0), float(event.xdata))
            if self.cursor_vlines:
                for vline in self.cursor_vlines:
                    vline.set_visible(False)
            if self.cursor_text:
                self.cursor_text.set_visible(False)
            self.canvas.draw_idle()
            return

        if self.cursor_vlines and self.cursor_text:
            if self._is_plot_axis(event.inaxes) and event.xdata is not None:
                x_cur = float(event.xdata)
                for vline in self.cursor_vlines:
                    vline.set_xdata([x_cur, x_cur])
                    vline.set_visible(True)
                self.cursor_text.set_text(f"X={x_cur:.2f}")
                self.cursor_text.set_visible(True)
            else:
                for vline in self.cursor_vlines:
                    vline.set_visible(False)
                self.cursor_text.set_visible(False)

        if self.dragging_label:
            if event.inaxes != self.drag_label_axis:
                self.drag_start_data = None
                return

            if event.x is None or event.y is None:
                self.drag_start_data = None
                return
            try:
                px, py = self.drag_label_axis.transAxes.inverted().transform((event.x, event.y))
                x_now = float(px)
                y_now = float(py)
            except Exception:
                logger.debug("Suppressed error", exc_info=True)
                self.drag_start_data = None
                return

            if self.drag_start_data is None:
                self.drag_start_data = (x_now, y_now)
                return

            dx = x_now - self.drag_start_data[0]
            dy = y_now - self.drag_start_data[1]
            ox, oy = self.global_label_offset
            self.global_label_offset = (ox + dx, oy + dy)
            self.reposition_labels()

            # Use incremental dragging to avoid hidden offset accumulation near bounds.
            self.drag_start_data = (x_now, y_now)

            self.canvas.draw_idle()
            return

        if self.pan_event and event.xdata is not None:
            dx = event.xdata - self.pan_event.xdata
            x0, x1 = self.pan_xlim_start
            self._set_shared_xlim(x0 - dx, x1 - dx)
            self.reposition_labels()
            self.canvas.draw_idle()
            return

        # Disable hover tooltip box while keeping cursor/zoom interactions.
        if not self._cursor_draw_timer.isActive():
            self._cursor_draw_timer.start()

    def on_mouse_release(self, event):
        self.dragging_curve_idx = None
        self.drag_curve_axis = None
        self.drag_curve_start_ydata = None
        self.drag_curve_start_shift = None
        self.drag_curve_moved = False

        self.dragging_label = False
        self.drag_start_data = None
        self.drag_label_axis = None

        self.pan_event = None
        if self.press_event:
            x0, x1 = self.press_event.xdata, event.xdata
            if x0 is not None and x1 is not None and abs(x1 - x0) > 0.01:
                self._push_undo_snapshot()
                self._set_shared_xlim(min(x0, x1), max(x0, x1))
            self._clear_zoom_guides()
            self.reposition_labels()
            self.canvas.draw_idle()
        self.press_event = None

    # ---------------- Double-Click: Edit Curve Label ---------------- #

    def handle_label_double_click(self, event):
        for entry in self.label_texts:
            art = entry["artist"]
            tag_line = entry.get("line_artist", None)
            hit_tag_line = bool(tag_line is not None and tag_line.contains(event)[0])
            if art.contains(event)[0] or hit_tag_line:
                idx = entry["idx"]
                old = self.patterns[idx]["label"] if 0 <= idx < len(self.patterns) else art.get_text()
                new_t, ok = FormattedTextDialog.get_text(self, "Edit label", "Label text:", old)
                if ok:
                    new_t = (new_t or "").strip()
                    if new_t == "":
                        new_t = ""
                    if new_t != old:
                        self._push_undo_snapshot()
                    art.set_text(self._format_chemical_plot(new_t))
                    self.patterns[idx]["label"] = new_t
                    if 0 <= idx < len(self.plot_lines):
                        self.plot_lines[idx]["label"] = new_t
                    self.refresh_files_list()
                    self.canvas.draw_idle()
                return True
        return False

    # ---------------- Double-Click: Title / Axis Labels / Color ---------------- #

    def _is_event_near_artist(self, event, artist, pad_px=8):
        if event is None or artist is None:
            return False
        if event.x is None or event.y is None:
            return False
        try:
            renderer = self.canvas.get_renderer()
            bbox = artist.get_window_extent(renderer=renderer).expanded(1.0, 1.0)
            return (
                (bbox.x0 - pad_px) <= event.x <= (bbox.x1 + pad_px)
                and (bbox.y0 - pad_px) <= event.y <= (bbox.y1 + pad_px)
            )
        except Exception:
            logger.debug("Suppressed error", exc_info=True)
            return False

    def handle_double_click(self, event):
        for art in [self.x_label_artist, self.y_label_artist, self.title_artist]:
            if art is None:
                continue
            if art.contains(event)[0] or self._is_event_near_artist(event, art, pad_px=10):
                old_t = art.get_text()
                if art == self.title_artist:
                    old_t = self.title_text
                elif art == self.x_label_artist:
                    old_t = self.x_axis_label_text
                elif art == self.y_label_artist:
                    old_t = self.y_axis_label_text
                new_t, ok = FormattedTextDialog.get_text(self, "Edit text", "Content:", old_t)
                if ok:
                    if new_t != old_t:
                        self._push_undo_snapshot()
                    art.set_text(self._format_chemical_plot(new_t))
                    if art == self.title_artist:
                        self.title_text = new_t
                    elif art == self.x_label_artist:
                        self.x_axis_label_text = new_t
                    elif art == self.y_label_artist:
                        self.y_axis_label_text = new_t

                self.canvas.draw_idle()
                return True

        for i, info in enumerate(self.plot_lines):
            if info["line"].contains(event)[0]:
                color = self._pick_curve_color(self.patterns[i].get("color") or "#000000")
                if color is not None:
                    c_hex = color.name()
                    if c_hex != self.patterns[i].get("color"):
                        self._push_undo_snapshot()
                    info["line"].set_color(c_hex)
                    self.patterns[i]["color"] = c_hex
                    for entry in self.label_texts:
                        if int(entry.get("idx", -1)) == i:
                            tag_line = entry.get("line_artist", None)
                            if tag_line is not None:
                                tag_line.set_color(c_hex)
                    self.refresh_files_list()
                    self.canvas.draw_idle()
                return True
        return False

    # ---------------- Export ---------------- #

    def export_csv(self):
        if not self.patterns:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export CSV", self.project_dir, "CSV (*.csv)")
        if not path:
            return
        try:
            # Export raw points per curve without interpolation.
            curves = []
            max_points = 0
            for idx, p in enumerate(self.patterns, start=1):
                x_arr = np.asarray(p.get("x", []), dtype=np.float64).reshape(-1)
                y_arr = np.asarray(p.get("y", []), dtype=np.float64).reshape(-1)
                n_points = int(min(x_arr.size, y_arr.size))
                if n_points <= 0:
                    continue
                if x_arr.size != n_points:
                    x_arr = x_arr[:n_points]
                if y_arr.size != n_points:
                    y_arr = y_arr[:n_points]

                label = to_plain(p.get("label", f"curve_{idx}"))
                curves.append((idx, label, x_arr, y_arr))
                max_points = max(max_points, n_points)

            if not curves:
                QMessageBox.warning(self, "Export CSV", "No valid curve data to export.")
                return

            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                header = []
                for idx, label, _x_arr, _y_arr in curves:
                    base = f"{idx}:{label}"
                    header.extend([f"{base}:2theta", f"{base}:intensity"])
                writer.writerow(header)

                for i in range(max_points):
                    row = []
                    for _idx, _label, x_arr, y_arr in curves:
                        if i < x_arr.size:
                            row.extend([float(x_arr[i]), float(y_arr[i])])
                        else:
                            row.extend(["", ""])
                    writer.writerow(row)
            QMessageBox.information(self, "Success", "CSV exported (raw points per curve).")
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def export_svg(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export SVG", self.project_dir, "SVG (*.svg)")
        if not path:
            return
        try:
            bg_mode = self._current_svg_background()
            svg_bytes = self._make_svg_bytes(background_mode=bg_mode)
            with open(path, "wb") as f:
                f.write(svg_bytes)
            bg_text = "transparent" if bg_mode == "transparent" else "white"
            QMessageBox.information(self, "Success", f"SVG exported ({bg_text} background).")
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def export_png(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export PNG", self.project_dir, "PNG (*.png)")
        if not path:
            return
        if not path.lower().endswith(".png"):
            path += ".png"
        try:
            bg_mode = self._current_svg_background()
            png_bytes = self._make_png_bytes(background_mode=bg_mode)
            with open(path, "wb") as f:
                f.write(png_bytes)
            bg_text = "transparent" if bg_mode == "transparent" else "white"
            QMessageBox.information(self, "Success", f"PNG exported ({bg_text} background).")
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def closeEvent(self, event):
        self._import_timer.stop()
        self._browser_search_timer.stop()
        self._cancel_browser_search()
        self._cursor_draw_timer.stop()
        # QtAgg queues a zero-delay draw; cancel it before Qt deletes the canvas.
        self.canvas._draw_pending = False
        self._cleanup_clipboard_temp_png()
        super().closeEvent(event)


def configure_logging():
    """Log warnings to %LOCALAPPDATA%/xStack/xstack.log; XSTACK_DEBUG=1 adds suppressed errors."""
    from logging.handlers import RotatingFileHandler
    log_dir = os.path.join(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(), "xStack")
    try:
        os.makedirs(log_dir, exist_ok=True)
        handler = RotatingFileHandler(os.path.join(log_dir, "xstack.log"), maxBytes=1_000_000,
                                      backupCount=2, encoding="utf-8")
    except OSError:
        handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if os.environ.get("XSTACK_DEBUG") else logging.WARNING)
    if xstack_richtext.script_metrics_error:
        logger.warning("%s; labels use matplotlib's default script placement",
                       xstack_richtext.script_metrics_error)
    else:
        logger.debug("Script metrics calibrated for word-processor sub/superscripts")


if __name__ == "__main__":
    configure_logging()
    startup_paths = [os.path.abspath(path) for path in sys.argv[1:]]
    app = QApplication(sys.argv)
    file_service = FileOpenService()
    try:
        if not file_service.start_or_forward(startup_paths):
            sys.exit(0)
    except RuntimeError as exc:
        QMessageBox.critical(None, "xStack", str(exc))
        sys.exit(1)
    app.aboutToQuit.connect(file_service.close)
    app_icon_path = resolve_app_icon_path()
    if app_icon_path:
        app.setWindowIcon(QIcon(app_icon_path))

    splash = None
    splash_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "starting_fig.png")
    if not startup_paths and os.path.isfile(splash_path):
        splash_pixmap = QPixmap(splash_path)
        if not splash_pixmap.isNull():
            # Keep the full 3x artwork. Downsampling to logical pixels here
            # would force Qt to enlarge a low-resolution image on HiDPI screens.
            splash_pixmap.setDevicePixelRatio(3.0)
            splash = ClickableSplashScreen(
                splash_pixmap,
                "https://orcid.org/0000-0001-9846-8127",
            )
            splash.show()
            app.processEvents()

    def _start_main_window():
        window = PXRDMultiCompareApp()
        window.show()
        if splash is not None:
            splash.finish(window)
        app.main_window = window
        file_service.attach_window(window)

    QTimer.singleShot(2000 if splash is not None else 0, _start_main_window)
    sys.exit(app.exec())

