from __future__ import annotations

"""Source-grounding + fabricated-placeholder filters (ONTA-259 / ONTA-380).

Job: drop LLM-invented placeholders and attributes neither name nor value
can ground in source_text. Do not reimplement these predicates in
discovery or write — omit the attribute (treat as unstated).
"""

import re

from infona_client.resolver.models import ExtractedEntity, ExtractionResult
# Call-time host lookups so tests that patch schema_resolver.logger /
# insert_facts / _entity_uri / env flags keep working after this extract.
from infona_client.resolver import schema_resolver as _sr

def _looks_like_url(value: str) -> bool:
    """Whether a record ``source`` is a fetch URL (web discovery) vs a bare label
    (e.g. a CSV filename). Only a URL source becomes an attribute's `_source_url`
    citation; a non-URL source is still recorded as `_provenance`."""
    return isinstance(value, str) and (
        value.startswith("http://") or value.startswith("https://")
    )


# --- ONTA-259: deterministic anti-fabrication backstop ----------------------
# Discovery / text extraction runs an LLM over a source and PROPOSES attribute
# VALUES. When the model has no real value but the prompt nudges it to fill the
# field anyway, it emits a placeholder — in one UCI-health run the NPI
# "1234567890" landed on 92 distinct physicians, silently breaking every
# ID-keyed join. The extraction prompts now forbid this (see EXTRACTION_SYSTEM /
# EXTRACTION_TARGET_SYSTEM), but a prompt is not a guarantee: this
# deterministic, model-agnostic filter is the defense-in-depth backstop. A value
# it flags is treated as UNSTATED — the attribute is omitted (never written),
# exactly as if the source gave no value.
#
# Conservative BY DESIGN. It fires ONLY on values that are placeholder-shaped in
# FULL (whole-value match, never a substring), so a legitimate price "1000", a
# year "2024", or a real short code ("AAA", "XYZ") is KEPT. It is a fabrication
# guard, not a data cleaner — when unsure it keeps the value.

#: Whole-value filler tokens (case-folded) an extractor emits in place of a real
#: value. Matched only when the ENTIRE trimmed value equals one of these.
#: DELIBERATELY excludes ambiguous tokens that carry a real reading in some
#: domains — "None"/"nil" is a clinical CONFIRMED-none (allergies="None",
#: medications="None"), and "NA"/"nan" is a real code (Namibia's ISO code, a
#: North-America region code, or a person's name). Dropping those would turn a
#: STATED "none" into indistinguishable-from-unknown = information loss, so only
#: UNAMBIGUOUS non-values live here. "N/A" (with the slash) stays: it reads only
#: as "not applicable / available", never as a value.
_PLACEHOLDER_FILLER_TOKENS = frozenset({
    "n/a", "n.a.", "null", "unknown", "unspecified", "undefined",
    "not available", "not applicable", "no data", "no value",
    "tbd", "tba", "test", "placeholder",
})

#: Glyphs that, repeated as the WHOLE value (length ≥ 3), read as "unknown"
#: filler — "xxx", "xxxx", "----", "????", "....".
_PLACEHOLDER_RUN_CHARS = frozenset("x-_.?*#")

#: Canonical monotonic digit rings. A digit-only value is a sequential
#: placeholder when it is a SUBSTRING of one of these — so "1234567890"
#: (phone-keypad order, wraps 9→0), "0123456789", "123456", and their reverses
#: all match, while a real NPI like "1023011178" (not a contiguous run) does not.
_SEQ_DIGITS_ASC = "01234567890"
_SEQ_DIGITS_DESC = "09876543210"

#: A value must reduce to at least this many bare digits before the digit-run /
#: all-same-digit rules can flag it — so a real year ("2024"), a small price
#: ("1000"), or a short code is never caught. NPIs / phones / SSNs are 9–10 long.
_MIN_PLACEHOLDER_DIGITS = 6


def _is_fabricated_placeholder(value: str | None) -> bool:
    """True when ``value`` is an OBVIOUS fabricated placeholder (ONTA-259).

    Two families, both WHOLE-value (never a substring match) so the check stays
    conservative:
      * a filler token / filler-glyph run ("N/A", "unknown", "TBD", "xxx", …); and
      * a digit-shaped identifier placeholder — all-same-digit ("0000000000") or
        a monotonic run ("1234567890", "0123456789") — of at least
        ``_MIN_PLACEHOLDER_DIGITS`` digits, after stripping separators so
        "000-00-0000" / "(000) 000-0000" normalize.

    A legitimate price ("1000"), year ("2024"), or short code is NOT flagged.
    """
    if not value:
        return False
    v = value.strip()
    if not v:
        return False
    low = v.casefold()
    if low in _PLACEHOLDER_FILLER_TOKENS:
        return True
    # A run of a single filler glyph as the whole value: "xxx", "----", "????".
    if len(v) >= 3 and len(set(low)) == 1 and low[0] in _PLACEHOLDER_RUN_CHARS:
        return True
    # Digit-shaped identifier placeholders. Only judge values that are
    # essentially all digits (digits + separators) so a real alphanumeric code
    # is never touched.
    if re.fullmatch(r"[0-9\s().+\-/]+", v):
        digits = re.sub(r"[^0-9]", "", v)
        if len(digits) >= _MIN_PLACEHOLDER_DIGITS:
            if len(set(digits)) == 1:  # 0000000000, 1111111111, …
                return True
            if digits in _SEQ_DIGITS_ASC or digits in _SEQ_DIGITS_DESC:
                return True
    return False


# --- ONTA-380: source-grounding anti-fabrication backstop (attr names+values) -
# ONTA-259 drops placeholder-shaped VALUES. That still leaves a second failure
# mode: the model invents an entire attribute family the page never states
# (e.g. ``online_activity_percentage_of_summer_instruction`` /
# ``affordability_ranking``) with a plausible non-placeholder value. Prompt
# clauses forbid this; this deterministic filter is defense-in-depth.
#
# An attribute is KEPT when at least one of:
#   * its VALUE is grounded in the source text (substring / digit-normalized), or
#   * its NAME's distinctive tokens are grounded in the source text.
# An attribute is DROPPED only when BOTH name and value lack source support —
# pure fabrication of a concept + reading the page never made. Conservative BY
# DESIGN: no source_text → keep everything (can't verify); short/stopword-only
# names fall through to value grounding; when unsure, keep.

#: Tokens that never count as evidence that an attribute NAME is "about" the
#: source — too generic (``percentage``, ``ranking``, ``year``) or pure glue.
_ATTR_NAME_STOPWORDS = frozenset({
    "a", "an", "the", "of", "for", "to", "in", "on", "at", "by", "and", "or",
    "is", "are", "was", "were", "be", "as", "from", "with", "per", "via",
    "id", "ids", "name", "names", "label", "title", "type", "types", "value",
    "values", "url", "uri", "code", "codes", "num", "number", "numbers",
    "count", "total", "pct", "percent", "percentage", "rate", "ratio",
    "score", "scores", "rank", "ranking", "rankings", "index", "level",
    "status", "date", "time", "year", "years", "month", "day", "amount",
    "avg", "average", "mean", "min", "max", "sum", "size", "length",
})

#: Minimum length for a value (or digit run) to count as source-grounded on its
#: own. Shorter strings (``"7"``, ``"yes"``) appear too often by chance.
_MIN_GROUNDED_VALUE_LEN = 3


def _value_grounded_in_source(value: str | None, source_cf: str) -> bool:
    """True when ``value`` (or a digit-normalized form) appears in ``source_cf``.

    ``source_cf`` is the source text already ``casefold()``-ed. Placeholder
    values are never grounded (ONTA-259 owns those). Short values need a
    stronger signal so a lone digit can't keep a fabricated attribute.
    """
    if not value or not source_cf:
        return False
    v = str(value).strip()
    if not v:
        return False
    if _is_fabricated_placeholder(v):
        return False
    v_cf = v.casefold()
    # Direct substring (case-insensitive).
    if len(v_cf) >= _MIN_GROUNDED_VALUE_LEN and v_cf in source_cf:
        return True
    # Digit-normalized form: "$12,450" / "25,000" / "70 000" → "12450" / "25000".
    digits = re.sub(r"[^0-9]", "", v)
    if len(digits) >= _MIN_GROUNDED_VALUE_LEN:
        source_digits = re.sub(r"[^0-9]", "", source_cf)
        if digits in source_digits:
            return True
    # Compact alphanumeric (strip spaces/punct) for codes like "SN-9F2A".
    compact = re.sub(r"[^a-z0-9]", "", v_cf)
    if len(compact) >= _MIN_GROUNDED_VALUE_LEN:
        source_compact = re.sub(r"[^a-z0-9]", "", source_cf)
        if compact in source_compact:
            return True
    return False


def _name_grounded_in_source(name: str | None, source_cf: str) -> bool:
    """True when distinctive tokens of the snake_case attribute name appear in source.

    Stopwords / short glue tokens are ignored so a name like
    ``affordability_ranking`` is judged on ``affordability`` alone (not
    ``ranking``), and a pure-generic name (``year``, ``score``) never counts
    as grounded by name alone.
    """
    if not name or not source_cf:
        return False
    tokens = [
        t for t in re.split(r"[_\s\-]+", str(name).casefold())
        if t and len(t) >= 3 and t not in _ATTR_NAME_STOPWORDS
    ]
    if not tokens:
        return False
    hits = sum(1 for t in tokens if t in source_cf)
    # Majority of distinctive tokens (ceil half) must appear.
    return hits >= max(1, (len(tokens) + 1) // 2)


def _attribute_grounded_in_source(
    name: str | None, value: str | None, source_text: str | None,
) -> bool:
    """True when the attribute is supported by the source (ONTA-380).

    Keep when the value is grounded OR the name's distinctive tokens are
    grounded. Drop only pure fabrications (neither). No source → keep
    (cannot verify).
    """
    if not source_text or not str(source_text).strip():
        return True
    source_cf = str(source_text).casefold()
    if _value_grounded_in_source(value, source_cf):
        return True
    if _name_grounded_in_source(name, source_cf):
        return True
    return False


def _drop_ungrounded_attributes(result: ExtractionResult) -> ExtractionResult:
    """ONTA-380: drop attributes neither name nor value can ground in source_text.

    Runs on the model-proposed extraction result (text / JSON / web-discovery).
    Pure fabrications of attribute families the page never stated are omitted
    before resolve/write — same "treat as unstated" semantics as ONTA-259
    placeholder drops. No-ops when ``source_text`` is empty.
    """
    source = result.source_text or ""
    if not source.strip() or not result.entities:
        return result
    kept_entities: list[ExtractedEntity] = []
    dropped = 0
    changed = False
    for e in result.entities:
        if not e.attributes:
            kept_entities.append(e)
            continue
        kept_attrs = []
        for a in e.attributes:
            if _attribute_grounded_in_source(a.name, a.value, source):
                kept_attrs.append(a)
            else:
                dropped += 1
                _sr.logger.info(
                    "discovery_ungrounded_attribute_dropped",
                    entity_id=e.id,
                    type_name=e.type_name,
                    attribute=a.name,
                    value=a.value,
                )
        if len(kept_attrs) != len(e.attributes):
            changed = True
            kept_entities.append(e.model_copy(update={"attributes": kept_attrs}))
        else:
            kept_entities.append(e)
    if not changed:
        return result
    _sr.logger.info(
        "discovery_ungrounded_attributes_filtered",
        dropped_attributes=dropped,
        entities=len(result.entities),
    )
    return ExtractionResult(
        entities=kept_entities,
        relationships=result.relationships,
        source_text=result.source_text,
    )
