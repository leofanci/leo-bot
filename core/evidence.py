"""Evidence log: an append-only bullet list under `## Evidence log` in a project's intake.

One entry per line: `- ev<N> <date> [<ref>] <text>`. Numbers only grow; an entry is never edited.
"""
import re

SECTION = "Evidence log"
TEXT_MAX = 500
_HEAD = re.compile(r"^##\s+(.*?)\s*$")
_ENTRY = re.compile(r"^-\s+ev(\d+)\s+(\d{4}-\d{2}-\d{2})\s+(?:\[([^\]]*)\]\s+)?(.*)$")


def clean(text: str) -> str:
    """One line, capped at TEXT_MAX."""
    return " ".join((text or "").split())[:TEXT_MAX]


def _bounds(lines: list[str]) -> tuple[int, int] | None:
    """(first body line, end) of the evidence section, or None."""
    start = None
    for i, line in enumerate(lines):
        m = _HEAD.match(line)
        if not m:
            continue
        if start is not None:
            return start, i
        if m.group(1).strip().lower() == SECTION.lower():
            start = i + 1
    return (start, len(lines)) if start is not None else None


def entries(intake_text: str) -> list[dict]:
    lines = (intake_text or "").splitlines()
    b = _bounds(lines)
    if not b:
        return []
    out = []
    for line in lines[b[0]:b[1]]:
        m = _ENTRY.match(line.strip())
        if m:
            out.append({"n": int(m.group(1)), "date": m.group(2), "ref": m.group(3) or None,
                        "text": m.group(4)})
    return out


def append(intake_text: str, text: str, ref: str | None, date: str) -> tuple[str, int]:
    """New intake text with one more entry, and the entry's number. Other lines stay as they were."""
    body = clean(text)
    if not body:
        raise ValueError("evidence text required")
    n = max([e["n"] for e in entries(intake_text)] or [0]) + 1
    bullet = f"- ev{n} {date} " + (f"[{ref}] " if ref else "") + body
    lines = (intake_text or "").rstrip("\n").splitlines()
    b = _bounds(lines)
    if b is None:
        lines += ["", f"## {SECTION}", "", bullet]
    else:
        end = b[1]
        while end > b[0] and not lines[end - 1].strip():
            end -= 1
        if end == b[0]:  # empty section: blank line after the heading
            lines.insert(end, "")
            end += 1
        lines.insert(end, bullet)
    return "\n".join(lines) + "\n", n
