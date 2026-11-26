"""Plans: a workflow a bot drafts for one request, as a block at the end of its reply.

    PLAN: <short title>
    brief: <the request, written to stand alone>
    1. bot: research | output: three rival prices with sources | gate: no
    2. skill: launch-plan | bot: gtm | output: the plan | gate: approval | checker: product

Same step-line syntax as `## Steps` in a workflow file (core/workflows.py), with `skill:` optional.
The server cuts the block out of the reply like its other control lines, and the owner's
Approve on the card starts the usual runner with these steps (dashboard/workflow_turns.py).
"""
import re

from core import workflows

BRIEF_MAX = 2000
TITLE_MAX = 80
_HEAD_RE = re.compile(r"^[ \t>#*_-]*PLAN[*_ \t]*:[*_ \t]*(.*)$")
_BRIEF_RE = re.compile(r"^[ \t>*_-]*brief[*_ \t]*:[*_ \t]*(.*)$", re.I)
_FENCE_RE = re.compile(r"^\s*(```|~~~)[\w-]*\s*$")


def _block_end(lines, start):
    """Index of the block's last line: the last `brief:` or step line after the head (blanks may sit between)."""
    end = start
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if _BRIEF_RE.match(line) or workflows._STEP_LINE_RE.match(line):
            end = i
        elif line.strip():
            break
    return end


def extract(text):
    """(reply without any plan block, the first block's text or None). A code fence directly around
    a block is cut with it."""
    text = text or ""
    lines = text.splitlines()
    keep, first, i = [], None, 0
    while i < len(lines):
        if not _HEAD_RE.match(lines[i]):
            keep.append(lines[i])
            i += 1
            continue
        end = _block_end(lines, i)
        if first is None:
            first = "\n".join(lines[i:end + 1]).strip()
        prev = len(keep) - 1
        while prev >= 0 and not keep[prev].strip():
            prev -= 1
        nxt = end + 1
        while nxt < len(lines) and not lines[nxt].strip():
            nxt += 1
        opener = prev >= 0 and sum(bool(_FENCE_RE.match(x)) for x in keep[:prev + 1]) % 2 == 1
        if opener and _FENCE_RE.match(keep[prev]) and nxt < len(lines) and _FENCE_RE.match(lines[nxt]):
            del keep[prev:]
            end = nxt
        i = end + 1
    if first is None:
        return text, None
    clean = re.sub(r"\n{3,}", "\n\n", "\n".join(keep)).strip()
    return clean, first


def parse(block):
    """The plan as a Workflow (`plan=True`, steps parsed with `skill:` optional); ValueError with a
    one-line reason when it does not hold up."""
    title, brief, lines = "", "", []
    for line in (block or "").splitlines():
        head, b, step = _HEAD_RE.match(line), _BRIEF_RE.match(line), workflows._STEP_LINE_RE.match(line)
        if head and not title:
            title = head.group(1).strip().strip("*_` ").strip()
        elif b and not brief:
            brief = b.group(1).strip().strip("*_` ").strip()
        elif step:
            lines.append(step.group(1).strip())
    if not title:
        raise ValueError("no title")
    if not brief:
        raise ValueError("no brief")
    steps = workflows.parse_lines(lines, need_skill=False)
    return workflows.Workflow(title[:TITLE_MAX], brief[:BRIEF_MAX], steps, "", plan=True, lines=tuple(lines))
