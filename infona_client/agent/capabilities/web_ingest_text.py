"""Text / field-list parsers for web-discovery planning.

Deterministic floor for user-named types and fields (no LLM). Used by
``plan`` so an explicit field list is never silently dropped.
"""
from __future__ import annotations

import json
import re

from infona_client.agent.registry import PlanStep
from infona_client.agent.capabilities import web_ingest_cap as _wic

_LEAD_FILLER = re.compile(
    r"^\s*(?:i['’]?m\s+looking\s+for|i\s+want|i\s+need|please\s+|can\s+you\s+|"
    r"could\s+you\s+|find\s+me|find|get\s+me|get|pull|fetch|add|search\s+for)\s+"
    r"(?:a\s+|an\s+|the\s+|me\s+)?",
    re.IGNORECASE,
)


# A leading META-FRAMING clause the user prepends to steer routing rather than to
# name the search subject — "this is a new discovery task, not enrichment — …",
# "note: not enrichment, …". Left in the query it leaks into the search string
# (persona-eval RCA: the executed job searched for "This is a new discovery task,
# not enrichment…"). We strip such a clause up to its trailing separator (dash /
# colon / semicolon / comma) so the REAL subject after it survives. Conservative:
# only fires on an explicit discovery/enrichment self-label, so a normal query is
# untouched. Case-insensitive.
_META_FRAMING_RE = re.compile(
    r"^\s*(?:note[:,]?\s*)?(?:this\s+is\s+)?(?:a\s+)?"
    r"(?:new\s+discovery(?:\s+task)?|not\s+(?:an?\s+)?enrichment"
    r"|discovery\s+task)\b[^-:;.]*[-:;,]\s*",
    re.IGNORECASE,
)

def _clean_query(instruction: str) -> str:
    """Best-effort tidy of the instruction into a discovery query. Uses the FIRST
    line (the original ask), strips a leading routing META-FRAME ("this is a new
    discovery task, not enrichment — …") if present, then one leading filler phrase,
    so the executed query is the SUBJECT, never the user's meta-correction."""
    if not instruction:
        return ""
    first = next(
        (ln.strip() for ln in instruction.splitlines() if ln.strip()),
        instruction.strip(),
    )
    # Drop a leading discover-vs-enrich self-label so it never becomes the search
    # string; keep looping in case the user stacked two (rare).
    stripped = first
    for _ in range(2):
        nxt = _META_FRAMING_RE.sub("", stripped, count=1).strip()
        if nxt == stripped:
            break
        stripped = nxt
    q = _LEAD_FILLER.sub("", stripped, count=1).strip()
    return q or stripped or first


def _current_request(instruction: str) -> str:
    """The user's CURRENT turn within the accumulated instruction.

    The planner concatenates the session's user turns oldest-first with newlines
    (``_effective_instruction``), so the ask in front of us is the LAST non-empty
    line. Weighting it keeps a STALE earlier turn from overriding the fields / type /
    search subject the current message names. Collapses to the whole instruction when
    there is only one turn (no newline)."""
    if not instruction:
        return ""
    lines = [ln for ln in instruction.splitlines() if ln.strip()]
    return lines[-1].strip() if lines else instruction.strip()


def _answer_step(text: str) -> PlanStep:
    """A single no-write 'answer' step (planner short-circuits it to kind:answer)."""
    return PlanStep(
        capability=_wic.WebIngestCapability.name,
        action="answer",
        params={"answer_payload": {"answer": text, "narrative": text}},
        rationale=text,
        confidence=1.0,
    )


def _parse_json_object(text: str) -> dict | None:
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        stripped = "\n".join(
            l for l in stripped.split("\n") if not l.strip().startswith("```")
        )
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        stripped = stripped[start : end + 1]
    try:
        data = json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _as_list(v) -> list[str]:
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        return [str(x) for x in v]
    return []


def _slug(v) -> str:
    """snake_case a single attribute name; drop surrounding junk."""
    s = re.sub(r"[^0-9a-zA-Z]+", "_", str(v or "").strip().lower()).strip("_")
    return s


def _pascal(v: str) -> str:
    parts = re.split(r"[^0-9a-zA-Z]+", str(v or "").strip())
    return "".join(p[:1].upper() + p[1:] for p in parts if p) or "WebRecord"


# The user's explicitly-named record TYPE, introduced by a discovery verb + the
# "records"/"entities" noun ("add Widget records", "discover Sprocket entities")
# or a "<Type> with <fields>" list frame ("Gadget records with sku, color").
# Deliberately CONSERVATIVE (high precision): the type token is 1-3 capitalized /
# identifier words captured immediately before "records"/"entities" or before a
# field-list "with". This only ever OVERRIDES the WebRecord placeholder, so a false
# negative is harmless (we keep WebRecord and clarify as today) and a false
# positive is bounded — it can't corrupt a real LLM-resolved type. Case-sensitive
# on the leading capital so a lowercased entity phrase ("collect the physicians")
# is NOT mistaken for a type name. Never overfit to a specific domain term.
# Words that lead a discovery ask but are NOT the type — excluded from the type
# capture so "Add Widget records" yields "Widget", never "AddWidget". The type
# token immediately precedes "records"/"entities"/"rows" (or the "<Type> with"
# field frame) and is 1-3 Capitalized words, none of them a lead verb / article.
_TYPE_STOPWORDS = frozenset(
    {
        "add", "discover", "find", "pull", "fetch", "get", "grab", "collect",
        "ingest", "import", "gather", "scrape", "the", "a", "an", "all", "these",
        "some", "more", "new",
    }
)
_TYPE_TOKEN = r"[A-Z][A-Za-z0-9]*(?:[ _-][A-Z][A-Za-z0-9]*){0,2}"
_EXPLICIT_TYPE_RE = re.compile(
    rf"\b(?:add|discover|find|pull|fetch|get|grab|collect|ingest|import|gather|scrape)\b"
    rf"[^.\n]*?\b({_TYPE_TOKEN})\s+(?:records?|entities|rows)\b",
)
_TYPE_WITH_FIELDS_RE = re.compile(
    rf"\b({_TYPE_TOKEN})\s+(?:records?\s+)?with\b",
)

# The "each <noun> record …" frame — a caller describing the SHAPE of the dataset
# ("each **model** record needs …", "every **product** entity should have …")
# names the record type explicitly even when the noun is lowercase, so the
# capitalized-only frames above miss it (the persona-eval RCA: the LLM degraded to
# WebRecord and "each model record needs …" left it there). "each"/"every" + a
# single common-noun word + "record"/"entity"/"row" is a strong, unambiguous "this
# is the per-record type" signal — far tighter than a bare entity phrase, so it
# won't fire on "collect the coffee shops". The noun is a single ``[a-z]`` word
# (an adjective before it, "each voice model record", is dropped — we take the word
# ADJACENT to the record noun); a stopword there ("each one record") is rejected by
# the caller's stopword filter. We singularize a trailing plural and PascalCase it.
_EACH_RECORD_TYPE_RE = re.compile(
    r"\b(?:each|every|per|a)\s+([a-z][a-z0-9]*)\s+(?:records?|entities|entity|rows?)\b",
    re.IGNORECASE,
)


def _singularize(word: str) -> str:
    """Best-effort English singularization for a type noun: "companies" → "company",
    "boxes" → "box", "models" → "model". Conservative — only the common regular
    plural endings, so a non-plural noun ("data", "series") is left unchanged rather
    than mangled. Not a full inflector; good enough to name a type."""
    w = word
    if len(w) > 3 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith(("ses", "xes", "zes", "ches", "shes")):
        return w[:-2]
    if len(w) > 2 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def _strip_type_stopwords(cand: str) -> str:
    """Drop leading lead-verb / article words from a captured type phrase so
    "Add Widget" → "Widget" and "the SolarPanel" → "SolarPanel"; '' if nothing
    substantive remains."""
    words = [w for w in re.split(r"[ _-]+", cand.strip()) if w]
    while words and words[0].lower() in _TYPE_STOPWORDS:
        words.pop(0)
    return " ".join(words)


def _explicit_user_type(instruction: str) -> str:
    """Deterministically extract a user-NAMED record type, or '' if none is clear.

    ONTA-244: the spec LLM's degrade default is ``WebRecord`` and it sometimes
    under-classifies a fully-specified ask to it too, silently dropping the type
    the user actually named ("Add **Widget** records …" → WebRecord). This parser
    recovers that named type from the raw instruction WITHOUT an LLM, so the plan
    never downgrades a named type to the placeholder. It fires on an unambiguous
    frame — a discovery verb followed by "<Type> records/entities", a "<Type> with
    <fields>" list, or an "each <noun> record …" shape description — and requires
    the type token to be either Capitalized (a lowercased entity phrase is not
    mistaken for a type) or introduced by the strong "each … record" frame. Returns
    a PascalCase type name (via ``_pascal``) or '' when nothing unambiguous is
    present (the caller then keeps WebRecord and clarifies, exactly as before)."""
    if not instruction:
        return ""
    text = instruction[:8000]
    for rx in (_EXPLICIT_TYPE_RE, _TYPE_WITH_FIELDS_RE):
        m = rx.search(text)
        if m:
            cand = _pascal(_strip_type_stopwords(m.group(1)))
            if cand and cand != "WebRecord":
                return cand
    # "each <noun> record …" — a shape description that names the per-record type
    # even in lowercase. Reject the generic record nouns themselves + non-type
    # fillers so "each record", "each data row" don't mint a junk type.
    m = _EACH_RECORD_TYPE_RE.search(text)
    if m:
        noun = m.group(1).lower()
        if noun not in _EACH_NOUN_STOPWORDS:
            cand = _pascal(_singularize(noun))
            if cand and cand != "WebRecord":
                return cand
    return ""


# Nouns that appear in an "each <noun> record" frame but are NOT a real record
# type — the record-noun synonyms themselves ("each record record" can't happen but
# "each data row" / "each result entity" can) and generic fillers. Rejecting these
# keeps the frame from minting a meaningless Data/Result/Item type. Conservative:
# a genuine domain noun (model, product, physician, company) is never in this set.
_EACH_NOUN_STOPWORDS = frozenset(
    {
        "record", "records", "entity", "entities", "row", "rows", "data",
        "result", "results", "item", "items", "thing", "things", "one", "single",
        "new", "such",
    }
)


# A field token in an explicit list: a snake_case / hyphenated identifier, or a
# short multi-word phrase ("word error rate"). We deliberately keep it tight — a
# word made of letters/digits/_/- optionally followed by up to THREE more such
# words (≤4 words total) — so a long trailing prose clause ("… if you can find
# them") is rejected rather than swallowed as one giant field name.
_FIELD_TOKEN = re.compile(
    r"^[A-Za-z][A-Za-z0-9_\-]*(?: [A-Za-z0-9][A-Za-z0-9_\-]*){0,3}$"
)

# An INLINE annotation a user commonly appends to a field name to clarify its
# meaning or enumerate its allowed values — "model_type (LLM/TTS/STT/…)",
# "latency [ms]", "cost_per_1M_tokens (USD)". The annotation is NOT part of the
# field name and would otherwise fail ``_FIELD_TOKEN`` (parens/brackets/slashes
# aren't identifier chars), which — because the harvest ``break``s on the first
# non-field token — silently truncated the whole list at the first annotated
# field (the persona-eval RCA: an explicit 20-field list collapsed to just the
# two un-annotated leading fields ``name, provider``). We blank out each
# ``(...)`` / ``[...]`` / ``{...}`` group so the bare field name survives AND a
# list separator INSIDE the annotation ("LLM/TTS/STT") can't shatter the token.
# Matches one balanced-free (non-nested) group at a time, applied globally, so it
# also protects a mid-list annotation, not just a trailing one. Domain-agnostic.
_FIELD_ANNOTATION_RE = re.compile(r"[\(\[\{][^\(\)\[\]\{\}]*[\)\]\}]")


def _strip_inline_annotations(segment: str) -> str:
    """Replace every inline "(…)"/"[…]"/"{…}" annotation in a field-list segment
    with a single space, so an annotated field collapses to its bare name in place
    — ``model_type (LLM/TTS/STT), latency [ms]`` → ``model_type , latency`` — and
    a separator hidden inside an annotation never fragments the list. Nested
    brackets aren't special-cased (a single pass removes the inner group and leaves
    a stray outer bracket, which then simply fails _FIELD_TOKEN — safe: it ends the
    run rather than harvesting garbage). Whitespace is left for the tokenizer to
    trim."""
    return _FIELD_ANNOTATION_RE.sub(" ", segment)

# STRICT markers that UNAMBIGUOUSLY introduce a field list, so we harvest even a
# single field after them — the "Use these:" chip, a "fields/columns/attributes"
# noun preposition-introduced ("with fields …") or colon-terminated ("fields: …").
# A false positive here would pollute the attribute floor with an entity phrase, so
# these stay conservative. Case-insensitive.
_FIELD_LIST_MARKERS = re.compile(
    r"(?:use\s+these"
    r"|(?:with|of|including|these|the\s+following)\s+"
    r"(?:the\s+)?(?:fields?|columns?|attributes?|properties)"
    r"|(?:fields?|columns?|attributes?|properties)\s*:)"
    r"\s*:?\s*",
    re.IGNORECASE,
)

# LOOSE marker — a "records/entities/rows with" frame ("Add Widget records with
# sku, color, weight"). The record noun before "with" signals a field list, but the
# frame is weaker than the strict markers: a single trailing phrase could be a
# FILTER ("records with high error rates") rather than a field list. So we only
# harvest from this frame when the tail is an actual ENUMERATION — 2+ items joined
# by a comma/semicolon/"and"/"or" — never a lone trailing phrase. This keeps the
# legitimate "with a, b, c" case while rejecting "with <prose filter>".
_LOOSE_FIELD_LIST_MARKER = re.compile(
    r"(?:records?|entities|rows)\s+with\s+",
    re.IGNORECASE,
)
# A tail is a real field ENUMERATION only if it carries a list joiner before the
# first sentence break — a comma/semicolon, or an "and"/"or" between two items.
_LIST_JOINER = re.compile(r"[,;]|\b(?:and|or)\b", re.IGNORECASE)


def _explicit_user_fields(instruction: str) -> list[str]:
    """Deterministically extract the fields the user EXPLICITLY enumerated.

    The persona-eval RCA (ONTA-239, Cluster 2): when the user hands over a concrete
    field list, the LLM spec resolver may non-deterministically drop or rename some
    of them (18 named fields collapsed to a generic 9). This parser is the
    authoritative FLOOR: it reads the user's list straight from the accumulated
    instruction WITHOUT an LLM, so the plan can guarantee no user-named field is
    lost, regardless of what the resolver returned.

    It fires after an unambiguous list MARKER — the server-generated
    ``Use these: …`` chip, a "fields/columns/attributes" noun preposition-introduced
    ("with fields …") or colon-terminated ("fields: …"), or a weaker "records/
    entities/rows with …" frame that requires a real ENUMERATION (2+ comma/"and"-
    joined items) so a lone filter phrase ("records with high error rates") is NOT
    mistaken for a field list — then harvests the comma/newline/semicolon-separated
    tokens on the SAME logical run. Deliberately conservative: bare verbs like
    "collect"/"include" are NOT markers ("collect the coffee shops in SF" is an
    entity phrase). Each token must look like a field name (a short identifier or
    ≤4-word phrase) — a longer prose run breaks the list. Returns snake_case,
    de-duped, order-preserving. Empty when the user gave no explicit list.
    """
    if not instruction:
        return []
    # Bound the work: this runs on the SYNCHRONOUS /agent request path (before the
    # discovery is backgrounded) and the per-marker ``tail`` slice + regex make the
    # scan O(n²) in the number of list markers. A real instruction is well under a
    # few KB; cap the scanned prefix so a pathologically large ``message`` payload
    # can never turn this into a request-path CPU sink. A field list a user cares
    # about always appears early, so truncation never loses a legitimate floor.
    instruction = instruction[:8000]
    out: list[str] = []
    seen: set[str] = set()

    def _harvest(tail: str, *, require_enumeration: bool) -> None:
        # Stop the list at the first hard sentence break so a following sentence of
        # prose is never harvested.
        segment = re.split(r"[.\n?!]", tail, maxsplit=1)[0]
        # LOOSE frame guard: only treat this as a field list when the tail is an
        # actual enumeration (a list joiner present) — a lone trailing phrase after
        # "records with" is a filter/prose, not a field list.
        if require_enumeration and not _LIST_JOINER.search(segment):
            return
        # "a, b, c and d" / "a; b" / "a, b, or c" — normalize joiners to commas.
        segment = re.sub(r"\b(?:and|or)\b", ",", segment, flags=re.IGNORECASE)
        # Blank out inline annotations BEFORE tokenizing, so a separator INSIDE an
        # annotation ("model_type (LLM/TTS/STT)") can't shatter the field into
        # bogus fragments and the annotation itself never becomes a token. We
        # replace each "(…)"/"[…]"/"{…}" group with a single space, collapsing the
        # annotated field down to its bare name in place. Domain-agnostic; keeps
        # slashes that are genuine separators ("a/b/c") splitting as before.
        segment = _strip_inline_annotations(segment)
        raw_tokens = re.split(r"[,;/]", segment)
        matched_any = False
        for tok in raw_tokens:
            t = tok.strip().strip("\"'`*").strip()
            if not t or not _FIELD_TOKEN.match(t):
                # A non-field token ends this list run: stop harvesting past prose
                # (e.g. "name, provider and the pricing if you can find it" keeps
                # name/provider/pricing but not the trailing clause).
                if matched_any:
                    break
                continue
            slug = _slug(t)
            if slug and slug not in seen:
                seen.add(slug)
                out.append(slug)
                matched_any = True

    for m in _FIELD_LIST_MARKERS.finditer(instruction):
        _harvest(instruction[m.end():], require_enumeration=False)
    for m in _LOOSE_FIELD_LIST_MARKER.finditer(instruction):
        _harvest(instruction[m.end():], require_enumeration=True)
    return out


def _snap_to_declared(names: list[str], declared: list[str]) -> list[str]:
    """Snap each attribute name to the type's EXISTING declared attribute (matched
    case-insensitively); keep it verbatim when the type has no such attribute.

    Mirrors enrichment's ``_validate_enrich_request`` (``enrich_cap.py``): the
    enrich rail is ontology-grounded and snaps to declared names, so web-discovery
    minting a divergent synonym for the SAME concept (``per_minute_pricing`` vs the
    already-declared ``realtime_audio_duration_per_minute``) forks the ontology
    across the two rails (ONTA-239, Cluster 2). Grounding discovery the same way
    converges the second rail onto the first's names. Order-preserving; a name with
    no declared match is a legitimately NEW attribute and passes through unchanged
    (soft-extraction still decides its final shape downstream)."""
    lookup = {d.lower(): d for d in declared if d}
    return [lookup.get(n.lower(), n) for n in names]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        s = (x or "").strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out
