from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "dashboard"


def test_ui_tokens_load_before_component_css():
    html = (DASHBOARD / "app.html").read_text(encoding="utf-8")
    assert html.index('/ui-tokens.css') < html.index('/app.css')


def test_ui_tokens_are_canonical_source():
    tokens = (DASHBOARD / "ui-tokens.css").read_text(encoding="utf-8")
    app_css = (DASHBOARD / "app.css").read_text(encoding="utf-8")

    required = (
        "--ui-color-canvas:",
        "--ui-type-body:",
        "--ui-space-2:",
        "--ui-radius-card:",
        "--ui-control-default:",
        "--ui-shell-nav:",
        "--canvas-w:",
        "--ui-header-height:",
        "--ui-motion-fast:",
    )
    assert all(token in tokens for token in required)
    assert ":root" not in app_css
    assert ".app{--chat-w:" not in app_css
    assert "grid-template-columns:250px" not in app_css


def test_all_css_custom_properties_resolve():
    styles = "\n".join(
        (DASHBOARD / name).read_text(encoding="utf-8")
        for name in ("ui-tokens.css", "app.css")
    )
    definitions = set(re.findall(r"(--[\w-]+)\s*:", styles))
    uses = set(re.findall(r"var\((--[\w-]+)", styles))
    assert uses <= definitions


def test_canvas_js_reads_css_tokens():
    js = (DASHBOARD / "app.js").read_text(encoding="utf-8")
    assert 'cssPx("--canvas-w"' in js
    assert 'cssPx("--ui-breakpoint-canvas-overlay", 1280)' in js


def test_responsive_breakpoints():
    css = (DASHBOARD / "app.css").read_text(encoding="utf-8")
    assert "@media (max-width:1280px)" in css   # canvas becomes overlay
    assert "@media (max-width:860px)" in css    # sidebar becomes drawer


def test_css_debt_removed():
    css = (DASHBOARD / "app.css").read_text(encoding="utf-8")
    assert css.count("!important") <= 4
    assert "#fff" not in css.lower() and "#ffffff" not in css.lower()
    assert "translateX(2px)" not in css
    assert "border-radius:999px;padding:" not in css.replace(" ", "")  # no pill buttons


def test_sidebar_sections_renamed():
    js = (DASHBOARD / "app.js").read_text(encoding="utf-8")
    assert 'section("teammates", "Teammates"' in js and "Direct messages" not in js


def test_server_serves_token_asset():
    server = (DASHBOARD / "server.py").read_text(encoding="utf-8")
    assert '"ui-tokens.css"' in server
    assert '"/ui-tokens.css"' in server


def _relative_luminance(hex_color: str) -> float:
    channels = [int(hex_color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast_ratio(foreground: str, background: str) -> float:
    lighter, darker = sorted((_relative_luminance(foreground), _relative_luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def test_functional_text_colors_meet_wcag_aa():
    tokens = (DASHBOARD / "ui-tokens.css").read_text(encoding="utf-8")

    def value(name: str) -> str:
        match = re.search(rf"{re.escape(name)}:\s*(#[0-9A-Fa-f]{{6}})", tokens)
        assert match, name
        return match.group(1)

    for name in (
        "--ui-color-text",
        "--ui-color-text-secondary",
        "--ui-color-accent",
        "--ui-color-danger",
    ):
        assert _contrast_ratio(value(name), "#FFFFFF") >= 4.5
    assert _contrast_ratio(value("--ui-color-text-muted"), value("--ui-color-canvas")) >= 4.5


def test_semantic_colors_meet_wcag_aa_on_tinted_backgrounds():
    tokens = (DASHBOARD / "ui-tokens.css").read_text(encoding="utf-8")

    def hex_value(name: str) -> str:
        match = re.search(rf"{re.escape(name)}:\s*(#[0-9A-Fa-f]{{6}})", tokens)
        assert match, name
        return match.group(1)

    def composited_soft(name: str) -> str:
        match = re.search(rf"{re.escape(name)}:\s*rgba\((\d+),\s*(\d+),\s*(\d+),\s*([.\d]+)\)", tokens)
        assert match, name
        red, green, blue = (int(match.group(index)) for index in (1, 2, 3))
        alpha = float(match.group(4))
        mixed = [round(channel * alpha + 255 * (1 - alpha)) for channel in (red, green, blue)]
        return "#" + "".join(f"{channel:02X}" for channel in mixed)

    pairs = (
        ("--ui-color-warning", "--ui-color-warning-soft"),
        ("--ui-color-success", "--ui-color-success-soft"),
        ("--ui-kind-activity", "--ui-kind-activity-soft"),
        ("--ui-kind-experiment", "--ui-kind-experiment-soft"),
        ("--ui-kind-milestone", "--ui-kind-milestone-soft"),
        ("--ui-kind-feature", "--ui-kind-feature-soft"),
    )
    for foreground, background in pairs:
        assert _contrast_ratio(hex_value(foreground), composited_soft(background)) >= 4.5


def test_calendar_events_have_non_color_and_keyboard_cues():
    app_js = (DASHBOARD / "app.js").read_text(encoding="utf-8")
    assert 'role="button" tabindex="0"' in app_js
    assert 'aria-label="${esc(label)}"' in app_js
    assert 'class="ev-mark"' in app_js
    assert 'e.key==="Enter"||e.key===" "' in app_js


def test_terminal_theme_reads_canonical_tokens():
    app_js = (DASHBOARD / "app.js").read_text(encoding="utf-8")
    app_css = (DASHBOARD / "app.css").read_text(encoding="utf-8")
    assert 'cssValue("--ui-font-mono"' in app_js
    assert 'cssPx("--ui-terminal-font-size"' in app_js
    assert 'cssValue("--ui-color-terminal"' in app_js
    assert "background:#1e1e28" not in app_css
    assert "background:#241F1B" not in app_css


def test_filter_chips_use_pointer_appropriate_targets():
    app_css = (DASHBOARD / "app.css").read_text(encoding="utf-8")
    assert ".chip,.kchip{min-height:var(--ui-control-dense)" in app_css
    assert ".chip,.kchip{min-width:var(--ui-control-touch);min-height:var(--ui-control-touch)}" in app_css


def test_kernel_logo_is_wired_into_dashboard_shell():
    logo = DASHBOARD / "logo.svg"
    html = (DASHBOARD / "app.html").read_text(encoding="utf-8")
    app_js = (DASHBOARD / "app.js").read_text(encoding="utf-8")
    server = (DASHBOARD / "server.py").read_text(encoding="utf-8")

    assert logo.read_text(encoding="utf-8").startswith("<svg")
    assert '<link rel="icon" href="/logo.svg" type="image/svg+xml">' in html
    assert '(w?.id === "hq" ? "/logo.svg" : null)' in app_js  # HQ's default workspace logo
    assert 'class="mark" src="${esc(wsLogoUrl(w))}"' in app_js
    assert '"/logo.svg"' in server
    assert '"image/svg+xml"' in server


def test_kernel_logo_follows_graphite_indigo_palette():
    logo = (DASHBOARD / "logo.svg").read_text(encoding="utf-8").upper()
    assert "#4F46E5" in logo or "#4338CA" in logo   # indigo accent
    assert "#D86A47" not in logo and "#A53A24" not in logo  # old rust


def _tokens():
    return (DASHBOARD / "ui-tokens.css").read_text(encoding="utf-8")


def test_graphite_palette_light():
    t = _tokens()
    for decl in ("--ui-color-rail: #17181C", "--ui-color-rail-deep: #0F1013",
                 "--ui-color-accent: #4F46E5", "--ui-color-canvas: #F4F4F6",
                 "--ui-color-surface: #FFFFFF", "--ui-color-text: #1B1D22"):
        assert decl.replace(" ", "") in t.replace(" ", "")


def test_dark_mode_blocks_exist_and_redefine_accent():
    t = _tokens()
    assert '@media (prefers-color-scheme:dark)' in t.replace(": ", ":")
    assert ':root:not([data-theme="light"])' in t
    assert ':root[data-theme="dark"]' in t
    assert t.replace(" ", "").count("--ui-color-accent:#8B8CF8") == 2


def test_compact_type_and_radius_scale():
    t = _tokens().replace(" ", "")
    for decl in ("--ui-type-body:14px", "--ui-radius-card:8px", "--ui-radius-control:6px",
                 "--ui-control-default:28px", "--ws-bar-w:56px", "--canvas-w:540px"):
        assert decl in t


def test_no_green_or_rust_left():
    t = _tokens().upper()
    for banned in ("#10302A", "#1F6F5C", "#B54A2B", "#F3F2EF"):
        assert banned not in t


def test_vendor_serves_woff2_as_font():
    server = (DASHBOARD / "server.py").read_text(encoding="utf-8")
    assert '"font/woff2"' in server


def test_fonts_are_vendored():
    fonts = DASHBOARD / "vendor" / "fonts"
    for name in ("instrument-sans-400.woff2", "instrument-sans-600.woff2",
                 "newsreader-600.woff2", "jetbrains-mono-500.woff2", "OFL.txt"):
        assert (fonts / name).is_file(), name


def test_sprite_and_no_glyph_icons_in_shell():
    html = (DASHBOARD / "app.html").read_text(encoding="utf-8")
    js = (DASHBOARD / "app.js").read_text(encoding="utf-8")
    assert '<symbol id="i-inbox"' in html
    for glyph in ("✔", "☰", "◆", "▦", "▣", "⊕", "📄", "⚗", "📘", "🗓"):
        assert glyph not in html and glyph not in js, glyph


def test_no_hardcoded_colors_in_rendered_markup():
    js = (DASHBOARD / "app.js").read_text(encoding="utf-8")
    styles = re.findall(r'style="([^"]*)"', js)
    colored = [s for s in styles if re.search(r"#[0-9A-Fa-f]{3,6}\b|rgba?\(", s)]
    assert colored == [], colored[:5]
