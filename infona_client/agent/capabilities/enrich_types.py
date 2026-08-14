"""Target-type resolution for the enrich capability.

Owns listing the tenant's declared types and matching a type name in the
instruction (CamelCase- / plural-tolerant, first-standalone-mention wins).

Invariants other agents must not break:
- Prefer a type named in the LIVE turn over the Explorer selection.
- Look up ``logger`` on the public ``enrich_cap`` module via :func:`_host`
  so patches keep working.
- Catalog-only type list (GraphStore). Do not add a SPARQL else-arm.
"""

from __future__ import annotations

import re

from infona_client.agent.capabilities.enrich_common import _host
from infona_client.agent.registry import AgentContext

# --- target-type resolution: prefer the type NAMED in the instruction --------- #
# The Explorer sends the currently-selected type as ``ctx.type_name``. That
# selection must NEVER override a type the user actually names in their message:
# "enrich brokers with their websites" enriches Broker even when PropertyListing
# is the selected type. We resolve the target type from the instruction text
# (case-insensitive, CamelCase- and plural-tolerant) and fall back to the
# selection ONLY when the message names no known type — so a missing/wrong UI
# selection no longer bails the plan to "couldn't determine the specifics".
#
# Three matcher failure modes this block defends against (grounded RCA against the
# voice-models persona eval — the `sp-refresh-pricing` arc):
#   1. An INCIDENTAL type that appears only as a SCOPE qualifier ("…whose
#      organization is X") or a NEGATION ("NOT Organization entities") must not
#      beat the HEAD type the user targeted. The old "longest name named anywhere
#      wins" tie-break picked Organization(12) over Model(5) — and the persona's
#      "(NOT Organization…)" workaround BACKFIRED by injecting the very token the
#      matcher then selected. Fix (A-3): first-STANDALONE-mention wins (the head
#      noun precedes its scope/negation qualifiers in English), longest only as a
#      same-position tie-break (so PropertyListing still beats Property).
#   2. A type named only INSIDE an attribute name ("supported_languages" →
#      Language) must not be selected. Fix (A-3): a snake/kebab/CamelCase COMPOUND
#      token contributes its parts ONLY to phrase matching, never as a standalone
#      single-word candidate — so "supported_languages" can't mint a bare
#      ``Language`` match (only a genuine standalone "language(s)" can).
#   3. A solidly-spelled multi-word type the user writes EXACTLY as the ontology
#      spells it ("RealtimeModel", "GeminiModel") must match. Fix (A-2): the text
#      tokenizer CamelCase-splits each word into adjacent sub-tokens, so the
#      type's phrase ['realtime','model'] matches the fused "RealtimeModel".
#
# A single "word" for matching keeps ``_``/``-`` joined so a snake/kebab compound
# stays atomic (mode 2); CamelCase splitting happens per-word in _camel_words.
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")


async def _list_types(ctx: AgentContext) -> list[str]:
    """The tenant's declared type names, for resolving the target type from text.

    GraphStore / Neo4j (ONTA-534 / ONTA-527): reads the ontology catalog — the
    SAME ``list_types`` the ``/ontology/types`` route uses. Production SPARQL
    HTTP is retired and used to raise on ``query()``, so a SPARQL-only list
    swallowed that error, returned ``[]``, and ``plan()`` bailed to "couldn't
    determine the specifics" whenever the Explorer had no type selected (the
    2026-08-13 Ask Onta regression). Catalog only — no SPARQL else-arm.
    """
    try:
        from infona_client.graph.ontology_catalog import list_types as cat_list_types
        from infona_client.graph.store import GraphConfigError, get_optional_graph_store

        store = get_optional_graph_store()
        records = await cat_list_types(
            store=store, tenant_id=ctx.tenant_id, layer="tenant"
        )
    except GraphConfigError:
        _host().logger.error("agent_enrich_list_types_no_store", tenant_id=ctx.tenant_id)
        return []
    except Exception:  # noqa: BLE001 — a type-list read must never break planning
        _host().logger.exception("agent_enrich_list_types_failed")
        return []
    seen: set[str] = set()
    names: list[str] = []
    for rec in records:
        label = (getattr(rec, "name", None) or "").strip()
        if label and label not in seen:
            seen.add(label)
            names.append(label)
    return names


def _singularize(word: str) -> str:
    """Tiny dependency-free English singularizer — for MATCHING only, not display."""
    w = word.lower()
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"  # companies -> company, agencies -> agency
    if len(w) > 4 and w.endswith(("ses", "xes", "zes", "ches", "shes")):
        return w[:-2]  # addresses -> address, boxes -> box
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]  # brokers -> broker, listings -> listing
    return w


def _camel_words(type_name: str) -> list[str]:
    """Split a type name into lowercase words: ``PropertyListing`` -> ['property',
    'listing'], ``URL`` -> ['url'], ``real_estate_agent`` -> ['real', 'estate',
    'agent']. Lets a multi-word type be phrase-matched against the instruction.

    Also used to CamelCase-/underscore-split each raw word of the instruction text
    (A-2), so a solidly-spelled multi-word type ("RealtimeModel") matches the same
    way the user wrote it (fused) — the text word and the type name are tokenized
    identically, so their sub-token phrases line up."""
    parts: list[str] = []
    for chunk in re.split(r"[\s_\-]+", type_name or ""):
        parts.extend(
            re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z][a-z0-9]*|[a-z0-9]+", chunk)
        )
    return [p.lower() for p in parts if p]


def _tokenize_for_match(text: str) -> tuple[dict[str, int], list[str]]:
    """Tokenize ``text`` for type-name matching → (simple_first, phrase).

    ``phrase`` is EVERY singularized sub-token in order, with each raw word's
    CamelCase/compound parts kept ADJACENT — so a multi-word type's phrase
    (``['realtime','model']``) matches a fused ``RealtimeModel`` the user typed
    (A-2). Position in ``phrase`` preserves word order for first-mention ordering.

    ``simple_first`` maps a singularized token → the earliest ``phrase`` index at
    which it appeared as a STANDALONE SIMPLE word (a raw word that is not a
    multi-part compound). A snake/kebab/CamelCase COMPOUND (typically an attribute
    the user named, e.g. ``supported_languages``, or a solid multi-word type) does
    NOT register its individual parts here — so a bare single-word type like
    ``Language`` cannot be matched by ``supported_languages`` (the attribute-name
    guard, A-3), only by a genuine standalone "language(s)".
    """
    phrase: list[str] = []
    simple_first: dict[str, int] = {}
    for raw in _WORD_RE.findall(text or ""):
        parts = _camel_words(raw)
        if not parts:
            continue
        singular = [_singularize(p) for p in parts]
        start = len(phrase)
        phrase.extend(singular)
        if len(parts) == 1 and singular[0] not in simple_first:
            simple_first[singular[0]] = start
    return simple_first, phrase


def _first_phrase_index(phrase: list[str], words: list[str]) -> int | None:
    """The earliest start index at which ``words`` appears as a contiguous run in
    ``phrase`` (both already singularized), or None."""
    span = len(words)
    if span == 0 or span > len(phrase):
        return None
    for i in range(len(phrase) - span + 1):
        if phrase[i : i + span] == words:
            return i
    return None


def _type_match_index(
    name: str, simple_first: dict[str, int], phrase: list[str]
) -> int | None:
    """Earliest token index at which type ``name`` is NAMED in the tokenized text,
    or None. A single-word type must appear as a STANDALONE simple word (the
    attribute-name guard, A-3); a multi-word (CamelCase) type as a contiguous
    phrase (A-2)."""
    words = _camel_words(name)
    if not words:
        return None
    if len(words) == 1:
        return simple_first.get(_singularize(words[0]))
    return _first_phrase_index(phrase, [_singularize(w) for w in words])


def _match_type_in_text(text: str, known_types: list[str]) -> str | None:
    """Return the known type NAMED in ``text``, or None.

    Selection order (A-3): the type whose STANDALONE mention appears EARLIEST in
    the text wins — the head noun the user targets precedes the scope-qualifier
    ("…whose organization is X") and negation ("NOT Organization") clauses that
    follow it in English, so first-mention naturally prefers the head over an
    incidental co-mention without a fragile clause parser. On a tie (same start
    position) the LONGER name wins, so a specific ``PropertyListing`` still beats a
    bare ``Property``.
    """
    simple_first, phrase = _tokenize_for_match(text)
    if not phrase or not known_types:
        return None
    best: str | None = None
    best_key: tuple[int, int] | None = None
    for name in known_types:
        idx = _type_match_index(name, simple_first, phrase)
        if idx is None:
            continue
        key = (idx, -len(name))
        if best_key is None or key < best_key:
            best, best_key = name, key
    return best


def _resolve_target_type(
    instruction: str,
    known_types: list[str],
    selected: str | None,
    current_message: str | None = None,
) -> str | None:
    """Pick the type to enrich, PREFERRING the one named in the LIVE turn.

    Order:
      1. a known type named in the CURRENT message wins — the user just said it,
         so it beats a stale mention still sitting in the accumulated
         ``instruction`` window (the session-context-bleed defense: without this,
         "longest type named anywhere in the instruction" let a type from a
         COMPLETED earlier request hijack the new one);
      2. else a known type named anywhere in the accumulated ``instruction`` —
         the clarify-chain fallback, where the type was named an earlier turn of
         the SAME open ask and the current reply is a terse scope answer;
      3. else the selected (UI) type, when it is a real KG type OR when we
         couldn't list types at all (preserve the legacy selection behavior) —
         this also honors a deliberate MCP ``type_name`` on a terse call that
         names no type in prose;
      4. else, when the KG has exactly one type, that type;
      5. else None — the caller asks which type to enrich.

    ``current_message`` is optional: when omitted (a direct/legacy call) step 1 is
    skipped and this collapses to the prior instruction-first behavior, so
    existing callers are unaffected.

    How the pf9 RCA matcher fixes land WITHOUT an explicit-type override
    -------------------------------------------------------------------
    The three tokenizer fixes below make first-STANDALONE-mention (steps 1-2)
    resolve the RCA cases directly: the head type the user targets appears BEFORE
    its scope/negation qualifiers in English, and a type named only inside an
    attribute name ("supported_languages" → Language) or a solidly-spelled
    CamelCase type ("RealtimeModel") is handled by the tokenizer — so
    ``type_name='Model'`` / ``RealtimeModel`` resolve correctly from prose alone,
    no separate "explicit arg wins" branch needed. Crucially this keeps a stale
    sticky UI ``selected`` from hijacking: it only wins via step 3, when the live
    turn names no type at all. (Honoring a DELIBERATE MCP ``type_name`` over a
    scope-FIRST phrasing whose head is a different type — "for each Organization,
    enrich its Models", type_name=Model — is a separate, plumbing-level follow-up:
    ``selected`` alone can't be told apart from a sticky Explorer default, so we
    don't guess. That case resolves to the head today, same as before this change.)
    """
    if current_message:
        named_now = _match_type_in_text(current_message, known_types)
        if named_now:
            return named_now
    named = _match_type_in_text(instruction, known_types)
    if named:
        return named
    if selected and (not known_types or selected in known_types):
        return selected
    if len(known_types) == 1:
        return known_types[0]
    return None
