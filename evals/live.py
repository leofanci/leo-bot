"""Live evals: a few known-answer prompts sent to a real engine, on request only (they spend).

The turns run in a throwaway home folder (a child process with its own LEOBOT_HOME), so nothing
a bot saves reaches your data. The engine settings are copied there for the run and deleted with
it. Each run is saved to <home>/evals/ and compared with the one before: which cases changed
and what the run cost.

    python -m evals --live [--engine ID|all] [--case ID ...] [--yes]
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from evals import turns

CASES = Path(__file__).parent / "cases" / "live.json"
_LIVE_KEYS = ("mentions", "max_chars", "max_bullets", "teammates_include", "reaches")
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+", re.M)
KEEP_RUNS = 20


def load(path=CASES):
    cases = json.loads(Path(path).read_text(encoding="utf-8"))
    for c in cases:
        std = {k: v for k, v in (c.get("expect") or {}).items() if k not in _LIVE_KEYS}
        turns.load_check({**c, "expect": std})
    return cases


def grade(case, outcome):
    """Failure lines: the turn checks, plus what only a real reply can show."""
    expect = case.get("expect") or {}
    fails = turns.check({"expect": {k: v for k, v in expect.items() if k not in _LIVE_KEYS}}, outcome)
    reply = outcome.get("reply") or ""
    if "mentions" in expect and not any(s.lower() in reply.lower() for s in expect["mentions"]):
        fails.append(f"mentions: none of {expect['mentions']!r} in the reply")
    if "max_chars" in expect and len(reply) > expect["max_chars"]:
        fails.append(f"max_chars: reply is {len(reply)} characters, over {expect['max_chars']}")
    if "max_bullets" in expect:
        n = len(_BULLET_RE.findall(reply))
        if n > expect["max_bullets"]:
            fails.append(f"max_bullets: {n} bullets, over {expect['max_bullets']}")
    missing = set(expect.get("teammates_include") or ()) - set(outcome.get("teammates") or ())
    if missing:
        fails.append(f"teammates_include: {sorted(missing)} not consulted")
    if "reaches" in expect:  # the teammate answers, consulted or handed the work (HANDOFF)
        got = set(outcome.get("teammates") or ()) | {outcome.get("handoff")}
        if expect["reaches"] not in got:
            fails.append(f"reaches: @{expect['reaches']} neither consulted nor handed the work")
    return fails


def _child(args):
    """Inside the throwaway home: run each case on the real engine, write the results."""
    from core import paths
    results = []
    for case in load():
        if args.case and case["id"] not in args.case:
            continue
        started = time.time()
        try:
            outcome = turns.run(case, live_root=paths.DATA)
            fails = grade(case, outcome)
        except Exception as e:  # noqa: BLE001 - one broken case must not lose the others
            outcome, fails = {}, [f"crashed: {type(e).__name__}: {e}"]
        results.append({"id": case["id"], "pass": not fails, "fails": fails,
                        "cost": outcome.get("cost") or 0, "engine": outcome.get("engine"),
                        "model": outcome.get("model"), "seconds": round(time.time() - started, 1),
                        "reply": (outcome.get("reply") or "")[:2000]})
        print(f"{'ok  ' if not fails else 'FAIL'} {case['id']}", file=sys.stderr, flush=True)
    Path(args.out).write_text(json.dumps(results, indent=1), encoding="utf-8")
    return 0


def compare(now, last):
    """Scorecard lines for this run, against the last one when there is one."""
    before = {r["id"]: r for r in (last or {}).get("results") or []}
    lines = []
    for r in now["results"]:
        was = before.get(r["id"])
        change = ""
        if was and was["pass"] != r["pass"]:
            change = "  (was passing)" if was["pass"] else "  (was failing)"
        lines.append(f"{'ok  ' if r['pass'] else 'FAIL'} {r['id']}  ${r['cost']:.4f}  {r['seconds']}s{change}")
        lines += [f"     {f}" for f in r["fails"]]
    total = sum(r["cost"] for r in now["results"])
    passed = sum(r["pass"] for r in now["results"])
    line = f"{passed}/{len(now['results'])} passed, ${total:.4f} estimated on {now.get('engine') or '?'}"
    if last:
        line += (f" (last run {last.get('when', '?')}: {sum(r['pass'] for r in last['results'])}/"
                 f"{len(last['results'])}, ${sum(r['cost'] for r in last['results']):.4f})")
    return "\n".join(lines + [line])


def _runs_dir():
    from core import paths
    return paths.HOME / "evals"


def last_run(folder, engine=None):
    """The newest saved run, of `engine` when given (a run is compared with its own engine's)."""
    for f in reversed(sorted(Path(folder).glob("live-*.json"))):
        try:
            run = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if engine is None or run.get("engine") == engine:
            return run
    return None


def save_run(folder, run):
    folder = Path(folder)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    tag = re.sub(r"[^a-z0-9-]", "", str(run.get("engine") or "").lower())
    path = folder / f"live-{time.strftime('%Y%m%d-%H%M%S')}{'-' + tag if tag else ''}.json"
    path.write_text(json.dumps(run, indent=1), encoding="utf-8")
    path.chmod(0o600)
    for old in sorted(folder.glob("live-*.json"))[:-KEEP_RUNS]:
        old.unlink(missing_ok=True)
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m evals --live", description="Run live evals on a real engine.")
    ap.add_argument("--engine", help="engine id to use, or all: every ready engine (default: your default engine)")
    ap.add_argument("--case", action="append", help="run only this case id (repeatable)")
    ap.add_argument("--yes", action="store_true", help="do not ask before spending")
    ap.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--out", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.child:
        return _child(args)

    from core import paths
    from dashboard.engines import config, registry
    cases = [c for c in load() if not args.case or c["id"] in args.case]
    unknown = set(args.case or ()) - {c["id"] for c in cases}
    if unknown or not cases:
        print(f"Unknown case: {', '.join(sorted(unknown)) or '(none selected)'}", file=sys.stderr)
        return 2
    if args.engine == "all":  # the same cases on every engine that is ready, so they stay alike
        engines = [r["id"] for r in registry.statuses(paths.DATA) if r.get("ok")]
        if not engines:
            print("No engine is ready to run live evals.", file=sys.stderr)
            return 2
    else:
        engines = [args.engine or registry.default_id(config.load(paths.DATA))]
    if not args.yes:
        answer = input(f"Live evals send {len(cases)} prompts (plus any teammate turns) to "
                       f"{', '.join(engines)}, billed to your account. Continue? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Nothing was sent.")
            return 1
    runs = []
    for engine in engines:
        run = _run_engine(engine, args)
        if run is None:
            return 1
        runs.append(run)
    if len(runs) > 1:
        print("\n".join(f"{r['engine']}: {sum(x['pass'] for x in r['results'])}/{len(r['results'])} passed"
                        for r in runs))
    return 0 if all(x["pass"] for r in runs for x in r["results"]) else 1


def _run_engine(engine, args):
    """One engine's live run in a throwaway home: saved, compared with that engine's last run
    and printed. None when the run stopped before it finished."""
    from core import paths
    with tempfile.TemporaryDirectory(prefix="leobot-eval-home-") as home:
        team = Path(home) / "data" / "team"
        team.mkdir(parents=True)
        src = paths.DATA / "team" / "engines.json"
        data = json.loads(src.read_text(encoding="utf-8")) if src.exists() else {}
        data["default"] = engine
        data["workspaces"], data["bots"] = {}, {}  # the fixture workspace runs on `engine`
        copy = team / "engines.json"
        copy.touch(mode=0o600)
        copy.write_text(json.dumps(data), encoding="utf-8")
        out = Path(home) / "results.json"
        cmd = [sys.executable, "-m", "evals.live", "--child", "--out", str(out)]
        for c in args.case or ():
            cmd += ["--case", c]
        proc = subprocess.run(cmd, cwd=str(paths.CODE), env={**os.environ, "LEOBOT_HOME": home})
        if proc.returncode != 0 or not out.exists():
            print(f"The live run on {engine} stopped before it finished; see the error above.",
                  file=sys.stderr)
            return None
        results = json.loads(out.read_text(encoding="utf-8"))
        shutil.rmtree(team, ignore_errors=True)  # the key copy goes first, before the folder
    run = {"when": time.strftime("%Y-%m-%d %H:%M"), "engine": engine, "results": results}
    folder = _runs_dir()
    last = last_run(folder, engine)
    save_run(folder, run)
    print(compare(run, last))
    return run


if __name__ == "__main__":
    sys.exit(main())
