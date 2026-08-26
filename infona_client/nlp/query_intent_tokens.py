"""Filter-token extract / collapse for intent sketches.

General English only — no domain stopwords. Used by
:mod:`infona_client.nlp.query_intent`. Keep this file under the new-file cap.
"""

from __future__ import annotations

import re
from infona_client.nlp.query_intent_lexicon import (
    _CODE_TOKEN_RE,
    _LABEL_DIGIT_STOP,
    _MEASURE_HEAD_STOP,
    _STATUS_VALUE_ALLOW,
    _STATUSISH_FREE_RE,
    _STOPWORDS,
)

# Measure-ish noun after aggregate verb: "sum unit_qty", "total price".
_MEASURE_AFTER_AGG_RE = re.compile(
    r"(?ix)\b(?:sum|total|average|avg|mean|min(?:imum)?|max(?:imum)?)\s+"
    r"(?:of\s+|the\s+)*"
    r"(?P<measure>[A-Za-z][A-Za-z0-9_]*)"
    r"(?=\s+(?:for|in|at|where|with|having)\b|\s*$|[^\w\s]|\s+\w)"
)

# Free tokens after dim prepositions / value-taking verbs.
# ``targeting`` is the same shape as ``named``/``matching`` (not a type list).
_AFTER_PREP_RE = re.compile(
    r"(?ix)\b(?P<prep>for|in|at|where|with|having|status|named|labelled|"
    r"labeled|matching|targeting)\s+"
    r"(?:the\s+|a\s+|an\s+)*"
    r"(?P<tok>['\"][^'\"]+['\"]|[A-Za-z][A-Za-z0-9_]*(?:\s+[A-Za-z0-9_]+){0,2})"
)

_VALUE_AFTER_COPULA_RE = re.compile(
    r"(?ix)\b(?:is|are|equals?|equal\s+to|=)\s+"
    r"(?:the\s+|a\s+|an\s+)*"
    r"(?P<tok>['\"][^'\"]+['\"]|[A-Za-z][A-Za-z0-9_]*)"
)

_QUOTED_RE = re.compile(r"['\"]([^'\"]+)['\"]")

_LABEL_DIGIT_RE = re.compile(
    r"(?ix)\b(?P<label>[A-Za-z][A-Za-z0-9_]*)\s+(?P<num>[0-9]+(?:\.[0-9]+)?)\b"
)

# ``in or after 2014`` / ``since 2014`` — one year, not a free dim phrase.
_YEAR_COMPARE_RE = re.compile(
    r"(?ix)(?:(?:in|on)\s+or\s+(?:after|before)|"
    r"(?:since|after|before|until))\s+(?P<year>\d{4})\b"
)

_AFTER_PREP_COMPARE_RE = re.compile(
    r"(?ix)^or\s+(?:after|before)\s+\d{4}$"
)

# Copula + participle + prep is a verb phrase, not a literal
# (``are involved in``, not ``status is completed``).
_PARTICIPLE_RE = re.compile(r"(?i)^[A-Za-z]+(?:ed|ing)$")
_PREP_AFTER_RE = re.compile(r"(?i)\s+(?:in|on|by|for|with|at|from)\b")

# Same generic inventory/lab nouns as origin/main. Do not add demo-set types.
_TYPE_TRAILER_ALTS = (
    r"tests?|lots?|bins?|panels?|assays?|products?|items?|records?|"
    r"entities|rows?|offerings?|sessions?|courses?|goods?|crates?|"
    r"trays?|parts?|assets?|units?"
)
_TYPE_TRAILER_RE = re.compile(rf"(?ix)\s+\b(?:{_TYPE_TRAILER_ALTS})\b\s*$")
_TYPE_TRAILER_WORD_RE = re.compile(rf"(?ix)^(?:{_TYPE_TRAILER_ALTS})$")

def _normalize_token(raw: str) -> str:
    t = (raw or "").strip().strip("\"'").strip()
    return re.sub(r"\s+", " ", t)


# Answer-format / task-boilerplate quoted in the question ("Answer: <number>",
# "in the format", "in the knowledge graph"). These are not dimension values.
_INSTRUCTION_TOKEN_RE = re.compile(
    r"(?ix)^(?:"
    r"answer\s*:.*"
    r"|format"
    r"|knowledge\s+graph"
    r"|shortest\s+path(?:\s*:.*)?"
    r"|yes\s*/\s*no"
    r"|nothing\s+else"
    r"|entity\s+label"
    r"|<number>"
    r"|<entity(?:\s+label)?>"
    r")$"
)


def _is_instruction_token(tok: str) -> bool:
    """True for answer-format / task-boilerplate tokens, not real dim values."""
    low = (tok or "").strip().lower()
    if not low:
        return False
    return bool(_INSTRUCTION_TOKEN_RE.fullmatch(low))


def _is_stop_token(tok: str) -> bool:
    low = tok.lower().strip()
    if not low or len(low) < 2:
        return True
    if low in _STOPWORDS and low not in _STATUS_VALUE_ALLOW:
        return True
    # Limits/thresholds like ``10`` are not dim labels. A four-digit year
    # from ``in or after 2014`` is a real constraint value and must survive
    # collapse so a second dim (ready / DockA) still counts as multi-filter.
    if re.fullmatch(r"(?:19|20)\d{2}", low):
        return False
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", low):
        return True
    return False


def _copula_is_verb_prep(question: str, match: re.Match[str]) -> bool:
    """``are involved in`` — not ``status is completed`` or ``are completed in``."""
    tok = (match.group("tok") or "").strip().strip("\"'")
    if not _PARTICIPLE_RE.fullmatch(tok):
        return False
    if tok.lower() in _STATUS_VALUE_ALLOW:
        return False
    return bool(_PREP_AFTER_RE.match(question[match.end() :]))


def _in_np_is_verb_complement(question: str, match: re.Match[str]) -> bool:
    """Drop ``in <NP>`` only when it is the complement of a skipped verb.

    ``are involved in books`` → drop books. ``are completed in West`` keeps
    West (completed is a status value, not a join verb).
    """
    before = question[: match.start()]
    m = re.search(
        r"(?i)\b(?:is|are|was|were)\s+([A-Za-z]+(?:ed|ing))\s+$",
        before,
    )
    if not m:
        return False
    if m.group(1).lower() in _STATUS_VALUE_ALLOW:
        return False
    tok = _normalize_token(match.group("tok") or "")
    if not tok:
        return False
    # Keep real dim values (West, DockA, Phase 3, ready lots). Drop only
    # all-lowercase type chatter (books, clinical trials).
    if re.search(r"\d", tok):
        return False
    words = tok.split()
    if any(w.lower() in _STATUS_VALUE_ALLOW for w in words):
        return False
    if any(_CODE_TOKEN_RE.fullmatch(w) for w in words):
        return False
    if any(any(ch.isupper() for ch in w) for w in words):
        return False
    return True


def _in_type_noun_phrase(prep: str, tok: str) -> bool:
    """Bare ``in products`` / ``in tests`` — type mention, not a dim value.

    Only a single word from the existing trailer list. Keep ``in North``,
    ``in DockA``, ``in ready tests``, and any multi-word ``in …`` NP.
    """
    if prep.lower() != "in":
        return False
    if re.search(r"\d", tok):
        return False
    words = tok.split()
    if len(words) != 1:
        return False
    w = words[0]
    if w.lower() in _STATUS_VALUE_ALLOW:
        return False
    if _CODE_TOKEN_RE.fullmatch(w):
        return False
    return bool(_TYPE_TRAILER_WORD_RE.fullmatch(w.lower()))


def _is_contig_subseq(short: list[str], long: list[str]) -> bool:
    if not short or short == long or len(short) > len(long):
        return False
    n = len(short)
    for i in range(len(long) - n + 1):
        if long[i : i + n] == short:
            return True
    return False


def extract_filter_tokens(question: str) -> list[str]:
    """Extract candidate constraint *values* from free-form NL.

    General patterns only. Deduped, order-preserving, stopword-filtered.
    """
    q = (question or "").strip()
    if not q:
        return []

    seen: set[str] = set()
    out: list[str] = []

    def _add(
        raw: str,
        *,
        allow_measure_head: bool = False,
        allow_year: bool = False,
        allow_snake: bool = False,
    ) -> None:
        tok = _normalize_token(raw)
        if not tok:
            return
        low_full = tok.lower()
        for sep in (" is ", " are ", " equals ", " equal to ", " = "):
            if sep in low_full:
                tok = tok[low_full.rfind(sep) + len(sep) :].strip()
                low_full = tok.lower()
                break
        if not tok:
            return
        if _is_instruction_token(tok):
            return
        if allow_year and re.fullmatch(r"\d{4}", tok):
            pass
        elif _is_stop_token(tok):
            return
        low = tok.lower()
        if not allow_measure_head and low in _MEASURE_HEAD_STOP and " " not in low:
            return
        if (
            not allow_snake
            and " " not in low
            and "_" in low
            and low not in _STATUS_VALUE_ALLOW
        ):
            return
        if low in seen:
            return
        seen.add(low)
        out.append(tok)

    for m in _YEAR_COMPARE_RE.finditer(q):
        _add(m.group("year"), allow_measure_head=True, allow_year=True)

    for m in _QUOTED_RE.finditer(q):
        # Quoted snake_case is a real constraint (rel leaf 'made_by'), not a
        # schema-chatter skip.
        _add(m.group(1), allow_measure_head=True, allow_snake=True)

    for m in _VALUE_AFTER_COPULA_RE.finditer(q):
        if _copula_is_verb_prep(q, m):
            continue
        _add(m.group("tok"), allow_measure_head=True)

    for m in _AFTER_PREP_RE.finditer(q):
        prep = m.group("prep") or ""
        tok = m.group("tok") or ""
        if _AFTER_PREP_COMPARE_RE.match(_normalize_token(tok)):
            continue
        if prep.lower() == "in" and _in_np_is_verb_complement(q, m):
            continue
        if _in_type_noun_phrase(prep, _normalize_token(tok)):
            continue
        _add(tok)

    for m in _LABEL_DIGIT_RE.finditer(q):
        label = m.group("label")
        if label.lower() in _LABEL_DIGIT_STOP:
            continue
        _add(f"{label} {m.group('num')}", allow_measure_head=True)

    for m in _STATUSISH_FREE_RE.finditer(q):
        _add(m.group("tok"), allow_measure_head=True)

    for m in _CODE_TOKEN_RE.finditer(q):
        _add(m.group("tok"), allow_measure_head=True)

    return out


def extract_measure_prop_candidates(question: str) -> list[str]:
    """Optional measure-property phrases after aggregate verbs (best-effort)."""
    q = (question or "").strip()
    if not q:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _MEASURE_AFTER_AGG_RE.finditer(q):
        raw = _normalize_token(m.group("measure"))
        if not raw:
            continue
        parts = [p for p in raw.split() if p.lower() not in ("for", "in", "at", "where")]
        if not parts:
            continue
        leaf = parts[-1]
        if _is_stop_token(leaf) and leaf.lower() not in _MEASURE_HEAD_STOP:
            continue
        key = leaf.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(leaf)
    return out


def collapse_filter_tokens(tokens: list[str] | tuple[str, ...]) -> list[str]:
    """Collapse redundant filter tokens for multi-filter counting.

    ``['ready tests', 'ready']`` → ``['ready']``.
    ``['or after 2014', 'after 2014']`` → ``['after 2014']`` (word-sequence
    suffix; keep the shorter value). Not character-subset (``active`` ⊄
    ``inactive``).
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in tokens or ():
        tok = _normalize_token(str(raw))
        if not tok:
            continue
        stripped = _TYPE_TRAILER_RE.sub("", tok).strip()
        if stripped:
            tok = stripped
        key = tok.lower()
        if key in seen or _is_stop_token(tok) or _is_instruction_token(tok):
            continue
        seen.add(key)
        cleaned.append(tok)
    if len(cleaned) < 2:
        return cleaned
    keep: list[str] = []
    lows = [t.lower() for t in cleaned]
    word_lists = [t.split() for t in lows]
    for i, t in enumerate(cleaned):
        li = lows[i]
        wi = word_lists[i]
        subsumed = False
        for j, _other in enumerate(cleaned):
            if i == j:
                continue
            lj = lows[j]
            wj = word_lists[j]
            if li == lj:
                continue
            word_hit = li in wj or lj in wi
            seq_hit = _is_contig_subseq(wi, wj) or _is_contig_subseq(wj, wi)
            if word_hit or seq_hit:
                if len(li) > len(lj):
                    subsumed = True
                    break
        if not subsumed:
            keep.append(t)
    out: list[str] = []
    seen2: set[str] = set()
    for t in keep:
        k = t.lower()
        if k not in seen2:
            seen2.add(k)
            out.append(t)
    return out
