"""A3 — the explicit Clean stage (ONTA-344).

Between **A2** (the candidate facts the extractor proposes) and **A4** (the typed,
verified triples the writer is allowed to persist) sits value CLEANING: datatype
coercion and lexical canonicalization. Historically this was FUSED into A4
(``resolver/validator.py::validate_triple``) with no structured record of what it
did — coercions were logged INFO, drops logged WARNING, but neither was collected
into an artifact a caller could inspect (a zero-silent-drops gap).

This module makes A3 an explicit, pure stage. :func:`clean_value` consumes ONE A2
candidate value and returns an A3 :class:`CleanFact`; :class:`CleanReport`
partitions EVERY consumed value exactly once into ``passed`` / ``transformed`` /
``dropped``, each carrying a reason — the zero-silent-drops ledger (the read-side
mirror of ADR 0003 §2 row conservation).

**A4 consumes A3, and stays byte-identical.** ``validate_triple`` (A4) now calls
:func:`clean_value` for the coerce/canonicalize/reject DECISION, then applies its
own XSD typing (``_typed_value``). Because :func:`clean_value` hands A4 exactly the
lexical value ``_typed_value`` was already stamping (the raw value when it conforms,
the coerced value when it does not), the A4 output — and therefore the frozen
``a4.json`` / ``a5.json`` boundary fixtures — is unchanged. A3 only SURFACES the
cleaning A4 was already doing silently.

Reuses the primitives in ``resolver/validator.py`` (``validate_value`` /
``coerce_value``) — it does NOT re-implement cleaning. OSS: stdlib +
``cograph_client.*`` only, no ``from cograph.*``.
"""
from __future__ import annotations

from cograph_client.resolver.models import CleanFact, CleanOutcome, CleanReport
from cograph_client.resolver.validator import coerce_value, validate_value

__all__ = ["clean_value", "CleanFact", "CleanOutcome", "CleanReport"]

# Datatypes whose CONFORMING lexical form can still be non-canonical, so a value
# that already passes ``validate_value`` may still need canonicalization (boolean
# case-folding, datetime → ISO-8601, geo → WKT ``POINT``). ``integer`` / ``float``
# are deliberately excluded: a conforming numeric is ALREADY a valid xsd lexical
# form and reformatting it (``str(int(float(x)))``) would corrupt large integers —
# the exact carve-out ``validator._typed_value`` makes when it stamps types.
_CANONICALIZED_DATATYPES = ("boolean", "datetime", "geo")


def _canonical_lexical(value: str, datatype: str) -> str:
    """The canonical lexical form for ``value`` — the SAME canonicalization
    ``validator._typed_value`` applies before stamping the XSD datatype, but WITHOUT
    the ``^^xsd`` stamp. boolean/datetime/geo route through ``coerce_value`` (which
    is idempotent on its own output); every other datatype is returned verbatim so
    numerics and strings are never reformatted."""
    if datatype in _CANONICALIZED_DATATYPES:
        canonical = coerce_value(value, datatype)
        if canonical is not None:
            return canonical
    return value


def clean_value(
    value: str,
    datatype: str,
    *,
    entity_id: str = "",
    attribute: str = "",
) -> CleanFact:
    """A3: clean ONE candidate value into its datatype's canonical lexical form, or
    drop it. Returns a :class:`CleanFact` recording the outcome + a reason.

    Three outcomes — the DECISION ``validate_triple`` (A4) then consumes:

      * **PASSED** — conforms as-is AND is already canonical (written verbatim).
      * **TRANSFORMED** — coerced to fit the datatype, and/or lexically canonicalized
        (``"yes"`` -> ``"true"``, ``"4/5/2020"`` -> ``2020-04-05T00:00:00``,
        ``"37.7,-122.4"`` -> ``POINT(-122.4 37.7)``, integer ``"4.6"`` -> ``"4"``).
      * **DROPPED** — cannot be coerced to the datatype → not written (its reason is
        the same message ``validate_triple`` puts on the ``RejectedValue``).

    ``conformed`` (True iff ``validate_value`` accepted the value as-is) is carried
    on the fact so A4 can reproduce its OK-vs-COERCED outcome unchanged even for a
    value that conformed yet was canonicalized (A3 TRANSFORMED, A4 OK)."""
    conformed = validate_value(value, datatype)
    if conformed:
        clean = _canonical_lexical(value, datatype)
        if clean == value:
            outcome, reason = CleanOutcome.PASSED, f"conforms as {datatype}"
        else:
            outcome, reason = (
                CleanOutcome.TRANSFORMED,
                f"canonicalized {datatype} lexical form",
            )
        return CleanFact(
            datatype=datatype,
            raw_value=value,
            clean_value=clean,
            outcome=outcome,
            conformed=True,
            reason=reason,
            entity_id=entity_id,
            attribute=attribute,
        )

    coerced = coerce_value(value, datatype)
    if coerced is not None:
        return CleanFact(
            datatype=datatype,
            raw_value=value,
            clean_value=_canonical_lexical(coerced, datatype),
            outcome=CleanOutcome.TRANSFORMED,
            conformed=False,
            reason=f"coerced to {datatype}",
            entity_id=entity_id,
            attribute=attribute,
        )

    return CleanFact(
        datatype=datatype,
        raw_value=value,
        clean_value=None,
        outcome=CleanOutcome.DROPPED,
        conformed=False,
        # Byte-identical to validator's legacy RejectedValue.reason so A4 output
        # (and the frozen a4 fixtures) do not shift.
        reason=f"Cannot coerce '{value}' to {datatype}",
        entity_id=entity_id,
        attribute=attribute,
    )
