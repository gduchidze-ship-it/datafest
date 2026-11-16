"""Build a restyled, restructured copy of an exported HTML slide deck.

The source deck is a single self-contained HTML file. This script reorders its
slides according to a TOML manifest, applies per-slide frame edits, inserts new
slides from HTML fragments, and restyles everything to the Impel brand palette
and typeface. Slide markup is never parsed into a tree or re-serialised: all
edits are targeted string substitutions, because text nodes rely on
``white-space: pre-wrap`` and must keep their exact bytes.

Example:
    python tools/build_deck.py --source index.html --manifest tools/deck.toml \
        --brand-dir path/to/impel-brand --out index_new.html --report
"""

from __future__ import annotations

import argparse
import base64
import binascii
import colorsys
import copy
import html
import json
import logging
import re
import sys
import tomllib
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import Any, NamedTuple

logger = logging.getLogger(__name__)

PALETTE: Mapping[str, str] = {
    "Purple": "#6C44E4",
    "White": "#FFFFFF",
    "Off-White": "#F2F2F2",
    "Midnight": "#121212",
    "Slate": "#211F23",
    "Graphite": "#5E5A62",
    "Shadowline": "#777581",
    "Powdercoat": "#B6B2C7",
    "Plume": "#E1DFEC",
    "Frost": "#EDECF4",
    "Ice": "#F6F5FA",
    "Ember": "#DD7846",
    "Zest": "#D1FF8B",
    "Breeze": "#8DDEB7",
    "Highland": "#306255",
    "Mist": "#BABFFA",
    "Mulberry": "#5F579F",
    "Indigo": "#3E245C",
    "Neon": "#A088EB",
}
_PALETTE_HEXES = frozenset(PALETTE.values())
_MIDNIGHT = PALETTE["Midnight"]
_MIDNIGHT_CHANNEL = 0x12

# Fallback buckets: (exclusive upper bound, palette name), checked in order.
_GREY_SATURATION = 0.18
_GREY_LIGHTNESS_STEPS = (
    (0.09, "Midnight"),
    (0.17, "Slate"),
    (0.42, "Graphite"),
    (0.56, "Shadowline"),
    (0.82, "Powdercoat"),
)
_DARK_LIGHTNESS = 0.20
_HUE_STEPS = (
    (40.0, "Ember"),
    (70.0, "Zest"),
    (170.0, "Breeze"),
    (250.0, "Neon"),
    (300.0, "Violet"),
    (345.0, "Mist"),
)
_PALE_VIOLET_LIGHTNESS = 0.72

_REGULAR_FAMILY = "'KH Teka', 'Helvetica Neue', Helvetica, Arial, sans-serif"
_MEDIUM_FAMILY = f"'KH Teka Medium', {_REGULAR_FAMILY}"
_MEDIUM_WEIGHT = 500
_HEAVY_KEYWORDS = frozenset({"bold", "bolder"})
_SLANTED_KEYWORDS = frozenset({"italic", "oblique"})

_COLOR_ATTRIBUTES = frozenset(
    {"color", "fill", "stroke", "stop-color", "flood-color", "lighting-color"}
)
# SMIL animation attributes hold colours when they animate a paint property.
_SMIL_ATTRIBUTES = frozenset({"values", "from", "to"})
_RENAMED_ATTRIBUTES = frozenset({"alt"})
_BACKGROUND_PROPERTIES = frozenset({"background", "background-image"})
_FRAME_BOX_PROPERTIES = frozenset({"left", "top", "width", "height"})
_GRADIENT_EXEMPT_CLASSES = frozenset({"impel-cover", "impel-divider", "impel-band"})
_VOID_ELEMENTS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta"}
    | {"source", "track", "wbr"}
)

_DEFAULT_BACKGROUND = _MIDNIGHT
_DEFAULT_TRANSITION = "fade"
_DEFAULT_BUILD_EFFECT = "fade"
_CSS_FILE = "deck.css"
_ICONS_FILE = "icons.toml"
_BANNED_MARKERS = ("fonts.googleapis", "@font-face", "mc-anthropicons")
_GLYPH_FONT = "mc-anthropicons"
_SLIDE_TAIL = "</div></div></div></section>"
_CONTEXT_CHARS = 30

# One alternative per quoted style so ``>`` inside attribute values is skipped.
_TAG_RE = re.compile(
    r"<!--.*?-->|</?(?P<name>[A-Za-z][\w:-]*)(?:[^>\"']|\"[^\"]*\"|'[^']*')*>",
    re.DOTALL,
)
_ATTR_RE = re.compile(
    r"(?P<lead>\s)(?P<name>[^\s\"'<>/=]+)\s*=\s*(?:\"(?P<dq>[^\"]*)\"|'(?P<sq>[^']*)')"
)
# ``url(...)`` is matched first so fragment references such as ``url(#abc)``
# are consumed whole and never mistaken for a three-digit hex colour.
_COLOR_RE = re.compile(
    r"url\([^)]*\)"
    r"|(?<![&\w])#(?:[0-9a-f]{8}|[0-9a-f]{6}|[0-9a-f]{3,4})(?![\w-])"
    r"|rgba?\([^()]*\)",
    re.IGNORECASE,
)
# Colour functions the palette pass cannot convert; any left over is a problem.
# The lookbehind keeps ``lab(`` from matching inside names such as ``oklab(``.
_COLOR_FUNCTION_RE = re.compile(
    r"(?<![\w-])(?:hsla?|color-mix|oklch|lab)\(", re.IGNORECASE
)
_COLOR_MIX_RE = re.compile(r"color-mix\([^)]*\)", re.IGNORECASE)
_SLASH_RE = re.compile(r"\s*/\s*")
_CSS_BLOCK_RE = re.compile(r"\{([^{}]*)\}")
_FONT_SHORTHAND_RE = re.compile(
    r"^(?P<prefix>(?:(?:italic|oblique|normal|bold|bolder|lighter|small-caps|\d{1,4})"
    r"\s+)*)(?P<size>[\d.]+[a-z%]+(?:\s*/\s*[\w.%]+)?)\s+(?P<family>.+)$",
    re.IGNORECASE | re.DOTALL,
)
_WEIGHT_RE = re.compile(
    r"font-weight\s*[:=]\s*[\"']?\s*(\d+(?:\.\d+)?|bold(?:er)?)\b", re.IGNORECASE
)
_SVG_URI_RE = re.compile(r"data:image/svg\+xml;base64,([A-Za-z0-9+/=]+)")
_BASE64_RE = re.compile(r"base64,[A-Za-z0-9+/=]+")
_ARIA_LABEL_RE = re.compile(r"\saria-label\s*=\s*(?:\"[^\"]*\"|'[^']*')")
_RENAMES = (
    (re.compile(r"\bJev\b"), "Decisions"),
    (re.compile(r"\bJEV\b"), "DECISIONS"),
)
_TOKEN_RE = re.compile(r"\{\{([^{}]*)\}\}")
_FRAME_ID_RE = re.compile(r"data-frame-id=\"([^\"]*)\"")
_SECTION_RE = re.compile(
    r"<section class=\"deck-slide\"(?:[^>\"]|\"[^\"]*\")*>.*?</section>", re.DOTALL
)
_STYLE_BLOCK_RE = re.compile(r"<style>(.*?)</style>", re.DOTALL)
_SCRIPT_BLOCK_RE = re.compile(r"<script\b.*?</script>", re.DOTALL)
_MOTION_RE = re.compile(
    r"(<script type=\"application/json\" id=\"deck-motion\">)(.*?)(</script>)",
    re.DOTALL,
)
_COMMENT_RE = re.compile(r"<!--.*?-->\n?", re.DOTALL)
_FONT_LINK_RE = re.compile(r"<link[^>]*fonts\.googleapis\.com[^>]*>\n?")
_FONT_HOST_RE = re.compile(r" https://fonts\.(?:googleapis|gstatic)\.com")
_TITLE_RE = re.compile(r"<title>.*?</title>", re.DOTALL)
_INLINE_SHEET_RE = re.compile(r"<(?:style|script)\b", re.IGNORECASE)
_COLOR_KEY_RE = re.compile(r"#(?:[0-9a-f]{6}|[0-9a-f]{8})")

_MANIFEST_KEYS = frozenset({"title", "forbidden", "colors", "sections", "slides"})
_SECTION_KEYS = frozenset({"short", "start", "end"})
_EDIT_KEYS = frozenset({"drop", "text", "style", "alt", "icons", "svg_edit"})
_SLIDE_KEYS = _EDIT_KEYS | {
    "source",
    "fragment",
    "label",
    "notes",
    "section",
    "bg",
    "slide_class",
    "transition",
    "prepend",
    "append",
    "builds",
    "build_effect",
    "colors",
}


class DeckBuildError(Exception):
    """The manifest, the source deck or the built output is not usable."""


@dataclass(frozen=True)
class Section:
    """One agenda section shown in the rail.

    Attributes:
        short: Label shown next to the section number.
        start: Minute at which the section starts.
        end: Minute at which the section ends.
    """

    short: str
    start: int
    end: int


@dataclass(frozen=True)
class TextEdit:
    """Replace the inner HTML of a frame's text element.

    Attributes:
        frame: Frame reference (full id or ``eN`` prefix).
        html: Replacement inner HTML, inserted verbatim.
    """

    frame: str
    html: str


@dataclass(frozen=True)
class StyleEdit:
    """Set style declarations on a frame.

    Attributes:
        frame: Frame reference (full id or ``eN`` prefix).
        declarations: ``(property, value)`` pairs in manifest order.
    """

    frame: str
    declarations: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class AltEdit:
    """Replace the alt text of a frame's image.

    Attributes:
        frame: Frame reference (full id or ``eN`` prefix).
        text: New alt text, unescaped.
    """

    frame: str
    text: str


@dataclass(frozen=True)
class IconEdit:
    """Replace a frame's glyph icon with a named stroke icon.

    Attributes:
        frame: Frame reference (full id or ``eN`` prefix).
        icon: Name of an entry in ``icons.toml``.
    """

    frame: str
    icon: str


@dataclass(frozen=True)
class SvgEdit:
    """Regex substitution inside the SVG image of a frame.

    Attributes:
        frame: Frame reference (full id or ``eN`` prefix).
        pattern: Regular expression matched against the decoded source SVG.
        repl: Replacement, in ``re.sub`` syntax.
    """

    frame: str
    pattern: str
    repl: str


@dataclass(frozen=True)
class FrameEdits:
    """Frame-level edits applied to one source slide.

    Attributes:
        drop: Frame references to delete.
        text: Text replacements.
        style: Style declaration overrides.
        alt: Image alt replacements.
        icons: Glyph icon replacements.
        svg_edit: Substitutions inside embedded SVG images.
    """

    drop: tuple[str, ...] = ()
    text: tuple[TextEdit, ...] = ()
    style: tuple[StyleEdit, ...] = ()
    alt: tuple[AltEdit, ...] = ()
    icons: tuple[IconEdit, ...] = ()
    svg_edit: tuple[SvgEdit, ...] = ()


@dataclass(frozen=True)
class SlideSpec:
    """One output slide as described by the manifest.

    Attributes:
        source: Number of the source slide to reuse, or None for a fragment.
        fragment: Path of the fragment holding the slide's inner HTML, or None.
        label: ``data-label`` value; None keeps the source slide's label.
        notes: ``data-speaker-notes`` value; None keeps the source notes.
        section: 1-based agenda section, or 0 for no rail.
        bg: Background colour of a fragment slide; None means the default.
        slide_class: Extra class for the ``.slide`` element, or None.
        transition: Slide transition; None keeps the source's or the default.
        edits: Frame edits (source slides only).
        prepend: Fragment inserted before the first frame, or None.
        append: Fragment inserted after the last frame, or None.
        builds: Click-to-reveal groups; None keeps the source's builds.
        build_effect: Effect name used for every build event.
        colors: Colour overrides for this slide only; they win over the
            manifest-wide table.
    """

    source: int | None = None
    fragment: Path | None = None
    label: str | None = None
    notes: str | None = None
    section: int = 0
    bg: str | None = None
    slide_class: str | None = None
    transition: str | None = None
    edits: FrameEdits = field(default_factory=FrameEdits)
    prepend: Path | None = None
    append: Path | None = None
    builds: tuple[tuple[str, ...], ...] | None = None
    build_effect: str = _DEFAULT_BUILD_EFFECT
    colors: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Manifest:
    """The parsed deck manifest.

    Attributes:
        title: Document title.
        forbidden: Case-insensitive substrings that must not reach the output.
        colors: Explicit colour overrides, ``#rrggbb[aa]`` to palette hex.
        sections: Agenda sections in order.
        slides: Output slides in order.
        base_dir: Directory that manifest-relative paths resolve against.
    """

    title: str
    forbidden: tuple[str, ...]
    colors: Mapping[str, str]
    sections: tuple[Section, ...]
    slides: tuple[SlideSpec, ...]
    base_dir: Path


@dataclass(frozen=True)
class SourceSlide:
    """One slide of the source deck.

    Attributes:
        number: 1-based position in the source deck.
        label: Raw (attribute-escaped) ``data-label`` value.
        notes: Raw (attribute-escaped) ``data-speaker-notes`` value.
        open_tag: The opening tag of the ``.slide`` element.
        inner: Inner HTML of the ``.slide`` element, byte for byte.
    """

    number: int
    label: str
    notes: str
    open_tag: str
    inner: str


@dataclass(frozen=True)
class SourceDeck:
    """The source deck split into the parts the build rewrites.

    Attributes:
        head: Everything before the first ``<style>``.
        editor_css: Content of the first (editor) stylesheet.
        deck_css: Content of the second (deck) stylesheet.
        runtime: Everything from after the motion script up to ``<main>``.
        main_tag: The opening ``<main>`` tag.
        slides: The source slides in order.
        tail: Everything from ``</main>`` to the end.
        motion: The parsed ``deck-motion`` JSON.
    """

    head: str
    editor_css: str
    deck_css: str
    runtime: str
    main_tag: str
    slides: tuple[SourceSlide, ...]
    tail: str
    motion: Mapping[str, Any]


@dataclass(frozen=True)
class BuildResult:
    """The built deck and its colour diagnostics.

    Attributes:
        html: The complete output document.
        fallbacks: Count per ``(source colour, palette name)`` fallback mapping.
    """

    html: str
    fallbacks: Mapping[tuple[str, str], int]


class ColorMapper:
    """Map arbitrary CSS colours onto the brand palette.

    Attributes:
        fallbacks: Count per ``(source colour, palette name)`` for every colour
            that had no explicit override and was not already in the palette.
    """

    def __init__(self, overrides: Mapping[str, str] | None = None) -> None:
        """Create a mapper.

        Args:
            overrides: Explicit mappings keyed by lowercase ``#rrggbb`` or, for
                translucent colours, ``#rrggbbaa``. Values are palette hexes.
        """
        self._overrides = dict(overrides or {})
        self.fallbacks: Counter[tuple[str, str]] = Counter()

    def with_overrides(self, extra: Mapping[str, str]) -> ColorMapper:
        """Return a mapper with extra overrides layered on top of this one.

        Args:
            extra: Overrides that win over this mapper's own.

        Returns:
            A new mapper that records its fallbacks in this mapper's
            ``fallbacks`` counter, so one report covers every slide.
        """
        scoped = ColorMapper({**self._overrides, **extra})
        scoped.fallbacks = self.fallbacks
        return scoped

    def map_color(self, token: str) -> str:
        """Return the palette hex for one colour token.

        Args:
            token: ``#rgb``, ``#rgba``, ``#rrggbb``, ``#rrggbbaa``, ``rgb(...)``
                or ``rgba(...)``.

        Returns:
            An uppercase ``#RRGGBB`` palette colour. Translucent colours without
            an explicit override are composited over Midnight first.

        Raises:
            DeckBuildError: If the token cannot be parsed as a colour.
        """
        channels, alpha = _parse_color(token)
        source = _format_color_key(channels, alpha)
        mapped = self._overrides.get(source)
        if mapped is not None:
            return mapped
        if alpha < 1:
            channels = _composite_over_midnight(channels, alpha)
        opaque = _format_color_key(channels, 1.0)
        mapped = self._overrides.get(opaque)
        if mapped is not None:
            return mapped
        if opaque.upper() in _PALETTE_HEXES:
            return opaque.upper()
        name = _pick_fallback_name(channels)
        self.fallbacks[(source, name)] += 1
        return PALETTE[name]

    def recolor(self, text: str) -> str:
        """Map every colour token in a colour-context string.

        Args:
            text: A style value, colour attribute value or JSON colour. Must
                not be free text: every ``#hex`` token in it is converted.

        Returns:
            The text with hex and ``rgb()``/``rgba()`` colours replaced.
            ``url(...)`` references, named colours and keywords are untouched.

        Raises:
            DeckBuildError: If a matched colour token cannot be parsed.
        """
        return _COLOR_RE.sub(self._map_match, text)

    def _map_match(self, match: re.Match[str]) -> str:
        """Map one regex match, passing ``url(...)`` through unchanged."""
        token = match[0]
        if token[:4].lower() == "url(":
            return token
        return self.map_color(token)


def _parse_color(token: str) -> tuple[tuple[int, int, int], float]:
    """Split a colour token into 0-255 channels and a 0-1 alpha."""
    text = token.strip().lower()
    try:
        if text.startswith("#"):
            return _parse_hex_color(text[1:])
        return _parse_rgb_color(text[text.index("(") + 1 : text.rindex(")")])
    except ValueError as error:
        raise DeckBuildError(f"cannot parse colour {token!r}") from error


def _parse_hex_color(digits: str) -> tuple[tuple[int, int, int], float]:
    """Parse 3, 4, 6 or 8 hex digits; raises ValueError on anything else."""
    short_lengths = (3, 4)
    if len(digits) in short_lengths:
        digits = "".join(digit * 2 for digit in digits)
    values = [int(digits[index : index + 2], 16) for index in range(0, len(digits), 2)]
    if len(values) == len("rgb"):
        values.append(0xFF)
    red, green, blue, alpha = values
    return (red, green, blue), alpha / 0xFF


def _parse_rgb_color(arguments: str) -> tuple[tuple[int, int, int], float]:
    """Parse the arguments of ``rgb()``/``rgba()``; raises ValueError if odd."""
    parts = [part for part in re.split(r"[\s,/]+", arguments.strip()) if part]
    if len(parts) == len("rgb"):
        parts.append("1")
    red, green, blue, alpha = parts
    channels = (_parse_channel(red), _parse_channel(green), _parse_channel(blue))
    return channels, _parse_fraction(alpha)


def _parse_channel(text: str) -> int:
    """Parse one colour channel given as a number or a percentage."""
    if text.endswith("%"):
        return round(_parse_fraction(text) * 0xFF)
    return max(0, min(0xFF, round(float(text))))


def _parse_fraction(text: str) -> float:
    """Parse an alpha value given as a 0-1 number or a percentage."""
    percent = 100.0
    value = float(text[:-1]) / percent if text.endswith("%") else float(text)
    return max(0.0, min(1.0, value))


def _format_color_key(channels: tuple[int, int, int], alpha: float) -> str:
    """Format the manifest lookup key: ``#rrggbb``, or ``#rrggbbaa`` if translucent."""
    red, green, blue = channels
    key = f"#{red:02x}{green:02x}{blue:02x}"
    if alpha < 1:
        key += f"{round(alpha * 0xFF):02x}"
    return key


def _composite_over_midnight(
    channels: tuple[int, int, int], alpha: float
) -> tuple[int, int, int]:
    """Flatten a translucent colour onto the Midnight slide background."""
    red, green, blue = (
        round(channel * alpha + _MIDNIGHT_CHANNEL * (1 - alpha)) for channel in channels
    )
    return red, green, blue


def _pick_fallback_name(channels: tuple[int, int, int]) -> str:
    """Choose the palette name for an off-palette colour by its HSL bucket."""
    red, green, blue = (channel / 0xFF for channel in channels)
    hue, lightness, saturation = colorsys.rgb_to_hls(red, green, blue)
    if saturation < _GREY_SATURATION:
        return _pick_step(_GREY_LIGHTNESS_STEPS, lightness, "White")
    if lightness < _DARK_LIGHTNESS:
        return "Slate"
    degrees = 360.0
    name = _pick_step(_HUE_STEPS, hue * degrees, "Ember")
    if name == "Violet":
        return "Mist" if lightness > _PALE_VIOLET_LIGHTNESS else "Neon"
    return name


def _pick_step(steps: Sequence[tuple[float, str]], value: float, default: str) -> str:
    """Return the name of the first step whose upper bound exceeds ``value``."""
    return next((name for bound, name in steps if value < bound), default)


def rename_brand(text: str) -> str:
    """Replace the old product name with the new one, whole words only.

    Args:
        text: Text or an attribute value. Must not be a base64 payload.

    Returns:
        The text with ``Jev`` and ``JEV`` replaced; longer words such as
        ``Jevons`` and other casings are left alone.
    """
    for pattern, replacement in _RENAMES:
        text = pattern.sub(replacement, text)
    return text


def _split_declarations(style: str) -> list[tuple[str, str]]:
    """Split CSS declarations on top-level semicolons into (property, value)."""
    chunks: list[str] = []
    depth = 0
    quote = ""
    start = 0
    for index, char in enumerate(style):
        if quote:
            quote = "" if char == quote else quote
        elif char in "\"'":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth = max(depth - 1, 0)
        elif char == ";" and depth == 0:
            chunks.append(style[start:index])
            start = index + 1
    chunks.append(style[start:])
    pairs = (chunk.partition(":") for chunk in chunks)
    return [(name.strip(), value.strip()) for name, _, value in pairs if name.strip()]


def _join_declarations(declarations: Sequence[tuple[str, str]]) -> str:
    """Serialise (property, value) pairs back into declaration text."""
    return "".join(f"{name}: {value}; " for name, value in declarations).rstrip()


def _is_heavy_weight(value: str) -> bool:
    """Tell whether a font-weight value is medium or heavier."""
    token = value.split()[0].lower() if value.split() else ""
    if token in _HEAVY_KEYWORDS:
        return True
    try:
        return float(token) >= _MEDIUM_WEIGHT
    except ValueError:
        return False


def _restyle_font_shorthand(value: str) -> str:
    """Swap the family in a ``font`` shorthand and clamp its weight and slant."""
    match = _FONT_SHORTHAND_RE.match(value)
    if match is None:
        return value
    tokens = match["prefix"].split()
    heavy = any(_is_heavy_weight(token) for token in tokens)
    kept = [
        token
        for token in tokens
        if token.lower() not in _SLANTED_KEYWORDS
        and not token.isdigit()
        and token.lower() not in _HEAVY_KEYWORDS
    ]
    if heavy:
        kept.append(str(_MEDIUM_WEIGHT))
    family = _MEDIUM_FAMILY if heavy else _REGULAR_FAMILY
    # The brand linter only parses a size/line-height pair without spaces.
    size = _SLASH_RE.sub("/", match["size"])
    return " ".join([*kept, size, family])


def _restyle_font_value(name: str, value: str, heavy: bool) -> str:
    """Apply the brand typeface rules to one declaration value."""
    if name == "font-family":
        return _MEDIUM_FAMILY if heavy else _REGULAR_FAMILY
    if name == "font-weight" and _is_heavy_weight(value):
        return str(_MEDIUM_WEIGHT)
    if name == "font-style" and value.lower() in _SLANTED_KEYWORDS:
        return "normal"
    if name == "font":
        return _restyle_font_shorthand(value)
    return value


def _restyle_declarations(
    declarations: Sequence[tuple[str, str]], mapper: ColorMapper, frame_rule: str
) -> list[tuple[str, str]]:
    """Restyle declarations; ``frame_rule`` is ``flatten``, ``exempt`` or ``css``."""
    heavy = any(
        name.lower() == "font-weight" and _is_heavy_weight(value)
        for name, value in declarations
    )
    restyled: list[tuple[str, str]] = []
    for name, value in declarations:
        prop = name.lower()
        if frame_rule != "css" and prop == "box-shadow":
            continue
        is_gradient = prop in _BACKGROUND_PROPERTIES and "gradient(" in value
        # The brand forbids gradients in content, so the whole declaration
        # becomes a flat background rather than a recoloured gradient.
        if frame_rule == "flatten" and is_gradient:
            restyled.append(("background", _MIDNIGHT))
            continue
        font_value = _restyle_font_value(prop, value, heavy)
        restyled.append((name, mapper.recolor(font_value)))
    return restyled


def restyle_style(style: str, mapper: ColorMapper, *, exempt: bool = False) -> str:
    """Restyle the declarations of one inline ``style`` attribute on a slide.

    Args:
        style: Unescaped declaration text.
        mapper: Colour mapper; its fallback counters are updated.
        exempt: True when the element sits inside a class that may keep CSS
            gradients (cover, divider and band layouts).

    Returns:
        Normalised declaration text (``name: value;`` pairs) with brand fonts,
        weights clamped to 500, italics removed, palette colours, no
        ``box-shadow`` and, unless exempt, gradients flattened to Midnight.

    Raises:
        DeckBuildError: If a colour in the style cannot be parsed.
    """
    frame_rule = "exempt" if exempt else "flatten"
    declarations = _split_declarations(style)
    return _join_declarations(_restyle_declarations(declarations, mapper, frame_rule))


def restyle_css(css: str, mapper: ColorMapper) -> str:
    """Restyle every declaration block of a stylesheet.

    Args:
        css: Stylesheet text.
        mapper: Colour mapper; its fallback counters are updated.

    Returns:
        The stylesheet with brand fonts and palette colours inside declaration
        blocks. Selectors and at-rule preludes are left byte for byte, so id
        selectors such as ``#abc`` are never treated as colours.

    Raises:
        DeckBuildError: If a colour in a declaration cannot be parsed.
    """

    def restyle_block(match: re.Match[str]) -> str:
        """Restyle one innermost ``{...}`` block if it holds declarations."""
        if ":" not in match[1]:
            return match[0]
        declarations = _split_declarations(match[1])
        restyled = _restyle_declarations(declarations, mapper, "css")
        return "{" + _join_declarations(restyled) + "}"

    return _CSS_BLOCK_RE.sub(restyle_block, css)


def _escape_text(text: str) -> str:
    """Escape text for use inside a double-quoted HTML attribute."""
    return html.escape(text, quote=False).replace('"', "&quot;")


def _escape_style(style: str) -> str:
    """Escape a style value, using single quotes so no entity is needed."""
    return html.escape(style, quote=False).replace('"', "'")


def _read_classes(tag: str) -> frozenset[str]:
    """Return the class tokens of an opening tag."""
    for attribute in _ATTR_RE.finditer(tag):
        if attribute["name"].lower() == "class":
            return frozenset((attribute["dq"] or attribute["sq"] or "").split())
    return frozenset()


def _restyle_attribute(
    attribute: re.Match[str], mapper: ColorMapper, exempt: bool
) -> str:
    """Return one attribute with its value restyled, or unchanged."""
    name = attribute["name"].lower()
    value = attribute["dq"] if attribute["dq"] is not None else attribute["sq"]
    if name == "style":
        style = restyle_style(html.unescape(value), mapper, exempt=exempt)
        restyled = _escape_style(style)
    elif name in _COLOR_ATTRIBUTES or name in _SMIL_ATTRIBUTES:
        restyled = mapper.recolor(value)
    elif name in _RENAMED_ATTRIBUTES:
        restyled = rename_brand(value)
    else:
        return attribute[0]
    if restyled == value:
        return attribute[0]
    restyled = restyled.replace('"', "&quot;")
    return f'{attribute["lead"]}{attribute["name"]}="{restyled}"'


def _restyle_tag(tag: str, mapper: ColorMapper, exempt: bool) -> str:
    """Restyle the attributes and embedded SVG images of one opening tag."""
    tag = _SVG_URI_RE.sub(lambda uri: _restyle_svg_uri(uri, mapper), tag)
    return _ATTR_RE.sub(
        lambda attribute: _restyle_attribute(attribute, mapper, exempt), tag
    )


def restyle_markup(markup: str, mapper: ColorMapper, *, exempt: bool = False) -> str:
    """Apply the global brand transforms to a run of slide or SVG markup.

    Tags are rewritten attribute by attribute and text nodes only get the
    product rename, so whitespace and line breaks in text survive untouched.

    Args:
        markup: HTML or SVG markup. Need not be a single element.
        mapper: Colour mapper; its fallback counters are updated.
        exempt: True when the markup already sits inside a gradient-exempt
            element.

    Returns:
        The markup with palette colours in ``style`` and paint attributes,
        brand fonts, flattened gradients (outside exempt classes), no
        ``box-shadow``, recoloured base64 SVG images and renamed text and alts.

    Raises:
        DeckBuildError: If a colour cannot be parsed or an embedded SVG image
            is not valid base64 UTF-8.
    """
    pieces: list[str] = []
    exempt_stack = [exempt]
    cursor = 0
    for tag in _TAG_RE.finditer(markup):
        pieces.append(rename_brand(markup[cursor : tag.start()]))
        pieces.append(_restyle_element_tag(tag, mapper, exempt_stack))
        cursor = tag.end()
    pieces.append(rename_brand(markup[cursor:]))
    return "".join(pieces)


def _restyle_element_tag(
    tag: re.Match[str], mapper: ColorMapper, exempt_stack: list[bool]
) -> str:
    """Restyle one tag and keep the gradient-exemption stack in step with nesting."""
    text = tag[0]
    if tag["name"] is None:
        return text
    if text.startswith("</"):
        # The base entry stays so unbalanced fragments cannot empty the stack.
        if len(exempt_stack) > 1:
            exempt_stack.pop()
        return text
    exempt = exempt_stack[-1] or bool(_read_classes(text) & _GRADIENT_EXEMPT_CLASSES)
    is_leaf = text.endswith("/>") or tag["name"].lower() in _VOID_ELEMENTS
    if not is_leaf:
        exempt_stack.append(exempt)
    return _restyle_tag(text, mapper, exempt)


def _decode_svg(payload: str) -> str:
    """Decode a base64 SVG payload into text."""
    try:
        return base64.b64decode(payload, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as error:
        raise DeckBuildError("embedded SVG image is not base64 UTF-8") from error


def _encode_svg(svg: str) -> str:
    """Encode SVG text as a base64 payload."""
    return base64.b64encode(svg.encode("utf-8")).decode("ascii")


def restyle_svg(svg: str, mapper: ColorMapper) -> str:
    """Recolour a standalone SVG document and drop its root ``aria-label``.

    Args:
        svg: SVG source text.
        mapper: Colour mapper; its fallback counters are updated.

    Returns:
        The SVG with palette colours in paint attributes, SMIL colour lists
        and inline styles. The root label is removed because the surrounding
        ``<img alt>`` already describes the image.

    Raises:
        DeckBuildError: If the text has no ``<svg>`` root or holds an
            unparseable colour.
    """
    root = next(
        (tag for tag in _TAG_RE.finditer(svg) if (tag["name"] or "").lower() == "svg"),
        None,
    )
    if root is None:
        raise DeckBuildError("embedded SVG image has no <svg> root")
    unlabelled = _ARIA_LABEL_RE.sub("", root[0], count=1)
    svg = svg[: root.start()] + unlabelled + svg[root.end() :]
    return restyle_markup(svg, mapper, exempt=True)


def _restyle_svg_uri(uri: re.Match[str], mapper: ColorMapper) -> str:
    """Decode, restyle and re-encode one base64 SVG data URI."""
    svg = restyle_svg(_decode_svg(uri[1]), mapper)
    return f"data:image/svg+xml;base64,{_encode_svg(svg)}"


def expand_tokens(text: str, brand_dir: Path) -> str:
    """Expand ``{{logo:...}}`` and ``{{bg:...}}`` tokens from the brand kit.

    Args:
        text: Fragment HTML or CSS.
        brand_dir: Brand directory holding ``assets/logos`` and
            ``assets/backgrounds``. Read from disk for every token.

    Returns:
        The text with ``{{logo:<kind>-<variant>:<height>}}`` replaced by the
        official SVG inlined in a sized span, and ``{{bg:<name>}}`` replaced by
        a JPEG data URI.

    Raises:
        DeckBuildError: If a token is malformed, of an unknown kind, or names
            an asset that does not exist.
    """
    return _TOKEN_RE.sub(lambda token: _expand_token(token[1], brand_dir), text)


def _expand_token(body: str, brand_dir: Path) -> str:
    """Expand the body of one ``{{...}}`` token."""
    match body.split(":"):
        case ["logo", name, height] if height.isdigit():
            path = brand_dir / "assets" / "logos" / f"impel-logo-{name}.svg"
            return _render_logo(_read_asset(path, body).decode("utf-8"), height)
        case ["bg", name]:
            path = brand_dir / "assets" / "backgrounds" / f"impel-bg-{name}.jpg"
            payload = base64.b64encode(_read_asset(path, body)).decode("ascii")
            return f"data:image/jpeg;base64,{payload}"
        case _:
            raise DeckBuildError(f"unknown token {{{{{body}}}}}")


def _read_asset(path: Path, token: str) -> bytes:
    """Read a brand asset, naming the token when the file is missing."""
    try:
        return path.read_bytes()
    except OSError as error:
        raise DeckBuildError(f"token {{{{{token}}}}}: cannot read {path}") from error


def _render_logo(svg: str, height: str) -> str:
    """Wrap an official logo SVG in a span, sized by height only."""
    root = re.search(r"<svg\b[^>]*>", svg)
    if root is None:
        raise DeckBuildError("logo file has no <svg> root")
    # Only the root's size changes; paths and fills are the official artwork.
    tag = re.sub(r"\s(?:width|height)=\"[^\"]*\"", "", root[0])
    tag = tag.replace("<svg", f'<svg height="{height}"', 1)
    sized = svg[: root.start()] + tag + svg[root.end() :]
    return (
        f'<span data-impel-logo style="display:inline-block;height:{height}px">'
        f"{sized}</span>"
    )


class _Element(NamedTuple):
    """Spans of one element inside a markup string."""

    open_start: int
    open_end: int
    close_start: int
    close_end: int


def _find_element(
    markup: str, is_wanted: Callable[[re.Match[str]], bool]
) -> _Element | None:
    """Locate the first element whose opening tag satisfies ``is_wanted``."""
    tags = _TAG_RE.finditer(markup)
    opening = next(
        (tag for tag in tags if _is_opening(tag) and is_wanted(tag)),
        None,
    )
    if opening is None:
        return None
    name = opening["name"].lower()
    if name in _VOID_ELEMENTS or opening[0].endswith("/>"):
        return _Element(opening.start(), opening.end(), opening.end(), opening.end())
    depth = 1
    for tag in tags:
        if (tag["name"] or "").lower() != name:
            continue
        depth += 1 if _is_opening(tag) else -1
        if depth == 0:
            return _Element(opening.start(), opening.end(), tag.start(), tag.end())
    raise DeckBuildError(f"unbalanced <{name}> element")


def _is_opening(tag: re.Match[str]) -> bool:
    """Tell whether a tag match is an opening (or self-closing) element tag."""
    return tag["name"] is not None and not tag[0].startswith("</")


def _has_attribute(name: str) -> Callable[[re.Match[str]], bool]:
    """Build a predicate matching opening tags that carry an attribute."""
    pattern = re.compile(rf"\s{re.escape(name)}(?=[\s=>/])")
    return lambda tag: pattern.search(tag[0]) is not None


def _has_name(name: str) -> Callable[[re.Match[str]], bool]:
    """Build a predicate matching opening tags of one element name."""
    return lambda tag: tag["name"].lower() == name


def split_frames(inner: str) -> list[str]:
    """Split a slide's inner HTML into its top-level frame elements.

    Args:
        inner: Inner HTML of a ``.slide`` element.

    Returns:
        One string per top-level ``<div>``; joining them gives ``inner`` back.

    Raises:
        DeckBuildError: If anything but whitespace sits between the frames or
            the ``<div>`` tags do not balance.
    """
    frames: list[str] = []
    cursor = 0
    while cursor < len(inner):
        frame = _find_element(inner[cursor:], _has_name("div"))
        gap_end = len(inner) if frame is None else cursor + frame.open_start
        if inner[cursor:gap_end].strip():
            raise DeckBuildError("slide has content outside its top-level frames")
        if frame is None:
            break
        frames.append(inner[cursor : cursor + frame.close_end])
        cursor += frame.close_end
    return frames


def _read_frame_id(frame: str) -> str:
    """Return the ``data-frame-id`` of a frame's own opening tag, or ''."""
    match = _FRAME_ID_RE.search(frame.partition(">")[0])
    return match[1] if match else ""


def _matches_frame(frame_id: str, reference: str) -> bool:
    """Tell whether a manifest frame reference names this frame id."""
    return frame_id == reference or frame_id.startswith(f"{reference}-")


def _edit_frames(
    frames: list[str], reference: str, edit: Callable[[str], str | None]
) -> None:
    """Apply ``edit`` in place to every frame matching ``reference``."""
    indexes = [
        index
        for index, frame in enumerate(frames)
        if _matches_frame(_read_frame_id(frame), reference)
    ]
    if not indexes:
        raise DeckBuildError(f"frame {reference!r} not found")
    for index in indexes:
        edited = edit(frames[index])
        if edited is None:
            raise DeckBuildError(f"frame {reference!r} has no element to edit")
        frames[index] = edited


def _drop_frame(_frame: str) -> str:
    """Replace a frame with nothing; the empty slot no longer matches any id."""
    return ""


def _replace_text(frame: str, inner_html: str) -> str | None:
    """Replace the inner HTML of the frame's text element."""
    element = _find_element(frame, _has_attribute("data-text-path"))
    if element is None:
        return None
    return frame[: element.open_end] + inner_html + frame[element.close_start :]


def _set_tag_style(tag: str, declarations: Sequence[tuple[str, str]]) -> str:
    """Set or replace style declarations on an opening tag."""
    style = next(
        (attr for attr in _ATTR_RE.finditer(tag) if attr["name"].lower() == "style"),
        None,
    )
    value = (style["dq"] or style["sq"] or "") if style else ""
    merged = dict(_split_declarations(html.unescape(value)))
    merged.update(declarations)
    attribute = f' style="{_escape_style(_join_declarations(list(merged.items())))}"'
    if style is None:
        return tag[:-1] + attribute + ">"
    return tag[: style.start()] + attribute + tag[style.end() :]


def _set_frame_style(frame: str, edit: StyleEdit) -> str | None:
    """Apply a style edit: box properties to the frame, the rest to its text."""
    box = [pair for pair in edit.declarations if pair[0] in _FRAME_BOX_PROPERTIES]
    rest = [pair for pair in edit.declarations if pair[0] not in _FRAME_BOX_PROPERTIES]
    if rest:
        text = _find_element(frame, _has_attribute("data-text-path"))
        if text is None:
            return None
        tag = _set_tag_style(frame[text.open_start : text.open_end], rest)
        frame = frame[: text.open_start] + tag + frame[text.open_end :]
    if box:
        end = frame.index(">") + 1
        frame = _set_tag_style(frame[:end], box) + frame[end:]
    return frame


def _replace_alt(frame: str, text: str) -> str | None:
    """Replace the alt text of the frame's image."""
    image = _find_element(frame, _has_name("img"))
    if image is None:
        return None
    tag = frame[image.open_start : image.open_end]
    alt = next(
        (attr for attr in _ATTR_RE.finditer(tag) if attr["name"].lower() == "alt"),
        None,
    )
    attribute = f' alt="{_escape_text(text)}"'
    if alt is None:
        tag = tag[:4] + attribute + tag[4:]
    else:
        tag = tag[: alt.start()] + attribute + tag[alt.end() :]
    return frame[: image.open_start] + tag + frame[image.open_end :]


def _replace_icon(frame: str, icon_markup: str) -> str | None:
    """Swap the frame's glyph icon for a stroke icon in the glyph's colour."""
    glyph = _find_element(frame, _has_attribute("data-icon-path"))
    if glyph is None:
        return None
    fill = re.search(r"\sfill=\"([^\"]*)\"", frame[glyph.open_end : glyph.close_end])
    stroke = fill[1] if fill else "currentColor"
    icon = (
        f'<svg viewBox="0 0 24 24" fill="none" stroke="{stroke}" stroke-width="2" '
        'stroke-linecap="round" stroke-linejoin="round" '
        'style="width:100%;height:100%;display:block" aria-hidden="true">'
        f"{icon_markup}</svg>"
    )
    return frame[: glyph.open_start] + icon + frame[glyph.close_end :]


def _edit_svg(frame: str, edit: SvgEdit) -> str | None:
    """Apply a regex substitution to the frame's base64 SVG image."""
    uri = _SVG_URI_RE.search(frame)
    if uri is None:
        return None
    try:
        svg, count = re.subn(edit.pattern, edit.repl, _decode_svg(uri[1]))
    except re.error as error:
        raise DeckBuildError(
            f"svg_edit pattern {edit.pattern!r} is not a valid regex: {error}"
        ) from error
    if not count:
        raise DeckBuildError(
            f"svg_edit pattern {edit.pattern!r} matches nothing in frame {edit.frame!r}"
        )
    return frame[: uri.start(1)] + _encode_svg(svg) + frame[uri.end(1) :]


def _look_up_icon(icons: Mapping[str, str], name: str) -> str:
    """Return the markup of a named icon."""
    if name not in icons:
        raise DeckBuildError(f"icon {name!r} is not defined in {_ICONS_FILE}")
    return icons[name]


def apply_frame_edits(inner: str, edits: FrameEdits, icons: Mapping[str, str]) -> str:
    """Apply a manifest slide's frame edits to a source slide.

    Edits run in a fixed order: drops, then text, style, alt, icon and SVG
    edits. SVG edits see the source image, before any recolouring.
    A frame reference matches a frame whose id equals it or starts with it
    followed by ``-`` (so ``e1`` matches ``e1-abc`` but not ``e12-abc``).

    Args:
        inner: Inner HTML of the source ``.slide`` element.
        edits: The edits to apply.
        icons: Icon name to inner SVG markup for a 24x24 viewBox.

    Returns:
        The edited inner HTML. Untouched frames keep their exact bytes.

    Raises:
        DeckBuildError: If a reference matches no frame, the frame lacks the
            element the edit targets, an icon name is unknown, or an SVG edit
            pattern is invalid or matches nothing.
    """
    frames = split_frames(inner)
    for reference in edits.drop:
        _edit_frames(frames, reference, _drop_frame)
    for text in edits.text:
        _edit_frames(frames, text.frame, partial(_replace_text, inner_html=text.html))
    for style in edits.style:
        _edit_frames(frames, style.frame, partial(_set_frame_style, edit=style))
    for alt in edits.alt:
        _edit_frames(frames, alt.frame, partial(_replace_alt, text=alt.text))
    for icon in edits.icons:
        markup = _look_up_icon(icons, icon.icon)
        _edit_frames(frames, icon.frame, partial(_replace_icon, icon_markup=markup))
    for svg_edit in edits.svg_edit:
        _edit_frames(frames, svg_edit.frame, partial(_edit_svg, edit=svg_edit))
    return "".join(frames)


def make_build_steps(
    groups: Sequence[Sequence[str]], effect: str
) -> list[dict[str, Any]]:
    """Generate the motion ``steps`` for click-to-reveal groups.

    Args:
        groups: Frame ids revealed per click, in order.
        effect: Effect name used for every reveal.

    Returns:
        Step 0 hides every listed frame; step ``k`` reveals group ``k`` and
        keeps later groups hidden. With no groups this is the single idle step
        an un-built slide has.
    """
    everything = [frame_id for group in groups for frame_id in group]
    steps: list[dict[str, Any]] = [{"hidden": everything, "events": [], "auto": False}]
    for index, group in enumerate(groups):
        later = [frame_id for rest in groups[index + 1 :] for frame_id in rest]
        events = [{"id": frame_id, "dir": "in", "effect": effect} for frame_id in group]
        steps.append({"hidden": later, "events": events, "auto": False})
    return steps


def render_rail(sections: Sequence[Section], current: int) -> str:
    """Render the agenda rail frame for a slide.

    Args:
        sections: All agenda sections in order.
        current: 1-based number of the slide's section.

    Returns:
        The rail frame markup: a label for the current section and one bar
        segment per section, marked done, now or next. Each segment's width
        is weighted by ``end - start``; the rail shows no times.
    """
    section = sections[current - 1]
    segments = "".join(
        f'<span class="rail-seg {_pick_rail_state(number, current)}" '
        f'style="flex:{each.end - each.start} 1 0"></span>'
        for number, each in enumerate(sections, start=1)
    )
    return (
        '<div data-frame-id="rail" class="rail" style="position:absolute;left:128px;'
        'top:1010px;width:1664px;height:46px"><div class="rail-label">'
        f'<span class="rail-n">{current}</span>'
        f'<span class="rail-name">{_escape_text(section.short)}</span>'
        "</div>"
        f'<div class="rail-bar">{segments}</div></div>'
    )


def render_page_number(number: int, total: int) -> str:
    """Render the slide-number frame shown at the top of a slide.

    Args:
        number: 1-based position of the slide in the output deck.
        total: Number of slides in the output deck.

    Returns:
        The ``pageno`` frame markup, with both numbers zero-padded to at
        least two digits. Colours and fonts come from the stylesheet.
    """
    return (
        '<div data-frame-id="pageno" class="pageno" style="position:absolute;'
        f'right:128px;top:60px"><span class="pageno-n">{number:02d}</span>'
        f'<span class="pageno-t">/ {total:02d}</span></div>'
    )


def _pick_rail_state(number: int, current: int) -> str:
    """Name the rail segment state for a section relative to the current one."""
    if number < current:
        return "done"
    return "now" if number == current else "next"


@dataclass(frozen=True)
class _Table:
    """A TOML table with typed accessors that name the table in errors."""

    raw: Mapping[str, Any]
    where: str

    def fail(self, message: str) -> DeckBuildError:
        """Build an error naming this table."""
        return DeckBuildError(f"{self.where}: {message}")

    def check_keys(self, allowed: frozenset[str]) -> None:
        """Reject keys the schema does not define."""
        unknown = sorted(set(self.raw) - allowed)
        if unknown:
            raise self.fail(f"unknown key(s) {', '.join(unknown)}")

    def get[T](self, key: str, kind: type[T]) -> T | None:
        """Return an optional value, checking its type."""
        value = self.raw.get(key)
        if value is None:
            return None
        if not isinstance(value, kind) or isinstance(value, bool):
            raise self.fail(f"'{key}' must be of type {kind.__name__}")
        return value

    def need[T](self, key: str, kind: type[T]) -> T:
        """Return a required value, checking its type."""
        value = self.get(key, kind)
        if value is None:
            raise self.fail(f"'{key}' is required")
        return value

    def strings(self, key: str) -> tuple[str, ...]:
        """Return an optional list of strings as a tuple."""
        return _read_strings(
            self.get(key, list) or [], self.fail(f"'{key}' must be a list of strings")
        )

    def tables(self, key: str, allowed: frozenset[str]) -> tuple[_Table, ...]:
        """Return an optional list of tables, each checked for unknown keys."""
        tables: list[_Table] = []
        for index, raw in enumerate(self.get(key, list) or []):
            if not isinstance(raw, dict):
                raise self.fail(f"'{key}' must be a list of tables")
            table = _Table(raw, f"{self.where} {key}[{index}]")
            table.check_keys(allowed)
            tables.append(table)
        return tuple(tables)

    def path(self, key: str, base_dir: Path) -> Path | None:
        """Return an optional path resolved against the manifest directory."""
        value = self.get(key, str)
        return None if value is None else base_dir / value


def _read_strings(values: Sequence[Any], error: DeckBuildError) -> tuple[str, ...]:
    """Return the values as a tuple of strings, raising ``error`` otherwise."""
    if not all(isinstance(value, str) for value in values):
        raise error
    return tuple(values)


def load_manifest(path: Path) -> Manifest:
    """Load and validate the deck manifest.

    Args:
        path: TOML manifest. Paths inside it resolve against its directory.

    Returns:
        The typed manifest.

    Raises:
        DeckBuildError: If the file cannot be read, is not valid TOML, has
            unknown keys, wrong types, or inconsistent slide entries.
    """
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise DeckBuildError(f"cannot load manifest {path}: {error}") from error
    table = _Table(raw, "manifest")
    table.check_keys(_MANIFEST_KEYS)
    sections = tuple(
        Section(each.need("short", str), each.need("start", int), each.need("end", int))
        for each in table.tables("sections", _SECTION_KEYS)
    )
    slides = tuple(
        _parse_slide(each, path.parent, len(sections))
        for each in table.tables("slides", _SLIDE_KEYS)
    )
    return Manifest(
        title=table.need("title", str),
        forbidden=table.strings("forbidden"),
        colors=_parse_colors(
            _Table(table.get("colors", dict) or {}, "manifest [colors]")
        ),
        sections=sections,
        slides=slides,
        base_dir=path.parent,
    )


def _parse_colors(table: _Table) -> dict[str, str]:
    """Validate the colour override table."""
    colors: dict[str, str] = {}
    for key in table.raw:
        value = table.need(key, str).upper()
        if not _COLOR_KEY_RE.fullmatch(key):
            raise table.fail(f"key {key!r} must be lowercase #rrggbb or #rrggbbaa")
        if value not in _PALETTE_HEXES:
            raise table.fail(f"{key!r} maps to {value}, which is not a palette colour")
        colors[key] = value
    return colors


def _parse_slide(table: _Table, base_dir: Path, section_count: int) -> SlideSpec:
    """Parse and cross-check one ``[[slides]]`` entry."""
    builds = table.get("builds", list)
    not_strings = table.fail("'builds' must be a list of lists of frame ids")
    spec = SlideSpec(
        source=table.get("source", int),
        fragment=table.path("fragment", base_dir),
        label=table.get("label", str),
        notes=table.get("notes", str),
        section=table.get("section", int) or 0,
        bg=table.get("bg", str),
        slide_class=table.get("slide_class", str),
        transition=table.get("transition", str),
        edits=_parse_edits(table),
        prepend=table.path("prepend", base_dir),
        append=table.path("append", base_dir),
        builds=None
        if builds is None
        else tuple(
            _read_strings(_read_group(group, not_strings), not_strings)
            for group in builds
        ),
        build_effect=table.get("build_effect", str) or _DEFAULT_BUILD_EFFECT,
        colors=_parse_colors(
            _Table(table.get("colors", dict) or {}, f"{table.where} colors")
        ),
    )
    problem = _find_slide_problem(spec, section_count)
    if problem:
        raise table.fail(problem)
    return spec


def _read_group(group: object, error: DeckBuildError) -> list[Any]:
    """Return one build group as a list, raising ``error`` otherwise."""
    if not isinstance(group, list):
        raise error
    return group


def _find_slide_problem(spec: SlideSpec, section_count: int) -> str | None:
    """Describe the first inconsistency in a slide entry, if any."""
    if (spec.source is None) == (spec.fragment is None):
        return "needs exactly one of 'source' or 'fragment'"
    if not 0 <= spec.section <= section_count:
        return f"'section' must be between 0 and {section_count}"
    if spec.fragment is None:
        return "'bg' applies to fragment slides only" if spec.bg else None
    if spec.label is None or spec.notes is None:
        return "a fragment slide needs 'label' and 'notes'"
    if spec.edits != FrameEdits():
        return "frame edits apply to source slides only; edit the fragment file"
    return None


def _parse_edits(table: _Table) -> FrameEdits:
    """Parse the frame edit lists of one slide entry."""
    styles = []
    for each in table.tables("style", frozenset({"frame", "set"})):
        declarations = _Table(each.need("set", dict), f"{each.where} set")
        pairs = tuple((key, declarations.need(key, str)) for key in declarations.raw)
        styles.append(StyleEdit(each.need("frame", str), pairs))
    return FrameEdits(
        drop=table.strings("drop"),
        text=tuple(
            TextEdit(each.need("frame", str), each.need("html", str))
            for each in table.tables("text", frozenset({"frame", "html"}))
        ),
        style=tuple(styles),
        alt=tuple(
            AltEdit(each.need("frame", str), each.need("text", str))
            for each in table.tables("alt", frozenset({"frame", "text"}))
        ),
        icons=tuple(
            IconEdit(each.need("frame", str), each.need("icon", str))
            for each in table.tables("icons", frozenset({"frame", "icon"}))
        ),
        svg_edit=tuple(
            SvgEdit(
                each.need("frame", str),
                each.need("pattern", str),
                each.need("repl", str),
            )
            for each in table.tables(
                "svg_edit", frozenset({"frame", "pattern", "repl"})
            )
        ),
    )


def _read_attribute(tag: str, name: str) -> str:
    """Return the raw value of an attribute on a tag, or ''."""
    for attribute in _ATTR_RE.finditer(tag):
        if attribute["name"].lower() == name:
            return attribute["dq"] if attribute["dq"] is not None else attribute["sq"]
    return ""


def _parse_source_slide(number: int, section: str) -> SourceSlide:
    """Split one source ``<section>`` into its header attributes and inner HTML."""
    tags = _TAG_RE.finditer(section)
    header = next(tags)[0]
    slide_tag = next((tag for tag in tags if "slide" in _read_classes(tag[0])), None)
    if slide_tag is None or not section.endswith(_SLIDE_TAIL):
        raise DeckBuildError(f"source slide {number} does not have the expected shape")
    return SourceSlide(
        number=number,
        label=_read_attribute(header, "data-label"),
        notes=_read_attribute(header, "data-speaker-notes"),
        open_tag=slide_tag[0],
        inner=section[slide_tag.end() : -len(_SLIDE_TAIL)],
    )


def parse_source_deck(source_html: str) -> SourceDeck:
    """Split the exported deck into the parts the build rewrites.

    Args:
        source_html: The complete source document.

    Returns:
        The head pieces, the slides and the parsed motion JSON.

    Raises:
        DeckBuildError: If the document does not have the exported deck's
            shape: two stylesheets, a ``deck-motion`` script, a ``<main>`` of
            ``.deck-slide`` sections, and one motion entry per slide.
    """
    main_start = source_html.find("<main")
    main_end = source_html.find("</main>")
    motion_script = _MOTION_RE.search(source_html)
    if main_start < 0 or main_end < 0 or motion_script is None:
        raise DeckBuildError("source deck has no <main> or no deck-motion script")
    styles = list(_STYLE_BLOCK_RE.finditer(source_html, 0, motion_script.start()))
    main_tag = _TAG_RE.match(source_html, main_start)
    editor_and_deck = 2
    if len(styles) != editor_and_deck or main_tag is None:
        raise DeckBuildError("source deck must have exactly two <style> blocks")
    try:
        motion = json.loads(motion_script[2])
    except json.JSONDecodeError as error:
        raise DeckBuildError("deck-motion script is not valid JSON") from error
    sections = _SECTION_RE.findall(source_html, main_tag.end(), main_end)
    if len(sections) != len(motion["slides"]):
        raise DeckBuildError("source deck slide count differs from deck-motion")
    return SourceDeck(
        head=source_html[: styles[0].start()],
        editor_css=styles[0][1],
        deck_css=styles[1][1],
        runtime=source_html[motion_script.end() : main_start],
        main_tag=main_tag[0],
        slides=tuple(
            _parse_source_slide(number, section)
            for number, section in enumerate(sections, start=1)
        ),
        tail=source_html[main_end:],
        motion=motion,
    )


@dataclass(frozen=True)
class _BuildContext:
    """Everything a slide build needs besides its own manifest entry."""

    deck: SourceDeck
    manifest: Manifest
    brand_dir: Path
    mapper: ColorMapper
    icons: Mapping[str, str]
    # 1-based output slide number; _build_slide sets it for the slide it builds.
    page: int = 0


@dataclass(frozen=True)
class _BuiltSlide:
    """One finished output slide."""

    label: str
    notes: str
    body: str
    motion: Mapping[str, Any]


def _read_fragment(path: Path, context: _BuildContext) -> str:
    """Read a fragment file and expand its brand tokens."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise DeckBuildError(f"cannot read fragment {path}") from error
    if _INLINE_SHEET_RE.search(text):
        raise DeckBuildError(
            f"fragment {path.name} contains <style> or <script>; "
            f"put styles in {_CSS_FILE} instead"
        )
    return expand_tokens(text, context.brand_dir)


def _add_extras(inner: str, spec: SlideSpec, context: _BuildContext) -> str:
    """Add the prepend/append fragments, agenda rail and page number to the frames."""
    before = _read_fragment(spec.prepend, context) if spec.prepend else ""
    after = _read_fragment(spec.append, context) if spec.append else ""
    rail = render_rail(context.manifest.sections, spec.section) if spec.section else ""
    page = render_page_number(context.page, len(context.manifest.slides))
    return before + inner + after + rail + page


def _resolve_builds(spec: SlideSpec, frame_ids: Sequence[str]) -> list[list[str]]:
    """Turn the manifest's build references into full frame ids of the slide."""
    groups: list[list[str]] = []
    for group in spec.builds or ():
        resolved: list[str] = []
        for reference in group:
            exact = [frame_id for frame_id in frame_ids if frame_id == reference]
            prefixed = [
                frame_id
                for frame_id in frame_ids
                if spec.source is not None and frame_id.startswith(f"{reference}-")
            ]
            matches = exact or prefixed
            if len(matches) != 1:
                raise DeckBuildError(
                    f"build frame {reference!r} matches {len(matches)} frames"
                )
            resolved.append(matches[0])
        groups.append(resolved)
    return groups


def _filter_steps(
    steps: Sequence[Mapping[str, Any]], dropped: frozenset[str]
) -> list[dict[str, Any]]:
    """Remove dropped frames from inherited build steps, and steps left empty."""
    filtered: list[dict[str, Any]] = []
    for index, step in enumerate(steps):
        events = [event for event in step["events"] if event["id"] not in dropped]
        hidden = [frame_id for frame_id in step["hidden"] if frame_id not in dropped]
        # A click that would reveal nothing is dropped; step 0 always stays
        # because the runtime expects every slide to have an initial step.
        if index and step["events"] and not events:
            continue
        filtered.append({**step, "hidden": hidden, "events": events})
    return filtered


def _check_motion_names(spec: SlideSpec, motion: Mapping[str, Any]) -> None:
    """Reject build effects and transitions the runtime does not define."""
    if spec.build_effect not in motion.get("effects", {}):
        raise DeckBuildError(f"unknown build_effect {spec.build_effect!r}")
    if spec.transition and spec.transition not in motion.get("transMs", {}):
        raise DeckBuildError(f"unknown transition {spec.transition!r}")


def _add_slide_class(open_tag: str, slide_class: str | None) -> str:
    """Add the manifest's extra class to the ``.slide`` opening tag."""
    if not slide_class:
        return open_tag
    return open_tag.replace('class="slide"', f'class="slide {slide_class}"', 1)


def _build_source_slide(
    spec: SlideSpec, number: int, context: _BuildContext
) -> _BuiltSlide:
    """Build an output slide from a source slide and its manifest edits."""
    slides = context.deck.slides
    if not 1 <= number <= len(slides):
        raise DeckBuildError(f"there is no source slide {number}")
    source = slides[number - 1]
    edited = apply_frame_edits(source.inner, spec.edits, context.icons)
    dropped = frozenset(_FRAME_ID_RE.findall(source.inner)) - frozenset(
        _FRAME_ID_RE.findall(edited)
    )
    inner = _add_extras(edited, spec, context)
    motion = copy.deepcopy(dict(context.deck.motion["slides"][number - 1]))
    motion["bg"] = context.mapper.recolor(motion["bg"])
    motion["transition"] = spec.transition or motion["transition"]
    if spec.builds is None:
        motion["steps"] = _filter_steps(motion["steps"], dropped)
    else:
        groups = _resolve_builds(spec, _FRAME_ID_RE.findall(inner))
        motion["steps"] = make_build_steps(groups, spec.build_effect)
    open_tag = _add_slide_class(source.open_tag, spec.slide_class)
    return _BuiltSlide(
        label=rename_brand(
            source.label if spec.label is None else _escape_text(spec.label)
        ),
        notes=rename_brand(
            source.notes if spec.notes is None else _escape_text(spec.notes)
        ),
        body=restyle_markup(open_tag + inner, context.mapper),
        motion=motion,
    )


def _build_fragment_slide(
    spec: SlideSpec, fragment: Path, context: _BuildContext
) -> _BuiltSlide:
    """Build an output slide from a fragment file."""
    inner = _add_extras(_read_fragment(fragment, context), spec, context)
    background = context.mapper.recolor(spec.bg or _DEFAULT_BACKGROUND)
    width, height = context.deck.motion["w"], context.deck.motion["h"]
    open_tag = _add_slide_class(
        f'<div style="width: {width}px; height: {height}px; position: relative; '
        f'background: {background}; overflow: clip; contain: paint;" class="slide" '
        f'data-canvas-width="{width}" data-canvas-height="{height}">',
        spec.slide_class,
    )
    groups = _resolve_builds(spec, _FRAME_ID_RE.findall(inner))
    motion = {
        "transition": spec.transition or _DEFAULT_TRANSITION,
        "bg": background,
        "steps": make_build_steps(groups, spec.build_effect),
        "embeds": [],
    }
    return _BuiltSlide(
        label=rename_brand(_escape_text(spec.label or "")),
        notes=rename_brand(_escape_text(spec.notes or "")),
        body=restyle_markup(open_tag + inner, context.mapper),
        motion=motion,
    )


def _build_slide(index: int, spec: SlideSpec, context: _BuildContext) -> _BuiltSlide:
    """Build one output slide, prefixing any error with the slide's identity."""
    if spec.source is not None:
        where = f"slides[{index}] (source slide {spec.source})"
    else:
        where = f"slides[{index}] (fragment {spec.fragment})"
    logger.debug("Building %s", where)
    # Slide-level colours and the page number are scoped by replacing the context,
    # not by mutating it, so they cannot leak into the slides that follow.
    context = replace(
        context, mapper=context.mapper.with_overrides(spec.colors), page=index + 1
    )
    try:
        _check_motion_names(spec, context.deck.motion)
        if spec.fragment is not None:
            return _build_fragment_slide(spec, spec.fragment, context)
        return _build_source_slide(spec, spec.source or 0, context)
    except DeckBuildError as error:
        raise DeckBuildError(f"{where}: {error}") from error


def _extract_base_layer(editor_css: str) -> str:
    """Return the editor's ``@layer base`` block with ``color-mix()`` flattened."""
    start = editor_css.find("@layer base")
    depth = 0
    for index in range(max(start, 0), len(editor_css)):
        if editor_css[index] == "{":
            depth += 1
        elif editor_css[index] == "}":
            depth -= 1
            if depth == 0:
                # The brand linter rejects non-hex colour functions, and the
                # reset only uses color-mix() to dim placeholder text.
                layer = editor_css[start : index + 1]
                return _COLOR_MIX_RE.sub("currentcolor", layer)
    raise DeckBuildError("editor stylesheet has no '@layer base' block")


def _render_head(context: _BuildContext, motion: Mapping[str, Any]) -> str:
    """Render everything before ``<main>``: metadata, styles and scripts."""
    deck, mapper = context.deck, context.mapper
    title = rename_brand(html.escape(context.manifest.title, quote=False))
    head = _FONT_LINK_RE.sub("", _FONT_HOST_RE.sub("", _COMMENT_RE.sub("", deck.head)))
    head = _TITLE_RE.sub(lambda _match: f"<title>{title}</title>", head, count=1)
    try:
        css_path = context.manifest.base_dir / _CSS_FILE
        brand_css = css_path.read_text(encoding="utf-8")
    except OSError as error:
        raise DeckBuildError(f"cannot read {css_path}") from error
    base_layer = restyle_css(_extract_base_layer(deck.editor_css), mapper)
    motion_json = json.dumps(motion, separators=(",", ":"), ensure_ascii=False)
    return (
        f"{head}<style>\n{base_layer}\n</style>\n"
        f"<style>{restyle_css(deck.deck_css, mapper)}</style>\n"
        f"<style>\n{expand_tokens(brand_css, context.brand_dir)}</style>\n"
        f'<script type="application/json" id="deck-motion">{motion_json}</script>'
        f"{deck.runtime}"
    )


def _render_section(number: int, total: int, slide: _BuiltSlide) -> str:
    """Render one output ``<section>`` around a built slide."""
    return (
        f'<section class="deck-slide" id="{number}" aria-roledescription="slide" '
        f'aria-label="Slide {number} of {total}" data-document-role="page" '
        f'data-label="{slide.label}" data-speaker-notes="{slide.notes}">'
        f'<div class="deck-fit"><div class="deck-scaler">{slide.body}{_SLIDE_TAIL}\n'
    )


def _load_icons(path: Path) -> dict[str, str]:
    """Load the icon table; a missing file simply defines no icons."""
    if not path.exists():
        return {}
    try:
        table = _Table(tomllib.loads(path.read_text(encoding="utf-8")), path.name)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise DeckBuildError(f"cannot load {path}: {error}") from error
    table.check_keys(frozenset({"icons"}))
    icons = _Table(table.get("icons", dict) or {}, f"{path.name} [icons]")
    return {name: icons.need(name, str) for name in icons.raw}


def build_deck(source_html: str, manifest: Manifest, brand_dir: Path) -> BuildResult:
    """Build the restyled deck described by the manifest.

    Reads the fragments, ``deck.css`` and ``icons.toml`` next to the manifest
    and the logo and background files under ``brand_dir``.

    Args:
        source_html: The complete source deck document.
        manifest: The parsed manifest.
        brand_dir: Brand directory holding ``assets/logos`` and
            ``assets/backgrounds``.

    Returns:
        The output document and the fallback colour mappings that were used.

    Raises:
        DeckBuildError: If the source deck has an unexpected shape, a manifest
            entry cannot be applied (the message names the slide and frame),
            or the finished document fails validation (every problem listed).
    """
    deck = parse_source_deck(source_html)
    context = _BuildContext(
        deck=deck,
        manifest=manifest,
        brand_dir=brand_dir,
        mapper=ColorMapper(manifest.colors),
        icons=_load_icons(manifest.base_dir / _ICONS_FILE),
    )
    slides = [
        _build_slide(index, spec, context) for index, spec in enumerate(manifest.slides)
    ]
    motion = {**deck.motion, "slides": [slide.motion for slide in slides]}
    main_tag = restyle_markup(deck.main_tag, context.mapper, exempt=True)
    sections = "".join(
        _render_section(number, len(slides), slide)
        for number, slide in enumerate(slides, start=1)
    )
    document = f"{_render_head(context, motion)}{main_tag}\n{sections}{deck.tail}"
    logger.info("Built %d slides, %d bytes", len(slides), len(document))
    problems = validate_deck(document, manifest.forbidden)
    if problems:
        logger.error("Validation found %d problems", len(problems))
        # Every problem is reported at once so the manifest can be fixed in one pass.
        listing = "\n".join(f"- {problem}" for problem in problems)
        raise DeckBuildError(f"built deck failed validation:\n{listing}")
    return BuildResult(html=document, fallbacks=dict(context.mapper.fallbacks))


def _iter_color_contexts(markup: str) -> Iterator[str]:
    """Yield every attribute value of the markup that may hold a colour."""
    checked = _COLOR_ATTRIBUTES | _SMIL_ATTRIBUTES | {"style"}
    for tag in _TAG_RE.finditer(markup):
        if not _is_opening(tag):
            continue
        for attribute in _ATTR_RE.finditer(tag[0]):
            if attribute["name"].lower() in checked:
                yield html.unescape(attribute["dq"] or attribute["sq"] or "")


def _iter_bad_colors(context: str) -> Iterator[str]:
    """Yield off-palette colours and unconvertible colour functions of a context."""
    for match in _COLOR_RE.finditer(context):
        token = match[0]
        if token[:4].lower() == "url(":
            continue
        if not token.startswith("#"):
            yield token
            continue
        channels, alpha = _parse_color(token)
        if alpha < 1 or _format_color_key(channels, 1.0).upper() not in _PALETTE_HEXES:
            yield token
    yield from (match[0] for match in _COLOR_FUNCTION_RE.finditer(context))


def _find_color_problems(
    visible: str, motion: Mapping[str, Any], svgs: Sequence[str]
) -> list[str]:
    """List off-palette colours in styles, paint attributes, CSS and motion."""
    body = visible[visible.find("<main") :]
    contexts = list(_iter_color_contexts(body))
    for sheet in _STYLE_BLOCK_RE.finditer(visible):
        contexts.extend(block[1] for block in _CSS_BLOCK_RE.finditer(sheet[1]))
    contexts.extend(str(slide.get("bg", "")) for slide in motion.get("slides", []))
    for svg in svgs:
        contexts.extend(_iter_color_contexts(svg))
    counts = Counter(
        token for context in contexts for token in _iter_bad_colors(context)
    )
    return [
        f"off-palette colour {token!r} ({count} occurrence(s))"
        for token, count in sorted(counts.items())
    ]


def _find_forbidden(
    visible: str, svgs: Sequence[str], forbidden: Sequence[str]
) -> list[str]:
    """List forbidden substrings found in the document or its decoded SVGs."""
    problems: list[str] = []
    for word in forbidden:
        needle = word.lower()
        for match in re.finditer(re.escape(needle), visible.lower()):
            start = max(match.start() - _CONTEXT_CHARS, 0)
            excerpt = visible[start : match.end() + _CONTEXT_CHARS]
            problems.append(f"forbidden word {needle!r} near {excerpt!r}")
        hits = sum(svg.lower().count(needle) for svg in svgs)
        if hits:
            problems.append(
                f"forbidden word {needle!r} in decoded SVG images ({hits}x)"
            )
    return problems


def _find_weight_problems(visible: str, svgs: Sequence[str]) -> list[str]:
    """List font weights above medium in styles, CSS and decoded SVGs."""
    # Scripts are excluded: the runtime is kept byte for byte and any
    # font-weight inside it is code, not styling this build controls.
    styled = _SCRIPT_BLOCK_RE.sub("", visible)
    weights = Counter(
        match[1].lower()
        for text in (styled, *svgs)
        for match in _WEIGHT_RE.finditer(text)
        if match[1].lower() in _HEAVY_KEYWORDS or float(match[1]) > _MEDIUM_WEIGHT
    )
    return [
        f"font-weight {weight} above {_MEDIUM_WEIGHT} ({count} occurrence(s))"
        for weight, count in sorted(weights.items())
    ]


def _find_slide_problems(
    number: int, total: int, section: str, entry: Mapping[str, Any]
) -> list[str]:
    """List structural problems of one output slide."""
    problems: list[str] = []
    header = section[: section.index(">") + 1]
    if _read_attribute(header, "id") != str(number):
        problems.append(f"slide {number}: section id is not sequential")
    if _read_attribute(header, "aria-label") != f"Slide {number} of {total}":
        problems.append(
            f"slide {number}: aria-label is not 'Slide {number} of {total}'"
        )
    counts = Counter(_FRAME_ID_RE.findall(section))
    duplicates = sorted(frame_id for frame_id, count in counts.items() if count > 1)
    if duplicates:
        problems.append(
            f"slide {number}: duplicate data-frame-id {', '.join(duplicates)}"
        )
    steps = entry.get("steps", [])
    used = {frame_id for step in steps for frame_id in step["hidden"]}
    used |= {event["id"] for step in steps for event in step["events"]}
    missing = sorted(used - set(counts))
    if missing:
        problems.append(
            f"slide {number}: build frame(s) {', '.join(missing)} not in slide"
        )
    glyphs = [
        _FRAME_ID_RE.findall(section[: match.start()])[-1:]
        for match in re.finditer(_GLYPH_FONT, section)
    ]
    if glyphs:
        frames = ", ".join(frame_id for found in glyphs for frame_id in found)
        problems.append(f"slide {number}: glyph icon ({_GLYPH_FONT}) left in {frames}")
    return problems


def _find_structure_problems(visible: str, motion: Mapping[str, Any]) -> list[str]:
    """List problems in slide numbering, frame ids and build references."""
    sections = _SECTION_RE.findall(visible)
    entries = motion.get("slides", [])
    if len(sections) != len(entries):
        return [f"{len(sections)} sections but {len(entries)} motion entries"]
    return [
        problem
        for number, (section, entry) in enumerate(
            zip(sections, entries, strict=True), 1
        )
        for problem in _find_slide_problems(number, len(sections), section, entry)
    ]


def validate_deck(document: str, forbidden: Sequence[str] = ()) -> list[str]:
    """Check a built deck against the structural and brand rules.

    Args:
        document: The complete output document.
        forbidden: Case-insensitive substrings that must not appear outside
            base64 payloads, nor inside decoded SVG images.

    Returns:
        One message per problem, empty when the deck is valid. Checked:
        forbidden words; section count against the motion JSON; sequential ids
        and aria-labels; duplicate frame ids; build ids that are not frames of
        their slide; leftover Google Fonts, ``@font-face`` and glyph-icon
        references; off-palette colours and ``rgb()``, ``hsl()``,
        ``color-mix()``, ``oklch()`` or ``lab()`` in styles, paint attributes,
        stylesheets, motion backgrounds and decoded SVGs; font weights above
        500.
    """
    # Base64 payloads are blanked first: random payload bytes can spell a
    # forbidden word or a marker, and they are checked decoded instead.
    visible = _BASE64_RE.sub("base64,", document)
    problems: list[str] = []
    svgs: list[str] = []
    for uri in _SVG_URI_RE.finditer(document):
        try:
            svgs.append(_decode_svg(uri[1]))
        except DeckBuildError as error:
            problems.append(str(error))
    motion_script = _MOTION_RE.search(visible)
    try:
        motion = json.loads(motion_script[2]) if motion_script else {}
    except json.JSONDecodeError:
        motion = {}
    if not motion:
        problems.append("deck-motion script is missing or not valid JSON")
    problems += _find_forbidden(visible, svgs, forbidden)
    problems += [
        f"banned marker {marker!r} present ({visible.count(marker)}x)"
        for marker in _BANNED_MARKERS
        if marker in visible
    ]
    problems += _find_structure_problems(visible, motion)
    problems += _find_color_problems(visible, motion, svgs)
    problems += _find_weight_problems(visible, svgs)
    return problems


def format_report(fallbacks: Mapping[tuple[str, str], int]) -> str:
    """Format the fallback colour mappings, most frequent first.

    Args:
        fallbacks: Count per ``(source colour, palette name)``.

    Returns:
        One line per mapping: source colour, palette name and hex, and count.
        The source colour is the key to use in the manifest ``[colors]`` table.
    """
    ordered = sorted(fallbacks.items(), key=lambda item: (-item[1], item[0]))
    lines = [
        f"{source:<10} -> {name:<10} {PALETTE[name]}  x{count}"
        for (source, name), count in ordered
    ]
    return "\n".join(lines)


def _parse_arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--source", type=Path, required=True, help="exported deck")
    parser.add_argument("--manifest", type=Path, required=True, help="deck manifest")
    parser.add_argument("--brand-dir", type=Path, required=True, help="brand kit dir")
    parser.add_argument("--out", type=Path, required=True, help="output document")
    parser.add_argument(
        "--report", action="store_true", help="print fallback colour mappings"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the build from the command line.

    Args:
        argv: Command-line arguments; None reads ``sys.argv``.

    Returns:
        0 on success, 1 when the build fails. On failure the problems are
        written to stderr and no output file is written.
    """
    arguments = _parse_arguments(argv)
    try:
        # newline="" keeps the deck's line endings byte for byte on every platform.
        with arguments.source.open(encoding="utf-8", newline="") as source_file:
            source_html = source_file.read()
        manifest = load_manifest(arguments.manifest)
        result = build_deck(source_html, manifest, arguments.brand_dir)
        with arguments.out.open("w", encoding="utf-8", newline="") as out_file:
            out_file.write(result.html)
    except (DeckBuildError, OSError) as error:
        sys.stderr.write(f"build_deck: {error}\n")
        return 1
    if arguments.report:
        sys.stdout.write(format_report(result.fallbacks) + "\n")
    return 0


if __name__ == "__main__":
    # NOTE: basicConfig is called only when run as a script, never on import.
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(main())
