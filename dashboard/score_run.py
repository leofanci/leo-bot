"""score_run.py: one run of the scoring job. For each experiment of a workspace that has a metric
source: read it (a read-only app action, no click), take the number, ask the engine for a
verdict against the criteria set in advance, and hold the verdict as a card. Nothing is saved
here: the experiment and the evidence log change only when the owner clicks Approve.

The source's output never reaches a bot turn. It is quoted into the judge call as data; only the
number read from it is used and shown. A failed read, a bad path or an unreadable judgement is an
`inconclusive` verdict with the reason, never a guess.
"""
import json
import time
import traceback
from pathlib import Path

from core import approvals, bots, jobs, scoring
from core.ids import build_id_registry, lk_experiment
from dashboard import db as _db

TITLE = "Results: {name}"


def _experiments(root, ws):
    """[{"project", "stem", "id", "body"}] for the workspace's experiments, as the index lists them."""
    root = Path(root)
    reg = build_id_registry(_db.tree(), _db.posts(), root=root)
    out = []
    for row in _db._rows("SELECT entity_slug, file_path FROM experiments ORDER BY entity_slug, file_path"):
        if ws != bots.HQ and row["entity_slug"] != ws:
            continue
        try:
            body = json.loads((root / row["file_path"]).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        stem = Path(row["file_path"]).stem
        if isinstance(body, dict):
            out.append({"project": row["entity_slug"], "stem": stem, "body": body,
                        "id": reg.get(lk_experiment(row["entity_slug"], stem)) or f"{row['entity_slug']}/{stem}"})
    return out


def _preview(exp, v):
    body = exp["body"]
    return {"title": f"Result: {body.get('assumption') or exp['stem']}"[:120], "fields": [
        ["Experiment", exp["id"]],
        ["Number", "-" if v["number"] is None else f"{v['number']:g}"],
        ["Window", f"{v['window_days']} days"],
        ["Date", v["date"]],
        ["Success criteria", body.get("success_criteria") or "-"],
        ["Kill criteria", body.get("kill_criteria") or "-"],
        ["Verdict", v["verdict"]],
        ["Why", v["reason"] or "-"]]}


def run(job, *, root, judge, read, now=time.time, records=None, post=None, experiments=None):
    """Score every experiment with a metric. `judge(prompt) -> (answer or None, cost)` and
    `read(app, action, args) -> object` are injected (the server passes the metered one-shot engine
    call and the Composio read). Returns {"status": "ok"|"capped", "cards": n, "chat", "cost", "detail"}."""
    from dashboard import spend
    ws, bot, mode = job.get("workspace") or bots.HQ, job["bot"], job.get("mode") or "approval"
    cap = (job["id"], job["cap"]) if job.get("cap") else None
    date = time.strftime("%Y-%m-%d", time.localtime(now()))
    exps = experiments if experiments is not None else _experiments(root, ws)
    results, cost, status = [], 0.0, "ok"
    try:
        for exp in exps:
            metric = exp["body"].get("metric")
            if not metric or exp["body"].get("status") == "done":
                continue
            if spend.refusal(ws, "owner", records=records, job=cap):
                status = "capped"
                break
            v = {"exp": exp["id"], "project": exp["project"], "stem": exp["stem"], "metric": metric,
                 "number": None, "window_days": metric.get("window_days"), "date": date,
                 "verdict": "inconclusive", "reason": "", "raw": ""}
            try:
                raw = read(metric["app"], metric["action"], metric.get("args") or {})
                v["raw"] = json.dumps(raw, default=str)[:scoring.RAW_MAX] if raw is not None else ""
                v["number"] = scoring.pick_number(raw, metric["path"])
            except scoring.ScoreError as e:
                v["reason"] = f"could not read a number: {e}"
            except Exception as e:  # noqa: BLE001 - one source failing never stops the others
                v["reason"] = f"read failed: {str(e)[:150]}"
            jobs.set_live(root, job["id"], cost)
            if v["number"] is not None:
                criteria = {"success": exp["body"].get("success_criteria"), "kill": exp["body"].get("kill_criteria")}
                answer, spent = judge(scoring.judge_prompt(criteria, v["number"], v["window_days"], date, v["raw"]))
                cost += float(spent or 0)
                jobs.set_live(root, job["id"], cost)
                if answer is None:
                    v["reason"] = "not judged (cap reached or no engine could answer)"
                else:
                    v.update(scoring.parse_judgement(answer))
            results.append((exp, v))
    finally:
        try:
            jobs.set_live(root, None, 0)
        except OSError:
            traceback.print_exc()
    lines = [f"- {e['id']}: {v['verdict']}" + (f", {v['number']:g}" if v["number"] is not None else "")
             + (f". {v['reason']}" if v["reason"] else "") for e, v in results]
    out = {"status": status, "cards": 0, "chat": None, "cost": cost, "detail": ""}
    if mode == "report":
        out["detail"] = "\n".join(lines)
        if lines and post:
            out["chat"] = post(TITLE.format(name=job.get("name") or "scoring"), f"{ws}/dm-{bot}", bot,
                               "Experiment results (nothing is saved in report mode):\n" + "\n".join(lines))["id"]
        return out
    fresh = [(e, v) for e, v in results if not approvals.has_pending_verdict(root, v)]
    if fresh and post:
        thread = post(TITLE.format(name=job.get("name") or "scoring"), f"{ws}/dm-{bot}", bot,
                      "Experiment results. Approve a card to save its result and evidence:\n" + "\n".join(lines))
        out["chat"] = thread["id"]
        for exp, v in fresh:
            approvals.add_verdict(root, ws=ws, room=f"{ws}/dm-{bot}", chat=thread["id"], bot=bot, verdict=v,
                                  summary=f"Result: {v['verdict']} for {exp['stem']}"[:200], preview=_preview(exp, v))
            out["cards"] += 1
    return out
