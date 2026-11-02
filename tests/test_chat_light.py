"""Light chat: avatar rows, slim deliverables and approvals, clear separation (static source checks)."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "dashboard" / "app.js"
APP_HTML = ROOT / "dashboard" / "app.html"


def test_message_row_structure():
    js = APP_JS.read_text(encoding="utf-8")
    assert 'class="msg-main"' in js and 'class="mh"' in js
    assert "function addMsg(role, text, bot, askedBy, ts, by, byName)" in js


def test_usage_moves_to_tooltip_and_steps_vanish_when_done():
    js = APP_JS.read_text(encoding="utf-8")
    assert 'meta.className = "msg-meta"' not in js
    assert "stepEl.remove()" in js


def test_deliverable_row_and_approval_row_keep_all_actions():
    js = APP_JS.read_text(encoding="utf-8")
    assert 'class="deliv-row"' in js and ">Open in canvas<" in js
    for act in ('data-ap="approved"', 'data-ap="edit"', 'data-ap="rejected"'):
        assert act in js
    assert ">Skip<" in js


def test_messages_are_separated():
    css = (ROOT / "dashboard" / "app.css").read_text(encoding="utf-8")
    rule = ".msg+.msg{border-top:1px solid var(--ui-color-border-subtle)}"
    assert rule.replace(" ", "") in css.replace(" ", "")


def test_header_has_no_member_stack_and_mentions_are_plain_accent_text():
    js = APP_JS.read_text(encoding="utf-8")
    html = APP_HTML.read_text(encoding="utf-8")
    assert 'id="room-members"' not in html and "roomMembersEl" not in js
    assert 'class="mention"' in js


def test_message_lookups_use_the_row_not_the_bubble_parent():
    js = APP_JS.read_text(encoding="utf-8")
    assert "bubble.parentElement" not in js and "b.parentElement" not in js


def test_live_marker_hiding_matches_the_server_markers_only():
    js = APP_JS.read_text(encoding="utf-8")
    assert "(?:APPROVAL|CONSULT)\\b" not in js   # hid "Approval of the budget…" prose
    assert "const APPROVAL_LINE_RE = /^[ \\t>*_-]*APPROVAL:.*$/gm;" in js
    assert "const CONSULT_LINE_RE = /^[ \\t>*_-]*CONSULT[*_ \\t]+@[a-z][a-z0-9-]*[*_ \\t]*:.*$/gim;" in js


def test_client_stops_reading_and_cancels_on_an_error_event():
    js = APP_JS.read_text(encoding="utf-8")
    assert 'if (obj.error) { bubble.innerHTML = formatChatText("Error: " + obj.error); reader.cancel().catch(() => {}); return; }' in js


def test_inbox_approve_only_answers_in_the_approvals_own_thread():
    js = APP_JS.read_text(encoding="utf-8")
    start = js.index("async function openThread(")
    body = js[start:js.index("\n  }\n", start)]
    assert "activeChatId !== a.chat" in body


def test_a_slow_thread_load_never_lands_in_the_room_you_moved_to():
    js = APP_JS.read_text(encoding="utf-8")
    start = js.index("async function openChat(")
    body = js[start:js.index("\n  }\n", start)]
    assert "const seq = ++chatSeq;" in body and "if (seq !== chatSeq) return;" in body
    start = js.index("function newChat(")
    assert "chatSeq++" in js[start:js.index("\n  }\n", start)]
