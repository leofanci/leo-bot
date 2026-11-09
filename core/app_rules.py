"""app_rules.py — what a bot's Composio tool call may do (connected apps).

A call is read (runs now), hold (waits on an Approve click), or refuse. A read
is narrow on purpose: the first action word is a read verb and no word is a
write verb, or the slug is a known noun-first read. Everything else is a write.
"""
import re

# "Reads" here reach arbitrary hosts or run code, so an injected email could
# make a bot "read" attacker.example/?d=<mail> with no card. Never grantable.
BLOCKED_TOOLKITS = frozenset({
    "composio_search", "firecrawl", "tavily", "exa", "serpapi", "perplexityai",
    "browserbase_tool", "browser_tool", "codeinterpreter", "composio",
    "googlesuper",  # bundles every Google app: would bypass per-app grants
})
# Reads that spend money (enrichment credits) wait on approval like writes.
PAID_READ_TOOLKITS = frozenset({"apollo"})

READ_VERBS = frozenset("GET LIST FETCH SEARCH READ FIND RETRIEVE LOOKUP VIEW CHECK COUNT".split())
WRITE_VERBS = frozenset((
    "SEND REPLY FORWARD CREATE ADD INSERT UPDATE PATCH MODIFY EDIT REPLACE MOVE COPY "
    "DELETE REMOVE TRASH SHARE INVITE ACCEPT DECLINE SET APPLY MARK ARCHIVE UPLOAD "
    # Mutating or call-out verbs that still can follow a read word. Common nouns (post, call,
    # start, schedule, request, export) stay out: "GET_POST" and "LIST_SCHEDULE" are reads.
    "EXECUTE INVOKE CLICK NAVIGATE SCRAPE CRAWL PUBLISH SUBMIT CANCEL DISABLE ENABLE IMPORT RESET "
    "REVOKE AUTHORIZE SUBSCRIBE UNSUBSCRIBE RENAME REOPEN").split())

# Noun-first or verb-without-read-verb names that only read.
KNOWN_READS = frozenset({
    "GMAIL_FETCH_EMAILS", "GMAIL_GET_PROFILE",
    "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID", "GMAIL_FETCH_MESSAGE_BY_THREAD_ID",
    "GOOGLECALENDAR_EVENTS_LIST", "GOOGLECALENDAR_EVENTS_GET", "GOOGLECALENDAR_LIST_CALENDARS",
    # Google Maps lookups (exact slugs pinned from the live catalog).
    "GOOGLE_MAPS_AUTOCOMPLETE", "GOOGLE_MAPS_GEOCODE_ADDRESS_WITH_QUERY",
    "GOOGLE_MAPS_GEOCODE_DESTINATIONS", "GOOGLE_MAPS_GEOCODE_PLACE",
    "GOOGLE_MAPS_GEOCODING_API", "GOOGLE_MAPS_REVERSE_GEOCODE_LOCATION",
    "GOOGLE_MAPS_NEARBY_SEARCH", "GOOGLE_MAPS_TEXT_SEARCH",
    "GOOGLE_MAPS_GET_PLACE_DETAILS", "GOOGLE_MAPS_PLACE_PHOTO",
    "GOOGLE_MAPS_GET_ROUTE", "GOOGLE_MAPS_COMPUTE_ROUTE_MATRIX",
    "GOOGLE_MAPS_GET_TIME_ZONE", "GOOGLE_MAPS_GET_2D_MAP_TILE",
    "GOOGLE_MAPS_GET_3D_TILES_ROOT", "GOOGLE_MAPS_EMBED_GOOGLE_MAP",
    "GOOGLE_MAPS_LOOKUP_AERIAL_VIDEO", "GOOGLE_MAPS_RENDER_AERIAL_VIDEO",
    "GOOGLE_MAPS_GEOLOCATE_DEVICE",
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
    return bool(words) and words[0] in READ_VERBS and not WRITE_VERBS.intersection(words)


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
_URLISH = re.compile(r"(?:[a-z][a-z0-9+.-]*://|\bwww\.|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|net|org|io|dev|ai|xyz|ru|cn|example|test|info|top|site|online)\b)", re.I)


def has_url(value, _depth=0) -> bool:
    """True when any string in the arguments looks like a web address: a "read" that takes a
    URL can make the app fetch an attacker's host with the mail in the query."""
    if _depth > 6:
        return True
    if isinstance(value, str):
        return bool(_URLISH.search(_EMAIL.sub(" ", value)))  # an address to search for is not a link
    if isinstance(value, dict):
        return any(has_url(k, _depth + 1) or has_url(v, _depth + 1) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(has_url(v, _depth + 1) for v in value)
    return False


def decide(slug, grant, catalog, arguments=None):
    """("read" | "hold" | "refuse", toolkit) for one app tool call under a bot's grant."""
    tk = toolkit_of(slug, catalog)
    if tk is None:
        return "refuse", None
    if tk in BLOCKED_TOOLKITS or grant.get(tk) not in ("read", "write"):
        return "refuse", tk
    if is_read(slug, tk) and tk not in PAID_READ_TOOLKITS and not has_url(arguments):
        return "read", tk
    return ("hold" if grant[tk] == "write" else "refuse"), tk
