"""project_schemas.py — canonical project artifact shapes (intake, memos, experiments, roadmap).

All writes (dashboard HTTP, osctl, chat) normalize through these helpers so manual
and agent output share the same structure.
"""

from __future__ import annotations

import datetime
import json
import re
from typing import Any

MEMO_TYPES = frozenset({
    "problem-validation", "assessment", "channels", "icp",
    "positioning", "competitors", "pricing", "launch", "market-sizing",
})

# Memo type → dashboard project tab (ids / section resolver).
MEMO_SECTION: dict[str, str] = {
    "problem-validation": "validation",
    "assessment": "overview",
    "positioning": "pricing",
    "pricing": "pricing",
    "competitors": "pricing",
    "icp": "pricing",
    "channels": "gtm",
    "launch": "gtm",
    "market-sizing": "pricing",
}


def canonical_memo_types_by_section() -> dict[str, tuple[str, ...]]:
    """Memo type → owning project tab (single parent — matches IdRegistry mm parent)."""
    buckets: dict[str, list[str]] = {}
    for mtype in sorted(MEMO_TYPES):
        buckets.setdefault(MEMO_SECTION[mtype], []).append(mtype)
    return {k: tuple(v) for k, v in buckets.items()}

MEMO_TYPE_LABELS: dict[str, str] = {
    "problem-validation": "Problem validation",
    "assessment": "GTM assessment",
    "positioning": "Positioning",
    "pricing": "Pricing",
    "competitors": "Competitors",
    "icp": "ICP",
    "channels": "Channels",
    "launch": "Launch",
    "market-sizing": "Market sizing",
}

# Dashboard memo card field order (_status = grouped validation_status + severity + frequency).
MEMO_RENDER_ORDER: dict[str, list[str]] = {
    "problem-validation": [
        "problem_statement", "who_has_it", "_status", "current_workaround",
        "willingness_to_pay_signal", "cheapest_next_test", "evidence", "recommendation",
    ],
    "assessment": ["pace_recommendation", "riskiest_assumption", "recommendation"],
    "market-sizing": ["segment", "sam", "som", "sizing_confidence", "recommendation"],
    "_default": ["summary", "recommendation"],
}

MEMO_FIELD_LABELS: dict[str, str | None] = {
    "problem_statement": "Problem",
    "who_has_it": "Who",
    "current_workaround": "Workaround",
    "cheapest_next_test": "Next test",
    "willingness_to_pay_signal": "WTP signal",
    "pace_recommendation": "Pace",
    "riskiest_assumption": "Riskiest assumption",
    "recommendation": "Call",
    "summary": None,
    "evidence": "Evidence",
    "segment": "Segment",
    "sam": "SAM",
    "som": "SOM",
    "sizing_confidence": "Confidence",
}

INTAKE_TITLE = "Venture intake"
INTAKE_SECTIONS = (
    "What it is",
    "Stage & evidence",
    "Market",
    "Resources",
    "Goals",
    "Evidence log",
)

# Shown on Problem & validation tab only (dashboard filters intake.md).
INTAKE_VALIDATION_TAB_SECTIONS = (
    "Stage & evidence",
    "Market",
    "Resources",
    "Goals",
    "Evidence log",
)

TECHNICAL_TITLE = "Technical"
TECHNICAL_SECTIONS = (
    "Stack",
    "Architecture",
    "Infrastructure",
    "APIs & integrations",
    "Data & storage",
    "Deployment",
    "Open questions",
)

ROADMAP_TITLE = "Roadmap"
ROADMAP_SECTIONS = (
    "Now",
    "Next",
    "Later / Ideas",
    "Shipped",
)

ROADMAP_SECTION_ALIASES = {
    "later": "Later / Ideas",
    "ideas": "Later / Ideas",
    "later / ideas": "Later / Ideas",
    "building": "Now",
    "planned": "Next",
}

SECTION_ALIASES = {
    "evidence": "Evidence log",
    "evidence log": "Evidence log",
    "stage": "Stage & evidence",
    "stage & evidence": "Stage & evidence",
    "what is it": "What it is",
    "market & icp": "Market",
}

EXPERIMENT_STATUSES = frozenset({"planned", "running", "done"})
METRIC_MAX_WINDOW_DAYS = 365
METRIC_DEFAULT_WINDOW_DAYS = 7
_METRIC_PATH = re.compile(r"[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*")
SEVERITY_VALUES = frozenset({"vitamin", "painkiller", "emergency"})
VALIDATION_STATUS_VALUES = frozenset({"unvalidated", "weak", "strong"})
FEATURE_PRIORITIES = frozenset({"critical", "high", "normal", "low"})

# UI + osctl field specs (order matters for forms and memo renderer)
MEMO_FIELD_SPECS: dict[str, list[dict[str, Any]]] = {
    "problem-validation": [
        {"key": "problem_statement", "type": "textarea", "label": "Problem (customer words)", "rows": 3},
        {"key": "who_has_it", "type": "text", "label": "Who has it"},
        {"key": "severity", "type": "select", "label": "Severity",
         "options": ["vitamin", "painkiller", "emergency"], "default": "vitamin"},
        {"key": "frequency", "type": "text", "label": "Frequency"},
        {"key": "current_workaround", "type": "textarea", "label": "Current workaround", "rows": 2},
        {"key": "validation_status", "type": "select", "label": "Validation status",
         "options": ["unvalidated", "weak", "strong"], "default": "unvalidated"},
        {"key": "willingness_to_pay_signal", "type": "textarea", "label": "WTP signal", "rows": 2},
        {"key": "cheapest_next_test", "type": "textarea", "label": "Cheapest next test", "rows": 2},
        {"key": "evidence", "type": "evidence", "label": "Evidence (one signal per line)"},
        {"key": "recommendation", "type": "textarea", "label": "Recommendation", "rows": 2},
    ],
    "assessment": [
        {"key": "pace_recommendation", "type": "text", "label": "Pace"},
        {"key": "riskiest_assumption", "type": "textarea", "label": "Riskiest assumption", "rows": 2},
        {"key": "recommendation", "type": "textarea", "label": "Recommendation", "rows": 2},
    ],
    "market-sizing": [
        {"key": "segment", "type": "text", "label": "Segment sized"},
        {"key": "sizing_confidence", "type": "select", "label": "Sizing confidence",
         "options": ["low", "medium", "high"], "default": "low"},
        {"key": "recommendation", "type": "textarea", "label": "Recommendation", "rows": 2},
    ],
    "_default": [
        {"key": "summary", "type": "textarea", "label": "Summary", "rows": 4},
        {"key": "recommendation", "type": "textarea", "label": "Recommendation", "rows": 2},
    ],
}

EXPERIMENT_FIELD_SPECS = [
    {"key": "assumption", "type": "textarea", "label": "Assumption under test", "rows": 3, "required": True},
    {"key": "success_criteria", "type": "text", "label": "Success criteria"},
    {"key": "kill_criteria", "type": "text", "label": "Kill criteria"},
]

def feature_form_fields(roadmap_sections: list[str] | tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    """Feature form schema; roadmap section options are per-project when provided."""
    sections = list(roadmap_sections or ROADMAP_SECTIONS)
    default_sec = "Next" if "Next" in sections else (sections[0] if sections else "Next")
    return [
        {"key": "title", "type": "text", "label": "Feature title", "required": True},
        {"key": "why", "type": "textarea", "label": "Description", "rows": 2},
        {"key": "section", "type": "select", "label": "Roadmap section",
         "options": sections, "default": default_sec,
         "options_source": "project.subsections.docs.roadmap"},
        {"key": "priority", "type": "select", "label": "Priority",
         "options": ["", "critical", "high", "normal", "low"], "default": ""},
    ]


FEATURE_FIELD_SPECS = feature_form_fields()


def _today_iso() -> str:
    return datetime.date.today().isoformat()


def _canon_section(title: str, *, roadmap: bool = False) -> str:
    t = (title or "").strip()
    low = t.lower()
    aliases = ROADMAP_SECTION_ALIASES if roadmap else SECTION_ALIASES
    if low in aliases:
        return aliases[low]
    canon = INTAKE_SECTIONS if not roadmap else ROADMAP_SECTIONS
    for s in canon:
        if s.lower() == low:
            return s
    return t


def build_markdown_starter(title: str, sections: tuple[str, ...]) -> str:
    lines = [f"# {title}", ""]
    for sec in sections:
        lines.extend([f"## {sec}", "", ""])
    return "\n".join(lines).rstrip() + "\n"


INTAKE_STARTER = build_markdown_starter(INTAKE_TITLE, INTAKE_SECTIONS)
TECHNICAL_STARTER = build_markdown_starter(TECHNICAL_TITLE, TECHNICAL_SECTIONS)
ROADMAP_STARTER = build_markdown_starter(ROADMAP_TITLE, ROADMAP_SECTIONS)


def parse_markdown_sections(text: str, *, roadmap: bool = False) -> dict[str, str]:
    """Split markdown on ## headings; preamble before first ## goes to _preamble."""
    out: dict[str, str] = {"_preamble": ""}
    if not (text or "").strip():
        return out
    lines = str(text).replace("\r\n", "\n").split("\n")
    cur_key = "_preamble"
    buf: list[str] = []

    def flush() -> None:
        body = "\n".join(buf).strip()
        if cur_key == "_preamble":
            # strip duplicate H1 from preamble
            plines = body.split("\n")
            if plines and re.match(r"^#\s+", plines[0].strip()):
                plines = plines[1:]
                while plines and not plines[0].strip():
                    plines = plines[1:]
                body = "\n".join(plines).strip()
            out["_preamble"] = body
        elif cur_key:
            out[cur_key] = body
        buf.clear()

    for line in lines:
        if line.startswith("## "):
            flush()
            raw = line[3:].strip()
            cur_key = _canon_section(raw, roadmap=roadmap) if raw else raw
        else:
            buf.append(line)
    flush()
    return out


def normalize_markdown(
    text: str,
    *,
    title: str,
    sections: tuple[str, ...],
    roadmap: bool = False,
) -> str:
    """Rebuild markdown with canonical H1 + section order; preserve section bodies."""
    parsed = parse_markdown_sections(text or "", roadmap=roadmap)
    preamble = (parsed.get("_preamble") or "").strip()
    first = sections[0] if sections else ""
    if preamble and first:
        existing = (parsed.get(first) or "").strip()
        parsed[first] = (preamble + ("\n\n" if preamble and existing else "") + existing).strip()

    lines = [f"# {title}", ""]
    for sec in sections:
        body = (parsed.get(sec) or "").strip()
        lines.extend([f"## {sec}", ""])
        if body:
            lines.append(body)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def normalize_intake(text: str, *, sections: tuple[str, ...] | None = None) -> str:
    return normalize_markdown(
        text, title=INTAKE_TITLE, sections=sections or INTAKE_SECTIONS,
    )


def normalize_technical(text: str, *, sections: tuple[str, ...] | None = None) -> str:
    return normalize_markdown(
        text, title=TECHNICAL_TITLE, sections=sections or TECHNICAL_SECTIONS,
    )


def normalize_roadmap(text: str, *, sections: tuple[str, ...] | None = None) -> str:
    return normalize_markdown(
        text, title=ROADMAP_TITLE, sections=sections or ROADMAP_SECTIONS, roadmap=True,
    )


def memo_starter(mtype: str, version: int) -> dict:
    base: dict[str, Any] = {"status": "proposed", "date": _today_iso(), "version": version}
    if mtype == "problem-validation":
        base.update({
            "problem_statement": "",
            "who_has_it": "",
            "severity": "vitamin",
            "frequency": "",
            "current_workaround": "",
            "evidence": [],
            "willingness_to_pay_signal": "",
            "validation_status": "unvalidated",
            "cheapest_next_test": "",
            "recommendation": "",
        })
    elif mtype == "assessment":
        base.update({
            "pace_recommendation": "",
            "riskiest_assumption": "",
            "recommendation": "",
        })
    elif mtype == "market-sizing":
        base.update({
            "segment": "",
            "sam": {"population": "", "reach_filter": "", "arpu_assumption": "",
                     "annual_revenue_low": 0, "annual_revenue_mid": 0, "annual_revenue_high": 0},
            "som": {"capture_rate": "", "customers_year1": 0, "mrr_year1": 0,
                    "arr_year1": 0, "what_changes_the_number": ""},
            "sizing_confidence": "low",
            "recommendation": "",
        })
    else:
        base.update({"summary": "", "recommendation": ""})
    return base


def _coerce_evidence(val: Any) -> list:
    if val is None or val == "":
        return []
    if isinstance(val, list):
        out = []
        for item in val:
            if isinstance(item, dict):
                out.append({
                    "signal": str(item.get("signal") or item.get("text") or "").strip(),
                    "source": str(item.get("source") or "none").strip() or "none",
                    "strength": str(item.get("strength") or "weak").strip() or "weak",
                })
            elif item:
                out.append({"signal": str(item).strip(), "source": "none", "strength": "weak"})
        return [e for e in out if e["signal"]]
    if isinstance(val, str):
        lines = [ln.strip() for ln in val.replace("\r\n", "\n").split("\n") if ln.strip()]
        return [{"signal": ln, "source": "none", "strength": "weak"} for ln in lines]
    return []


def _allowed_memo_keys(mtype: str) -> frozenset[str]:
    starter = memo_starter(mtype, 1)
    return frozenset(starter.keys()) | frozenset({"superseded_memo"})


def normalize_memo_body(mtype: str, body: dict | None, *, version: int | None = None) -> dict:
    """Merge partial body onto canonical starter; drop unknown keys; coerce types."""
    mtype = (mtype or "").strip()
    if mtype not in MEMO_TYPES:
        raise ValueError(f"unknown memo type '{mtype}'")
    ver = int(version or (body or {}).get("version") or 1)
    out = memo_starter(mtype, ver)
    src = dict(body or {})
    allowed = _allowed_memo_keys(mtype)

    # osctl/UI aliases
    if src.get("assumption") and mtype == "assessment":
        src.setdefault("riskiest_assumption", src["assumption"])

    for key in allowed:
        if key not in src or src[key] is None:
            continue
        val = src[key]
        if key == "evidence":
            out[key] = _coerce_evidence(val)
        elif key == "version":
            out[key] = int(val)
        elif key == "severity" and str(val).strip().lower() in SEVERITY_VALUES:
            out[key] = str(val).strip().lower()
        elif key == "validation_status" and str(val).strip().lower() in VALIDATION_STATUS_VALUES:
            out[key] = str(val).strip().lower()
        elif isinstance(out.get(key), str):
            out[key] = str(val).strip() if val is not None else ""
        else:
            out[key] = val

    if "status" in src and src["status"]:
        out["status"] = str(src["status"]).strip()
    if "date" in src and src["date"]:
        out["date"] = str(src["date"]).strip()
    return out


def normalize_metric(raw: Any) -> dict | None:
    """Shape of an experiment's metric source, or None when it is not one. Shape only: whether
    the action is read-only is app_rules.metric_problem, checked when the metric is saved.
    `path` is dotted keys into the action result (a digit picks a list item); not final until a
    real Composio read-only result is checked."""
    if not isinstance(raw, dict):
        return None
    app = str(raw.get("app") or "").strip().lower()
    action = str(raw.get("action") or "").strip()
    path = str(raw.get("path") or "").strip()
    args = raw.get("args", {})
    if args is None:
        args = {}
    try:
        days = raw.get("window_days")
        days = METRIC_DEFAULT_WINDOW_DAYS if days in (None, "") else int(days)
    except (TypeError, ValueError):
        return None
    if not app or not action or len(path) > 200 or not _METRIC_PATH.fullmatch(path) \
            or not isinstance(args, dict) or not 1 <= days <= METRIC_MAX_WINDOW_DAYS:
        return None
    try:
        json.dumps(args)
    except (TypeError, ValueError):
        return None
    return {"app": app, "action": action, "args": args, "path": path, "window_days": days}


def normalize_experiment_body(body: dict | None) -> dict:
    src = dict(body or {})
    assumption = (
        str(src.get("assumption_under_test") or src.get("assumption") or "").strip()
    )
    status = str(src.get("status") or "planned").strip().lower()
    if status not in EXPERIMENT_STATUSES:
        status = "planned"
    out = {
        "status": status,
        "date": str(src.get("date") or _today_iso()).strip(),
        "assumption_under_test": assumption,
        "assumption": assumption,
        "success_criteria": str(src.get("success_criteria") or "").strip(),
        "kill_criteria": str(src.get("kill_criteria") or "").strip(),
    }
    for key in ("result", "decision", "notes"):
        if key in src and src[key] not in (None, ""):
            out[key] = src[key]
    # GTM lens tag — stem of the strategy this experiment validates (sec07). The
    # tag lives here on the child, pointing up; the GTM tab pulls it in by tag.
    strat = str(src.get("strategy") or "").strip()
    if strat:
        out["strategy"] = strat
    metric = normalize_metric(src.get("metric"))
    if metric:
        out["metric"] = metric
    return out


def memo_form_fields(mtype: str) -> list[dict[str, Any]]:
    return list(MEMO_FIELD_SPECS.get(mtype) or MEMO_FIELD_SPECS["_default"])


def memo_render_order(mtype: str) -> list[str]:
    return list(MEMO_RENDER_ORDER.get(mtype) or MEMO_RENDER_ORDER["_default"])


def schemas_for_api() -> dict:
    from core.ids import PROJECT_SECTIONS, PROJECT_SECTION_LAYOUT, PROJ_SEC_NUM
    from core.subsections import schemas_subsections_meta

    section_layout = {
        key: {
            "label": layout["label"],
            "sec": f"sec{PROJ_SEC_NUM[key]}",
            "memo_types": list(layout.get("memo_types") or ()),
            "files": list(layout.get("files") or ()),
            "skill": layout.get("skill"),
            "skills": list(layout.get("skills") or ()),
        }
        for key, layout in PROJECT_SECTION_LAYOUT.items()
    }
    return {
        "memo_types": sorted(MEMO_TYPES),
        "memo_type_labels": dict(MEMO_TYPE_LABELS),
        "memo_section": dict(MEMO_SECTION),
        "memo_types_by_section": {
            k: list(v) for k, v in canonical_memo_types_by_section().items()
        },
        "project_sections": [{"key": k, "label": lbl, "sec": f"sec{PROJ_SEC_NUM[k]}"} for k, lbl in PROJECT_SECTIONS],
        "section_layout": section_layout,
        "id_cascade": [
            "prN — project",
            "prN.sec01–sec07 — project tabs (overview, validation, experiments, pricing, product, technical, gtm)",
            "prN.secNN.mmM — memo under owning tab (memo_section map)",
            "prN.sec03.exM — experiment",
            "prN.sec05.pdM — product",
            "prN.sec05.pdM.ftM — roadmap feature under product",
            "prN.sec07.phM — GTM phase (time-boxed bet)",
            "prN.sec07.phM.stM — GTM strategy (channel-motion) under a phase",
            "prN.secNN.docM — intake/technical/project doc under tab",
            "prN.secNN.docM.ssK — ## subsection inside that doc (Stack, Architecture, …)",
            "prN.pfM — profile",
            "prN.pfM.sec00.poM — post slot",
            "prN.pfM.sec00.poM.br1 — post brief",
            "prN.pfM.sec00.poM.br1.fdM — brief field",
            "prN.pfM.sec01.br1 — profile brief-spec (Setup)",
            "prN.pfM.sec01.vc1 — profile brand voice (Setup)",
            "prN.pfM.chM — channel",
            "vw02 — calendar view",
        ],
        "memo_render_order": {
            t: memo_render_order(t) for t in sorted(MEMO_TYPES)
        },
        "memo_field_labels": {
            k: v for k, v in MEMO_FIELD_LABELS.items() if v is not None
        },
        "intake": {
            "title": INTAKE_TITLE,
            "sections": list(INTAKE_SECTIONS),
            "default_subsections": list(INTAKE_SECTIONS),
            "validation_tab_sections": list(INTAKE_VALIDATION_TAB_SECTIONS),
        },
        "technical": {
            "title": TECHNICAL_TITLE,
            "sections": list(TECHNICAL_SECTIONS),
            "default_subsections": list(TECHNICAL_SECTIONS),
        },
        "roadmap": {
            "title": ROADMAP_TITLE,
            "sections": list(ROADMAP_SECTIONS),
            "default_subsections": list(ROADMAP_SECTIONS),
        },
        "memos": {t: memo_form_fields(t) for t in sorted(MEMO_TYPES)},
        "experiment": EXPERIMENT_FIELD_SPECS,
        "feature": feature_form_fields(),
        "subsections": schemas_subsections_meta(),
    }


_MEMO_FNAME_RE = re.compile(r"^(.+)-v(\d+)\.json$")


def normalize_workspace_artifacts(root) -> list[str]:
    """Canonicalize all project artifacts on disk before index. Returns changed paths."""
    from pathlib import Path

    root = Path(root)
    changed: list[str] = []
    projects = root / "projects"
    if not projects.is_dir():
        return changed

    def _write_if_changed(path: Path, text: str) -> None:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return
        if raw != text:
            path.write_text(text, encoding="utf-8")
            try:
                changed.append(str(path.relative_to(root)))
            except ValueError:
                changed.append(str(path))

    from core.subsections import (
        config_path,
        ensure_config,
        is_project_slug,
        normalize_doc_text,
        save_config,
        subsections_for_doc,
    )

    for proj in sorted(projects.glob("*")):
        # A hand-made folder whose name is not a slug is indexed but never
        # normalized: neither the dashboard nor osctl can address it either.
        if not proj.is_dir() or not is_project_slug(proj.name):
            continue
        slug = proj.name
        cfg = ensure_config(root, slug)
        cfg_changed = False

        intake = proj / "strategy" / "intake.md"
        if intake.is_file():
            raw = intake.read_text(encoding="utf-8")
            norm, cfg = normalize_doc_text(raw, doc_key="intake", config=cfg)
            _write_if_changed(intake, norm)
            cfg_changed = True

        technical = proj / "technical.md"
        if technical.is_file():
            raw = technical.read_text(encoding="utf-8")
            norm, cfg = normalize_doc_text(raw, doc_key="technical", config=cfg)
            _write_if_changed(technical, norm)
            cfg_changed = True

        memo_dir = proj / "strategy" / "memos"
        if memo_dir.is_dir():
            for f in sorted(memo_dir.glob("*.json")):
                m = _MEMO_FNAME_RE.match(f.name)
                if not m or m.group(1) not in MEMO_TYPES:
                    continue
                mtype, ver = m.group(1), int(m.group(2))
                try:
                    raw = f.read_text(encoding="utf-8")
                    body = json.loads(raw) if raw.strip() else {}
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(body, dict):
                    continue
                norm = dumps_json(normalize_memo_body(mtype, body, version=ver))
                if raw != norm:
                    f.write_text(norm, encoding="utf-8")
                    changed.append(str(f.relative_to(root)))
        exp_dir = proj / "strategy" / "experiments"
        if exp_dir.is_dir():
            for f in sorted(exp_dir.glob("*.json")):
                try:
                    raw = f.read_text(encoding="utf-8")
                    body = json.loads(raw) if raw.strip() else {}
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(body, dict):
                    continue
                norm = dumps_json(normalize_experiment_body(body))
                if raw != norm:
                    f.write_text(norm, encoding="utf-8")
                    changed.append(str(f.relative_to(root)))
        products = proj / "products"
        if products.is_dir():
            for roadmap in sorted(products.glob("*/roadmap.md")):
                raw = roadmap.read_text(encoding="utf-8")
                norm, cfg = normalize_doc_text(raw, doc_key="roadmap", config=cfg)
                _write_if_changed(roadmap, norm)
                cfg_changed = True
        if cfg_changed:
            save_config(root, slug, cfg)
    return changed


def dumps_json(body: dict) -> str:
    return json.dumps(body, indent=2, ensure_ascii=False) + "\n"