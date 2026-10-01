"""Curve color palettes for publication figures.

Categorical palettes cycle through fixed colors. Gradient palettes spread a
matplotlib colormap evenly over all loaded curves, which suits ordered series
(temperature, time, composition). The journal-style sets follow the widely used
ggsci definitions; they are not official journal requirements.
"""
from collections import namedtuple

from matplotlib import colormaps
from matplotlib.colors import to_hex, to_rgb

Palette = namedtuple("Palette", "label kind colors")

PALETTES = {
    # "colorful" is the key saved by xStack 1.5 and earlier for its single multicolor set.
    "colorful": Palette("Classic", "categorical", (
        "#ED1C24", "#343695", "#B0A06B", "#1EB8F1", "#0DAB59", "#F9A642", "#A2459F", "#59A3AC")),
    "okabe_ito": Palette("Okabe–Ito (colorblind safe)", "categorical", (
        "#0072B2", "#D55E00", "#009E73", "#CC79A7", "#E69F00", "#56B4E9", "#000000", "#F0E442")),
    "tol_bright": Palette("Tol bright (colorblind safe)", "categorical", (
        "#4477AA", "#EE6677", "#228833", "#CCBB44", "#66CCEE", "#AA3377", "#BBBBBB")),
    "tol_muted": Palette("Tol muted (colorblind safe)", "categorical", (
        "#332288", "#88CCEE", "#44AA99", "#117733", "#999933", "#DDCC77", "#CC6677", "#882255", "#AA4499")),
    "npg": Palette("Nature style (NPG)", "categorical", (
        "#E64B35", "#4DBBD5", "#00A087", "#3C5488", "#F39B7F", "#8491B4", "#91D1C2", "#DC0000", "#7E6148", "#B09C85")),
    "aaas": Palette("Science style (AAAS)", "categorical", (
        "#3B4992", "#EE0000", "#008B45", "#631879", "#008280", "#BB0021", "#5F559B", "#A20056", "#808180", "#1B1919")),
    "lancet": Palette("Lancet style", "categorical", (
        "#00468B", "#ED0000", "#42B540", "#0099B4", "#925E9F", "#FDAF91", "#AD002A", "#ADB6B6", "#1B1919")),
    "nejm": Palette("NEJM style", "categorical", (
        "#BC3C29", "#0072B5", "#E18727", "#20854E", "#7876B1", "#6F99AD", "#FFDC91", "#EE4C97")),
    "jama": Palette("JAMA style", "categorical", (
        "#374E55", "#DF8F44", "#00A1D5", "#B24745", "#79AF97", "#6A6599", "#80796B")),
    "tableau10": Palette("Tableau 10", "categorical", (
        "#4E79A7", "#F28E2B", "#E15759", "#76B7B2", "#59A14F", "#EDC948", "#B07AA1", "#FF9DA7", "#9C755F", "#BAB0AC")),
    "dark2": Palette("ColorBrewer Dark2", "categorical", (
        "#1B9E77", "#D95F02", "#7570B3", "#E7298A", "#66A61E", "#E6AB02", "#A6761D", "#666666")),
    "set1": Palette("ColorBrewer Set1", "categorical", (
        "#E41A1C", "#377EB8", "#4DAF4A", "#984EA3", "#FF7F00", "#A65628", "#F781BF", "#999999")),
    # Gradients: (colormap name, start, end). Ends are trimmed where the map gets too pale on white.
    "viridis": Palette("Viridis gradient", "gradient", ("viridis", 0.0, 0.85)),
    "plasma": Palette("Plasma gradient", "gradient", ("plasma", 0.0, 0.85)),
    "cividis": Palette("Cividis gradient", "gradient", ("cividis", 0.0, 0.85)),
    "coolwarm": Palette("Blue–red gradient", "gradient", ("coolwarm", 0.0, 1.0)),
    "blues": Palette("Blues gradient", "gradient", ("Blues", 1.0, 0.35)),
}


def is_gradient(key):
    palette = PALETTES.get(key)
    return palette is not None and palette.kind == "gradient"


def palette_colors(key, count):
    """Return *count* hex colors for palette *key* (black for unknown keys)."""
    count = max(0, int(count))
    palette = PALETTES.get(key)
    if palette is None:
        return ["#000000"] * count
    if palette.kind == "categorical":
        return [palette.colors[i % len(palette.colors)] for i in range(count)]
    name, start, end = palette.colors
    cmap = colormaps[name]
    if count == 1:
        return [to_hex(cmap(start))]
    step = (end - start) / max(1, count - 1)
    return [to_hex(cmap(start + i * step)) for i in range(count)]


# Shade steps for the color dialog grid, dark to light: negative mixes toward
# black, positive toward white, 0 is the palette color itself.
SHADE_LEVELS = (-0.45, -0.25, 0.0, 0.3, 0.55, 0.75)
NEUTRAL_GRAY = "#7F7F7F"


def shade(color, amount):
    """Mix *color* toward black (amount < 0) or white (amount > 0)."""
    rgb = to_rgb(color)
    target = 0.0 if amount < 0 else 1.0
    amount = abs(float(amount))
    return to_hex(tuple(c + (target - c) * amount for c in rgb))


def shade_grid(key, columns=8):
    """Rows of shades (dark to light) for the first *columns* colors of palette *key*.

    Palettes with fewer colors are padded with a neutral gray column.
    """
    palette = PALETTES.get(key)
    if palette is not None and palette.kind == "categorical":
        base = list(palette.colors[:columns])
    else:
        base = palette_colors(key, columns)
    base += [NEUTRAL_GRAY] * (columns - len(base))
    return [[shade(color, level) for color in base] for level in SHADE_LEVELS]
