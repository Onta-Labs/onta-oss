"""Refresh / overwrite / composite-scope detectors for the enrich capability.

Owns conflict-policy selection (stage / verify / overwrite) and the
conservative verb detectors that flip a plan onto the refresh or
replace rail.

Invariants other agents must not break:
- A bare "refresh / re-verify" stays ``verify`` (ONTA-245). Only an
  explicit replace intent escalates to ``overwrite``.
- ``overwrite`` is conservative: a false-positive destroys data.
- Composite-scope check is delimiter-only; do not invent a second
  normalizer here.
"""

from __future__ import annotations

import re

from infona_client.agent.capabilities.enrich_common import _COMPOSITE_DELIMS


def _default_conflict_policy():
    from infona_client.enrichment.models import ConflictPolicy

    return ConflictPolicy.stage


def _refresh_conflict_policy():
    """Refresh-existing mode uses the `verify` policy: it re-confirms existing
    values and advances each fact's freshness stamp (`_verified_at`) WITHOUT
    overwriting the primary value or holding conflicts for review — the decay-
    refresh contract (ONTA-245 F2/F3). This stays the DEFAULT for a plain
    "refresh / re-verify / re-check / re-confirm" so ONTA-245's contract is
    preserved; only an EXPLICIT replace intent (see `_looks_like_overwrite`)
    escalates to `overwrite`."""
    from infona_client.enrichment.models import ConflictPolicy

    return ConflictPolicy.verify


def _overwrite_conflict_policy():
    """Refresh-REPLACE mode uses the `overwrite` policy: it REPLACES a changed
    existing value with the fresh one (delete-old + insert-new, the ONTA-236
    attribute-update contract) and stamps the new value's source + `_verified_at`,
    instead of re-confirming the stale value in place.

    Reached ONLY when the instruction carries an EXPLICIT replace / update-to-
    current intent (`_looks_like_overwrite`), NOT for a bare refresh — a
    false-positive overwrite destroys data, so the default stays `verify`
    (ONTA-245). Motivated by the pf10 Speko persona-eval task sp-refresh-pricing
    ("refresh … so every number is CURRENT and sourced"), where `verify` correctly
    fetched the fresh value but dropped it, leaving the stale one in place."""
    from infona_client.enrichment.models import ConflictPolicy

    return ConflictPolicy.overwrite


# Verbs that signal a REFRESH-EXISTING (re-verify a subset) intent rather than a
# discover-new or first-fill enrich. Matched as whole words, case-insensitively,
# so "refresh the pricing", "re-verify affiliations", "re-check the numbers",
# "update the verified dates", "keep the address current" all route to the
# verify-policy refresh. Kept in lockstep with the planner's deterministic
# refresh-existing router (`_REFRESH_EXISTING_RE`) so the SAME verb set that forces
# the enrich rail also flips the run to verify mode — a message the planner treats
# as a refresh must not land as a plain first-fill enrich. Generic — no persona
# field is referenced.
_REFRESH_RE = re.compile(
    r"\b(re-?verif\w*|re-?check\w*|re-?confirm\w*|refresh\w*|update(?:d|s)?|"
    r"re-?validat\w*|keep\s+(?:it\s+|them\s+)?current|"
    r"make\s+(?:it\s+|them\s+)?current|freshness|decay(?:ing|s)?)\b",
    re.IGNORECASE,
)


def _looks_like_refresh(instruction: str) -> bool:
    """True when the instruction asks to REFRESH / re-verify existing values."""
    return bool(_REFRESH_RE.search(instruction or ""))


# EXPLICIT replace intent — the SUBSET of asks that want a changed value REPLACED,
# not merely re-confirmed or first-filled. Kept deliberately CONSERVATIVE: a
# false-positive overwrite destroys data with no review in the auto-confirm/MCP path
# (ONTA-245 warns the default must stay `verify`/`stage`), so `_looks_like_overwrite`
# fires ONLY when the replace intent is UNMISTAKABLE. Two triggers (see the function):
#
# (A) An explicit REPLACE VERB — replace / overwrite / supersede / swap out. This
#     names the destructive act directly, so it fires REGARDLESS of a refresh verb.
_REPLACE_VERB_RE = re.compile(
    r"\b(?:replace|replaces|replacing|replaced|overwrite|overwrites|overwriting|"
    r"over-write|supersede|supersedes|superseding|superseded|"
    r"swap(?:s|ped|ping)?\s+out)\b",
    re.IGNORECASE,
)

# (B) An explicit replace-GOAL signal that is only trusted TOGETHER WITH a refresh
#     verb (see `_looks_like_overwrite`), so a first-fill "enrich/fill/map/link …
#     with the latest X" — which is a value DESCRIPTOR for a FILL, not a replace —
#     NEVER routes to overwrite (it stays the safe non-destructive `stage`). Three
#     goal shapes, all requiring the refresh gate:
#       * shape B — "correct/fix the <stale> <data-noun>" (a value-targeted fix);
#       * imperative — "update/bring/make/set <noun> TO (the) current|latest|
#         up-to-date|most recent" (the "to <target>" is what marks a replace, not a
#         bare "with the latest" descriptor);
#       * purpose clause — "SO (that) … is|are|be|stay|remain current|the latest|
#         up-to-date|most recent" (anchored on "so"/"so that", NOT a bare
#         "that … is current" state-check — this carries the pf10 persona case
#         "refresh … so every number is current and sourced").
_REPLACE_GOAL_RE = re.compile(
    r"(?:"
    r"\b(?:correct|correcting|corrects|corrected|fix|fixing|fixes|fixed)\s+"
    r"(?:the\s+|these\s+|those\s+|any\s+|all\s+|our\s+)?"
    r"(?:outdated\s+|out-of-date\s+|stale\s+|old\s+|wrong\s+|incorrect\s+|bad\s+)?"
    r"(?:value|values|number|numbers|price|prices|figure|figures|entr\w*|"
    r"record|records|field|fields|data|datum|rate|rates|score|scores|stat\w*|"
    r"amount|amounts|address|addresses)\b"
    r"|"
    r"\b(?:update|updates|updating|bring|brings|bringing|make|makes|making|"
    r"set|sets|setting|reset|resets|resetting)\s+"
    r"(?:\w+\s+){0,3}?to\s+(?:the\s+|their\s+|its\s+)?"
    r"(?:current|latest|newest|up[-\s]?to[-\s]?date|most\s+recent)\b"
    r"|"
    r"\bso\s+(?:that\s+)?(?:\w+\s+){0,6}?"
    r"(?:is|are|be|stay|stays|remain|remains)\s+(?:the\s+)?"
    r"(?:current|latest|newest|up[-\s]?to[-\s]?date|most\s+recent)\b"
    r")",
    re.IGNORECASE,
)


def _looks_like_overwrite(instruction: str) -> bool:
    """True when the instruction EXPLICITLY asks to REPLACE existing values with
    fresh ones (route → `overwrite`), not merely re-verify (`verify`) or first-fill
    (`stage`). Conservative by design — a false-positive overwrite deletes a
    conflicting value with no review. Fires ONLY when EITHER an explicit replace
    VERB is present (A), OR the ask is already a refresh AND carries an explicit
    replace-GOAL signal (B). Gating the goal signals behind `_looks_like_refresh`
    is what keeps a benign first-fill ("enrich each vendor with the latest pricing")
    on the safe `stage` path instead of destructively overwriting."""
    text = instruction or ""
    if _REPLACE_VERB_RE.search(text):
        return True
    return bool(_looks_like_refresh(text) and _REPLACE_GOAL_RE.search(text))


def _looks_composite(samples: list[str]) -> bool:
    """Cheap composite check: any sampled target value carries a list delimiter."""
    for v in samples:
        for d in _COMPOSITE_DELIMS:
            if d in v:
                return True
    return False
