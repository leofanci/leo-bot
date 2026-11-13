"""app_rules.py — what a bot's Composio tool call may do (connected apps).

A call is read (runs now), hold (waits on an Approve click), or refuse. A read
is narrow on purpose, and two checks must agree: Composio tags the tool
readOnlyHint (an allowlist from its own metadata), and the name reads (the first
action word is a read verb and no word is a write verb, or the slug is a known
noun-first read). Neither is enough alone: Composio tags some writes read-only
(setting a user active, managing secrets). An untagged or unknown tool is a write.
"""
import re

# "Reads" here reach arbitrary hosts or run code, so an injected email could
# make a bot "read" attacker.example/?d=<mail> with no card. Never grantable.
BLOCKED_TOOLKITS = frozenset({
    "composio_search", "firecrawl", "tavily", "exa", "serpapi", "perplexityai",
    "browserbase_tool", "browser_tool", "codeinterpreter", "composio",
    "googlesuper",  # bundles every Google app: would bypass per-app grants
})
# Reads that spend money (enrichment or model credits) or fetch arbitrary pages wait on
# approval like writes: these toolkits, and any toolkit Composio files under one of
# PAID_READ_CATEGORIES (composio.paid_read_toolkits), so a new app of that kind is covered too.
PAID_READ_TOOLKITS = frozenset({"apollo", "hunter", "peopledatalabs", "lusha", "zoominfo", "clay",
                                "openai", "anthropic", "apify", "scrapingbee"})
PAID_READ_CATEGORIES = frozenset({
    "people & contact data", "ai sales tools", "ai web scraping", "web scraping & browser",
    "web search & research", "ai models", "ai content generation", "ai chatbots", "seo & ai visibility",
})

READ_VERBS = frozenset("GET LIST FETCH SEARCH READ FIND RETRIEVE LOOKUP VIEW CHECK COUNT".split())
WRITE_VERBS = frozenset((
    "SEND REPLY FORWARD CREATE ADD INSERT UPDATE PATCH MODIFY EDIT REPLACE MOVE COPY "
    "DELETE REMOVE TRASH SHARE INVITE ACCEPT DECLINE SET APPLY MARK ARCHIVE UPLOAD "
    "EXECUTE INVOKE CLICK NAVIGATE SCRAPE CRAWL PUBLISH SUBMIT CANCEL DISABLE ENABLE IMPORT RESET "
    "GENERATE ENRICH VERIFY VALIDATE TEST PING WEBHOOK EMIT FIRE DISPATCH BROADCAST NOTIFY POKE "
    "REVOKE AUTHORIZE SUBSCRIBE UNSUBSCRIBE RENAME REOPEN APPROVE REJECT ASSIGN TRANSFER").split())
# Words that are a verb OR the thing a read is about ("GET_POST", "LIST_SCHEDULE", "GET_CALL"). They
# count as a write only when a joiner puts them in verb position ("SEARCH_AND_POST").
NOUNISH_VERBS = frozenset("POST PUT CALL REQUEST SCHEDULE EXPORT START STOP PIN SYNC CLOSE DOWNLOAD RUN TRIGGER CONNECT".split())
JOINERS = frozenset(("AND", "THEN", "OR"))

# Noun-first or verb-without-read-verb names that only read.
KNOWN_READS = frozenset({
    "GMAIL_FETCH_EMAILS", "GMAIL_GET_PROFILE",
    "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID", "GMAIL_FETCH_MESSAGE_BY_THREAD_ID",
    "GMAIL_LIST_SEND_AS", "GMAIL_SETTINGS_SEND_AS_GET", "GMAIL_SETTINGS_GET_IMAP", "GMAIL_SETTINGS_GET_POP",
    "GOOGLECALENDAR_EVENTS_LIST", "GOOGLECALENDAR_EVENTS_GET", "GOOGLECALENDAR_LIST_CALENDARS",
    "GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS", "GOOGLECALENDAR_EVENTS_INSTANCES",
    "GOOGLECALENDAR_FREE_BUSY_QUERY", "GOOGLECALENDAR_CALENDAR_LIST_GET",
    "GOOGLECALENDAR_ACL_GET", "GOOGLECALENDAR_ACL_LIST", "GOOGLECALENDAR_COLORS_GET",
    "GOOGLECALENDAR_SETTINGS_GET", "GOOGLECALENDAR_SETTINGS_LIST",
    # Google Maps lookups (exact slugs pinned from the live catalog, 2026-11-09).
    "GOOGLE_MAPS_AUTOCOMPLETE", "GOOGLE_MAPS_GEOCODE_ADDRESS_WITH_QUERY",
    "GOOGLE_MAPS_GEOCODE_DESTINATIONS", "GOOGLE_MAPS_GEOCODE_PLACE", "GOOGLE_MAPS_GEOCODE_ADDRESS",
    "GOOGLE_MAPS_GEOCODING_API", "GOOGLE_MAPS_GEOCODE_LOCATION",
    "GOOGLE_MAPS_NEARBY_SEARCH", "GOOGLE_MAPS_TEXT_SEARCH",
    "GOOGLE_MAPS_GET_PLACE_DETAILS", "GOOGLE_MAPS_PLACE_PHOTO",
    "GOOGLE_MAPS_GET_ROUTE", "GOOGLE_MAPS_COMPUTE_ROUTE_MATRIX",
    "GOOGLE_MAPS_GET_DIRECTION", "GOOGLE_MAPS_DISTANCE_MATRIX_API",
    "GOOGLE_MAPS_GET_TIME_ZONE", "GOOGLE_MAPS_GET2D_TILE",
    "GOOGLE_MAPS_GET3D_TILES_ROOT", "GOOGLE_MAPS_MAPS_EMBED_API",
    "GOOGLE_MAPS_LOOKUP_AERIAL_VIDEO", "GOOGLE_MAPS_RENDER_AERIAL_VIDEO",
    "GOOGLE_MAPS_GEOLOCATE",
})

# The only Composio meta tools a bot sees. Connection management, the
# workbench, bash and raw proxy calls stay hidden (dashboard-only or opaque).
META_EXPOSED = ("COMPOSIO_SEARCH_TOOLS", "COMPOSIO_GET_TOOL_SCHEMAS", "COMPOSIO_MULTI_EXECUTE_TOOL")
MULTI_EXECUTE = "COMPOSIO_MULTI_EXECUTE_TOOL"
MAX_CALLS = 50


def toolkit_of(slug, catalog):
    """Longest catalog toolkit whose prefix the slug carries (slugs may hold underscores)."""
    best = None
    for tk in catalog:
        if slug.startswith(tk.upper() + "_") and (best is None or len(tk) > len(best)):
            best = tk
    return best


def is_read(slug, toolkit):
    if slug in KNOWN_READS:
        return True
    words = [w for w in re.split(r"[^A-Z0-9]+", slug[len(toolkit) + 1:].upper()) if w]
    if not words or words[0] not in READ_VERBS or WRITE_VERBS.intersection(words):
        return False
    return not any(w in NOUNISH_VERBS and words[i - 1] in JOINERS for i, w in enumerate(words) if i)


def calls_in(name, args):
    """The app tool calls inside a multi-execute, or None when it cannot be read whole."""
    if name != MULTI_EXECUTE or not isinstance(args, dict):
        return None
    tools = args.get("tools")
    if not isinstance(tools, list) or not 1 <= len(tools) <= MAX_CALLS:
        return None
    out = []
    for item in tools:
        slug = item.get("tool_slug") if isinstance(item, dict) else None
        if not isinstance(slug, str) or not slug:
            return None
        account = item.get("account")
        if account is not None and not isinstance(account, str):
            return None
        out.append({"tool_slug": slug, "arguments": item.get("arguments") or {},
                    **({"account": account} if account else {})})
    return out


_EMAIL = re.compile(r"[^\s@/]+@[^\s@/]+")
_HOSTISH = re.compile(r"(?:[a-z][a-z0-9+.-]*://|\bwww\.|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.([a-z]{2,24})\b)", re.I)
# A final label that is a file type, not a top-level domain ("notes.txt", "report.pdf").
_FILE_TYPES = frozenset("txt md pdf png jpg jpeg gif svg csv tsv json xml html htm doc docx xls xlsx ppt pptx zip gz "
                        "tar py js ts tsx jsx css log yml yaml toml ini cfg rtf eml ics".split())
# Mail and calendar apps take addresses as search terms; they do not contact the address.
ADDRESS_SAFE_TOOLKITS = frozenset({"gmail", "outlook", "googlecalendar"})


def has_url(value, strip_emails=False, _depth=0) -> bool:
    """True when any string in the arguments looks like a web address: a "read" that takes a
    URL can make the app fetch an attacker's host with the mail in the query. `strip_emails`
    drops whole addresses first (mail apps only; elsewhere an address's domain is checked)."""
    if _depth > 6:
        return True
    if isinstance(value, str):
        text = _EMAIL.sub(" ", value) if strip_emails else value
        return any(m.group(1) is None or m.group(1).lower() not in _FILE_TYPES for m in _HOSTISH.finditer(text))
    if isinstance(value, dict):
        return any(has_url(k, strip_emails, _depth + 1) or has_url(v, strip_emails, _depth + 1) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(has_url(v, strip_emails, _depth + 1) for v in value)
    return False


MAX_READ_ARG_CHARS = 2000  # a read takes a search term or an id, not a document


def _size(value, _depth=0):
    if _depth > 6:
        return MAX_READ_ARG_CHARS + 1
    if isinstance(value, str):
        return len(value)
    if isinstance(value, dict):
        return sum(len(str(k)) + _size(v, _depth + 1) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return sum(_size(v, _depth + 1) for v in value)
    return 0


def decide(slug, grant, catalog, arguments=None, read_only=None, paid=frozenset()):
    """("read" | "hold" | "refuse", toolkit) for one app tool call under a bot's grant.
    `read_only(slug)` says whether Composio tags the tool readOnlyHint (True, False, or None
    when unknown); asked only when everything else allows a read. Without it, nothing reads.
    `paid`: more toolkits whose reads wait like writes (from Composio's categories)."""
    # Blocked toolkits stay in the attribution set (the catalog drops them): a slug of a blocked
    # toolkit is pinned to it and refused, never to a shorter allowed prefix.
    tk = toolkit_of(slug, set(catalog) | BLOCKED_TOOLKITS)
    if tk is None:
        return "refuse", None
    if tk in BLOCKED_TOOLKITS or grant.get(tk) not in ("read", "write"):
        return "refuse", tk
    if is_read(slug, tk) and tk not in PAID_READ_TOOLKITS and tk not in paid \
            and not has_url(arguments, tk in ADDRESS_SAFE_TOOLKITS) \
            and _size(arguments) <= MAX_READ_ARG_CHARS and read_only is not None and read_only(slug) is True:
        return "read", tk
    return ("hold" if grant[tk] == "write" else "refuse"), tk


_SLUG = re.compile(r"[A-Z0-9][A-Z0-9_]{0,127}")


def metric_problem(app, action, catalog, args=None, read_only=None, paid=frozenset()):
    """Why `action` of `app` cannot be an experiment's metric source, or None when it can.
    Same rules as a bot's own read (decide): the metric job reads without a click, so it
    holds to the exact same bar. `read_only(slug)` is Composio's readOnlyHint tag."""
    if not isinstance(action, str) or not _SLUG.fullmatch(action):
        return "action must be a Composio tool slug like APP_GET_SOMETHING"
    tk = toolkit_of(action, set(catalog) | BLOCKED_TOOLKITS)
    if tk is None:
        return f"{action} belongs to no known app"
    if tk != app:
        return f"{action} belongs to {tk}, not {app}"
    if tk in BLOCKED_TOOLKITS:
        return f"{tk} can never be a metric source"
    if tk in PAID_READ_TOOLKITS or tk in paid:
        return f"{tk} reads spend credits or fetch pages: they wait for approval, so they cannot run on a schedule"
    if not is_read(action, tk):
        return f"{action} does not read: only read actions (GET, LIST, FETCH, ...) can be a metric source"
    if has_url(args, tk in ADDRESS_SAFE_TOOLKITS):
        return "read arguments may not hold a URL or address"
    if _size(args) > MAX_READ_ARG_CHARS:
        return f"read arguments over {MAX_READ_ARG_CHARS} characters"
    if read_only is None:
        return "cannot check Composio's read-only tag"
    tagged = read_only(action)
    if tagged is None:
        return f"cannot confirm Composio tags {action} read-only"
    if tagged is not True:
        return f"Composio does not tag {action} read-only"
    return None
