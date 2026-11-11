"""python -m evals: the offline scorecard (free). python -m evals --live: see evals/live.py."""
import atexit
import os
import shutil
import sys
import tempfile


def main(argv):
    if "--live" in argv:
        from evals import live
        return live.main([a for a in argv if a != "--live"])
    if argv:
        print("usage: python -m evals [--live [--engine ID] [--case ID ...] [--yes]]", file=sys.stderr)
        return 2
    # Offline evals never touch the real home folder: a temp one, set before anything imports paths.
    home = tempfile.mkdtemp(prefix="leobot-evals-")
    atexit.register(shutil.rmtree, home, ignore_errors=True)
    os.environ["LEOBOT_HOME"] = home
    from evals import recall, turns
    results = turns.run_all()
    print(turns.scorecard(results))
    data = recall.load()
    found = recall.run(data)
    print(recall.scorecard(found, data["max_miss"]))
    failed = any(fails and not case.get("gap") for case, fails in results)
    return 1 if failed or found["miss"] > data["max_miss"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
