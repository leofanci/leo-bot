"""general.py: Settings, General. Machine-wide basics, kept in leobot.json next to the update settings.

  about_me        what every bot knows about the owner (private turns only), at most ABOUT_MAX chars
  reply_language  the language bots reply in; "" replies in the language of the message
  timezone        an IANA name for schedules and dates; "" keeps the machine's own
  short_replies   Leo Bot's short reply style (on by default)
  caps            {"owner" | "workspace" | "person": dollars per 24 hours}; 0 is no cap. An
                  environment variable (dashboard/spend.py) still wins over a saved value.
  chain_hops      bot-to-bot hops one message may take (core/chain.py); 0 is no limit
  chain_cost      dollars one message's chain may spend; 0 is no cap
"""
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from core import updater

ABOUT_MAX = 1500
LANGUAGE_MAX = 40
CAP_KINDS = ("owner", "workspace", "person")
CAP_MAX = 10000.0
CHAIN_HOPS_MAX = 1000
CHAIN_COST_MAX = CAP_MAX
_MACHINE_TZ = os.environ.get("TZ")  # what the process started with, for "" (the machine's own)


def valid_timezone(name):
    if not name:
        return True
    if not isinstance(name, str) or name.startswith(("/", ".")) or ".." in name:
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):  # OSError: a folder name like "America"
        return False
    return True


def view():
    """The General screen's values, cleaned: a hand-edited file never breaks a turn."""
    s = updater.settings()
    about = s.get("about_me") if isinstance(s.get("about_me"), str) else ""
    lang = s.get("reply_language") if isinstance(s.get("reply_language"), str) else ""
    tz = s.get("timezone") if isinstance(s.get("timezone"), str) and valid_timezone(s.get("timezone")) else ""
    raw = s.get("caps") if isinstance(s.get("caps"), dict) else {}
    caps = {}
    for kind in CAP_KINDS:
        v = raw.get(kind)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and 0 <= v <= CAP_MAX:
            caps[kind] = float(v)
    hops = s.get("chain_hops")
    cost = s.get("chain_cost")
    return {"about_me": about[:ABOUT_MAX], "reply_language": lang[:LANGUAGE_MAX], "timezone": tz,
            "short_replies": s["short_replies"], "caps": caps,
            "chain_hops": hops if _whole(hops, CHAIN_HOPS_MAX) else 0,
            "chain_cost": float(cost) if _dollars(cost, CHAIN_COST_MAX) else 0.0}


def _whole(v, top):
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= top


def _dollars(v, top):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and 0 <= v <= top


def update(body):
    """Save the fields `body` names; ValueError on a bad one (nothing is saved then)."""
    if not isinstance(body, dict):
        raise ValueError("expected an object")
    out = {}
    if "about_me" in body:
        if not isinstance(body["about_me"], str):
            raise ValueError("about_me: text")
        text = body["about_me"].replace("\r\n", "\n").strip()
        if len(text) > ABOUT_MAX:
            raise ValueError(f"about_me: at most {ABOUT_MAX} characters")
        out["about_me"] = text
    if "reply_language" in body:
        if not isinstance(body["reply_language"], str):
            raise ValueError("reply_language: text")
        text = " ".join(body["reply_language"].split())
        if len(text) > LANGUAGE_MAX or any(c in text for c in "#`<>"):
            raise ValueError(f"reply_language: a language name, at most {LANGUAGE_MAX} characters")
        out["reply_language"] = text
    if "timezone" in body:
        tz = body["timezone"] if isinstance(body["timezone"], str) else None
        if tz is None or not valid_timezone(tz.strip()):
            raise ValueError("timezone: a name like Europe/Rome, or empty for this machine's")
        out["timezone"] = tz.strip()
    if "short_replies" in body:
        if not isinstance(body["short_replies"], bool):
            raise ValueError("short_replies: true or false")
        out["short_replies"] = body["short_replies"]
    if "caps" in body:
        raw = body["caps"]
        if not isinstance(raw, dict) or set(raw) - set(CAP_KINDS):
            raise ValueError(f"caps: {', '.join(CAP_KINDS)}")
        caps = dict(view()["caps"])
        for kind, v in raw.items():
            if v is None or v == "":
                caps.pop(kind, None)
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= CAP_MAX:
                raise ValueError(f"caps.{kind}: dollars from 0 (no cap) to {CAP_MAX:.0f}")
            caps[kind] = float(v)
        out["caps"] = caps
    if "chain_hops" in body:
        v = body["chain_hops"]
        if v is None or v == "":
            v = 0
        if not _whole(v, CHAIN_HOPS_MAX):
            raise ValueError(f"chain_hops: a whole number from 0 (no limit) to {CHAIN_HOPS_MAX}")
        out["chain_hops"] = v
    if "chain_cost" in body:
        v = body["chain_cost"]
        if v is None or v == "":
            v = 0
        if not _dollars(v, CHAIN_COST_MAX):
            raise ValueError(f"chain_cost: dollars from 0 (no cap) to {CHAIN_COST_MAX:.0f}")
        out["chain_cost"] = float(v)
    if out:
        updater.save_settings(out)
    if "timezone" in out:
        apply_timezone(out["timezone"])
    return view()


def adopt_browser_timezone(zone):
    """The owner's browser reports its zone on each visit. While no timezone was ever saved and
    this machine's clock is on another zone (a server is often on UTC), save the browser's once.
    A saved choice, "This machine's" included, is never changed. True when it saved."""
    zone = zone.strip() if isinstance(zone, str) else ""
    if not zone or not valid_timezone(zone):
        return False
    saved = updater.settings().get("timezone")
    if isinstance(saved, str) and valid_timezone(saved):  # a broken hand edit counts as never set
        return False
    if same_zone(zone, machine_timezone()):
        return False
    update({"timezone": zone})
    return True


def same_zone(a, b):
    """True when two zone names keep the same clock, aliases included (a browser may still say
    Asia/Calcutta where the machine says Asia/Kolkata): same offset at each month of two years."""
    if not a or not b:
        return False
    if a == b:
        return True
    try:
        za, zb = ZoneInfo(a), ZoneInfo(b)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    year = time.gmtime().tm_year
    for y in (year, year + 1):
        for m in range(1, 13):
            at = datetime(y, m, 15, 12, tzinfo=timezone.utc)
            if at.astimezone(za).utcoffset() != at.astimezone(zb).utcoffset():
                return False
    return True


def cap(kind):
    """The saved cap for `kind` in dollars, or None when the owner saved none."""
    try:
        return view()["caps"].get(kind)
    except Exception:  # noqa: BLE001 - a broken settings file falls back to the defaults
        return None


def apply_timezone(name=None):
    """Set this process's timezone (schedules, dates, and every program it starts next)."""
    if name is None:
        try:
            name = view()["timezone"]
        except Exception:  # noqa: BLE001
            name = ""
    if name:
        os.environ["TZ"] = name
    elif _MACHINE_TZ is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = _MACHINE_TZ
    if hasattr(time, "tzset"):
        time.tzset()


def machine_timezone():
    """This machine's own zone name (what "" keeps), "" when it can't be told."""
    if _MACHINE_TZ:
        return _MACHINE_TZ.lstrip(":")
    try:
        target = str(Path("/etc/localtime").resolve())
    except OSError:
        return ""
    return target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else ""


def timezones():
    """IANA names for the picker, sorted."""
    return sorted(z for z in available_timezones() if "/" in z and not z.startswith(("Etc/", "SystemV/", "posix/", "right/")))
