"""Tiny rich-text markup for labels: <sup>, <sub>, <i> and <b>.

Labels are stored as plain strings with these tags, for example
``Cu<sub>3</sub>(BTC)<sub>2</sub>`` or ``2<i>θ</i> (deg)``. They are converted
to matplotlib mathtext for the figure, to HTML for Qt item views, and to plain
text where formatting cannot be shown (CSV headers, combo boxes).
Unknown or unbalanced tags are kept as literal text.
"""
import html
import re
from functools import lru_cache

TAGS = ("sup", "sub", "i", "b")
SOFT_HYPHEN = "­"
_TAG_RE = re.compile(r"<(/?)(sup|sub|i|b)>", re.IGNORECASE)
_MATH_ESCAPES = {
    "\\": r"\backslash{}", "{": r"\{", "}": r"\}", "_": r"\_", "^": r"\^{}",
    "$": r"\$", "%": r"\%", "#": r"\#", "&": r"\&", "~": r"\~{}", " ": r"\ ",
    # Braces make +/- ordinary symbols, so charges like 3+ or 2- stay tight.
    "+": "{+}", "-": "{-}",
}


def has_markup(text):
    return bool(_TAG_RE.search(str(text or "")))


def parse_runs(text):
    """Split *text* into ``(segment, styles)`` runs; styles is a frozenset of tag names."""
    text = str(text or "")
    tokens = []
    pos = 0
    for match in _TAG_RE.finditer(text):
        if match.start() > pos:
            tokens.append(("text", text[pos:match.start()]))
        tokens.append(("close" if match.group(1) else "open", match.group(2).lower(), match.group(0)))
        pos = match.end()
    if pos < len(text):
        tokens.append(("text", text[pos:]))

    # Pair tags first so a stray or crossed tag stays visible as literal text.
    stack, paired = [], set()
    for index, token in enumerate(tokens):
        if token[0] == "open":
            stack.append(index)
        elif token[0] == "close":
            if stack and tokens[stack[-1]][1] == token[1]:
                paired.update((stack.pop(), index))
    runs, active = [], []
    for index, token in enumerate(tokens):
        if token[0] == "text" or index not in paired:
            segment = token[1] if token[0] == "text" else token[2]
            styles = frozenset(active)
            if runs and runs[-1][1] == styles:
                runs[-1] = (runs[-1][0] + segment, styles)
            else:
                runs.append((segment, styles))
        elif token[0] == "open":
            active.append(token[1])
        else:
            active.remove(token[1])
    return runs


def to_plain(text):
    """Text with formatting tags removed."""
    return "".join(segment for segment, _styles in parse_runs(text))


def to_html(text):
    """Escaped HTML for Qt rich-text views."""
    out = []
    for segment, styles in parse_runs(text):
        chunk = html.escape(segment)
        for tag in TAGS:
            if tag in styles:
                chunk = f"<{tag}>{chunk}</{tag}>"
        out.append(chunk)
    return "".join(out)


def _math_body(segment, styles):
    body = "".join(_MATH_ESCAPES.get(ch, ch) for ch in segment)
    if not styles & {"sub", "sup"}:
        # Outside scripts "-" is a hyphen (MOF-5), not the minus sign used for charges.
        # Mathtext always turns "-" into a minus; the soft hyphen keeps Arial's hyphen
        # glyph in the run's own weight and slant.
        body = body.replace("{-}", SOFT_HYPHEN)
    if "b" in styles and "i" in styles:
        body = r"\mathbfit{%s}" % body
    elif "b" in styles:
        body = r"\mathbf{%s}" % body
    elif "i" in styles:
        # \mathit leaves digits upright, so italic uses the sf slot mapped to Arial italic.
        body = r"\mathsf{%s}" % body
    else:
        body = r"\mathrm{%s}" % body
    if "sub" in styles:
        body = "_{%s}" % body
    if "sup" in styles:
        body = "^{%s}" % body
    return body


@lru_cache(maxsize=512)
def to_mathtext(text):
    """Matplotlib text for a label; unformatted text is returned unchanged.

    Adjacent formatted runs share one math block. Neighbouring scripts are always
    set side by side, never stacked: mathtext lowers a subscript that shares a
    nucleus with a superscript, so stacking would put the 4 of SO<sub>4</sub><sup>+</sup>
    lower than the 2 of H<sub>2</sub>. Every subscript and every superscript thus
    keeps one fixed offset, as in a word processor. If a block cannot be parsed it
    falls back to plain text.
    """
    text = str(text or "")
    if not has_markup(text):
        return text
    from matplotlib.mathtext import MathTextParser
    parser = MathTextParser("path")
    out, block, block_plain = [], [], []
    previous_script = None  # script kind of the previous run in this block

    def flush():
        nonlocal previous_script
        previous_script = None
        if not block:
            return
        math = "$" + "".join(block) + "$"
        try:
            parser.parse(math)
        except ValueError:
            out.append("".join(block_plain).replace("$", r"\$"))
        else:
            out.append(math)
        block.clear()
        block_plain.clear()

    for segment, styles in parse_runs(text):
        if styles:
            script = "sup" if "sup" in styles else "sub" if "sub" in styles else None
            if script and previous_script:
                # An empty group starts a new nucleus, so the scripts sit side by side
                # at their standard offsets (and never fail as a double superscript).
                block.append("{}")
            previous_script = script
            block.append(_math_body(segment, styles))
            block_plain.append(segment)
        else:
            flush()
            # A lone "$" would otherwise switch matplotlib into math mode.
            out.append(segment.replace("$", r"\$"))
    flush()
    return "".join(out)


# Word-processor style script placement, in em of the label font size.
SUPERSCRIPT_RAISE = 0.33
SUBSCRIPT_DROP = 0.14
# Set when calibration fails; the app logs it once logging is configured
# (calibration runs at import time, before the log file handler exists).
script_metrics_error = None


def configure_math_fonts(rc, family="Arial"):
    """Render formatted runs in the same font as the rest of the figure."""
    rc["mathtext.fontset"] = "custom"
    rc["mathtext.rm"] = family
    rc["mathtext.it"] = f"{family}:italic"
    rc["mathtext.sf"] = f"{family}:italic"  # used for <i>, see _math_body
    rc["mathtext.bf"] = f"{family}:bold"
    rc["mathtext.bfit"] = f"{family}:italic:bold"
    _register_script_metrics(family)


def _measure_raise(parser, prop, script):
    """Baseline offset (em) of a one-character script relative to its base character."""
    _w, _h, _d, glyphs, _rects = parser.parse(r"$\mathrm{x}%s{\mathrm{1}}$" % script, dpi=72, prop=prop)
    base, scripted = glyphs[0][4], glyphs[1][4]
    return (scripted - base) / prop.get_size_in_points()


def _register_script_metrics(family):
    """Give *family* word-processor script offsets instead of mathtext's TeX defaults.

    Mathtext places scripts in multiples of its own x-height estimate, which for
    Arial comes out near a full em: superscripts end up 0.7 em high, subscripts
    0.3 em low, with extra space after each. The ratios are calibrated by
    measurement so the result does not depend on how that estimate is computed.
    Uses matplotlib's private font-constant table; if it changes, the TeX
    defaults remain and ``script_metrics_error`` describes why.
    """
    global script_metrics_error
    try:
        from matplotlib import _mathtext
        from matplotlib.font_manager import FontProperties
        from matplotlib.mathtext import MathTextParser

        class ScriptMetrics(_mathtext.FontConstantsBase):
            script_space = 0.0
            delta = 0.0
            sup1 = 1.0
            sub1 = 1.0
            sub2 = 1.0

        _mathtext._font_constant_mapping[family] = ScriptMetrics
        parser = MathTextParser("path")
        prop = FontProperties(family=family, size=100)
        unit = _measure_raise(parser, prop, "^")  # raise for sup1 == 1, in em
        if not unit > 0:
            raise ValueError("unexpected script metrics")
        ScriptMetrics.sup1 = SUPERSCRIPT_RAISE / unit
        ScriptMetrics.sub1 = ScriptMetrics.sub2 = SUBSCRIPT_DROP / unit
        # Layouts are cached per string; drop the probe laid out with sup1 == 1.
        cache_clear = getattr(MathTextParser._parse_cached, "cache_clear", None)
        if cache_clear:
            cache_clear()
        script_metrics_error = None
    except Exception as exc:
        script_metrics_error = f"Could not set script metrics for {family}: {exc!r}"


def remove_tags(text):
    """Drop every formatting tag, balanced or not (used by "Clear formatting")."""
    return _TAG_RE.sub("", str(text or ""))
