"""config.py: engine settings, in gitignored team/engines.json (owner-only, 0600).

{"providers": [{id, name, format, base_url, key, models: {fast, strong}}],
 "default": "claude-code" | "api:<id>" | ...,      the owner's turns
 "people": "api:<id>" | "",                        invited people, and shared threads an engine cannot serve
 "workspaces": {slug: engine id}, "bots": {"slug/bot": engine id},
 "acp_agents": [{id, name, command, args}],
 "terminal": "shell" | "claude" | "codex"}

Keys never leave this module except to the api driver: public() shows the last
four characters only. team/ is already denied to bots and to osctl read-file.
"""
import json
import os
import re
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
FORMATS = ("anthropic", "openai")
TERMINALS = ("shell", "claude", "codex")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

# name: format, base url, needs a key, fast model, strong model
PRESETS = {
    "anthropic": {"name": "Anthropic", "format": "anthropic", "base_url": "https://api.anthropic.com",
                  "models": {"fast": "claude-haiku-5-5", "strong": "claude-sonnet-5-5"}},
    "openai": {"name": "OpenAI", "format": "openai", "base_url": "https://api.openai.com/v1",
               "models": {"fast": "gpt-5-mini", "strong": "gpt-5"}},
    "openrouter": {"name": "OpenRouter", "format": "openai", "base_url": "https://openrouter.ai/api/v1",
                   "models": {"fast": "anthropic/claude-haiku-5.5", "strong": "anthropic/claude-sonnet-5.5"}},
    "ollama": {"name": "Ollama (local)", "format": "openai", "base_url": "http://127.0.0.1:11434/v1",
               "no_key": True, "models": {"fast": "", "strong": ""}},
}


# ACP agents people commonly have: name, command, args.
ACP_PRESETS = {
    "opencode": {"name": "OpenCode", "command": "opencode", "args": ["acp"]},
    "gemini": {"name": "Gemini CLI", "command": "gemini", "args": ["--experimental-acp"]},
}


def _file(root):
    return Path(root) / "team" / "engines.json"


def load(root=ROOT):
    try:
        data = json.loads(_file(root).read_text())
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data["providers"] = [p for p in data.get("providers") or [] if isinstance(p, dict) and p.get("id")]
    data["acp_agents"] = [a for a in data.get("acp_agents") or [] if isinstance(a, dict) and a.get("id")]
    for key in ("workspaces", "bots"):
        data[key] = data.get(key) if isinstance(data.get(key), dict) else {}
    return data


def save(data, root=ROOT):
    path = _file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")  # mkstemp is 0600
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def provider(provider_id, root=ROOT):
    return next((p for p in load(root)["providers"] if p["id"] == provider_id), None)


def upsert_provider(fields, root=ROOT):
    """Add or edit one provider. An empty key on an edit keeps the saved key."""
    pid = str(fields.get("id") or "").strip().lower()
    if not _ID_RE.match(pid):
        raise ValueError("id: lowercase letters, digits and dashes")
    preset = PRESETS.get(pid, {})
    data = load(root)
    old = next((p for p in data["providers"] if p["id"] == pid), {})
    # An edit sends only what changed: what is saved fills in the rest.
    fmt = fields.get("format") or old.get("format") or preset.get("format")
    if fmt not in FORMATS:
        raise ValueError(f"format: one of {', '.join(FORMATS)}")
    base = str(fields.get("base_url") or old.get("base_url") or preset.get("base_url") or "").strip().rstrip("/")
    if not re.match(r"^https?://[^\s/]+", base):
        raise ValueError("base_url: an http(s) address")
    key = str(fields.get("key") or "").strip()
    if not key and old.get("key"):
        if old.get("base_url") != base:  # a saved key never follows a new address on its own
            raise ValueError("the address changed: enter the key again")
        key = old["key"]
    if not key and not (preset.get("no_key") or fields.get("no_key") or old.get("no_key")):
        raise ValueError("key required")
    wanted = fields.get("models") if isinstance(fields.get("models"), dict) else {}
    defaults = old.get("models") or preset.get("models") or {}
    models = {t: str(wanted.get(t) or defaults.get(t) or "").strip() for t in ("fast", "strong")}
    if not models["fast"] and not models["strong"]:
        raise ValueError("at least one model name required")
    models = {"fast": models["fast"] or models["strong"], "strong": models["strong"] or models["fast"]}
    row = {"id": pid, "name": str(fields.get("name") or old.get("name") or preset.get("name") or pid)[:60],
           "format": fmt, "base_url": base, "key": key, "models": models,
           **({"no_key": True} if not key else {})}
    data["providers"] = [p for p in data["providers"] if p["id"] != pid] + [row]
    save(data, root)
    return row


def upsert_acp(fields, root=ROOT):
    """A CLI agent, from a preset or a command of your own. The server allows this from this machine only."""
    aid = str(fields.get("id") or "").strip().lower()
    if not _ID_RE.match(aid):
        raise ValueError("id: lowercase letters, digits and dashes")
    preset = ACP_PRESETS.get(aid, {})
    command = str(fields.get("command") or preset.get("command") or "").strip()
    args = fields.get("args") if isinstance(fields.get("args"), list) else preset.get("args") or []
    if not command or not all(isinstance(a, str) for a in args):
        raise ValueError("command required")
    row = {"id": aid, "name": str(fields.get("name") or preset.get("name") or aid)[:60], "command": command, "args": args}
    data = load(root)
    data["acp_agents"] = [a for a in data["acp_agents"] if a["id"] != aid] + [row]
    save(data, root)
    return row


def remove_acp(agent_id, root=ROOT):
    data = load(root)
    data["acp_agents"] = [a for a in data["acp_agents"] if a["id"] != agent_id]
    _clear_engine(data, f"acp:{agent_id}")
    save(data, root)


def _clear_engine(data, gone):
    for key in ("default", "people"):
        if data.get(key) == gone:
            data[key] = ""
    for key in ("workspaces", "bots"):
        data[key] = {k: v for k, v in data[key].items() if v != gone}


def forget_workspace(slug, root=ROOT):
    """A deleted workspace: its engine picks and its bots' picks go too."""
    data = load(root)
    data["workspaces"].pop(slug, None)
    data["bots"] = {k: v for k, v in data["bots"].items() if not k.startswith(f"{slug}/")}
    save(data, root)


def remove_provider(provider_id, root=ROOT):
    data = load(root)
    data["providers"] = [p for p in data["providers"] if p["id"] != provider_id]
    _clear_engine(data, f"api:{provider_id}")
    save(data, root)


def set_choice(name, value, scope=None, root=ROOT):
    """Set default | people | terminal, or one workspace's or bot's engine ("" clears it)."""
    data = load(root)
    value = str(value or "")
    if name == "terminal":
        if value not in TERMINALS:
            raise ValueError("unknown terminal")
        data["terminal"] = value
    elif name in ("default", "people"):
        if name == "people" and value and not value.startswith("api:"):
            raise ValueError("invited people run on an API key")
        data[name] = value
    elif name in ("workspaces", "bots"):
        if not scope:
            raise ValueError("scope required")
        if value:
            data[name][scope] = value
        else:
            data[name].pop(scope, None)
    else:
        raise ValueError("unknown setting")
    save(data, root)


def public(root=ROOT):
    """The settings for the engines screen: never a key, only its last four characters."""
    data = load(root)
    rows = []
    for p in data["providers"]:
        key = p.get("key") or ""
        rows.append({**{k: p.get(k) for k in ("id", "name", "format", "base_url", "models")},
                     "key_hint": key[-4:] if key else "", "no_key": not key})
    return {"providers": rows, "default": data.get("default") or "", "people": data.get("people") or "",
            "workspaces": data["workspaces"], "bots": data["bots"],
            "terminal": data.get("terminal") or "shell", "acp_agents": data["acp_agents"],
            "acp_presets": ACP_PRESETS,
            "presets": {k: {f: v.get(f) for f in ("name", "format", "base_url", "models", "no_key")}
                        for k, v in PRESETS.items()}}
