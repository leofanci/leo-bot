"""ids.py — canonical composed ID scheme for the Leo Bot.

Every referencable UI thing gets a short numeric ID composed from its ancestry.

Format
    <seg>[.<seg>…]   segments are 2-letter prefix + digits, dot-separated.

Examples (full ancestry — every segment is parent → child)
    pr1                    — project 1 (tree order)
    pr1.sec02              — Problem & validation tab
    pr1.sec02.mm1          — problem-validation memo on that tab
    pr1.sec03.ex1          — experiment on Experiments tab
    pr1.sec04.mm1          — positioning memo on Positioning & pricing tab
    pr1.sec05.pd1          — product on Product tab
    pr1.sec05.pd1.ft2      — roadmap feature under that product
    pr1.sec06.doc1.ss2     — technical.md subsection (e.g. Architecture)
    pr1.sec06              — Technical tab
    pr1.pf1                — profile 1
    pr1.pf1.sec00          — profile Posts tab
    pr1.pf1.sec00.po3      — post 3 (plan slot / idea)
    pr1.pf1.sec00.po3.sl02 — slot field (working_title, concept)
    pr1.pf1.sec00.po3.br1  — brief on that post
    pr1.pf1.sec00.po3.br1.fd02 — brief field (caption, slide-1, …)
    pr1.pf1.sec01.br1      — profile brief-spec (Setup tab)
    pr1.pf1.sec01.vc1      — profile brand voice (Setup tab)
    pr1.pf1.ch1.sec00      — channel Guidelines tab
    vw02                   — global calendar view

Segment prefixes
    pr project · sec section/tab · pf profile · ch channel · po post
    sl slot field · br brief · fd field (under br/pf/ch) · vc voice · pd product
    ex experiment · mm memo · ft feature · ss subsection · vw view

Filesystem slugs (acme, draft-003) stay valid @-mentions when unambiguous.
Composed IDs are always unambiguous. Build via IdRegistry.build(tree, posts).

ID assignment rules
    1. One composed id per thing — registry rejects duplicates.
    2. Entities: pr, pf, ch, po, br (brief), sec (tab), pd, ft, ex, mm, vw.
    3. Full cascade — every artifact's parent is its UI container (tab/entity), never skip levels.
    4. References are not fields — post.channels → ch ids; profile/channel forms → pf/ch id.
    5. No fd## under pr/pf/ch for form metadata (name, topic, platform, handle, …).
       Profile Setup only: pf.sec01.br1 = brief-spec, pf.sec01.vc1 = brand voice.
    6. Post slot metadata (date, format, pillar, objective, channels) — no ids; edit via po.
    7. Pre-brief creative slot only: sl## under po (working_title, concept).
    8. Brief content only: br under po; fd## under br (caption, slide-N, gen-prompt-N, …).
    9. Section artifacts: mm/ex/pd under pr.secNN; posts under pf.sec00; features under pd.
"""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path
from typing import Any, Optional

from core.brief_spec_util import list_brief_ids
from core.project_schemas import MEMO_SECTION, MEMO_TYPES, canonical_memo_types_by_section
from core.subsections import DOC_KEYS, load_config, subsections_for_doc
from core.voice_util import list_voice_ids

# --------------------------------------------------------------------------- #
# static catalogs (UI skeleton — dynamic entities appended at runtime)
# --------------------------------------------------------------------------- #

GLOBAL_VIEWS = ("needs", "calendar", "operations")

PANELS = ("rail", "main", "chat", "terminal", "toast")

PROJECT_SECTIONS = (
    ("overview", "Overview"),
    ("validation", "Problem & validation"),
    ("experiments", "Experiments"),
    ("pricing", "Positioning & pricing"),
    ("product", "Product"),
    ("technical", "Technical"),
    ("gtm", "Go-to-market"),
)

# Section keys → artifact sources (single source for dashboard, osctl, chat).
# Sections are views over existing files — no per-section content files.
PROJECT_SECTION_LAYOUT: dict[str, dict[str, Any]] = {
    "overview": {
        "label": "Overview",
        "rollup": True,
        "files": ["project.md"],
    },
    "validation": {
        "label": "Problem & validation",
        "files": ["strategy/intake.md"],
        "skill": "problem-validation",
    },
    "experiments": {
        "label": "Experiments",
        "experiment_dir": "strategy/experiments",
        "skill": "experiment-design",
    },
    "pricing": {
        "label": "Positioning & pricing",
        "skills": [
            "positioning", "pricing-strategy", "competitor-scan",
            "icp-research", "channel-strategy",
        ],
    },
    "product": {
        "label": "Product",
        "product_dir": "products",
        "skill": "product-build",
    },
    "technical": {
        "label": "Technical",
        "files": ["technical.md"],
        "skill": "product-build",
    },
    "gtm": {
        "label": "Go-to-market",
        # Phases (time-boxed bets) contain strategies (channel-motions). The tab is
        # a lens: experiments/launch tagged to a strategy live in their home tabs and
        # are pulled in by tag — no duplication. See core/gtm.py.
        "gtm_dir": "gtm",
        "skills": ["channel-strategy", "launch-plan", "gtm-assessment"],
    },
}

# Single parent per memo type — derived from project_schemas.MEMO_SECTION.
for _sec, _types in canonical_memo_types_by_section().items():
    if _sec in PROJECT_SECTION_LAYOUT:
        PROJECT_SECTION_LAYOUT[_sec]["memo_types"] = list(_types)

PROFILE_TABS = (
    ("posts", "Posts"),
    ("setup", "Setup"),
)

CHANNEL_TABS = (
    ("guidelines", "Guidelines"),
    ("setup", "Setup"),
)

# Entity metadata edited via pr/pf/ch id — not registered as sub-field ids.
PROJECT_META = ("name", "kind", "priority", "status", "hours_per_week", "voice")
PROFILE_META = ("name", "topic")
CHANNEL_META = ("platform", "handle", "name", "bio", "guidelines")
# Plan-slot keys that may exist on a post (UI / fileops). Not all get composed ids.
POST_SLOT_FIELDS = ("working_title", "concept", "date", "format", "pillar", "objective", "channels")
# Only pre-brief creative fields get sl## ids. Metadata + channel refs do not.
POST_SLOT_ID_FIELDS = ("working_title", "concept")
GENERATE_PLAN_FIELDS = ("period_start", "period_end", "platforms", "cadence", "focus")

# Fixed section/tab numbers (same code across all projects/profiles).
PROJ_SEC_NUM = {k: f"{i:02d}" for i, (k, _) in enumerate(PROJECT_SECTIONS, 1)}
PROF_TAB_NUM = {"posts": "00", "setup": "01"}
CHAN_TAB_NUM = {"guidelines": "00", "setup": "01"}
GLOBAL_VIEW_NUM = {"needs": "01", "calendar": "02", "operations": "03"}

COMPOSED_ID_RE = re.compile(r"^[a-z]{2,3}\d{1,3}(\.[a-z]{2,3}\d{1,3})*$")
# Brief keys that are identity, slot mirrors, or channel refs — never br.fd ids.
BRIEF_FIELD_SKIP = frozenset({
    "id", "channels", "platform", "format", "objective", "pillar", "_error",
})

_TS_FMT = "%Y%m%d-%H%M%S"


# --------------------------------------------------------------------------- #
# lookup keys (internal — map to composed ids via IdRegistry)
# --------------------------------------------------------------------------- #

def lk_proj(slug: str) -> str:
    return f"proj:{slug}"


def lk_prof(slug: str) -> str:
    return f"prof:{slug}"


def lk_prod(slug: str) -> str:
    return f"prod:{slug}"


def lk_chan(slug: str) -> str:
    return f"chan:{slug}"


def lk_tab_proj(project: str, section: str) -> str:
    return f"tab:proj:{project}:{section}"


def lk_tab_prof(profile: str, panel_key: str) -> str:
    return f"tab:prof:{profile}:{panel_key}"


def lk_tab_chan(channel: str, panel_key: str) -> str:
    return f"tab:chan:{channel}:{panel_key}"


def lk_post(post_id: str) -> str:
    return f"post:{post_id}"


def lk_sl_post(post_id: str, field: str) -> str:
    return f"sl:post:{post_id}:{field}"


def lk_brief(post_id: str) -> str:
    return f"brief:post:{post_id}"


def lk_fld_brief(post_id: str, field: str) -> str:
    return f"fld:brief:{post_id}:{field}"


def lk_fld_post(post_id: str, field: str) -> str:
    """Deprecated — use lk_sl_post or lk_fld_brief."""
    return f"fld:post:{post_id}:{field}"


def lk_fld_prof(profile: str, field: str) -> str:
    return f"fld:prof:{profile}:{field}"


def lk_prof_brief_spec(profile: str, brief_id: str = "br1") -> str:
    return f"brief-spec:prof:{profile}:{brief_id}"


def lk_prof_voice(profile: str, voice_id: str = "vc1") -> str:
    return f"voice:prof:{profile}:{voice_id}"


def lk_fld_chan(channel: str, field: str) -> str:
    return f"fld:chan:{channel}:{field}"


def lk_memo(project: str, mtype: str, version: int) -> str:
    return f"memo:proj:{project}:{mtype}-v{version}"


def lk_experiment(project: str, stem: str) -> str:
    return f"exp:proj:{project}:{stem}"


def lk_phase(project: str, phase_id: str) -> str:
    return f"phase:proj:{project}:{phase_id}"


def lk_strategy(project: str, stem: str) -> str:
    return f"strategy:proj:{project}:{stem}"


def lk_feature(product: str, title_key: str) -> str:
    return f"feat:prod:{product}:{title_key}"


def lk_doc(project: str, doc_key: str) -> str:
    return f"doc:proj:{project}:{doc_key}"


def lk_doc_subsection(project: str, doc_key: str, title: str) -> str:
    return f"sub:proj:{project}:{doc_key}:{slug_key(title)}"


# Repo-relative paths → (section key, doc lookup key)
PROJECT_DOC_FILES = (
    ("strategy/intake.md", "validation", "intake"),
    ("technical.md", "technical", "technical"),
    ("project.md", "overview", "project"),
)


def lk_view(key: str) -> str:
    return f"view:{key}"


# Back-compat aliases used in a few call sites during migration.
proj = lk_proj
prof = lk_prof
prod = lk_prod
chan = lk_chan
tab_proj = lk_tab_proj
tab_prof = lk_tab_prof
post = lk_post


# --------------------------------------------------------------------------- #
# persisted numbering — the ONE id, assigned once, never recomputed
#
# Every composed-id segment number (pr, pf, ch, mm, ex, pd, po, ...) used to
# come from enumerate() over the live tree — recomputed fresh on every
# reindex, so an id's meaning shifted whenever an earlier sibling was added,
# removed, or reordered. This registry is the durable source: a natural key
# (slug/stem) gets a number the first time it's ever seen, and that number is
# never reassigned or reused, even after the entity is deleted.
# --------------------------------------------------------------------------- #

ID_REGISTRY_RELPATH = "database/data/id_registry.json"


def load_id_registry(root: Path) -> dict:
    path = root / ID_REGISTRY_RELPATH
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_id_registry(root: Path, registry: dict) -> None:
    path = root / ID_REGISTRY_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(registry, indent=2, sort_keys=True), encoding="utf-8")


def allocate(registry: dict, scope: str, key: str) -> int:
    """Persisted number for `key` within `scope` — same key always returns the
    same number; a new key gets the next free number in that scope."""
    scope_map = registry.setdefault(scope, {"assigned": {}, "next": 1})
    assigned = scope_map["assigned"]
    if key in assigned:
        return assigned[key]
    n = scope_map["next"]
    assigned[key] = n
    scope_map["next"] = n + 1
    return n


def next_counter(registry: dict, scope: str) -> int:
    """Monotonic number in `scope` with no natural key to look up later —
    for minting a brand new id (e.g. a post) whose full composed form gets
    stored on the entity itself immediately, so it never needs re-deriving."""
    scope_map = registry.setdefault(scope, {"assigned": {}, "next": 1})
    n = scope_map["next"]
    scope_map["next"] = n + 1
    return n


class IdRegistry:
    """Maps lookup keys ↔ composed ids for the live tree."""

    def __init__(self) -> None:
        self.lookup: dict[str, str] = {}
        self.by_id: dict[str, dict] = {}
        self.entries: list[dict] = []

    def get(self, key: str) -> Optional[str]:
        return self.lookup.get(key)

    def resolve(self, cid: str) -> Optional[dict]:
        return self.by_id.get((cid or "").strip().lower())

    def _add(self, cid: str, label: str, *, kind: str, parent: str | None = None,
             ref: dict | None = None, meta: str | None = None) -> str:
        cid = cid.lower()
        if cid in self.by_id:
            raise ValueError(f"duplicate composed id: {cid}")
        entry = {
            "id": cid,
            "kind": kind,
            "label": label,
            "parent": parent,
            "ref": ref or {},
            "meta": meta,
            "describe": label,
        }
        self.by_id[cid] = entry
        self.entries.append(entry)
        return cid

    def _bind(self, key: str, cid: str) -> None:
        self.lookup[key] = cid

    @classmethod
    def build(
        cls,
        tree: list[dict],
        posts: list[dict] | None = None,
        *,
        root: Path | None = None,
        features: list[dict] | None = None,
    ) -> "IdRegistry":
        reg = cls()
        posts = posts or []
        posts_by_prof: dict[str, list[dict]] = {}
        for p in posts:
            ps = p.get("profile_slug") or ""
            if ps:
                posts_by_prof.setdefault(ps, []).append(p)
        for rows in posts_by_prof.values():
            rows.sort(key=lambda x: ((x.get("date") is None), x.get("date") or "", x.get("id") or ""))

        # Every segment number below comes from the persisted registry, not
        # live position — see the "persisted numbering" block above.
        registry = load_id_registry(root) if root else {}

        for vkey, vnum in GLOBAL_VIEW_NUM.items():
            cid = f"vw{vnum}"
            reg._add(cid, vkey.replace("-", " ").title(), kind="view", ref={"view": vkey})
            reg._bind(lk_view(vkey), cid)

        for proj_row in tree or []:
            pslug = proj_row.get("slug") or ""
            if not pslug:
                continue
            pr_id = f"pr{allocate(registry, 'project', pslug)}"
            pname = proj_row.get("name") or pslug
            reg._add(pr_id, pname, kind="project", meta=proj_row.get("kind") or proj_row.get("type"),
                     ref={"project": pslug})
            reg._bind(lk_proj(pslug), pr_id)

            for skey, slabel in PROJECT_SECTIONS:
                snum = PROJ_SEC_NUM[skey]
                cid = f"{pr_id}.sec{snum}"
                reg._add(cid, slabel, kind="tab", parent=pr_id,
                         ref={"project": pslug, "section": skey})
                reg._bind(lk_tab_proj(pslug, skey), cid)

            exp_sec_id = f"{pr_id}.sec{PROJ_SEC_NUM['experiments']}"
            prod_sec_id = f"{pr_id}.sec{PROJ_SEC_NUM['product']}"
            gtm_sec_id = f"{pr_id}.sec{PROJ_SEC_NUM['gtm']}"
            proj_dir = (root / "projects" / pslug) if root else None
            proj_sub_cfg = load_config(root, pslug) if root else None
            if proj_dir and proj_dir.is_dir():
                for rel_path, sec_key, doc_key in PROJECT_DOC_FILES:
                    fpath = proj_dir / rel_path
                    if not fpath.is_file():
                        continue
                    snum = PROJ_SEC_NUM[sec_key]
                    sec_id = f"{pr_id}.sec{snum}"
                    doc_n = allocate(registry, f"doc:{pslug}:{sec_key}", doc_key)
                    cid = f"{sec_id}.doc{doc_n}"
                    reg._add(cid, doc_key, kind="doc", parent=sec_id,
                             ref={"project": pslug, "section": sec_key, "path": rel_path})
                    reg._bind(lk_doc(pslug, doc_key), cid)
                    if proj_sub_cfg is not None and doc_key in DOC_KEYS:
                        for title in subsections_for_doc(proj_sub_cfg, doc_key):
                            si = allocate(registry, f"subsection:{pslug}:{doc_key}", title)
                            ss_cid = f"{cid}.ss{si}"
                            reg._add(
                                ss_cid, title, kind="subsection", parent=cid,
                                ref={
                                    "project": pslug,
                                    "section": sec_key,
                                    "doc": doc_key,
                                    "subsection": title,
                                    "path": rel_path,
                                },
                            )
                            reg._bind(lk_doc_subsection(pslug, doc_key, title), ss_cid)
                memo_dir = proj_dir / "strategy" / "memos"
                if memo_dir.is_dir():
                    for f in sorted(memo_dir.glob("*.json")):
                        m = re.match(r"^(.+)-v(\d+)\.json$", f.name)
                        if not m:
                            continue
                        mtype, ver = m.group(1), int(m.group(2))
                        sec_key = MEMO_SECTION.get(mtype, "overview")
                        snum = PROJ_SEC_NUM[sec_key]
                        sec_id = f"{pr_id}.sec{snum}"
                        mm_n = allocate(registry, f"memo:{pslug}:{sec_key}", f.stem)
                        cid = f"{sec_id}.mm{mm_n}"
                        reg._add(cid, f"{mtype} v{ver}", kind="memo", parent=sec_id,
                                 ref={"project": pslug, "section": sec_key,
                                      "memo_type": mtype, "version": ver})
                        reg._bind(lk_memo(pslug, mtype, ver), cid)
                exp_dir = proj_dir / "strategy" / "experiments"
                if exp_dir.is_dir():
                    for f in sorted(exp_dir.glob("*.json")):
                        stem = f.stem
                        ex_n = allocate(registry, f"experiment:{pslug}", stem)
                        cid = f"{exp_sec_id}.ex{ex_n}"
                        reg._add(cid, stem, kind="exp", parent=exp_sec_id,
                                 ref={"project": pslug, "section": "experiments", "stem": stem})
                        reg._bind(lk_experiment(pslug, stem), cid)
                prod_root = proj_dir / "products"
                if prod_root.is_dir():
                    for d in sorted(prod_root.iterdir()):
                        if not d.is_dir() or not (d / "product.md").is_file():
                            continue
                        pslug2 = d.name
                        pd_n = allocate(registry, f"product:{pslug}", pslug2)
                        cid = f"{prod_sec_id}.pd{pd_n}"
                        reg._add(cid, pslug2, kind="prod", parent=prod_sec_id,
                                 ref={"product": pslug2, "section": "product"})
                        reg._bind(lk_prod(pslug2), cid)

                # GTM phases (time-boxed bets) → strategies (channel-motions) nested under them.
                from core import gtm as _gtm  # local import — gtm has no ids dependency
                phase_cids: dict[str, str] = {}
                for phase in _gtm.load_phases(root, pslug):
                    ph_id = phase.get("id") or ""
                    if not ph_id:
                        continue
                    ph_n = allocate(registry, f"phase:{pslug}", ph_id)
                    cid = f"{gtm_sec_id}.ph{ph_n}"
                    reg._add(cid, phase.get("name") or ph_id, kind="phase", parent=gtm_sec_id,
                             meta=phase.get("status"),
                             ref={"project": pslug, "section": "gtm", "phase": ph_id})
                    reg._bind(lk_phase(pslug, ph_id), cid)
                    phase_cids[ph_id] = cid
                for strat in _gtm.load_strategies(root, pslug):
                    stem = strat.get("stem") or ""
                    if not stem:
                        continue
                    parent_cid = phase_cids.get(strat.get("phase") or "", gtm_sec_id)
                    st_n = allocate(registry, f"strategy:{pslug}", stem)
                    cid = f"{parent_cid}.st{st_n}"
                    reg._add(cid, strat.get("name") or stem, kind="strategy", parent=parent_cid,
                             meta=strat.get("status"),
                             ref={"project": pslug, "section": "gtm", "stem": stem,
                                  "phase": strat.get("phase") or ""})
                    reg._bind(lk_strategy(pslug, stem), cid)

            for prof_row in proj_row.get("profiles") or []:
                prf_slug = prof_row.get("slug") or ""
                if not prf_slug:
                    continue
                pf_id = f"{pr_id}.pf{allocate(registry, f'profile:{pslug}', prf_slug)}"
                reg._add(pf_id, prof_row.get("name") or prf_slug, kind="profile", parent=pr_id,
                         ref={"profile": prf_slug, "project": pslug})
                reg._bind(lk_prof(prf_slug), pf_id)

                for tkey, tlabel in PROFILE_TABS:
                    tnum = PROF_TAB_NUM[tkey]
                    cid = f"{pf_id}.sec{tnum}"
                    reg._add(cid, tlabel, kind="tab", parent=pf_id,
                             ref={"profile": prf_slug, "tab": tkey})
                    reg._bind(lk_tab_prof(prf_slug, tkey), cid)

                setup_tab_id = f"{pf_id}.sec{PROF_TAB_NUM['setup']}"
                prof_dir = (root / "projects" / pslug / "profiles" / prf_slug) if root else None
                brief_ids = list_brief_ids(prof_dir) if prof_dir and prof_dir.is_dir() else ["br1"]
                for bid in brief_ids:
                    br_cid = f"{setup_tab_id}.{bid}"
                    reg._add(br_cid, "brief spec", kind="brief_spec", parent=setup_tab_id,
                             ref={"profile": prf_slug, "field": "brief-spec", "brief_id": bid})
                    reg._bind(lk_prof_brief_spec(prf_slug, bid), br_cid)
                voice_ids = list_voice_ids(prof_dir) if prof_dir and prof_dir.is_dir() else ["vc1"]
                for vid in voice_ids:
                    vc_cid = f"{setup_tab_id}.{vid}"
                    reg._add(vc_cid, "voice", kind="voice", parent=setup_tab_id,
                             ref={"profile": prf_slug, "field": "voice", "voice_id": vid})
                    reg._bind(lk_prof_voice(prf_slug, vid), vc_cid)

                for ch_row in prof_row.get("channels") or []:
                    cslug = ch_row.get("slug") or ""
                    if not cslug:
                        continue
                    ch_id = f"{pf_id}.ch{allocate(registry, f'channel:{prf_slug}', cslug)}"
                    reg._add(ch_id, ch_row.get("name") or cslug, kind="channel", parent=pf_id,
                             meta=ch_row.get("platform"), ref={"channel": cslug, "profile": prf_slug})
                    reg._bind(lk_chan(cslug), ch_id)
                    for tkey, tlabel in CHANNEL_TABS:
                        tnum = CHAN_TAB_NUM[tkey]
                        cid = f"{ch_id}.sec{tnum}"
                        reg._add(cid, tlabel, kind="tab", parent=ch_id,
                                 ref={"channel": cslug, "tab": tkey})
                        reg._bind(lk_tab_chan(cslug, tkey), cid)

                posts_tab_id = f"{pf_id}.sec{PROF_TAB_NUM['posts']}"
                legacy_poi = 0
                for slot in posts_by_prof.get(prf_slug, []):
                    pid = slot.get("id") or ""
                    if not pid:
                        continue
                    if is_canonical_id(pid):
                        # Already the one true id, minted at creation — use as-is.
                        po_id = pid
                    else:
                        # Pre-migration id: derive a display position (unstable,
                        # only until this post is regenerated with a real id).
                        legacy_poi += 1
                        po_id = f"{posts_tab_id}.po{legacy_poi}"
                    label = slot.get("working_title") or slot.get("pillar") or pid
                    reg._add(po_id, label, kind="post", parent=posts_tab_id, meta=slot.get("status"),
                             ref={"post": pid, "profile": prf_slug, "tab": "posts"})
                    reg._bind(lk_post(pid), po_id)
                    _register_post_fields(reg, po_id, pid, prf_slug, pslug, root)

            prod_slugs = {d.name for d in ((proj_dir / "products").iterdir() if proj_dir and (proj_dir / "products").is_dir() else [])
                          if d.is_dir() and (d / "product.md").is_file()}
            for prod_row in proj_row.get("products") or []:
                if prod_row.get("slug"):
                    prod_slugs.add(prod_row["slug"])
            for pslug2 in sorted(prod_slugs):
                if reg.get(lk_prod(pslug2)):
                    continue
                pd_n = allocate(registry, f"product:{pslug}", pslug2)
                pd_id = f"{prod_sec_id}.pd{pd_n}"
                reg._add(pd_id, pslug2, kind="prod", parent=prod_sec_id,
                         ref={"product": pslug2, "section": "product"})
                reg._bind(lk_prod(pslug2), pd_id)

            proj_feats = [f for f in (features or []) if _product_belongs_to_project(f.get("product_slug"), pslug, tree, root)]
            by_prod: dict[str, list[dict]] = {}
            for f in proj_feats:
                by_prod.setdefault(f.get("product_slug") or "", []).append(f)
            for pslug2, feats in sorted(by_prod.items()):
                if not pslug2:
                    continue
                pd_id = reg.get(lk_prod(pslug2))
                if not pd_id:
                    continue
                for feat in sorted(feats, key=lambda x: x.get("title") or ""):
                    title = feat.get("title") or "untitled"
                    tk = slug_key(title)
                    ft_n = allocate(registry, f"feature:{pslug2}", tk)
                    ft_id = f"{pd_id}.ft{ft_n}"
                    reg._add(ft_id, title, kind="feat", parent=pd_id, meta=feat.get("status"),
                             ref={"product": pslug2, "title_key": tk})
                    reg._bind(lk_feature(pslug2, tk), ft_id)

        if root:
            save_id_registry(root, registry)
        return reg


def _product_belongs_to_project(
    product_slug: str | None,
    project_slug: str,
    tree: list,
    root: Path | None,
) -> bool:
    if not product_slug:
        return False
    if root and (root / "projects" / project_slug / "products" / product_slug).is_dir():
        return True
    for p in tree:
        if p.get("slug") != project_slug:
            continue
        return any(pr.get("slug") == product_slug for pr in p.get("products") or [])
    return False


def _find_plan_slot(root: Path | None, project_slug: str, profile_slug: str, post_id: str) -> dict:
    if not root:
        return {}
    content = root / "projects" / project_slug / "profiles" / profile_slug / "content"
    if not content.is_dir():
        return {}
    import json
    for plan in sorted(content.glob("plan-*.json")):
        try:
            data = json.loads(plan.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for slot in data.get("posts", []) if isinstance(data, dict) else []:
            if slot.get("id") == post_id:
                return slot if isinstance(slot, dict) else {}
    return {}


def _register_post_fields(
    reg: IdRegistry,
    po_id: str,
    post_id: str,
    profile_slug: str,
    project_slug: str,
    root: Path | None,
) -> None:
    slot = _find_plan_slot(root, project_slug, profile_slug, post_id)
    sl_n = 0
    for key in POST_SLOT_ID_FIELDS:
        val = slot.get(key)
        if val is None or val == "" or val == []:
            continue
        sl_n += 1
        cid = f"{po_id}.sl{sl_n:02d}"
        reg._add(cid, key, kind="slot_field", parent=po_id,
                 ref={"post": post_id, "field": key, "profile": profile_slug, "scope": "slot"})
        reg._bind(lk_sl_post(post_id, key), cid)

    for cslug in slot.get("channels") or []:
        if not isinstance(cslug, str) or not cslug.strip():
            continue
        ch_id = reg.get(lk_chan(cslug.strip()))
        if ch_id:
            reg._bind(f"post:{post_id}:ref:chan:{cslug.strip()}", ch_id)

    brief: dict = {}
    if root:
        bf = root / "projects" / project_slug / "profiles" / profile_slug / "content" / "briefs" / f"{post_id}.json"
        if bf.is_file():
            try:
                import json
                brief = json.loads(bf.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
    if not brief:
        return

    br_id = f"{po_id}.br1"
    reg._add(br_id, "brief", kind="brief", parent=po_id,
             ref={"post": post_id, "profile": profile_slug})
    reg._bind(lk_brief(post_id), br_id)

    fd_n = 0
    for key, val in brief.items():
        if key.startswith("_") or key in BRIEF_FIELD_SKIP:
            continue
        if key == "slide_overlays" and isinstance(val, list):
            for item in val:
                if not isinstance(item, dict):
                    continue
                n = int(item.get("slide") or (fd_n + 1))
                field = f"slide-{n}"
                fd_n += 1
                cid = f"{br_id}.fd{fd_n:02d}"
                reg._add(cid, field, kind="brief_field", parent=br_id,
                         ref={"post": post_id, "field": field, "profile": profile_slug, "scope": "brief"})
                reg._bind(lk_fld_brief(post_id, field), cid)
            continue
        if key == "gen_prompts" and isinstance(val, list) and val and isinstance(val[0], str):
            for i, _ in enumerate(val, 1):
                field = f"gen-prompt-{i}"
                fd_n += 1
                cid = f"{br_id}.fd{fd_n:02d}"
                reg._add(cid, field, kind="brief_field", parent=br_id,
                         ref={"post": post_id, "field": field, "profile": profile_slug, "scope": "brief"})
                reg._bind(lk_fld_brief(post_id, field), cid)
            continue
        fd_n += 1
        cid = f"{br_id}.fd{fd_n:02d}"
        reg._add(cid, key, kind="brief_field", parent=br_id,
                 ref={"post": post_id, "field": key, "profile": profile_slug, "scope": "brief"})
        reg._bind(lk_fld_brief(post_id, key), cid)


def subsection_id_map(registry: IdRegistry | None, project_slug: str) -> dict[str, dict[str, str]]:
    """Map doc_key → subsection title → composed id (for dashboard technical/intake tabs)."""
    out: dict[str, dict[str, str]] = {k: {} for k in DOC_KEYS}
    if not registry:
        return out
    for ent in registry.entries:
        if ent.get("kind") != "subsection":
            continue
        ref = ent.get("ref") or {}
        if ref.get("project") != project_slug:
            continue
        doc = ref.get("doc")
        title = ref.get("subsection")
        cid = ent.get("id")
        if doc in out and title and cid:
            out[doc][title] = cid
    return out


def build_id_registry(
    tree: list[dict],
    posts: list[dict] | None = None,
    *,
    root: Path | None = None,
    features: list[dict] | None = None,
) -> IdRegistry:
    if features is None:
        try:
            from dashboard import db  # noqa: WPS433
            features = db._rows("SELECT product_slug, title, status FROM features ORDER BY product_slug, title")
        except Exception:  # noqa: BLE001
            features = []
    return IdRegistry.build(tree, posts, root=root, features=features)


# --------------------------------------------------------------------------- #
# parse / validate
# --------------------------------------------------------------------------- #

def parse_id(raw: str) -> Optional[dict[str, Any]]:
    """Return {raw, segments} for composed ids, or None."""
    s = (raw or "").strip().lower()
    if not s or not COMPOSED_ID_RE.match(s):
        return None
    return {"raw": s, "segments": tuple(s.split("."))}


def is_canonical_id(raw: str) -> bool:
    return parse_id(raw) is not None


def bare_slug(raw: str, registry: IdRegistry | None = None) -> str:
    """Resolve composed id → filesystem slug when possible."""
    if registry:
        ent = registry.resolve(raw)
        if ent:
            ref = ent.get("ref") or {}
            for k in ("post", "project", "profile", "channel", "product"):
                if ref.get(k):
                    return str(ref[k])
    return raw


def describe_id(raw: str, registry: IdRegistry | None = None) -> str:
    if registry:
        ent = registry.resolve(raw)
        if ent:
            return ent.get("describe") or ent.get("label") or raw
    p = parse_id(raw)
    if not p:
        return f"unknown id '{raw}'"
    return f"composed · {p['raw']}"


# --------------------------------------------------------------------------- #
# generated raw ids (stored in files — wrap with post()/ms()/activity())
# --------------------------------------------------------------------------- #

def _stamp(prefix: str, existing: set[str]) -> str:
    base = prefix + datetime.datetime.now().strftime(_TS_FMT)
    out = base
    while out in existing:
        out += "x"
    return out


def _max_baked_post_number(root: Path, project_slug: str, profile_slug: str, base: str) -> int:
    """Highest po number already sitting in this profile's plan files.

    The registry's per-profile counter is the normal source of the next
    number, but it can fall behind reality — batch/migrated plans, or a
    registry file that got reset, can bake `{base}.poN` ids straight into
    plan JSON without ever calling mint_post_ids(). If the counter doesn't
    know about those, it reissues an already-used N and two posts end up
    sharing one id. Scanning plan files at mint time is the floor that
    keeps every mint unique regardless of how the counter got here."""
    content = root / "projects" / project_slug / "profiles" / profile_slug / "content"
    if not content.is_dir():
        return 0
    pat = re.compile(rf"^{re.escape(base)}\.po(\d+)$")
    highest = 0
    for plan in content.glob("plan-*.json"):
        try:
            data = json.loads(plan.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for slot in data.get("posts", []) if isinstance(data, dict) else []:
            m = pat.match(slot.get("id") or "")
            if m:
                highest = max(highest, int(m.group(1)))
    return highest


def mint_post_ids(root: Path, project_slug: str, profile_slug: str, n: int) -> list[str]:
    """Mint n brand new post ids — each one IS the final composed id
    (pr{N}.pf{N}.sec00.po{N}), the only id this post will ever have. No
    separate internal/storage id, no later recomputation: IdRegistry.build
    uses this string as-is once it's written into the plan JSON.

    project/profile numbers come from the same persisted registry the
    catalog builder reads, so they always agree. Before minting, the counter
    is floored against ids already baked into plan files (see
    _max_baked_post_number) so a stale/behind registry can never reissue one."""
    registry = load_id_registry(root)
    pr_n = allocate(registry, "project", project_slug)
    pf_n = allocate(registry, f"profile:{project_slug}", profile_slug)
    base = f"pr{pr_n}.pf{pf_n}.sec{PROF_TAB_NUM['posts']}"
    scope = f"post:{project_slug}:{profile_slug}"
    scope_map = registry.setdefault(scope, {"assigned": {}, "next": 1})
    floor = _max_baked_post_number(root, project_slug, profile_slug, base) + 1
    if scope_map["next"] < floor:
        scope_map["next"] = floor
    ids = [f"{base}.po{next_counter(registry, scope)}" for _ in range(n)]
    save_id_registry(root, registry)
    return ids


# --------------------------------------------------------------------------- #
# workspace-wide duplicate guard — catches collisions no single mint call can
# see (posts baked directly into plan files by batch/migration jobs, files
# edited by hand, etc.)
# --------------------------------------------------------------------------- #

def find_duplicate_post_ids(root: Path) -> dict[str, list[str]]:
    """Every composed post id that appears in more than one plan file,
    anywhere in the workspace, mapped to the plan files that share it.

    Reads plan-*.json directly rather than the SQLite index: the index is
    keyed by id and upserts on reindex, so a genuine on-disk collision (two
    posts, one id) silently collapses to whichever file was reindexed last —
    invisible from the index alone, but a live bug for anyone acting on that
    id (see mint_post_ids for how it happens)."""
    seen: dict[str, list[str]] = {}
    for plan in sorted(root.glob("projects/*/profiles/*/content/plan-*.json")):
        try:
            data = json.loads(plan.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for slot in data.get("posts", []) if isinstance(data, dict) else []:
            pid = slot.get("id")
            if pid:
                seen.setdefault(pid, []).append(_rel_path(root, plan))
    return {pid: files for pid, files in seen.items() if len(files) > 1}


def find_duplicate_ids(root: Path, tree: list[dict], posts: list[dict] | None = None,
                        *, features: list[dict] | None = None) -> list[str]:
    """Workspace-wide duplicate-id guard across every kind, not just posts.

    Post ids are checked against the raw plan files (find_duplicate_post_ids)
    since those can collide without IdRegistry ever seeing both sides (the
    SQLite-backed `posts` list it's normally built from has one row per id).
    Every other kind (project/profile/channel/memo/experiment/product/
    feature/doc/subsection/brief/field) is allocated from a natural key via
    allocate(), which can't mint the same composed id for two different
    keys within one scope — so IdRegistry.build() raising ValueError is
    itself the guard for those; this just runs it and turns that crash into
    a reportable message instead of an exception.

    Returns a list of human-readable problem descriptions; empty means clean.
    """
    problems: list[str] = []
    dupes = find_duplicate_post_ids(root)
    for pid, files in sorted(dupes.items()):
        problems.append(f"duplicate post id '{pid}' in: {', '.join(files)}")
    try:
        build_id_registry(tree, posts, root=root, features=features)
    except ValueError as e:
        problems.append(str(e))
    return problems


def next_milestone_id(existing: set[str]) -> str:
    return _stamp("ms-", existing)


def next_activity_id(existing: set[str]) -> str:
    return _stamp("act-", existing)


def slug_key(text: str) -> str:
    """Stable key segment from a title (for feat: ids)."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "untitled"


def next_memo_version(memo_dir: Path, mtype: str) -> int:
    """Next N for {mtype}-vN.json in a project's strategy/memos/."""
    n = 0
    if memo_dir.is_dir():
        for f in memo_dir.glob(f"{mtype}-v*.json"):
            m = re.match(rf"^{re.escape(mtype)}-v(\d+)\.json$", f.name)
            if m:
                n = max(n, int(m.group(1)))
    return n + 1


def next_experiment_stem(exp_dir: Path) -> str:
    """Next exp-NNN-design stem under strategy/experiments/."""
    n = 0
    if exp_dir.is_dir():
        for f in exp_dir.glob("exp-*.json"):
            m = re.match(r"^exp-(\d+)", f.stem)
            if m:
                n = max(n, int(m.group(1)))
    return f"exp-{n + 1:03d}-design"


def experiment_stem_from_path(file_path: str | None) -> str:
    """Canonical exp id segment from os.db file_path."""
    if not file_path:
        return ""
    return Path(file_path).stem


# --------------------------------------------------------------------------- #
# catalog
# --------------------------------------------------------------------------- #

def build_catalog(
    tree: list[dict],
    *,
    root: Path | None = None,
    posts: list[dict] | None = None,
    features: list[dict] | None = None,
) -> list[dict]:
    """Live composed ID catalog from tree + posts."""
    reg = build_id_registry(tree, posts, root=root, features=features)
    return list(reg.entries)


# --------------------------------------------------------------------------- #
# project sections (composed views)
# --------------------------------------------------------------------------- #

def _rel_path(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _latest_memos(memos: list[dict], types: tuple[str, ...]) -> list[dict]:
    by_type: dict[str, dict] = {}
    for m in memos or []:
        t = m.get("type") or ""
        if t not in types:
            continue
        cur = by_type.get(t)
        if cur is None or (m.get("version") or 0) > (cur.get("version") or 0):
            by_type[t] = m
    return [by_type[t] for t in types if t in by_type]


def _memo_artifacts(
    project_slug: str,
    memos: list[dict],
    types: tuple[str, ...],
    registry: IdRegistry | None = None,
) -> list[dict]:
    out: list[dict] = []
    for m in _latest_memos(memos, types):
        mtype = m.get("type") or ""
        ver = int(m.get("version") or 0)
        key = lk_memo(project_slug, mtype, ver)
        out.append({
            "id": registry.get(key) if registry else key,
            "kind": "memo",
            "path": m.get("file_path"),
            "label": f"{mtype} v{ver}",
            "meta": m.get("status"),
        })
    return out


def _file_artifacts(
    root: Path,
    proj_dir: Path,
    rel_paths: list[str],
    *,
    project_slug: str | None = None,
    registry: IdRegistry | None = None,
) -> list[dict]:
    doc_keys = {rel: key for rel, _sec, key in PROJECT_DOC_FILES}
    out: list[dict] = []
    for rel in rel_paths:
        path = proj_dir / rel
        if path.is_file():
            item: dict = {
                "id": None,
                "kind": "file",
                "path": _rel_path(root, path),
                "label": rel,
            }
            if registry and project_slug and rel in doc_keys:
                item["id"] = registry.get(lk_doc(project_slug, doc_keys[rel]))
            if rel.endswith(".md"):
                try:
                    item["text"] = path.read_text(encoding="utf-8")
                except OSError:
                    item["text"] = ""
            out.append(item)
    return out


def resolve_section(
    project_slug: str,
    section_key: str,
    root: Path,
    *,
    project_data: dict | None = None,
    registry: IdRegistry | None = None,
) -> dict | None:
    """Resolve project section → label, artifacts, empty, skill hints."""
    layout = PROJECT_SECTION_LAYOUT.get(section_key)
    if layout is None:
        return None

    proj_dir = root / "projects" / project_slug
    pdata = project_data or {}
    memos = pdata.get("memos") or []
    experiments = pdata.get("experiments") or []
    products = pdata.get("products") or []
    features = pdata.get("features") or []

    artifacts: list[dict] = []

    if layout.get("rollup"):
        artifacts.extend(_file_artifacts(
            root, proj_dir, layout.get("files") or [],
            project_slug=project_slug, registry=registry))
        artifacts.extend(_memo_artifacts(project_slug, memos, tuple(layout.get("memo_types") or ()), registry))
    elif section_key in ("validation", "technical"):
        artifacts.extend(_file_artifacts(
            root, proj_dir, layout.get("files") or [],
            project_slug=project_slug, registry=registry))
        if section_key == "validation":
            artifacts.extend(_memo_artifacts(project_slug, memos, tuple(layout.get("memo_types") or ()), registry))
    elif section_key == "experiments":
        if experiments:
            for x in experiments:
                stem = x.get("stem") or experiment_stem_from_path(x.get("file_path")) or ""
                key = lk_experiment(project_slug, stem)
                artifacts.append({
                    "id": registry.get(key) if registry else key,
                    "kind": "exp",
                    "path": x.get("file_path"),
                    "label": x.get("assumption") or stem,
                    "meta": x.get("status"),
                })
        else:
            exp_dir = proj_dir / (layout.get("experiment_dir") or "strategy/experiments")
            if exp_dir.is_dir():
                for f in sorted(exp_dir.glob("*.json")):
                    key = lk_experiment(project_slug, f.stem)
                    artifacts.append({
                        "id": registry.get(key) if registry else key,
                        "kind": "exp",
                        "path": _rel_path(root, f),
                        "label": f.stem,
                    })
    elif section_key == "pricing":
        artifacts.extend(_memo_artifacts(project_slug, memos, tuple(layout.get("memo_types") or ()), registry))
    elif section_key == "gtm":
        from core import gtm as _gtm
        for phase in _gtm.load_phases(root, project_slug):
            ph_id = phase.get("id") or ""
            artifacts.append({
                "id": registry.get(lk_phase(project_slug, ph_id)) if registry else lk_phase(project_slug, ph_id),
                "kind": "phase",
                "label": phase.get("name") or ph_id,
                "meta": phase.get("status"),
            })
        for strat in _gtm.load_strategies(root, project_slug):
            stem = strat.get("stem") or ""
            artifacts.append({
                "id": registry.get(lk_strategy(project_slug, stem)) if registry else lk_strategy(project_slug, stem),
                "kind": "strategy",
                "label": strat.get("name") or stem,
                "meta": strat.get("status"),
            })
        artifacts.extend(_memo_artifacts(project_slug, memos, tuple(layout.get("memo_types") or ()), registry))
    elif section_key == "product":
        if products:
            for p in products:
                pslug = p.get("slug") or ""
                prod_dir = proj_dir / "products" / pslug
                name = p.get("name") or pslug
                prod_md = prod_dir / "product.md"
                if prod_md.is_file():
                    artifacts.append({
                        "id": registry.get(lk_prod(pslug)) if registry else lk_prod(pslug),
                        "kind": "prod",
                        "path": _rel_path(root, prod_md),
                        "label": name,
                    })
                roadmap = prod_dir / "roadmap.md"
                if roadmap.is_file():
                    artifacts.append({
                        "id": None,
                        "kind": "file",
                        "path": _rel_path(root, roadmap),
                        "label": f"{name} roadmap",
                    })
                for feat in features:
                    if feat.get("product_slug") != pslug:
                        continue
                    title = feat.get("title") or "untitled"
                    tk = slug_key(title)
                    key = lk_feature(pslug, tk)
                    artifacts.append({
                        "id": registry.get(key) if registry else key,
                        "kind": "feat",
                        "path": _rel_path(root, roadmap) if roadmap.is_file() else None,
                        "label": title,
                        "meta": feat.get("status"),
                    })
        else:
            prod_root = proj_dir / (layout.get("product_dir") or "products")
            if prod_root.is_dir():
                for d in sorted(prod_root.iterdir()):
                    if d.is_dir() and (d / "product.md").is_file():
                        key = lk_prod(d.name)
                        artifacts.append({
                            "id": registry.get(key) if registry else key,
                            "kind": "prod",
                            "path": _rel_path(root, d / "product.md"),
                            "label": d.name,
                        })

    skill = layout.get("skill")
    skills = layout.get("skills")
    empty = len(artifacts) == 0
    tab_key = lk_tab_proj(project_slug, section_key)
    sec_id = registry.get(tab_key) if registry else tab_key

    return {
        "id": sec_id,
        "section": section_key,
        "project": project_slug,
        "label": layout["label"],
        "empty": empty,
        "skill": skill,
        "skills": skills,
        "artifacts": artifacts,
    }


def build_project_sections(
    project_slug: str,
    root: Path,
    project_data: dict | None = None,
    *,
    registry: IdRegistry | None = None,
) -> dict[str, dict]:
    """All section views for get-project / dashboard."""
    out: dict[str, dict] = {}
    for key, _label in PROJECT_SECTIONS:
        sec = resolve_section(project_slug, key, root, project_data=project_data, registry=registry)
        if sec is not None:
            out[key] = sec
    return out


def section_tally(
    project_slug: str,
    section_key: str,
    root: Path,
    *,
    memos: list[dict] | None = None,
    experiments: list[dict] | None = None,
    products: list[dict] | None = None,
    features: list[dict] | None = None,
) -> str:
    """One-line status for compact chat index."""
    layout = PROJECT_SECTION_LAYOUT.get(section_key)
    if not layout:
        return "empty"
    proj_dir = root / "projects" / project_slug
    memos = memos or []
    experiments = experiments or []
    products = products or []
    features = features or []
    parts: list[str] = []

    if section_key in ("overview", "validation"):
        for rel in layout.get("files") or []:
            if (proj_dir / rel).is_file():
                if rel.endswith("intake.md"):
                    parts.append("intake ✓")
                elif rel == "project.md":
                    parts.append("project ✓")
                else:
                    parts.append(rel)
        n = len(_latest_memos(memos, tuple(layout.get("memo_types") or ())))
        if n:
            parts.append(f"{n} memo{'s' if n != 1 else ''}")
    elif section_key == "experiments":
        exp_dir = proj_dir / "strategy/experiments"
        if experiments:
            n = len(experiments)
        elif exp_dir.is_dir():
            n = len(list(exp_dir.glob("*.json")))
        else:
            n = 0
        if n:
            parts.append(f"{n} exp")
    elif section_key == "pricing":
        n = len(_latest_memos(memos, tuple(layout.get("memo_types") or ())))
        if n:
            parts.append(f"{n} memo{'s' if n != 1 else ''}")
    elif section_key == "product":
        prod_dir = proj_dir / "products"
        if products:
            n_prod = len(products)
        elif prod_dir.is_dir():
            n_prod = sum(1 for d in prod_dir.iterdir() if d.is_dir())
        else:
            n_prod = 0
        if n_prod:
            parts.append(f"{n_prod} product{'s' if n_prod != 1 else ''}")
        if features:
            parts.append(f"{len(features)} feat")
    elif section_key == "technical":
        for rel in layout.get("files") or []:
            if (proj_dir / rel).is_file():
                parts.append("technical ✓")
    elif section_key == "gtm":
        from core import gtm as _gtm
        n_ph = len(_gtm.load_phases(root, project_slug))
        n_st = len(_gtm.load_strategies(root, project_slug))
        n_memo = len(_latest_memos(memos, tuple(layout.get("memo_types") or ())))
        if n_ph:
            parts.append(f"{n_ph} phase{'s' if n_ph != 1 else ''}")
        if n_st:
            parts.append(f"{n_st} strateg{'ies' if n_st != 1 else 'y'}")
        if n_memo:
            parts.append(f"{n_memo} memo{'s' if n_memo != 1 else ''}")

    return " · ".join(parts) if parts else "empty"


def catalog_as_text(entries: list[dict], *, max_lines: int = 200) -> str:
    lines = ["## ID catalog (canonical references)", ""]
    for e in entries[:max_lines]:
        parent = f"  parent={e['parent']}" if e.get("parent") else ""
        lines.append(f"- `{e['id']}` — {e['label']}{parent}")
    if len(entries) > max_lines:
        lines.append(f"… and {len(entries) - max_lines} more (use get-id-catalog --json)")
    return "\n".join(lines)