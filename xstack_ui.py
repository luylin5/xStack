"""Presentation-only builders and shared Chemary-inspired Qt styling.

Keep toolbar and splitter order stable; the inspector follows the approved compact design.
Scientific data, plotting and export behavior belong to the main window.
"""
from PyQt6.QtCore import Qt, pyqtSignal, QSize
from PyQt6.QtGui import QFont, QShortcut, QKeySequence, QIcon, QPixmap, QPainter, QColor
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QLineEdit,
    QCheckBox, QComboBox, QGroupBox, QGridLayout, QSpinBox, QDoubleSpinBox,
    QAbstractItemView, QSizePolicy, QScrollArea, QSplitter, QButtonGroup,
)
from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas

# Shared surface, text and interaction rules, following Chemary's Qt UI.
# Scope these rules to this window; matplotlib figure/export colors are untouched.
STYLESHEET = """
QMainWindow, QDialog, QWidget#workspace { background: #f4f6f8; color: #1f2a37; }
QLabel, QCheckBox, QGroupBox { color: #263344; }
QLabel[role="muted"] { color: #657282; }
QGroupBox {
    background: #fbfcfd; border: 1px solid #d8e0e7; border-radius: 6px;
    margin-top: 9px; padding-top: 7px; font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin; left: 10px; padding: 0 4px;
    background: #f4f6f8; color: #263344;
}
QPushButton {
    background: #f8fafc; color: #1f2a37; border: 1px solid #b8c6d4;
    border-radius: 4px; padding: 3px 7px;
}
QPushButton:hover { background: #eef4f8; border-color: #8fb0c7; }
QPushButton:pressed { background: #dbe8f0; }
QPushButton:checked { background: #e4eff6; border-color: #8fb0c7; color: #245a7b; }
QPushButton:focus { border-color: #2f6f95; }
QPushButton:disabled { color: #8a969f; background: #eef1f3; border-color: #d6dee6; }
QPushButton[role="primary"] { background: #2f6f95; border-color: #2f6f95; color: white; }
QPushButton[role="primary"]:hover { background: #245a7b; }
QPushButton[role="primary"]:pressed { background: #1e4964; }
QPushButton[role="danger"]:hover { background: #fff1ef; border-color: #d9a39d; color: #a73529; }
QLineEdit, QComboBox {
    border: 1px solid #cfd8e3; border-radius: 4px;
    background: white; color: #1f2a37; padding: 2px 4px;
    selection-background-color: #2f6f95; selection-color: white;
}
QLineEdit:focus, QComboBox:focus {
    border-color: #2f6f95;
}
QComboBox QAbstractItemView {
    background: white; color: #1f2a37; selection-background-color: #2f6f95;
    selection-color: white; border: 1px solid #cfd8e3;
}
QTreeWidget, QListWidget {
    background: white; alternate-background-color: #f5f8fa; color: #1f2a37;
    border: 1px solid #d8e0e7; border-radius: 4px;
    selection-background-color: #2f6f95; selection-color: white;
}
QTreeWidget::item:hover, QListWidget::item:hover { background: #eef4f8; }
QTreeWidget::item:selected, QListWidget::item:selected { background: #2f6f95; color: white; }
QHeaderView::section {
    background: #f0f4f7; color: #506070; border: none;
    border-bottom: 1px solid #d8e0e7; padding: 4px;
}
QSplitter::handle { background: #e4ebf1; }
QSplitter::handle:hover { background: #8fb0c7; }
QScrollArea#plotViewport { background: #eef1f4; border: none; }
QWidget#plotCanvas { background: white; border: 1px solid #d8e0e7; }
QLabel#scaleOverlay {
    background: rgba(255,255,255,225); color: #506070;
    border: 1px solid #cfd8e3; border-radius: 4px; padding: 2px 6px;
}
QWidget#inspector { background: #fbfcfd; }
QScrollArea#inspectorScroll { border: 1px solid #d8e0e7; border-radius: 5px; background: #fbfcfd; }
QScrollBar:vertical {
    background: transparent; width: 9px; margin: 3px 2px 3px 0;
}
QScrollBar::handle:vertical {
    background: #c3ced8; min-height: 28px; border-radius: 3px;
}
QScrollBar::handle:vertical:hover { background: #97a8b7; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: transparent; border: none; height: 0;
}
QScrollBar:horizontal {
    background: transparent; height: 9px; margin: 0 3px 2px 3px;
}
QScrollBar::handle:horizontal {
    background: #c3ced8; min-width: 28px; border-radius: 3px;
}
QScrollBar::handle:horizontal:hover { background: #97a8b7; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal,
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
    background: transparent; border: none; width: 0;
}
QWidget#inspectorSection { border-top: 1px solid #e1e7ee; }
QLabel[role="heading"] { font-weight: 600; color: #263344; }
QLabel#patternCount { background: #e4edf4; color: #36586e; border-radius: 8px; padding: 1px 7px; }
QListWidget#patternList {
    border: 1px solid #cfd8e3; border-radius: 0; background: white;
    selection-background-color: #e4eff8; selection-color: #23485f;
}
QListWidget#patternList::item { border-bottom: 1px solid #edf1f5; }
QListWidget#patternList::item:selected { background: #e4eff8; color: #23485f; }
QWidget#inspector QPushButton { padding: 2px 6px; }
QPushButton[role="segment"] { border-radius: 3px; }
QPushButton[role="segment"]:checked { color: white; background: #2f6f95; border-color: #2f6f95; }
QPushButton[role="quietDanger"] { background: transparent; border-color: transparent; color: #657282; }
QPushButton[role="quietDanger"]:hover { color: #a73529; background: #fff1ef; border-color: #efd1cd; }
QToolTip { background: #ffffff; color: #263344; border: 1px solid #cfd8e3; padding: 4px; }
"""


class InspectorSection(QWidget):
    """A permanently visible inspector section with a plain heading."""
    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.setObjectName("inspectorSection")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(5)
        heading = QLabel(title)
        heading.setProperty("role", "heading")
        self.content = QWidget()
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(5)
        layout.addWidget(heading)
        layout.addWidget(self.content)


class CompactLabel(QLabel):
    """Long field names elide rather than forcing the inspector wider."""
    def __init__(self, text):
        super().__init__(text)
        self.setToolTip(text)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignVCenter,
                         self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, self.width()))


class ParameterGrid(QWidget):
    """Two label/value pairs per row at every inspector width."""
    def __init__(self):
        super().__init__()
        self.pairs = []
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(3)
        self.grid.setVerticalSpacing(5)
        for column in range(4):
            self.grid.setColumnStretch(column, 1)

    def add_pair(self, label, control):
        index = len(self.pairs)
        self.pairs.append((label, control))
        row, column = index // 2, (index % 2) * 2
        self.grid.addWidget(label, row, column)
        self.grid.addWidget(control, row, column + 1)


class DisplayRow(QWidget):
    def __init__(self, checkbox, switch):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(3)
        row.addWidget(checkbox)
        row.addWidget(switch)


class PlotModeSwitch(QWidget):
    """Segmented presentation of the existing boolean superimpose setting."""
    stateChanged = pyqtSignal(int)

    def __init__(self):
        super().__init__()
        self._checked = False
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.group = QButtonGroup(self)
        self.stacked_button = QPushButton("Stacked")
        self.overlay_button = QPushButton("Overlay")
        for index, button in enumerate((self.stacked_button, self.overlay_button)):
            button.setCheckable(True)
            button.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            button.setMinimumWidth(0)
            button.setProperty("role", "segment")
            self.group.addButton(button, index)
            layout.addWidget(button)
        self.stacked_button.setChecked(True)
        self.group.idClicked.connect(lambda index: self.setChecked(index == 1))

    def isChecked(self):
        return self._checked

    def setChecked(self, checked):
        checked = bool(checked)
        changed = checked != self._checked
        self._checked = checked
        self.overlay_button.setChecked(checked)
        self.stacked_button.setChecked(not checked)
        if changed:
            self.stateChanged.emit(2 if checked else 0)


def pattern_icon(color):
    """Native vector-painted drag grip and curve-color swatch."""
    pixmap = QPixmap(56, 32)
    pixmap.setDevicePixelRatio(2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#9aa9b6"))
    for x in (3, 6):
        for y in (4, 8, 12):
            painter.drawEllipse(x, y, 1, 1)
    painter.setBrush(QColor(color))
    painter.drawEllipse(13, 3, 10, 10)
    painter.end()
    icon = QIcon(pixmap)
    icon.addPixmap(pixmap, QIcon.Mode.Selected)
    return icon


class MainWindowUiMixin:
    def _init_ui(self):
        self.setFont(QFont("Segoe UI", 9))
        self.setStyleSheet(STYLESHEET)
        central_widget = QWidget()
        central_widget.setObjectName("workspace")
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(10)
        main_layout.addWidget(self._build_toolbar())
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(True)
        splitter.addWidget(self._build_browser_panel())
        controls = self._build_controls_panel()
        splitter.addWidget(self._build_plot_panel())
        splitter.addWidget(controls)
        splitter.setCollapsible(1, True)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([230, 690, 380])
        main_layout.addWidget(splitter, 1)
        self._build_empty_plot()
        for key, callback in (
            ("Ctrl+Shift+C", self.copy_svg_to_clipboard),
            ("Ctrl+Shift+P", self.copy_png_to_clipboard),
            ("Delete", self.delete_current_selected_curve),
            ("Ctrl+Z", self.undo_last_action),
        ):
            QShortcut(QKeySequence(key), self, activated=callback)
        self._connect_plot_events()
        self.populate_browser_tree()

    @staticmethod
    def _number_control(kind, limits, value, *, step=None, decimals=None,
                        tracking=None, callback=None):
        control = kind()
        control.setRange(*limits)
        if decimals is not None:
            control.setDecimals(decimals)
        if step is not None:
            control.setSingleStep(step)
        control.setValue(value)
        if tracking is not None:
            control.setKeyboardTracking(tracking)
        if callback is not None:
            control.valueChanged.connect(callback)
        control.setMinimumWidth(76)
        return control

    def _build_toolbar(self):
        # Top toolbar
        top_bar = QWidget()
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(8)

        btn_add = QPushButton("Add PXRD files")
        btn_add.clicked.connect(self.add_patterns_dialog)
        top_layout.addWidget(btn_add)

        btn_save_proj = QPushButton("Save project")
        btn_save_proj.clicked.connect(self.save_project)
        top_layout.addWidget(btn_save_proj)

        btn_load_proj = QPushButton("Open project")
        btn_load_proj.clicked.connect(self.load_project)
        top_layout.addWidget(btn_load_proj)

        self.curve_labels_btn = QPushButton("Curve labels: On")
        self.curve_labels_btn.setCheckable(True)
        self.curve_labels_btn.setChecked(True)
        self.curve_labels_btn.toggled.connect(self.on_toggle_curve_labels)
        top_layout.addWidget(self.curve_labels_btn)

        folder_label = QLabel("Default folder:")
        top_layout.addWidget(folder_label)
        self.browser_root_edit = QLineEdit(self.browser_root_dir)
        self.browser_root_edit.setMinimumWidth(180)
        self.browser_root_edit.returnPressed.connect(self.on_browser_root_changed)
        top_layout.addWidget(self.browser_root_edit)

        btn_pick_browser_root = QPushButton("Choose folder")
        btn_pick_browser_root.clicked.connect(self.pick_browser_root_dir)
        top_layout.addWidget(btn_pick_browser_root)

        tip = QLabel("Drag files to import. Drag list items to reorder curves. Scroll to scale curve intensity; Ctrl+scroll zooms the view. In Overlay mode, drag curves vertically or drag all curve labels together. Ctrl+Shift+C copies SVG; Ctrl+Shift+P copies PNG.")
        tip.setProperty("role", "muted")
        tip.setToolTip(tip.text())
        tip.setText("Drag files to import · Ctrl+wheel to zoom")
        tip.setMinimumWidth(0)
        tip.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        top_layout.addWidget(tip, 1)

        return top_bar

    def _build_browser_panel(self):
        # Far left: standalone file browser column
        browser_panel = QWidget()
        browser_panel.setMinimumWidth(180)
        browser_layout = QVBoxLayout(browser_panel)
        browser_layout.setContentsMargins(0, 0, 0, 0)
        browser_layout.setSpacing(10)

        gb_browser = QGroupBox("PXRD file browser")
        gb_browser.setToolTip("Multi-select files and drag to the plot to import")
        gb_browser_layout = QVBoxLayout(gb_browser)
        gb_browser_layout.setContentsMargins(10, 10, 10, 10)

        self.browser_search_edit = QLineEdit()
        self.browser_search_edit.setPlaceholderText("Search files and subfolders…")
        self.browser_search_edit.setClearButtonEnabled(True)
        self.browser_search_edit.textChanged.connect(self.on_browser_search_changed)
        gb_browser_layout.addWidget(self.browser_search_edit)

        self.browser_tree = self.browser_tree_type()
        self.browser_tree.setMinimumHeight(320)
        self.browser_tree.setItemDelegate(self.text_delegate_type(self._format_chemical_html, self.browser_tree))
        self.browser_tree.itemDoubleClicked.connect(self.on_browser_item_double_clicked)
        gb_browser_layout.addWidget(self.browser_tree)

        self.sort_by_filename_cb = QCheckBox("Sort by modified time (newest first)")
        self.sort_by_filename_cb.setChecked(True)
        self.sort_by_filename_cb.stateChanged.connect(self.populate_browser_tree)
        gb_browser_layout.addWidget(self.sort_by_filename_cb)

        btn_refresh_browser = QPushButton("Refresh browser")
        btn_refresh_browser.clicked.connect(self.populate_browser_tree)
        gb_browser_layout.addWidget(btn_refresh_browser)
        browser_layout.addWidget(gb_browser, 1)

        return browser_panel

    def _build_controls_panel(self):
        panel = QWidget()
        panel.setObjectName("inspector")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(6, 8, 6, 14)
        layout.setSpacing(6)
        loaded_panel = QWidget()
        loaded_layout = QVBoxLayout(loaded_panel)
        loaded_layout.setContentsMargins(6, 8, 6, 0)
        heading = QHBoxLayout()
        title = QLabel("Loaded patterns")
        title.setProperty("role", "heading")
        heading.addWidget(title)
        self.clear_all_button = QPushButton("Clear all")
        self.clear_all_button.setProperty("role", "quietDanger")
        self.clear_all_button.clicked.connect(self.clear_patterns)
        heading.addWidget(self.clear_all_button)
        heading.addStretch()
        self.loaded_count_label = QLabel("0")
        self.loaded_count_label.setObjectName("patternCount")
        self.loaded_count_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        heading.addWidget(self.loaded_count_label)
        loaded_layout.addLayout(heading)
        self.files_list = self.file_list_type()
        self.files_list.setObjectName("patternList")
        self.files_list.setMinimumHeight(50)
        self.files_list.setIconSize(QSize(28, 16))
        self.files_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.files_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.files_list.setItemDelegate(self.text_delegate_type(self._format_chemical_html, self.files_list))
        self.files_list.setToolTip("Drag to reorder curves. Hover a row for the source path and curve details.")
        self.files_list.orderChanged.connect(self.on_files_order_changed)
        self.files_list.currentRowChanged.connect(self.on_list_selection_changed)
        loaded_layout.addWidget(self.files_list)

        self.inspector_sections = {}
        def section(title):
            widget = InspectorSection(title)
            self.inspector_sections[title] = widget
            layout.addWidget(widget)
            return widget.content_layout

        display = section("Display")
        self.norm_cb = QCheckBox("Full-range norm.")
        self.norm_cb.setToolTip("Normalize each curve so its maximum intensity across the full data range is 1, before curve scaling and offsets. Visible-range normalization takes priority when enabled. Original data is unchanged.")
        self.norm_cb.setChecked(True)
        self.norm_cb.toggled.connect(lambda: self.update_plot(preserve_view=True))
        self.superimpose_cb = PlotModeSwitch()
        self.superimpose_cb.setToolTip("Stack curves or overlay them on a shared axis")
        self.superimpose_cb.stateChanged.connect(self.on_superimpose_toggled)
        display.addWidget(DisplayRow(self.norm_cb, self.superimpose_cb))
        self.live_norm_cb = QCheckBox("Normalize to visible range")
        self.live_norm_cb.setToolTip("Normalize each curve so its maximum intensity in the visible X range is 1, before curve scaling and offsets. Updates as the view changes. Overrides full-range normalization. Original data is unchanged.")
        self.live_norm_cb.toggled.connect(self.on_live_normalization_toggled)
        display.addWidget(self.live_norm_cb)
        row = QHBoxLayout()
        row.addWidget(QLabel("Color mode"))
        self.color_mode_combo = QComboBox()
        self.color_mode_combo.addItem("Black", "black")
        self.color_mode_combo.addItem("Multicolor", "colorful")
        self.color_mode_combo.currentIndexChanged.connect(self.on_color_mode_changed)
        row.addWidget(self.color_mode_combo, 1)
        display.addLayout(row)

        curves = section("Curves & text")
        fields = ParameterGrid()
        specs = (
            ("Curve label size", "curve_label_fs_spin", QSpinBox, (5, 40), 10, {}),
            ("Line width", "curve_lw_spin", QDoubleSpinBox, (0.1, 10.0), 1.5, {"step": 0.1}),
            ("Tick label size", "tick_fs_spin", QSpinBox, (5, 40), 12, {}),
            ("Frame width", "frame_lw_spin", QDoubleSpinBox, (0.1, 10.0), 1.5, {"step": 0.1}),
            ("Axis label size", "axis_label_fs_spin", QSpinBox, (5, 40), 12, {}),
            ("Y offset", "curve_offset_spin", QDoubleSpinBox, (-1000.0, 1000.0), 0.03, {"step": 0.01, "decimals": 3}),
        )
        for index, (title, name, kind, limits, value, options) in enumerate(specs):
            control = self._number_control(kind, limits, value,
                callback=lambda: self.update_plot(preserve_view=True), **options)
            control.setMinimumWidth(36)
            control.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            control.setAccessibleName(title)
            setattr(self, name, control)
            label = CompactLabel(title)
            label.setBuddy(control)
            fields.add_pair(label, control)
        curves.addWidget(fields)

        canvas = section("Canvas & range")
        fields = ParameterGrid()
        for index, (title, name, limits, value, options) in enumerate((
            ("Width (in)", "fig_w_spin", (1, 20), 7.0, {"callback": self._on_fig_size_changed}),
            ("Height (in)", "fig_h_spin", (1, 20), 6.0, {"callback": self._on_fig_size_changed}),
            ("X min", "xmin_spin", (-9999.0, 9999.0), 2.0, {"step": 0.1, "decimals": 2}),
            ("X max", "xmax_spin", (-9999.0, 9999.0), 30.0, {"step": 0.1, "decimals": 2}),
        )):
            control = self._number_control(QDoubleSpinBox, limits, value, tracking=True, **options)
            control.setMinimumWidth(36)
            control.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            control.setAccessibleName(title)
            setattr(self, name, control)
            label = CompactLabel(title)
            label.setBuddy(control)
            fields.add_pair(label, control)
        for control in (self.xmin_spin, self.xmax_spin):
            control.valueChanged.connect(self.on_display_range_live_changed)
            control.editingFinished.connect(lambda: self.set_display_range(show_warning=False))
        canvas.addWidget(fields)
        parameter_buttons = QHBoxLayout()
        self.save_plot_defaults_button = QPushButton("Save parameters")
        self.save_plot_defaults_button.setToolTip("Use the current plotting parameters when xStack starts")
        self.save_plot_defaults_button.clicked.connect(self.save_plot_defaults)
        parameter_buttons.addWidget(self.save_plot_defaults_button, 1)
        self.reset_plot_defaults_button = QPushButton("restore parameters")
        self.reset_plot_defaults_button.setToolTip("Restore the original plotting parameters. Click Save parameters to use them on future starts.")
        self.reset_plot_defaults_button.clicked.connect(self.reset_plot_defaults)
        parameter_buttons.addWidget(self.reset_plot_defaults_button, 1)
        layout.addLayout(parameter_buttons)
        button = QPushButton("PXRD tools")
        button.clicked.connect(self.open_powerxrd_tools_dialog)
        layout.addWidget(button)

        exports = section("Export")
        fields = QWidget()
        grid = QGridLayout(fields)
        grid.setContentsMargins(0, 0, 0, 4)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(4)
        grid.setColumnMinimumWidth(0, 72)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 1)
        for row, (format_name, export, copy) in enumerate((
            ("CSV", self.export_csv, None),
            ("PNG", self.export_png, self.copy_png_to_clipboard),
            ("SVG", self.export_svg, self.copy_svg_to_clipboard),
        )):
            grid.addWidget(QLabel(format_name), row, 0)
            button = QPushButton("Export")
            button.setMinimumWidth(0)
            button.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            button.setAccessibleName("Export " + format_name)
            button.setToolTip("Export " + format_name)
            button.clicked.connect(export)
            grid.addWidget(button, row, 1)
            if copy:
                button = QPushButton("Copy")
                button.setMinimumWidth(0)
                button.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
                button.setAccessibleName("Copy " + format_name)
                button.setToolTip("Copy " + format_name + " to clipboard")
                button.clicked.connect(copy)
                grid.addWidget(button, row, 2)
        self.svg_bg_label = QLabel("Background")
        self.svg_bg_label.setObjectName("svgBackgroundLabel")
        grid.addWidget(self.svg_bg_label, 3, 0)
        self.svg_bg_combo = QComboBox()
        self.svg_bg_combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.svg_bg_combo.setMinimumWidth(0)
        self.svg_bg_combo.addItem("Transparent", "transparent")
        self.svg_bg_combo.addItem("White", "white")
        grid.addWidget(self.svg_bg_combo, 3, 1, 1, 2)
        exports.addWidget(fields)
        layout.addStretch(1)
        self.inspector_scroll = QScrollArea()
        self.inspector_scroll.setObjectName("inspectorScroll")
        self.inspector_scroll.setWidget(panel)
        self.inspector_scroll.setWidgetResizable(True)
        self.inspector_scroll.setViewportMargins(0, 0, 1, 1)
        self.inspector_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.inspector_scroll.setMinimumWidth(240)
        self.controls_splitter = QSplitter(Qt.Orientation.Vertical)
        self.controls_splitter.setChildrenCollapsible(False)
        self.controls_splitter.setHandleWidth(7)
        self.controls_splitter.addWidget(loaded_panel)
        self.controls_splitter.addWidget(self.inspector_scroll)
        self.inspector_scroll.setMinimumHeight(150)
        self.controls_splitter.setStretchFactor(0, 0)
        self.controls_splitter.setStretchFactor(1, 1)
        self.controls_splitter.setSizes([150, 560])
        self.controls_splitter.handle(1).setToolTip("Drag to resize the loaded patterns list")
        return self.controls_splitter

    def _build_plot_panel(self):
        # Right: Matplotlib area
        self.fig = Figure(figsize=(7, 6), dpi=100)
        self.base_fig_dpi = float(self.fig.dpi)
        self.canvas = FigureCanvas(self.fig)
        self.canvas.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        # UI-only border around canvas display area; it is not part of matplotlib exports.
        self.canvas.setObjectName("plotCanvas")
        self.canvas.setAcceptDrops(True)
        self._apply_canvas_size_lock()

        # Expandable middle container keeps sidebars at edges while canvas stays fixed-size centered.
        # Using QScrollArea allows splitter to continue shrinking this middle region when side panels grow.
        self.canvas_scroll = QScrollArea()
        self.canvas_scroll.setWidget(self.canvas)
        self.canvas_scroll.setWidgetResizable(False)
        self.canvas_scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.canvas_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.canvas_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.canvas_scroll.setMinimumWidth(0)
        self.canvas_scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.canvas_scroll.setObjectName("plotViewport")
        self.canvas_scroll.viewport().installEventFilter(self)

        self.scale_overlay = QLabel("Zoom: 130%", self.canvas_scroll.viewport())
        self.scale_overlay.setObjectName("scaleOverlay")
        self.scale_overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.scale_overlay.show()
        self._update_scale_overlay()

        return self.canvas_scroll

    def _connect_plot_events(self):
        # Register events
        self.canvas.mpl_connect("button_press_event", self.on_mouse_press)
        self.canvas.mpl_connect("motion_notify_event", self.on_mouse_move)
        self.canvas.mpl_connect("button_release_event", self.on_mouse_release)
        self.canvas.mpl_connect("scroll_event", self.on_scroll)
        self.canvas.installEventFilter(self)

