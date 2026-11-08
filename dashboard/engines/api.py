"""api.py: the api engine. myOS runs the agent loop over HTTP on a provider's API key.

Two wire formats: Anthropic Messages and OpenAI-compatible Chat Completions
(OpenAI, OpenRouter, DeepSeek, Fireworks, Ollama, LM Studio). Stdlib urllib only.
This driver never reads a CLI's login: only keys saved in team/engines.json.

Thread history lives in chats/<chat-id>.<provider>.jsonl, one message per line.
Anthropic assistant content is stored whole (thinking and server-tool blocks
stay as they came back), because the API needs them unchanged on later requests.
"""
import http.client
import json
import re
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

from dashboard.engines.api_tools import ToolBox
from dashboard.engines.base import Engine
from dashboard.engines.claude_code import CHAT_MODEL, ESCALATED_MODEL

ANTHROPIC_VERSION = "2023-06-01"
MAX_TOKENS = 16000
MAX_TOOL_CALLS = 25
TURN_TIMEOUT = 900
READ_TIMEOUT = 300
RETRIES = 2
HISTORY_CHARS = 400_000  # about 100k tokens; oldest messages go first, the first one stays
# Current Anthropic prices, USD per million tokens (input, output, cache read). Other models report no cost.
PRICES = {"claude-haiku-5-5": (0.10, 0.50, 0.01), "claude-sonnet-5-5": (2.0, 10.0, 0.20),
          "claude-opus-5-5": (4.0, 20.0, 0.20)}
# Backends the server plugs in (os_relay, apps_relay); generate.py jobs need none.
BACKENDS = {}


class ProviderError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def set_backends(**kw):
    BACKENDS.update(kw)


def friendly_status(status, name):
    if status in (401, 403):
        return f"{name} refused the API key. Check it in HQ, Engines."
    if status == 404:
        return f"{name} does not know this model. Check the model names in HQ, Engines."
    if status == 429:
        return f"{name} is rate limiting this key. Try again in a minute."
    if status == 402:
        return f"{name} says this key is out of credit."
    return f"{name} returned an error ({status})."


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # urllib would resend the API key to the new host
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class _Cancel:
    """What ChatSession.close() terminates mid-turn."""

    def __init__(self):
        self.stopped = False
        self.resp = None

    def poll(self):
        return 0 if self.stopped else None

    def terminate(self):
        self.stopped = True
        resp = self.resp
        if resp is not None:
            try:
                resp.close()
            except OSError:
                pass


def _sse(resp):
    """Yield decoded `data:` payloads from a streaming response."""
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            return
        try:
            obj = json.loads(data)
        except ValueError:
            continue
        if isinstance(obj, dict):
            yield obj


class ApiEngine(Engine):
    kind = "api"
    shared_safe = True  # a shared turn is only ever offered osctl (plus web when allowed)
    supports = frozenset({"web", "apps", "folder_edit", "complete"})

    def __init__(self, provider, root):
        self.provider = provider
        self.root = Path(root)
        self.id = f"api:{provider['id']}"
        self.label = f"{provider.get('name') or provider['id']} API"

    # ---- models ------------------------------------------------------- #
    def resolve_model(self, model):
        models = self.provider.get("models") or {}
        if model in (CHAT_MODEL, None):
            return models.get("fast") or models.get("strong")
        if model == ESCALATED_MODEL:
            return models.get("strong") or models.get("fast")
        return model

    # ---- history ------------------------------------------------------ #
    @staticmethod
    def _repair(messages):
        """Make a stored thread one the API accepts. It refuses a thread for good over a tool
        call with no result or an empty block, and a crash, a dropped stream or a cut-off
        reply can leave exactly that behind. So those go, every turn."""
        out = []
        for i, m in enumerate(messages):
            role, content = m.get("role"), m.get("content")
            if role == "tool":
                owner = next((p for p in reversed(out) if p.get("role") == "assistant"), {})
                if m.get("tool_call_id") in {c.get("id") for c in owner.get("tool_calls") or []}:
                    out.append(m)
                continue
            if role != "assistant":
                out.append(m)
                continue
            if m.get("tool_calls"):  # OpenAI: every call needs its answer right after
                answered = set()
                for later in messages[i + 1:]:
                    if later.get("role") != "tool":
                        break
                    answered.add(later.get("tool_call_id"))
                if not {c.get("id") for c in m["tool_calls"]} <= answered:
                    m = {k: v for k, v in m.items() if k != "tool_calls"}
                if not m.get("tool_calls") and not m.get("content"):
                    continue
            elif isinstance(content, list):  # Anthropic
                after = messages[i + 1].get("content") if i + 1 < len(messages) else None
                answered = {b.get("tool_use_id") for b in after if isinstance(b, dict)} if isinstance(after, list) else set()
                served = {b.get("tool_use_id") for b in content if isinstance(b, dict) and b.get("tool_use_id")}
                blocks = [b for b in content if isinstance(b, dict) and not (
                    (b.get("type") == "tool_use" and b.get("id") not in answered)
                    or (b.get("type") == "server_tool_use" and b.get("id") not in served)
                    or (b.get("type") == "text" and not b.get("text")))]
                if not any(b.get("type") not in ("thinking", "redacted_thinking") for b in blocks):
                    continue
                m = {**m, "content": blocks}
            elif not content:
                continue
            out.append(m)
        return out

    def _history_path(self, session):
        return self.root / "chats" / f"{session.session_id}.{self.provider['id']}.jsonl"

    def _load(self, session):
        if getattr(session, "ephemeral", False):
            return list(getattr(session, "_mem_history", []))
        try:
            lines = self._history_path(session).read_text().splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def _append(self, session, messages):
        if getattr(session, "ephemeral", False):
            session._mem_history = list(getattr(session, "_mem_history", [])) + list(messages)
            return
        path = self._history_path(session)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            for m in messages:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")

    def _trim(self, messages):
        """Keep the first message (the snapshot) and the newest ones that fit, at a turn start."""
        if sum(len(json.dumps(m)) for m in messages) <= HISTORY_CHARS or len(messages) < 3:
            return messages
        kept, size = [], len(json.dumps(messages[0]))
        for m in reversed(messages[1:]):
            size += len(json.dumps(m))
            if size > HISTORY_CHARS:
                break
            kept.append(m)
        kept.reverse()
        kept = kept or [messages[-1]]  # the message being answered always goes
        while len(kept) > 1 and not self._starts_turn(kept[0]):  # never split a tool call from its result
            kept.pop(0)
        trimmed = [self._no_thinking(m) for m in [messages[0], self._bridge()] + kept]
        return [m for m in trimmed if m.get("content") != []]

    @staticmethod
    def _bridge():
        return {"role": "assistant", "content": "(Older messages in this thread were trimmed.)"}

    @staticmethod
    def _starts_turn(m):
        if m.get("role") != "user":
            return False
        c = m.get("content")
        return not (isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c))

    @staticmethod
    def _no_thinking(m):
        """History was edited: thinking blocks would no longer validate, so they go."""
        c = m.get("content")
        if isinstance(c, list):
            return {**m, "content": [b for b in c if not (isinstance(b, dict) and
                                     b.get("type") in ("thinking", "redacted_thinking"))]}
        return m

    # ---- HTTP --------------------------------------------------------- #
    def _open(self, path, payload, cancel=None):
        p = self.provider
        headers = {"Content-Type": "application/json", "User-Agent": "myOS"}
        if p.get("format") == "anthropic":
            headers.update({"x-api-key": p.get("key") or "", "anthropic-version": ANTHROPIC_VERSION})
        elif p.get("key"):
            headers["Authorization"] = f"Bearer {p['key']}"
        req = urllib.request.Request(p["base_url"].rstrip("/") + path,
                                     data=json.dumps(payload).encode(), headers=headers, method="POST")
        last = None
        for attempt in range(RETRIES + 1):
            if cancel is not None and cancel.stopped:
                raise ProviderError("stopped")
            try:
                return _OPENER.open(req, timeout=READ_TIMEOUT)
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read(2000).decode("utf-8", "replace")
                except OSError:
                    pass
                last = ProviderError(self._error_text(e.code, detail), e.code)
                if e.code not in (408, 429, 500, 502, 503, 529) or attempt == RETRIES:
                    raise last from None
                wait = e.headers.get("retry-after") if e.headers else None
                time.sleep(min(float(wait), 10) if wait and wait.replace(".", "", 1).isdigit() else 2 ** attempt)
            except (urllib.error.URLError, socket.timeout, OSError) as e:
                last = ProviderError(f"Could not reach {self.label}: {getattr(e, 'reason', e)}")
                if attempt == RETRIES:
                    raise last from None
                time.sleep(2 ** attempt)
        raise last

    def _error_text(self, status, detail):
        text = friendly_status(status, self.label)
        if status == 400:
            try:
                msg = (json.loads(detail).get("error") or {}).get("message")
            except (ValueError, AttributeError):
                msg = None
            if msg:
                text = f"{self.label}: {str(msg)[:300]}"
        return text

    # ---- one turn ----------------------------------------------------- #
    def turn(self, session, text, policy, model, tokens):
        self.check(policy)
        box = ToolBox(policy, self.root, tokens, self.id, BACKENDS)
        session.engine_state.setdefault(self.id, {})
        cancel = _Cancel()
        session._proc = cancel
        model = self.resolve_model(model)
        if not model:
            yield ("error", f"{self.label} has no model set. Add one in HQ, Engines.")
            return
        fmt = self.provider.get("format")
        run = self._anthropic_turn if fmt == "anthropic" else self._openai_turn
        try:
            yield from run(session, text, policy, model, box, cancel)
        except ProviderError as e:
            if not cancel.stopped:
                yield ("error", str(e))
        except (OSError, ValueError, http.client.HTTPException) as e:  # ValueError: closed by stop
            if not cancel.stopped:
                yield ("error", f"The connection to {self.label} dropped: {e}")
        finally:
            session._proc = None

    def _done(self, parts, usage, model):
        tokens = sum(int(usage.get(k) or 0) for k in (
            "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens",
            "prompt_tokens", "completion_tokens"))
        cost = usage.get("cost")
        price = PRICES.get(model)
        if cost is None and price:
            cost = ((int(usage.get("input_tokens") or 0) * price[0]
                     + int(usage.get("cache_read_input_tokens") or 0) * price[2]
                     + int(usage.get("cache_creation_input_tokens") or 0) * price[0] * 1.25
                     + int(usage.get("output_tokens") or 0) * price[1]) / 1_000_000)
        return ("done", {"result": "".join(parts), "subtype": "success", "tokens": tokens, "cost": cost})

    # ---- Anthropic Messages ------------------------------------------- #
    def _anthropic_tools(self, box, policy, model):
        tools = [{"name": s["name"], "description": s["description"], "input_schema": s["input_schema"]}
                 for s in box.specs]
        if policy.web:
            # Dynamic filtering needs Sonnet or Opus; Haiku keeps the basic variant.
            kind = "web_search_20260209" if re.search(r"sonnet|opus", model) else "web_search_20250305"
            tools.append({"type": kind, "name": "web_search", "max_uses": 5})
        return tools

    def _anthropic_turn(self, session, text, policy, model, box, cancel):
        started = time.time()
        pending = [{"role": "user", "content": text}]
        self._append(session, pending)
        messages = self._trim(self._repair(self._load(session)))
        tools = self._anthropic_tools(box, policy, model)
        parts, usage, calls = [], {}, 0
        while True:
            if cancel.stopped:
                return
            if time.time() - started > TURN_TIMEOUT:
                yield ("error", f"This turn on {self.label} took too long, so it was stopped.")
                return
            resp = self._open("/v1/messages", {
                "model": model, "max_tokens": MAX_TOKENS, "stream": True, "system": session.rail,
                "messages": messages, **({"tools": tools} if tools else {})}, cancel)
            cancel.resp = resp
            content, stop = [], None
            with resp:
                for ev in _sse(resp):
                    et = ev.get("type")
                    if et == "message_start":
                        for k, v in ((ev.get("message") or {}).get("usage") or {}).items():
                            if isinstance(v, int) and k != "output_tokens":  # message_delta has the real count
                                usage[k] = usage.get(k, 0) + v
                    elif et == "content_block_start":
                        block = dict(ev.get("content_block") or {})
                        content.append(block)
                        if block.get("type") in ("tool_use", "server_tool_use"):
                            block["_json"] = ""
                            yield ("tool", block.get("name", ""))
                    elif et == "content_block_delta" and content:
                        d = ev.get("delta") or {}
                        block, dt = content[-1], d.get("type")
                        if dt == "text_delta":
                            block["text"] = block.get("text", "") + d.get("text", "")
                            parts.append(d.get("text", ""))
                            yield ("delta", d.get("text", ""))
                        elif dt == "input_json_delta":
                            block["_json"] = block.get("_json", "") + d.get("partial_json", "")
                        elif dt == "thinking_delta":
                            block["thinking"] = block.get("thinking", "") + d.get("thinking", "")
                        elif dt == "signature_delta":
                            block["signature"] = block.get("signature", "") + d.get("signature", "")
                        elif dt == "citations_delta":
                            block.setdefault("citations", []).append(d.get("citation"))
                    elif et == "message_delta":
                        stop = (ev.get("delta") or {}).get("stop_reason") or stop
                        out = (ev.get("usage") or {}).get("output_tokens")
                        if isinstance(out, int):
                            usage["output_tokens"] = usage.get("output_tokens", 0) + out
                    elif et == "error":
                        raise ProviderError(f"{self.label}: {(ev.get('error') or {}).get('message', 'stream error')}")
            if cancel.stopped:
                return
            if stop is None:  # the stream ended without saying why: don't pass it off as the answer
                raise ProviderError(f"The connection to {self.label} dropped.")
            bad_input = {}
            for i, block in enumerate(content):
                raw = block.pop("_json", None)
                if raw is not None:
                    try:
                        block["input"] = json.loads(raw) if raw else {}
                    except ValueError:  # cut off mid-way (max_tokens): never run it
                        block["input"] = {}
                        bad_input[i] = True
            assistant = {"role": "assistant", "content": content}
            messages.append(assistant)
            self._append(session, [assistant])
            if stop == "refusal":
                yield ("delta", "\n\n(The model declined to answer this.)")
                break
            if stop == "pause_turn":
                continue
            if stop != "tool_use":
                break
            results = []
            for i, block in enumerate(content):
                if block.get("type") != "tool_use":
                    continue
                calls += 1
                if bad_input.get(i):
                    out, failed = "That call was cut off. Send it again, shorter.", True
                elif calls > MAX_TOOL_CALLS:
                    out, failed = "Too many tool calls this turn. Answer with what you have.", True
                else:
                    out, failed = box.run(block.get("name"), block.get("input"))
                results.append({"type": "tool_result", "tool_use_id": block.get("id"), "content": out or "(empty)",
                                **({"is_error": True} if failed else {})})
            user = {"role": "user", "content": results}
            messages.append(user)
            self._append(session, [user])
            if calls > MAX_TOOL_CALLS + 1:
                break
        yield self._done(parts, usage, model)

    # ---- OpenAI-compatible Chat Completions ---------------------------- #
    def _openai_tools(self, box):
        return [{"type": "function", "function": {"name": s["name"], "description": s["description"],
                                                  "parameters": s["input_schema"]}} for s in box.specs]

    def _openai_turn(self, session, text, policy, model, box, cancel):
        started = time.time()
        pending = [{"role": "user", "content": text}]
        self._append(session, pending)
        messages = self._trim(self._repair(self._load(session)))
        tools = self._openai_tools(box)
        parts, usage, calls = [], {}, 0
        while True:
            if cancel.stopped:
                return
            if time.time() - started > TURN_TIMEOUT:
                yield ("error", f"This turn on {self.label} took too long, so it was stopped.")
                return
            resp = self._open("/chat/completions", {
                "model": model, "stream": True, "stream_options": {"include_usage": True},
                "messages": [{"role": "system", "content": session.rail}] + messages,
                **({"tools": tools} if tools else {})}, cancel)
            cancel.resp = resp
            reply, tool_calls, finish = [], {}, None
            with resp:
                for ev in _sse(resp):
                    if isinstance(ev.get("error"), dict):
                        raise ProviderError(f"{self.label}: {ev['error'].get('message', 'stream error')}")
                    u = ev.get("usage")
                    if isinstance(u, dict):
                        for k in ("prompt_tokens", "completion_tokens"):
                            usage[k] = usage.get(k, 0) + int(u.get(k) or 0)
                        if u.get("cost") is not None:
                            usage["cost"] = (usage.get("cost") or 0) + float(u["cost"])
                    for choice in ev.get("choices") or []:
                        d = choice.get("delta") or {}
                        if d.get("content"):
                            reply.append(d["content"])
                            parts.append(d["content"])
                            yield ("delta", d["content"])
                        for call in d.get("tool_calls") or []:
                            slot = tool_calls.setdefault(call.get("index", 0), {"id": "", "name": "", "args": ""})
                            slot["id"] = call.get("id") or slot["id"]
                            fn = call.get("function") or {}
                            if fn.get("name"):
                                slot["name"] = fn["name"]
                                yield ("tool", fn["name"])
                            slot["args"] += fn.get("arguments") or ""
                        finish = choice.get("finish_reason") or finish
            if cancel.stopped:
                return
            assistant = {"role": "assistant", "content": "".join(reply) or None}
            if tool_calls:
                assistant["tool_calls"] = [
                    {"id": c["id"] or f"call_{i}", "type": "function",
                     "function": {"name": c["name"], "arguments": c["args"] or "{}"}}
                    for i, c in sorted(tool_calls.items())]
            messages.append(assistant)
            self._append(session, [assistant])
            if not tool_calls or finish == "length":
                break
            results = []
            for i, c in sorted(tool_calls.items()):
                calls += 1
                try:
                    args = json.loads(c["args"]) if c["args"] else {}
                except ValueError:
                    args = None
                if args is None:
                    out, failed = "That call's arguments were not valid JSON. Send it again.", True
                elif calls > MAX_TOOL_CALLS:
                    out, failed = "Too many tool calls this turn. Answer with what you have.", True
                else:
                    out, failed = box.run(c["name"], args)
                results.append({"role": "tool", "tool_call_id": c["id"] or f"call_{i}",
                                "content": ("Error: " if failed else "") + (out or "(empty)")})
            messages.extend(results)
            self._append(session, results)
            if calls > MAX_TOOL_CALLS + 1:
                break
        yield self._done(parts, usage, model)

    # ---- one-shot jobs ------------------------------------------------- #
    def complete(self, prompt, stdin_text, model):
        """Zero tools, nothing stored: generate.py jobs. Returns the reply text."""
        model = self.resolve_model(model)
        if not model:
            raise ProviderError(f"{self.label} has no model set")
        content = f"{stdin_text}" if stdin_text else "Begin."
        if self.provider.get("format") == "anthropic":
            payload = {"model": model, "max_tokens": MAX_TOKENS, "stream": True, "system": prompt,
                       "messages": [{"role": "user", "content": content}]}
            out = []
            with self._open("/v1/messages", payload) as resp:
                for ev in _sse(resp):
                    if ev.get("type") == "content_block_delta":
                        d = ev.get("delta") or {}
                        if d.get("type") == "text_delta":
                            out.append(d.get("text", ""))
                    elif ev.get("type") == "error":
                        raise ProviderError(str((ev.get("error") or {}).get("message", "stream error")))
            return "".join(out)
        payload = {"model": model, "stream": True,
                   "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": content}]}
        out = []
        with self._open("/chat/completions", payload) as resp:
            for ev in _sse(resp):
                for choice in ev.get("choices") or []:
                    text = (choice.get("delta") or {}).get("content")
                    if text:
                        out.append(text)
        return "".join(out)

    def status(self):
        p = self.provider
        return {"id": self.id, "label": self.label, "kind": self.kind, "installed": True,
                "ok": bool(p.get("key") or p.get("no_key")),
                "detail": f"key ending {p['key'][-4:]}" if p.get("key") else "no key needed"}

    def test(self):
        """One tiny request, for the Test button. Returns an error string or None."""
        try:
            return None if self.complete("Reply with: ok", "ping", ESCALATED_MODEL) is not None else "no reply"
        except ProviderError as e:
            return str(e)
