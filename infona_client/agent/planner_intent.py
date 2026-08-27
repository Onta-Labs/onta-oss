"""Intent maps + deterministic routing guards for the agent planner.

Implementation sibling of :mod:`infona_client.agent.planner`. Public names
are re-exported from that facade. Web-ingest discover is hosted-only —
this module must not import a web-ingest capability.
"""
from __future__ import annotations

import re

from infona_client.web_sources.url_extract import extract_urls


_INTENT_TO_CAPABILITY = {
    "enrich": "enrich",
    "clean": "normalize",
    "dedup": "dedup",  # registered (DedupCapability) → plans an ER rebuild
    "ontology": "ontology",  # registered (OntologyCapability) → inspect/declare
    "ingest": "ingest_steward",  # premium CSV-file interview; OSS does not register it
    "discover": "web_ingest",  # premium capability; OSS does not register it
    "research": "web_research",  # registered (WebResearchCapability) → cited answer/artifact, no KG write
    "subscribe": "subscribe",  # registered (SubscribeCapability) → recurring notify schedule
}

# Honest OSS answer when the user asks to mint records from the web. The
# capability lives in hosted Infona (`infona.web_ingest`); falling through to
# /ask would look like a query failure instead of a product-boundary message.
_WEB_INGEST_HOSTED_ONLY = (
    "Web discovery ingest (find records on the web and add them "
    "to the graph) is not included in this OSS build. Ingest a "
    "CSV or JSON file, or use hosted Infona."
)

# Honest OSS answer when the user asks the Cloud ingest-steward interview
# (source / column-drop / mapping confirm on a CSV they already have). The
# capability lives in hosted Infona (`infona.ingest_steward`); falling through
# to /ask would look like a query failure. Ungated `infona ingest` still works.
_INGEST_STEWARD_HOSTED_ONLY = (
    "The Cloud ingest steward (interviewed CSV ingest: source, column "
    "keep/drop, mapping confirm) is not included in this OSS build. Use "
    "`infona ingest` for mapping review, or hosted Infona."
)


def _hosted_only_web_ingest_answer() -> dict:
    return {
        "kind": "answer",
        "answer": _WEB_INGEST_HOSTED_ONLY,
        "narrative": "",
    }


def _hosted_only_ingest_steward_answer() -> dict:
    return {
        "kind": "answer",
        "answer": _INGEST_STEWARD_HOSTED_ONLY,
        "narrative": "",
    }

# When the user asks for SEVERAL actions in one breath ("clean the names and
# dedupe"), we plan each capability and compose them into one ordered plan. This
# is the order they run in: cleaning the VALUES first means the dedup/enrich pass
# operates on already-normalized data — the documented clean-before-dedup /
# clean-before-enrich pattern. Lower number = earlier.
_INTENT_PLAN_ORDER = {
    "clean": 0,
    "enrich": 1,
    "dedup": 2,
    "ontology": 3,
    "ingest": 4,  # CSV/file ingest interview (premium steward)
    "discover": 5,
    "research": 6,
    # A subscribe/standing-alert is set up LAST — it schedules a recurring watch
    # over whatever the other actions in the same breath just built/cleaned.
    "subscribe": 7,
}

# Convergence guard (COG-130): once the agent has asked this many clarifying
# questions in a session, the classifier is told to STOP asking and commit. The
# real fix is feeding it the dialogue (below) so it rarely needs to; this caps
# the worst case so the panel can never loop forever on `clarify`.
# (_MAX_CLARIFY_ROUNDS / _PROMPT_HISTORY_TURNS / _EXECUTING_STALE_S live
# with the modules that consume them.)


_CLASSIFY_SYSTEM = """\
You are the intent router for a knowledge-graph data assistant. Read the WHOLE \
conversation (not just the latest message) and classify what the user wants into \
one or MORE of these intents:

- "question": a read-only question about the data (counts, lookups, "how many", \
"which", "list", "show me"). The assistant will answer with SPARQL.
- "enrich": fill in / look up / find missing ATTRIBUTE values for a type from \
external sources ("enrich", "fill in the X", "look up the Y for Z").
- "clean": normalize / clean / split / tidy messy VALUES of a field \
("clean the speaks field", "split the skills", "strip emoji from titles", \
"clean up the names").
- "dedup": find and merge duplicate entities ("remove duplicates", "de-dupe", \
"merge duplicate records").
- "ontology": change the schema / types / attributes / relationships.
- "ingest": load a CSV or file the user ALREADY HAS into the graph, reviewing \
columns and mapping first ("ingest this csv", "load this file", "import the \
contacts export", "ingest the attached spreadsheet"). This is FILE ingest — \
distinct from "discover", which finds records FROM THE WEB. Prefer "ingest" \
when a file/CSV is attached or named; prefer "discover" when the records are \
not in hand and must be fetched from the web.
- "discover": find a NEW set of records FROM THE WEB and ingest them as a new \
dataset ("find a list of X from the web", "pull all Y", "add data about Z from \
the web", "get me <records> and add them"). ALSO route question-phrased requests \
to bring in EXTERNAL records here — "can we ingest <X>", "can we get/pull <X>", \
"do you have <X that some site offers>" — when X is a set of real-world things \
NOT already in this graph (e.g. "open-router's TTS models", "S&P 500 companies"). \
This CREATES new entities that don't exist in the graph yet — distinct from \
"question" (read-only about data ALREADY in the graph), from "enrich" (fills \
attributes on entities that ALREADY exist), and from "ingest" (a CSV/file the \
user already has).
- "research": answer a question using the WEB and return a cited answer plus a \
downloadable table (CSV/JSON), WITHOUT storing anything in the graph ("research \
X and give me a CSV", "what's the <value> of every <thing> on <site>", \
"compare/look up <facts> across the web and make me a table/report"). This \
returns an ANSWER/artifact FROM the web — distinct from "discover" (which INGESTS \
web records as NEW graph entities to keep) and from "question" (read-only about \
data ALREADY in the graph). Prefer "discover" when the user wants the results \
ADDED to the graph; prefer "research" when they want an answer/report/CSV back.
- "subscribe": set up a RECURRING standing alert / scheduled refresh — the user \
wants something to run ON A CADENCE (weekly, daily, …) and NOTIFY / deliver \
automatically when watched values CHANGE, set up ONCE instead of re-run by hand \
("set up a standing weekly alert", "notify my webhook when X changes", "a weekly \
refresh delivered to me automatically", "I don't want to re-run this by hand — I \
want a standing trigger", "subscribe me to changes in …"). This creates a \
recurring trigger — distinct from "question"/"research" (a one-off answer) and \
from "enrich"/"discover" (a one-off data update). The tell is a CADENCE + \
"automatically / on its own / don't want to re-run it".
- "ambiguous": you genuinely cannot tell what is wanted and must ask ONE \
clarifying question.

When the user supplies explicit LINKS to parse (one or more http(s) URLs in the \
message, or attached page links), route by what they want done with those pages: \
filling in attributes on entities that ALREADY exist → "enrich"; bringing in a \
NEW set of records from those pages → "discover".

CRITICAL rules:
- The user may ask for several things at once. "clean up the names and remove \
duplicates" is BOTH "clean" AND "dedup" — return both, do not ask which one.
- USE THE PRIOR TURNS. If you already asked a clarifying question and the user \
answered it (even tersely, e.g. "both", "yes", "just the names"), treat the \
question as ANSWERED and commit — never re-ask the same dimension.
- Only return "ambiguous" when the conversation as a whole still does not say \
what to do. If you can act, act.

You are also given the available capabilities (one line each). Respond with \
STRICT JSON only:
{"intents": ["<one or more of the above>"], "clarify": "<a clarifying question, \
ONLY when the single intent is ambiguous>", "options": ["<2-4 short clickable \
answer choices>"]}

When you ask a clarifying question, ALSO provide "options": a short list (2-4) of \
the distinct answers the user is choosing between, each a few words, phrased as \
the user would say them (e.g. for clean-vs-merge: ["Clean up the values", "Merge \
duplicates", "Both"]). The user can click one instead of typing. Omit "options" \
(or use []) only when the answer is genuinely free-form (a field name, a value) \
and no small set of choices fits."""

# Generic action options offered on a fall-back clarify (greeting, "I can't yet
# handle X", or when the classifier didn't suggest its own). Each maps cleanly to
# an intent when the user clicks it, so the next turn routes straight to a plan.
_DEFAULT_ACTION_OPTIONS = [
    "Ask a question about the data",
    "Add data from the web",
    "Clean up messy values",
    "Enrich missing attributes",
    "Merge duplicate records",
    "Change the schema",
]

# --- deterministic web-discovery guard --------------------------------------- #
# The LLM classifier occasionally mis-files an explicit "… from the web" ingest
# as "question" (its payload usually contains "list"/"show me …") or "ambiguous".
# That strands the Explorer's "Add data from the web" entry point — and the CLI /
# MCP — on a generic clarify the user can't escape. When the phrasing is an
# UNMISTAKABLE imperative web fetch we force the discover intent ourselves rather
# than trusting the LLM. Kept narrow so genuine read-only questions are untouched.
_WEB_FETCH_RE = re.compile(
    r"\b(?:add|pull|fetch|find|get|grab|collect|ingest|import|gather|scrape|discover)\b"
    r"[^?]*\bfrom\s+the\s+web\b",
    re.IGNORECASE,
)
# Widened discovery guard (ONTA-244): a "… from the web" suffix is NOT the only
# unmistakable discovery framing. Two more, kept just as conservative:
#   * A "discover"/"scrape" imperative — those verbs are never a read-only query
#     verb (unlike "find"/"get", which can lead a question), so "discover all
#     orthopedic surgeons in Orange County" is unambiguously a mint-new-records ask
#     even without the literal web suffix. We require the verb to LEAD the message
#     so "how many did we discover" (question) is untouched.
#   * An explicit "new discovery" / "not enrichment" self-label the user adds to
#     disambiguate ("… this is a new discovery task, not enrichment"). Honoring the
#     caller's stated intent is the whole point of ONTA-244.
# Both still defer to the interrogative guard below (trailing '?' / question lead).
_DISCOVER_IMPERATIVE_RE = re.compile(
    r"^\s*(?:please\s+)?(?:discover|scrape)\b",
    re.IGNORECASE,
)
#
# "new discovery" and "discovery task" are POSITIVE discovery self-labels — they
# should force-route wherever they appear, including MID-SENTENCE ("new discovery
# run of Widgets", "kick off a discovery task for Sprockets now"). They match on a
# plain word boundary (\b), no clause-boundary requirement.
#
# "not enrichment" is the one that needs a guard: the word "enrichment" as an
# ADJECTIVE ("not enrichment candidates", "not enrichment targets") in an ordinary
# read-only ask must NOT be read as a routing self-label. So ONLY that branch is
# anchored to a CLAUSE boundary — end-of-string, punctuation, or a conjunction
# ("but"/"so"/"then"/…). "… not enrichment - find Gadgets" (dash = boundary),
# "… not enrichment." and "…, not enrichment" still match; "… not enrichment
# candidates" (a following noun, no boundary) does not.
_CLAUSE_BOUNDARY = r"(?=$|[\s]*[-:;,.]|\s+(?:but|so|then|and|please)\b)"
_EXPLICIT_DISCOVERY_INTENT_RE = re.compile(
    r"\bnew\s+discovery\b|"
    r"\bdiscovery\s+task\b|"
    r"\bnot\s+(?:an?\s+)?enrichment" + _CLAUSE_BOUNDARY,
    re.IGNORECASE,
)
# Read-only framings we must NOT hijack even when they mention the web (e.g.
# "how many companies did we add from the web?"). Includes the read-only display
# imperatives "show me" / "list" / "give me" — a "show me all records that are not
# enrichment candidates" is a read-only ask, not a discovery job, even though it
# lacks a trailing '?'.
_QUESTION_LEAD_RE = re.compile(
    r"^\s*(?:how\s+many|how\s+much|what|which|who|whom|whose|when|where|why|"
    r"do\s+we|did\s+we|does|is\s+there|are\s+there|count|"
    r"show\s+me|list|give\s+me)\b",
    re.IGNORECASE,
)


def _is_web_discovery_request(message: str) -> bool:
    """True when ``message`` is an unmistakable discovery (mint-new-records) request.

    Fires on any of: an explicit "… from the web" fetch, a leading
    "discover"/"scrape" imperative, or a caller-stated "new discovery / not
    enrichment" intent. Conservative on purpose: a trailing '?' or a question-word
    lead disqualifies it, so a real read-only question that merely mentions the web
    (or the word "discover" mid-sentence) is never forced into discovery.
    """
    msg = (message or "").strip()
    if not msg or msg.endswith("?") or _QUESTION_LEAD_RE.match(msg):
        return False
    return bool(
        _WEB_FETCH_RE.search(msg)
        or _DISCOVER_IMPERATIVE_RE.match(msg)
        or _EXPLICIT_DISCOVERY_INTENT_RE.search(msg)
    )


# --- deterministic subscribe / standing-alert guard -------------------------- #
# The classifier can mis-file "set up a standing weekly alert …" as a plain
# question / research (the payload reads like "notify me whenever …"), stranding
# the persona who explicitly wants a RECURRING trigger. When the phrasing is an
# unmistakable "set up a standing / recurring alert on a cadence, delivered
# automatically" ask, force the subscribe intent ourselves. Kept narrow: it
# requires BOTH a recurrence/cadence signal AND an alert/notify/refresh signal, so
# a one-off "notify me the answer" or a bare "what changed this week?" is untouched.
_SUBSCRIBE_CADENCE_RE = re.compile(
    r"\b(standing|recurring|weekly|daily|hourly|monthly|"
    r"every\s+(?:week|day|hour|month|morning|monday)|"
    r"each\s+(?:week|day|hour|month|monday)|on\s+a\s+cadence|"
    r"don'?t\s+want\s+to\s+re-?run|not\s+.*re-?issue|by\s+itself|"
    r"automatically|on\s+its\s+own)\b",
    re.IGNORECASE,
)
_SUBSCRIBE_ALERT_RE = re.compile(
    r"\b(alert|notif(?:y|ication)|standing\s+trigger|subscribe|"
    r"watch\s+for|refresh|monitor)\b",
    re.IGNORECASE,
)


def _is_subscribe_request(message: str) -> bool:
    """True when ``message`` is an unmistakable recurring-standing-alert request.

    Requires a cadence/recurrence signal AND an alert/notify/refresh signal, so a
    one-off "notify me the count" or a read-only "what changed?" never trips it.
    A trailing '?' or question-word lead still disqualifies (a genuine question).
    """
    msg = (message or "").strip()
    if not msg or msg.endswith("?") or _QUESTION_LEAD_RE.match(msg):
        return False
    return bool(
        _SUBSCRIBE_CADENCE_RE.search(msg) and _SUBSCRIBE_ALERT_RE.search(msg)
    )


# --- deterministic refresh-existing / re-verify guard ------------------------ #
# A "refresh / re-verify / update / re-check the <attributes> for <existing
# subset>" ask is ENRICHMENT in re-verify mode (ONTA-245) — re-confirm existing
# values and advance their freshness stamp, scoped to the matching existing
# records. It is NOT a new web discovery. But the LLM classifier keeps mis-filing
# it as "discover" (the goal text reads like "pull current numbers from the web
# for OpenAI, Google, …"), which mints a fresh dataset instead of refreshing the
# rows that already exist — the reported persona-eval gap (ran_enrich=false,
# ran_build=true). When the phrasing is an unmistakable refresh-EXISTING verb we
# force the enrich intent ourselves so ONTA-245's refresh path fires regardless of
# the LLM. Kept narrow: a refresh verb near a "from the web"/"discover" framing
# (genuinely minting new records) still defers to the web-discovery guard below,
# and a recurring-cadence "standing refresh" still defers to subscribe.
_REFRESH_EXISTING_RE = re.compile(
    r"\b(?:re-?verif\w*|re-?check\w*|re-?confirm\w*|re-?validat\w*|"
    r"refresh(?:ed|es|ing)?|update(?:d|s)?|keep\s+(?:it\s+)?current|"
    r"make\s+(?:it\s+|them\s+)?current|freshness|decay(?:ing|s)?)\b",
    re.IGNORECASE,
)


def _is_refresh_existing_request(message: str) -> bool:
    """True when ``message`` unmistakably asks to REFRESH / re-verify EXISTING data.

    Fires on a refresh/re-verify/re-check/update/keep-current verb. Conservative:
    a trailing '?' or question-word lead disqualifies it (a genuine question), and
    the caller only lets it force the enrich rail when the message is NOT already a
    web-discovery framing ("… from the web", a leading "discover") and NOT a
    recurring standing-alert (which routes to subscribe). Deterministic so a scoped
    refresh recovers even when the LLM classifier returns the wrong (discover)
    intent — the whole point of the guard (mirrors the enrich cap's own
    ``_looks_like_refresh`` verb detection, which flips the run to verify mode).
    """
    msg = (message or "").strip()
    if not msg or msg.endswith("?") or _QUESTION_LEAD_RE.match(msg):
        return False
    return bool(_REFRESH_EXISTING_RE.search(msg))


# --- deterministic links-to-parse guard -------------------------------------- #
# When the user hands us explicit URLs (in the message, or attached as structured
# request context) the turn is an URL-targeted web extraction, NOT a plain
# question — even though the payload often reads like one ("get the prices from
# https://…"). We route it ourselves so it can't be mis-filed by the classifier:
# an enrich-type verb (fill attributes on entities that ALREADY exist) → Rail B
# ("enrich"); anything else (bring in a NEW set of records) → Rail A
# ("discover"). The actual fetching lives behind the premium URL-targeted seam;
# capabilities read the URLs themselves (ctx.urls or extract_urls(instruction)).
_ENRICH_VERB_RE = re.compile(
    r"\b(?:enrich|fill|update|complete|populate)\b", re.IGNORECASE
)


def _message_has_urls(message: str, ctx_urls: list[str] | None) -> bool:
    """True when this turn carries explicit URLs — in the message or the ctx."""
    return bool(ctx_urls) or bool(extract_urls(message))


def _url_intent(message: str) -> str:
    """Route a URL-bearing turn: 'enrich' on an enrich-type verb, else 'discover'.

    Enrich-type verbs (enrich/fill/update/complete/populate) mean "fill in
    attributes on entities that ALREADY exist" (Rail B). Everything else —
    add/ingest/import/pull/parse/scrape/extract/collect a NEW set — is Rail A
    (new entities), so it defaults to discovery.
    """
    return "enrich" if _ENRICH_VERB_RE.search(message or "") else "discover"


def _is_interrogative(message: str) -> bool:
    """True when ``message`` reads as a read-only question — a trailing '?' or a
    leading question word. Mirrors the web-discovery guard so a genuine question
    that merely contains a link is answered, not hijacked into an action."""
    msg = (message or "").strip()
    return bool(msg) and (msg.endswith("?") or bool(_QUESTION_LEAD_RE.match(msg)))

