from __future__ import annotations

import base64
import json
import re
import shutil
from pathlib import Path

import pytest

import build_deck
from build_deck import (
    PALETTE,
    ColorMapper,
    DeckBuildError,
    FrameEdits,
    IconEdit,
    Section,
    StyleEdit,
    SvgEdit,
    TextEdit,
)

FIXTURES = Path(__file__).parent / "fixtures"
BRAND = FIXTURES / "brand"
REGULAR = "'KH Teka', 'Helvetica Neue', Helvetica, Arial, sans-serif"
MEDIUM = "'KH Teka Medium', 'KH Teka', 'Helvetica Neue', Helvetica, Arial, sans-serif"
ICONS = {"person": '<circle cx="12" cy="8" r="4"/>'}


def source_html() -> str:
    return (FIXTURES / "source.html").read_text(encoding="utf-8")


def source_slide_inner(number: int) -> str:
    return build_deck.parse_source_deck(source_html()).slides[number - 1].inner


def build_fixture(manifest_dir: Path = FIXTURES) -> build_deck.BuildResult:
    manifest = build_deck.load_manifest(manifest_dir / "deck.toml")
    return build_deck.build_deck(source_html(), manifest, BRAND)


def copy_fixture(tmp_path: Path, old: str, new: str) -> Path:
    """Copy the fixture deck and rewrite one snippet of its manifest."""
    target = tmp_path / "deck"
    shutil.copytree(FIXTURES, target)
    manifest = target / "deck.toml"
    text = manifest.read_text(encoding="utf-8")
    assert old in text
    manifest.write_text(text.replace(old, new), encoding="utf-8")
    return target


def output_sections(html: str) -> list[str]:
    return re.findall(r'<section class="deck-slide".*?</section>', html, re.DOTALL)


def output_motion(html: str) -> dict[str, object]:
    match = re.search(r'id="deck-motion">(.*?)</script>', html, re.DOTALL)
    assert match is not None
    motion: dict[str, object] = json.loads(match.group(1))
    return motion


# --- colour mapping -------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("#0b0d10", PALETTE["Slate"]),
        ("#12161c", PALETTE["Slate"]),
        ("#222", PALETTE["Slate"]),
        ("#000", PALETTE["Midnight"]),
        ("#FFF", PALETTE["White"]),
        ("rgb(238, 240, 242)", PALETTE["White"]),
        ("rgb(125, 135, 148)", PALETTE["Shadowline"]),
        ("rgb(245, 165, 36)", PALETTE["Ember"]),
        ("#9ece6a", PALETTE["Breeze"]),
        ("#fde68a", PALETTE["Zest"]),
        ("#7aa2f7", PALETTE["Neon"]),
        ("#c4b5fd", PALETTE["Mist"]),
        ("#a78bfa", PALETTE["Mist"]),
        ("rgb(107, 92, 168)", PALETTE["Neon"]),
        ("#f472b6", PALETTE["Mist"]),
        ("#1e3a42", PALETTE["Slate"]),
        ("#2a313b", PALETTE["Graphite"]),
        ("#4a525c", PALETTE["Graphite"]),
        ("#b8c0ca", PALETTE["Powdercoat"]),
    ],
)
def test_fallback_maps_colour_to_palette_bucket(token: str, expected: str) -> None:
    assert ColorMapper().map_color(token) == expected


def test_palette_colours_pass_through_in_uppercase() -> None:
    mapper = ColorMapper()

    assert mapper.map_color("#6c44e4") == "#6C44E4"
    assert mapper.map_color("rgb(209, 255, 139)") == "#D1FF8B"
    assert not mapper.fallbacks


def test_explicit_override_wins_over_fallback() -> None:
    mapper = ColorMapper({"#22d3ee": "#6C44E4"})

    assert mapper.map_color("rgb(34, 211, 238)") == "#6C44E4"
    assert mapper.map_color("#22D3EE") == "#6C44E4"


def test_translucent_colour_uses_alpha_override_or_composites_over_midnight() -> None:
    overridden = ColorMapper({"#22d3ee55": "#3E245C"})
    plain = ColorMapper()

    assert overridden.map_color("rgba(34, 211, 238, 0.333)") == "#3E245C"
    assert overridden.map_color("#22d3ee55") == "#3E245C"
    # 1% white over Midnight stays a near-black grey.
    assert plain.map_color("rgba(255, 255, 255, 0.01)") == PALETTE["Midnight"]
    assert plain.map_color("rgba(0,0,0,.7)") == PALETTE["Midnight"]


def test_fallbacks_are_counted_by_source_colour() -> None:
    mapper = ColorMapper()

    mapper.recolor("color: #22d3ee; background: rgb(34, 211, 238)")

    assert mapper.fallbacks == {("#22d3ee", "Neon"): 2}
    assert "#22d3ee" in build_deck.format_report(mapper.fallbacks)


def test_recolor_leaves_non_colour_hashes_alone() -> None:
    mapper = ColorMapper()

    text = "fill: url(#abc); stroke: currentColor; color: none; mask: url('#fade')"

    assert mapper.recolor(text) == text


def test_markup_recolour_skips_ids_hrefs_and_text() -> None:
    markup = (
        '<div data-frame-id="abc" id="fed" style="color: #abc">'
        '<a href="#abc">#abc rgb(1, 2, 3)</a><rect fill="#abc" clip-path="url(#abc)"/>'
        "</div>"
    )

    restyled = build_deck.restyle_markup(markup, ColorMapper())

    assert 'data-frame-id="abc" id="fed"' in restyled
    assert '<a href="#abc">#abc rgb(1, 2, 3)</a>' in restyled
    assert 'style="color: #A088EB;"' in restyled
    assert '<rect fill="#A088EB" clip-path="url(#abc)"/>' in restyled


INLINE_SCENE = (
    '<svg viewBox="0 0 100 100" fill="none"><defs>'
    '<pattern id="g4" width="8" height="8" patternUnits="userSpaceOnUse">'
    '<path d="M0 0H8" stroke="#A088EB" stroke-opacity="0.35"/></pattern>'
    '<filter id="gl4"><feGaussianBlur stdDeviation="3"/></filter></defs>'
    '<rect width="100" height="100" fill="url(#g4)" filter="url(#gl4)"/>'
    '<polygon points="0,0 10,0 5,8" fill="#6C44E4"/>'
    '<circle r="4" fill="#FFFFFF"><animate attributeName="opacity" '
    'values="0;0.7;0" keyTimes="0;0.5;1" dur="2s" repeatCount="indefinite"/>'
    '<animateMotion path="M0 0L90 90" dur="3s"/>'
    '<animateTransform attributeName="transform" type="rotate" from="0 50 50" '
    'to="360 50 50" dur="4s"/></circle></svg>'
)


def test_inline_svg_scene_in_palette_colours_is_left_untouched() -> None:
    mapper = ColorMapper()

    assert build_deck.restyle_markup(INLINE_SCENE, mapper) == INLINE_SCENE
    assert not mapper.fallbacks


def test_validation_accepts_inline_svg_patterns_filters_and_smil() -> None:
    html = build_fixture().html.replace(
        'data-frame-id="fan-b"', f'data-frame-id="fan-b">{INLINE_SCENE}<i', 1
    )

    assert build_deck.validate_deck(html, ()) == []


def test_unparseable_colour_is_an_error() -> None:
    with pytest.raises(DeckBuildError, match="colour"):
        ColorMapper().map_color("rgb(var(--x))")


# --- embedded SVG -----------------------------------------------------------


def test_svg_data_uri_is_recoloured_and_loses_root_aria_label() -> None:
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" aria-label="Jev core" '
        'viewBox="0 0 1 1">'
        '<linearGradient id="abc"><stop stop-color="#22d3ee"/></linearGradient>'
        '<rect fill="url(#abc)" stroke="#0b0d10"><animate attributeName="stroke" '
        'values="#0b0d10;#eef0f2" fill="freeze"/></rect></svg>'
    )
    uri = "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()
    raster = "data:image/png;base64,iVBORw0KGgo="
    markup = f'<img alt="x" src="{uri}"><img alt="y" src="{raster}">'

    restyled = build_deck.restyle_markup(markup, ColorMapper())

    payload = re.search(r"data:image/svg\+xml;base64,([A-Za-z0-9+/=]+)", restyled)
    assert payload is not None
    decoded = base64.b64decode(payload.group(1), validate=True).decode("utf-8")
    assert decoded.startswith(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1">'
    )
    assert "aria-label" not in decoded
    assert '<stop stop-color="#A088EB"/>' in decoded
    assert 'fill="url(#abc)" stroke="#211F23"' in decoded
    assert 'values="#211F23;#FFFFFF" fill="freeze"' in decoded
    assert f'src="{raster}"' in restyled


# --- rename ---------------------------------------------------------------


def test_rename_replaces_whole_words_only() -> None:
    text = "Jev answers. JEV 1.13, “Jev”? Jevons and jev.ai stay."

    assert build_deck.rename_brand(text) == (
        "Decisions answers. DECISIONS 1.13, “Decisions”? Jevons and jev.ai stay."
    )


def test_rename_reaches_text_and_alt_but_not_urls() -> None:
    markup = '<img alt="Jev core" src="x"><a href="https://x/with-jev">Jev</a>'

    restyled = build_deck.restyle_markup(markup, ColorMapper())

    assert restyled == (
        '<img alt="Decisions core" src="x"><a href="https://x/with-jev">Decisions</a>'
    )


# --- type -----------------------------------------------------------------


def test_font_family_is_replaced_and_heavy_weight_clamped() -> None:
    mapper = ColorMapper()

    regular = build_deck.restyle_style(
        'font-family: "Geist Mono", monospace; font-weight: 400; font-style: italic',
        mapper,
    )
    heavy = build_deck.restyle_style("font-family: Geist; font-weight: 700", mapper)
    bold = build_deck.restyle_style("font-weight: bold; font-family: Geist", mapper)

    assert regular == f"font-family: {REGULAR}; font-weight: 400; font-style: normal;"
    assert heavy == f"font-family: {MEDIUM}; font-weight: 500;"
    assert bold == f"font-weight: 500; font-family: {MEDIUM};"


def test_font_shorthand_keeps_size_and_swaps_family() -> None:
    mapper = ColorMapper()

    assert (
        build_deck.restyle_style("font:16px / 24px ui-sans-serif, 'Segoe UI'", mapper)
        == f"font: 16px/24px {REGULAR};"
    )
    assert (
        build_deck.restyle_style("font: italic 700 12px/1.5 system-ui", mapper)
        == f"font: 500 12px/1.5 {MEDIUM};"
    )
    assert build_deck.restyle_style("font: inherit", mapper) == "font: inherit;"


def test_style_attribute_is_emitted_with_single_quotes() -> None:
    markup = '<div style="font-family: &quot;Geist Mono&quot;, monospace;">x</div>'

    restyled = build_deck.restyle_markup(markup, ColorMapper())

    assert restyled == f'<div style="font-family: {REGULAR};">x</div>'


def test_css_blocks_are_restyled_but_selectors_are_not() -> None:
    css = (
        "#abc b, strong { font-weight: bolder; color: #fff }\n"
        "@media print{a{color:#abc}}"
    )

    restyled = build_deck.restyle_css(css, ColorMapper())

    assert restyled == (
        "#abc b, strong {font-weight: 500; color: #FFFFFF;}\n"
        "@media print{a{color: #A088EB;}}"
    )


# --- gradients and shadows -----------------------------------------------------


def test_gradient_background_is_flattened_and_box_shadow_removed() -> None:
    markup = (
        '<div style="width: 1px; background-image: linear-gradient(rgb(11, 13, 16) 0%, '
        'rgb(13, 19, 34) 100%); box-shadow: rgba(34, 211, 238, 0.25) 0px 0px 40px;">'
        "</div>"
    )

    restyled = build_deck.restyle_markup(markup, ColorMapper())

    assert restyled == '<div style="width: 1px; background: #121212;"></div>'


@pytest.mark.parametrize("exempt", ["impel-cover", "impel-divider", "impel-band"])
def test_gradient_survives_inside_exempt_class(exempt: str) -> None:
    markup = (
        f'<div class="slide {exempt}"><img alt="" src="x">'
        '<div style="background: linear-gradient(#6C44E4, #121212)"></div></div>'
        '<div style="background: radial-gradient(#6C44E4, #121212)"></div>'
    )

    restyled = build_deck.restyle_markup(markup, ColorMapper())

    assert 'style="background: linear-gradient(#6C44E4, #121212);"' in restyled
    assert "radial-gradient" not in restyled


# --- frame edits ------------------------------------------------------------


def frame_ids(inner: str) -> list[str]:
    return re.findall(r'data-frame-id="([^"]*)"', inner)


def test_drop_matches_entry_prefix_but_not_longer_numbers() -> None:
    inner = source_slide_inner(1)
    assert frame_ids(inner) == ["e0-aaa111", "e1-aaa111", "e2-aaa111", "e12-aaa111"]

    edited = build_deck.apply_frame_edits(inner, FrameEdits(drop=("e1",)), ICONS)

    assert frame_ids(edited) == ["e0-aaa111", "e2-aaa111", "e12-aaa111"]


def test_text_edit_replaces_only_the_text_div_content() -> None:
    edits = FrameEdits(text=(TextEdit(frame="e1", html="How <b>Decisions</b> works"),))

    edited = build_deck.apply_frame_edits(source_slide_inner(1), edits, ICONS)

    assert 'white-space: pre-wrap;">How <b>Decisions</b> works</div></div>' in edited
    assert "Line two" not in edited
    assert "Jevons <i>paradox</i>" in edited


def test_text_edit_on_a_frame_without_text_is_an_error() -> None:
    edits = FrameEdits(text=(TextEdit(frame="e2", html="x"),))

    with pytest.raises(DeckBuildError, match="e2"):
        build_deck.apply_frame_edits(source_slide_inner(1), edits, ICONS)


def test_style_edit_routes_box_properties_to_frame_and_rest_to_text() -> None:
    declarations = (("left", "700px"), ("font-size", "40px"), ("opacity", "0.5"))
    edits = FrameEdits(style=(StyleEdit(frame="e1", declarations=declarations),))

    edited = build_deck.apply_frame_edits(source_slide_inner(1), edits, ICONS)

    frame = next(
        part for part in build_deck.split_frames(edited) if "e1-aaa111" in part
    )
    frame_tag, text_tag = re.findall(r"<div[^>]*>", frame)[:2]
    assert "left: 700px;" in frame_tag
    assert "left: 128px" not in frame_tag
    assert "font-size: 40px;" in text_tag
    assert "font-size: 96px" not in text_tag
    assert text_tag.endswith('opacity: 0.5;">')


def test_alt_edit_replaces_and_escapes_the_image_alt() -> None:
    edits = FrameEdits(alt=(build_deck.AltEdit(frame="e2", text='A "core" & more'),))

    edited = build_deck.apply_frame_edits(source_slide_inner(1), edits, ICONS)

    assert 'alt="A &quot;core&quot; &amp; more"' in edited
    assert "Animated Jev core" not in edited


def test_icon_edit_swaps_glyph_for_stroke_icon_in_the_glyph_colour() -> None:
    edits = FrameEdits(icons=(IconEdit(frame="e2", icon="person"),))

    edited = build_deck.apply_frame_edits(source_slide_inner(2), edits, ICONS)

    assert "mc-anthropicons" not in edited
    assert (
        '<svg viewBox="0 0 24 24" fill="none" stroke="#f472b6" stroke-width="2" '
        'stroke-linecap="round" stroke-linejoin="round" '
        'style="width:100%;height:100%;display:block" aria-hidden="true">'
        '<circle cx="12" cy="8" r="4"/></svg></div>'
    ) in edited


def decode_frame_svg(inner: str, frame_id: str) -> str:
    frame_onwards = inner[inner.index(frame_id) :]
    payload = re.search(r"data:image/svg\+xml;base64,([A-Za-z0-9+/=]+)", frame_onwards)
    assert payload is not None
    return base64.b64decode(payload.group(1), validate=True).decode("utf-8")


def test_svg_edit_substitutes_inside_the_decoded_source_image() -> None:
    edit = SvgEdit(frame="e2", pattern=r"<circle.*</circle>|#0b0d10", repl="")
    inner = source_slide_inner(1)
    assert "<circle" in decode_frame_svg(inner, "e2-aaa111")

    edited = build_deck.apply_frame_edits(inner, FrameEdits(svg_edit=(edit,)), ICONS)

    decoded = decode_frame_svg(edited, "e2-aaa111")
    assert "<circle" not in decoded
    assert 'fill="url(#abc)" stroke=""/></svg>' in decoded
    assert 'alt="Animated Jev core"' in edited


@pytest.mark.parametrize(
    ("frame", "pattern", "message"),
    [
        ("e2", "<ellipse", "matches nothing"),
        ("e2", "(unclosed", "regex"),
        ("e1", "<circle", "'e1'"),
    ],
)
def test_svg_edit_that_cannot_apply_is_an_error(
    frame: str, pattern: str, message: str
) -> None:
    edits = FrameEdits(svg_edit=(SvgEdit(frame=frame, pattern=pattern, repl=""),))

    with pytest.raises(DeckBuildError, match=message):
        build_deck.apply_frame_edits(source_slide_inner(1), edits, ICONS)


def test_unknown_icon_name_is_an_error() -> None:
    edits = FrameEdits(icons=(IconEdit(frame="e2", icon="rocket"),))

    with pytest.raises(DeckBuildError, match="rocket"):
        build_deck.apply_frame_edits(source_slide_inner(2), edits, ICONS)


@pytest.mark.parametrize(
    "edits",
    [
        FrameEdits(drop=("e9",)),
        FrameEdits(text=(TextEdit(frame="e9", html="x"),)),
        FrameEdits(style=(StyleEdit(frame="e9", declarations=(("left", "1px"),)),)),
        FrameEdits(alt=(build_deck.AltEdit(frame="e9", text="x"),)),
        FrameEdits(icons=(IconEdit(frame="e9", icon="person"),)),
        FrameEdits(svg_edit=(SvgEdit(frame="e9", pattern="x", repl=""),)),
    ],
)
def test_missing_frame_is_an_error_naming_the_frame(edits: FrameEdits) -> None:
    with pytest.raises(DeckBuildError, match="'e9'"):
        build_deck.apply_frame_edits(source_slide_inner(1), edits, ICONS)


def test_missing_frame_error_names_the_slide(tmp_path: Path) -> None:
    deck = copy_fixture(tmp_path, 'drop = ["e4"]', 'drop = ["e99"]')

    with pytest.raises(DeckBuildError, match=r"source slide 2.*'e99'"):
        build_fixture(deck)


# --- builds and rail ---------------------------------------------------------


def test_build_steps_reveal_one_group_per_click() -> None:
    steps = build_deck.make_build_steps((("a", "b"), ("c",)), "rise")

    assert steps == [
        {"hidden": ["a", "b", "c"], "events": [], "auto": False},
        {
            "hidden": ["c"],
            "events": [
                {"id": "a", "dir": "in", "effect": "rise"},
                {"id": "b", "dir": "in", "effect": "rise"},
            ],
            "auto": False,
        },
        {
            "hidden": [],
            "events": [{"id": "c", "dir": "in", "effect": "rise"}],
            "auto": False,
        },
    ]


def test_no_builds_gives_the_single_idle_step() -> None:
    assert build_deck.make_build_steps((), "fade") == [
        {"hidden": [], "events": [], "auto": False}
    ]


def test_rail_marks_done_now_and_next_sections() -> None:
    sections = (
        Section(short="Intro", start=0, end=4),
        Section(short="Q&A", start=4, end=10),
        Section(short="Close", start=10, end=11),
    )

    rail = build_deck.render_rail(sections, 2)

    assert rail == (
        '<div data-frame-id="rail" class="rail" style="position:absolute;left:128px;'
        'top:1010px;width:1664px;height:46px"><div class="rail-label">'
        '<span class="rail-n">2</span><span class="rail-name">Q&amp;A</span>'
        '</div><div class="rail-bar">'
        '<span class="rail-seg done" style="flex:4 1 0"></span>'
        '<span class="rail-seg now" style="flex:6 1 0"></span>'
        '<span class="rail-seg next" style="flex:1 1 0"></span></div></div>'
    )
    assert "rail-time" not in rail
    assert "min" not in rail


def test_page_number_pads_the_number_to_two_digits() -> None:
    assert build_deck.render_page_number(4, 30) == (
        '<div data-frame-id="pageno" class="pageno" style="position:absolute;'
        'right:128px;top:60px"><span class="pageno-n">04</span>'
        '<span class="pageno-t">/ 30</span></div>'
    )
    assert '<span class="pageno-n">12</span><span class="pageno-t">/ 07</span>' in (
        build_deck.render_page_number(12, 7)
    )


def test_every_slide_ends_with_its_page_number() -> None:
    sections = output_sections(build_fixture().html)

    for number, section in enumerate(sections, start=1):
        assert section.count('data-frame-id="pageno"') == 1
        assert section.endswith(
            f'<span class="pageno-n">{number:02d}</span>'
            f'<span class="pageno-t">/ {len(sections):02d}</span></div>'
            "</div></div></div></section>"
        )
    assert 'data-frame-id="rail"' not in sections[0]
    assert sections[2].index('data-frame-id="rail"') < sections[2].index(
        'data-frame-id="pageno"'
    )


# --- tokens -----------------------------------------------------------------


def test_logo_token_inlines_the_official_svg_at_the_requested_height() -> None:
    expanded = build_deck.expand_tokens("<p>{{logo:symbol-white:48}}</p>", BRAND)

    assert expanded == (
        '<p><span data-impel-logo style="display:inline-block;height:48px">'
        '<svg height="48" viewBox="0 0 256 222" fill="none" '
        'xmlns="http://www.w3.org/2000/svg">\n'
        '<path d="M106 0H64L0 111Z" fill="white"/>\n'
        "</svg>\n</span></p>"
    )


def test_bg_token_becomes_a_jpeg_data_uri() -> None:
    expanded = build_deck.expand_tokens("url({{bg:glimmer-dark}})", BRAND)

    payload = expanded.removeprefix("url(data:image/jpeg;base64,").removesuffix(")")
    assert base64.b64decode(payload, validate=True).startswith(b"\xff\xd8")


@pytest.mark.parametrize(
    "token",
    ["{{logo:nope-white:48}}", "{{bg:nope}}", "{{icon:x}}", "{{logo:symbol-white}}"],
)
def test_unknown_token_is_an_error(token: str) -> None:
    with pytest.raises(DeckBuildError, match="token"):
        build_deck.expand_tokens(token, BRAND)


# --- manifest ----------------------------------------------------------------


def test_manifest_is_parsed_into_typed_entries() -> None:
    manifest = build_deck.load_manifest(FIXTURES / "deck.toml")

    first, fragment, last = manifest.slides
    assert manifest.forbidden == ("jev", "typesafe")
    assert manifest.colors["#22d3ee55"] == "#3E245C"
    assert manifest.sections[1] == Section(short="How it works", start=4, end=10)
    assert (first.source, first.builds, first.build_effect) == (
        2,
        (("e1",), ("e3",)),
        "rise",
    )
    assert first.edits.icons == (IconEdit(frame="e2", icon="person"),)
    assert (fragment.fragment, fragment.bg) == (FIXTURES / "slides/fanout.html", None)
    assert last.builds is None
    assert last.edits.svg_edit == (
        SvgEdit(frame="e2", pattern="<circle.*</circle>", repl=""),
    )
    assert last.edits.style == (
        StyleEdit(frame="e1", declarations=(("left", "700px"), ("font-size", "40px"))),
    )


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('build_effect = "rise"', "colour = 1", "colour"),
        ("source = 2", 'source = 2\nfragment = "slides/fanout.html"', "exactly one"),
        ('label = "Fan-out"\n', "", "label"),
        ('"#22d3ee" = "#A088EB"', '"#22d3ee" = "#123456"', "#123456"),
        ("section = 2", "section = 3", "section"),
        ("source = 2", "source = 9", "source slide 9"),
        ('build_effect = "rise"', 'build_effect = "spin"', "spin"),
        ('builds = [["e1"], ["e3"]]', 'builds = [["e1"], ["e7"]]', "e7"),
        ('frame = "e2", icon', 'frame = "e2", extra = 1, icon', "extra"),
        (
            'drop = ["e4"]',
            'drop = ["e4"]\ncolors = { "#f472b6" = "#f472b6" }',
            "palette",
        ),
    ],
)
def test_invalid_manifest_is_rejected(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    deck = copy_fixture(tmp_path, old, new)

    with pytest.raises(DeckBuildError, match=message):
        build_fixture(deck)


# --- end to end --------------------------------------------------------------


def test_build_reorders_slides_and_renumbers_them() -> None:
    html = build_fixture().html

    sections = output_sections(html)
    assert [re.findall(r' id="(\d+)"', part[:200]) for part in sections] == [
        ["1"],
        ["2"],
        ["3"],
    ]
    assert [re.findall(r'aria-label="([^"]*)"', part[:200]) for part in sections] == [
        ["Slide 1 of 3"],
        ["Slide 2 of 3"],
        ["Slide 3 of 3"],
    ]
    assert (
        'data-label="Three north stars" data-speaker-notes="Reliability notes."'
        in (sections[0])
    )
    assert (
        'data-label="Fan-out" '
        'data-speaker-notes="One call, many &quot;typed&quot; answers."'
    ) in sections[1]
    assert 'data-label="Why “Decisions”?"' in sections[2]
    assert build_deck.validate_deck(html, ("jev", "typesafe")) == []


def test_build_rebuilds_motion_entries_in_output_order() -> None:
    motion = output_motion(build_fixture().html)

    slides = motion["slides"]
    assert isinstance(slides, list)
    assert [entry["transition"] for entry in slides] == ["fade", "fade", "fade"]
    assert [entry["bg"] for entry in slides] == ["#121212"] * 3
    assert slides[0]["steps"] == build_deck.make_build_steps(
        (("e1-bbb222",), ("e3-bbb222",)), "rise"
    )
    assert slides[1]["steps"] == build_deck.make_build_steps(
        (("fan-a", "fan-b"), ("fan-c",)), "fade"
    )
    assert slides[2]["steps"] == [{"hidden": [], "events": [], "auto": False}]
    assert slides[1]["embeds"] == []
    assert motion["buildMs"] == 380


def test_dropped_frames_leave_inherited_build_steps(tmp_path: Path) -> None:
    deck = copy_fixture(
        tmp_path, 'builds = [["e1"], ["e3"]]\nbuild_effect = "rise"\n', ""
    )

    steps = output_motion(build_fixture(deck).html)["slides"][0]["steps"]  # type: ignore[index]

    assert steps == build_deck.make_build_steps((("e3-bbb222",),), "fade")


def test_empty_builds_clear_inherited_build_steps(tmp_path: Path) -> None:
    deck = copy_fixture(tmp_path, 'builds = [["e1"], ["e3"]]', "builds = []")

    steps = output_motion(build_fixture(deck).html)["slides"][0]["steps"]  # type: ignore[index]

    assert steps == [{"hidden": [], "events": [], "auto": False}]


def test_build_applies_edits_rail_and_fragments() -> None:
    first, fragment, last = output_sections(build_fixture().html)

    assert "e4-bbb222" not in first
    assert 'stroke="#BABFFA"' in first
    assert 'data-frame-id="rail"' not in first
    assert 'class="slide impel-divider"' in fragment
    assert "linear-gradient(#6C44E4, #121212)" in fragment
    assert f"font-family: {MEDIUM}; font-weight: 500; color: #FFFFFF;" in fragment
    assert (
        '<span data-impel-logo style="display: inline-block; height: 48px;">'
        in fragment
    )
    assert (
        '<span class="rail-n">1</span><span class="rail-name">Agents are the users'
        in (fragment)
    )
    assert "Why Decisions?</div>" in last
    assert "linear-gradient" not in last
    assert "<circle" not in decode_frame_svg(last, "e2-aaa111")
    assert last.index('data-frame-id="footer"') < last.index('data-frame-id="rail"')
    assert '<span class="rail-seg done" style="flex: 4 1 0;"></span>' in last
    assert "rail-time" not in last


def test_slide_colours_override_the_global_table_for_that_slide_only(
    tmp_path: Path,
) -> None:
    deck = copy_fixture(
        tmp_path,
        'label = "Three north stars"\n',
        'label = "Three north stars"\n'
        'colors = { "#eef0f2" = "#F2F2F2", "#f472b6" = "#8DDEB7" }\n',
    )

    result = build_fixture(deck)

    first, _, last = output_sections(result.html)
    assert "color: #F2F2F2;" in first
    assert 'stroke="#8DDEB7"' in first
    assert "color: #F2F2F2;" not in last
    assert ("#f472b6", "Mist") not in result.fallbacks


def test_build_rewrites_the_head() -> None:
    html = build_fixture().html
    source = source_html()

    head = html[: html.index("</head>")]
    assert (
        "<title>The Classifier Strikes Back: Decisions as a Decision Layer</title>"
        in head
    )
    assert "style-src 'unsafe-inline'; font-src data:; img-src data: blob:" in head
    assert "<link" not in head
    assert "<!--" not in head
    assert "editor" not in head
    assert head.count("<style>") == 3
    assert "*, ::after, ::before {box-sizing: border-box;" in head
    assert f"html, :host {{line-height: 1.5; font-family: {REGULAR};}}" in head
    assert "html{background: #121212; color-scheme: dark;}" in head
    assert f"font: 12px/1.5 {REGULAR};" in head
    assert ".rail-seg.now{background:#6C44E4}" in head
    assert "background-image:url(data:image/jpeg;base64," in head
    runtime = source[source.index("<script>(function") : source.index("</head>")]
    assert runtime in html
    assert f'style="font: 16px/24px {REGULAR}; color: #FFFFFF;">' in html
    assert html.endswith(
        '</main>\n<div class="deck-pager" aria-hidden="true"></div>\n</body>\n</html>\n'
    )


def test_kept_base_layer_has_color_mix_replaced_by_currentcolor() -> None:
    assert "color: color-mix(currentcolor, transparent)" in source_html()

    html = build_fixture().html

    head = html[: html.index("</head>")]
    assert "color-mix" not in head
    assert "::placeholder {color: currentcolor;}" in head
    assert "@supports (color:currentcolor) {" in head


def test_build_reports_fallback_mappings() -> None:
    result = build_fixture()

    assert result.fallbacks[("#f472b6", "Mist")] == 1
    assert ("#22d3ee", "Neon") not in result.fallbacks
    assert "Mist" in build_deck.format_report(result.fallbacks)


def test_leftover_glyph_icon_fails_the_build_naming_slide_and_frame(
    tmp_path: Path,
) -> None:
    deck = copy_fixture(tmp_path, 'icons = [ { frame = "e2", icon = "person" } ]\n', "")

    with pytest.raises(DeckBuildError, match=r"slide 1.*e2-bbb222"):
        build_fixture(deck)


def test_fragment_with_inline_style_element_is_rejected(tmp_path: Path) -> None:
    deck = copy_fixture(tmp_path, "section = 1", "section = 1 ")
    fragment = deck / "slides/fanout.html"
    fragment.write_text("<style>.a{color:red}</style>", encoding="utf-8")

    with pytest.raises(DeckBuildError, match=r"deck\.css"):
        build_fixture(deck)


def test_cli_writes_the_deck_and_prints_the_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "index_new.html"
    arguments = ["--source", str(FIXTURES / "source.html")]
    arguments += ["--manifest", str(FIXTURES / "deck.toml")]
    arguments += ["--brand-dir", str(BRAND), "--out", str(out), "--report"]

    exit_code = build_deck.main(arguments)

    assert exit_code == 0
    assert out.read_text(encoding="utf-8") == build_fixture().html
    assert "#f472b6" in capsys.readouterr().out


def test_cli_reports_build_errors_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    deck = copy_fixture(tmp_path, 'drop = ["e4"]', 'drop = ["e99"]')
    out = tmp_path / "index_new.html"
    arguments = ["--source", str(FIXTURES / "source.html")]
    arguments += ["--manifest", str(deck / "deck.toml")]
    arguments += ["--brand-dir", str(BRAND), "--out", str(out)]

    exit_code = build_deck.main(arguments)

    assert exit_code == 1
    assert not out.exists()
    assert "e99" in capsys.readouterr().err


# --- validation --------------------------------------------------------------


def test_validation_flags_forbidden_word_in_markup() -> None:
    html = (
        build_fixture()
        .html.replace("Thank", "Thank")
        .replace("Why Decisions?</div>", "Why TypeSafe?</div>")
    )

    problems = build_deck.validate_deck(html, ("jev", "typesafe"))

    assert len(problems) == 1
    assert "typesafe" in problems[0]


def test_validation_flags_forbidden_word_inside_decoded_svg() -> None:
    html = build_fixture().html
    svg = '<svg xmlns="http://www.w3.org/2000/svg"><title>jev</title></svg>'
    uri = "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()
    html = html.replace('alt="Animated core"', f'alt="Animated core" data-x="{uri}"')

    problems = build_deck.validate_deck(html, ("jev",))

    assert len(problems) == 1
    assert "jev" in problems[0]
    assert "SVG" in problems[0]


def test_validation_ignores_forbidden_letters_inside_base64() -> None:
    html = build_fixture().html.replace(
        'alt="Animated core"',
        'alt="Animated core" data-x="data:image/png;base64,AAjevAA="',
    )

    assert build_deck.validate_deck(html, ("jev",)) == []


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("color: #FFFFFF;", "color: #123456;", "#123456"),
        ("color: #FFFFFF;", "color: rgb(1, 2, 3);", "rgb"),
        ("color: #FFFFFF;", "color: hsl(1 2% 3%);", "hsl"),
        ("color: #FFFFFF;", "color: oklch(0.7 0.1 200);", "oklch("),
        ("color: #FFFFFF;", "color: lab(50% 40 59);", "'lab('"),
        (
            ".rail-seg.now{background:#6C44E4}",
            ".rail-seg.now{background:color-mix(in lab, #6C44E4, #121212)}",
            "color-mix(",
        ),
        (
            ".rail-seg.now{background:#6C44E4}",
            ".rail-seg.now{background:#6c44e5}",
            "#6c44e5",
        ),
        ("font-weight: 500;", "font-weight: 700;", "font-weight"),
        ('"bg":"#121212"', '"bg":"#0b0d10"', "#0b0d10"),
        (' id="2" ', ' id="5" ', "id"),
        ("Slide 3 of 3", "Slide 3 of 21", "aria-label"),
        ('"id":"fan-c"', '"id":"fan-z"', "fan-z"),
        ('data-frame-id="fan-b"', 'data-frame-id="fan-a"', "duplicate"),
        ("<style>\nhtml{", "<style>\n@font-face{font-family:x}\nhtml{", "@font-face"),
        (
            "<title>",
            '<link href="https://fonts.googleapis.com/x"><title>',
            "fonts.googleapis",
        ),
    ],
)
def test_validation_flags_broken_output(old: str, new: str, message: str) -> None:
    html = build_fixture().html
    assert old in html

    problems = build_deck.validate_deck(html.replace(old, new, 1), ())

    assert problems
    assert any(message in problem for problem in problems)


def test_validation_flags_motion_and_section_count_mismatch() -> None:
    html = build_fixture().html
    last = output_sections(html)[-1]

    problems = build_deck.validate_deck(html.replace(last, ""), ())

    assert any("3 motion entries" in problem for problem in problems)


def test_build_fails_when_forbidden_word_survives(tmp_path: Path) -> None:
    deck = copy_fixture(
        tmp_path, 'forbidden = ["jev", "typesafe"]', 'forbidden = ["paradox"]'
    )

    with pytest.raises(DeckBuildError, match="paradox"):
        build_fixture(deck)
