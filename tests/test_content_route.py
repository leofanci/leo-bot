"""Content view in the canvas: route, breadcrumb bar, per-profile cards (static source checks)."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "dashboard" / "app.js"
APP_CSS = ROOT / "dashboard" / "app.css"


def test_content_route_and_tabs():
    js = APP_JS.read_text(encoding="utf-8")
    assert r"/^\/content(?:\/(posts|calendar|briefs|voice))?$/" in js
    assert "async function renderContent(tab" in js
    assert 'data-f="ready"' in js or '["ready", "Ready"]' in js


def test_canvas_bar_shows_breadcrumb_not_sections_select():
    js = APP_JS.read_text(encoding="utf-8")
    assert 'class="cn-crumb"' in js
    assert '<option value="">Sections…</option>' not in js   # moved into the More menu
    assert 'data-cn-more' in js


def test_focus_expands_card_beyond_page_size():
    js = APP_JS.read_text(encoding="utf-8")
    assert "focusId" in js and "PAGE_SIZE" in js
    assert 'card.querySelector(".pf-more")' in js or "limit: Infinity" in js


def test_workspace_switch_lands_on_content():
    js = APP_JS.read_text(encoding="utf-8")
    assert 'navigate(ws === "hq" ? "#/calendar" : "#/content")' in js


def test_content_view_has_empty_states():
    js = APP_JS.read_text(encoding="utf-8")
    assert "No profiles yet" in js
    assert "No posts yet" in js


def test_calendar_can_be_scoped_to_a_workspace():
    js = APP_JS.read_text(encoding="utf-8")
    assert "async function renderTimeline({ scope = null, head = null } = {})" in js
    assert "scope.has(r.entity_slug)" in js


def test_profile_cards_are_styled_with_sticky_headers():
    css = APP_CSS.read_text(encoding="utf-8")
    assert ".pf-head{" in css and "position:sticky" in css
    assert ".post-row.focused{" in css
    assert ".flash" not in css


def test_post_detail_has_explicit_approve_for_drafts():
    js = APP_JS.read_text(encoding="utf-8")
    assert 'id="approvePost"' in js
    assert 'status:"approved"' in js.replace(" ", "")


def test_profile_page_keeps_every_action():
    js = APP_JS.read_text(encoding="utf-8")
    for hook in ('id="addIdea"', 'id="genIdeas"', 'id="setupBtn"', 'id="logPosted"',
                 'id="writeAll"', 'class="chan-gear"', 'data-sel=', 'id="addChanBtn"'):
        assert hook in js, hook


def test_profile_page_uses_the_shared_post_rows():
    js = APP_JS.read_text(encoding="utf-8")
    start = js.index("async function renderProfile(")
    body = js[start:js.index("\n}\n", start)]
    assert "postRowHtml(" in body and "profileHeadHtml(" in body
    assert 'class="post postcard' not in body
    assert "style=" not in body.replace('style="width:', "")   # bulk bar etc. use classes


# ---- final review fixes ----

def test_canvas_more_menu_is_not_clipped():
    css = APP_CSS.read_text(encoding="utf-8").replace(" ", "")
    assert ".canvas-nav{flex:none;overflow:visible;" in css


def test_focused_row_clears_the_sticky_profile_header():
    css = APP_CSS.read_text(encoding="utf-8")
    assert "scroll-margin-top:" in css[css.index(".post-row{"):]


def test_content_render_does_not_overwrite_a_newer_view():
    js = APP_JS.read_text(encoding="utf-8")
    start = js.index("async function renderContent(")
    body = js[start:js.index("\n}\n", start)]
    assert body.count("if (!stillOn()) return;") >= 2


def test_show_all_is_reachable_without_a_project():
    js = APP_JS.read_text(encoding="utf-8")
    start = js.index("function renderCanvasNav(")
    body = js[start:js.index("\n}\n", start)]
    between = body[body.index("data-cn-edit"):body.index("data-cn-all")]
    assert "</div></div>` : \"\"}" in between   # rendered after the project-only More menu closes


def test_focus_note_reads_just_now_and_auto_open_carries_the_bot():
    js = APP_JS.read_text(encoding="utf-8")
    assert "just now" in js
    assert "openDeliverable({ ...saved[0], bot:" in js


def test_filter_resets_only_when_a_deliverable_arrives():
    js = APP_JS.read_text(encoding="utf-8")
    assert 'if (focusArrived) CONTENT_FILTER = "all";' in js
    assert "if (focusId) CONTENT_FILTER" not in js


def test_folded_cards_stay_folded_across_redraws():
    js = APP_JS.read_text(encoding="utf-8")
    assert "const CONTENT_COLLAPSED = new Set();" in js
    assert 'addEventListener("toggle"' in js


def test_row_titles_and_menus_work_from_the_keyboard():
    js = APP_JS.read_text(encoding="utf-8")
    assert '<button type="button" class="pr-title"' in js
    assert 'e.key === "Escape"' in js and "aria-expanded" in js[js.index("function renderCanvasNav("):]


def test_focused_profile_card_is_highlighted_and_content_is_in_the_sidebar():
    js = APP_JS.read_text(encoding="utf-8")
    css = APP_CSS.read_text(encoding="utf-8")
    assert ".pf.focused{" in css
    assert 'data-view="content"' in js


def test_deliverable_subtitle_lists_channels():
    js = APP_JS.read_text(encoding="utf-8")
    start = js.index("function decorateReply(")
    assert "delivChannels(" in js[start:start + 3000]


# ---- efficiency and bug pass ----

def test_menus_share_one_toggle_that_tracks_its_own_button():
    js = APP_JS.read_text(encoding="utf-8")
    assert "function toggleMenu(btn, menu)" in js
    assert "previousElementSibling?.setAttribute" not in js   # brand menu's sibling is the compose button
    assert 'toggleMenu(e.currentTarget, $("#brand-menu"))' in js
    assert "toggleMenu(e.currentTarget, e.currentTarget.nextElementSibling)" in js


def test_async_views_skip_drawing_after_the_route_changed():
    js = APP_JS.read_text(encoding="utf-8")
    assert "ROUTE_SEQ += 1;" in js[js.index("function parseRoute("):]
    for fn in ("async function renderContent(", "async function renderProfile(", "async function renderPostDetail(",
               "async function renderFiles(", "async function renderTimeline(", "async function renderDay(",
               "async function renderTeam(", "async function renderOperations(", "async function renderNeeds(",
               "async function renderProjectSection("):
        start = js.index(fn)
        body = js[start:js.index("\n}\n", start)]
        assert "routeGuard()" in body, fn


def test_rail_loads_the_id_registry_once():
    js = APP_JS.read_text(encoding="utf-8")
    start = js.index("async function renderRail(")
    body = js[start:js.index("\n}\n", start)]
    assert "ensureIdRegistry()" not in body


def test_profile_redraws_keep_the_channel_filter():
    js = APP_JS.read_text(encoding="utf-8")
    assert "let CHAN_FILTER = initChanFilter || PROFILE_CHAN[slug] || null;" in js
    assert "CHAN_FILTER = PROFILE_CHAN[slug] =" in js


def test_calendar_add_activity_carries_a_date():
    # An activity without a date lands in Operations, not on the calendar:
    # the calendar and day "+ Activity" buttons pass a date and require one.
    js = (Path(__file__).resolve().parents[1] / "dashboard" / "app.js").read_text(encoding="utf-8")
    assert 'navigate("#/activity/new",{date:iso(new Date()),from:"calendar"})' in js
    assert 'navigate("#/activity/new",{date:dateStr,from:"calendar"})' in js
    assert 'onCal?\'type="date" required\'' in js
    assert 'navigate("#/activity/new");' not in js.split("function renderOperations")[0].split("async function renderTimeline")[-1]
