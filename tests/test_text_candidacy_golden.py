"""Golden-fixture suite for schema-time free-text candidacy (ONTA-177).

Each fixture under ``tests/fixtures/text_candidacy/`` is one RECORDED run of
the ADR 0003 schema pass: CSV headers + sample rows, the recorded REASON
output (optionally REFUTE/COMPLETE — defaulting to a clean echo / no-op), and
the FULL expected resolved schema. The test replays ``infer_schema`` with the
recordings scripted into the ``_call_llm_v2`` seam (the established
recorded-output pattern from ``test_csv_resolver.py``) and asserts the entire
``CSVSchemaMapping`` — every entity spec, every column's role/datatype/
attribute/owner/target/``text_kind``, and the relationships.

Why full-schema golden assertions: the ``text_kind`` marker is produced by the
interplay of (a) the name-blind candidacy classifier over the fixture rows,
(b) the recorded REASON adjudication, and (c) the profiler-proposes gate in
``_decide_text_kind``. Pinning the complete resolved schema means THIS prompt
edit and every future one is guarded — if a change to the REASON prompt or
its output contract makes these recordings resolve differently (or fail
validation and exhaust the recording queue), this suite fails loudly.

The fixture set spans the representative bands: parliamentary speeches and
listing remarks (unambiguous long prose → auto tier, including an old-shape
recording with NO text_kind field at all), reviews/eligibility criteria
(ambiguous band, adjudicated free_text), addresses and organization names
(ambiguous band, adjudicated NOT free text — omitted and explicit-null forms),
codes/numbers (never candidates, including adversarial recordings the gate
must discard), a multi-entity table with a relationship-role column, and an
all-numeric table with a fully legacy recording.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from cograph_client.resolver.csv_resolver import (
    COMPLETE_SYSTEM,
    REASON_SYSTEM,
    REFUTE_SYSTEM,
    CSVResolver,
)
from cograph_client.resolver.models import CSVSchemaMapping

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "text_candidacy"
FIXTURE_PATHS = sorted(FIXTURE_DIR.glob("*.json"))


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _script_recordings(monkeypatch, resolver: CSVResolver, recorded: dict) -> None:
    """Feed the recorded pass outputs through the v2 LLM seam, one per pass.

    A missing REFUTE recording defaults to the clean echo (the prompt
    contract for "nothing wrong"); a missing COMPLETE recording defaults to
    the benign no-op ``{"types": []}``. Each queue holds exactly one entry:
    if a future prompt/contract edit makes a recording fail validation, the
    retry pops an empty queue and the test fails loudly — that IS the guard.
    """
    reason = recorded["reason"]
    refute = recorded.get("refute") or {
        "violations": [],
        "corrected": copy.deepcopy(reason),
    }
    complete = recorded.get("complete") or {"types": []}
    queues = {
        REASON_SYSTEM: [copy.deepcopy(reason)],
        REFUTE_SYSTEM: [copy.deepcopy(refute)],
        COMPLETE_SYSTEM: [copy.deepcopy(complete)],
    }

    async def scripted(system, user_content, temperature=0.0, max_tokens=None):
        assert system in queues, "unexpected pass system prompt"
        assert queues[system], (
            "recording exhausted — the pass output failed validation and "
            "retried, meaning the prompt/output contract changed shape"
        )
        return queues[system].pop(0)

    monkeypatch.setattr(resolver, "_call_llm_v2", scripted)


def _project(mapping: CSVSchemaMapping) -> dict:
    """The full structural resolved schema, in fixture JSON shape."""
    rels = [
        {"subject": r.subject, "predicate": r.predicate, "object": r.object}
        for r in (mapping.relationships or [])
    ]
    return {
        "entity_type": mapping.entity_type,
        "entities": [
            {
                "name": e.name,
                "type_name": e.type_name,
                "key_strategy": e.key_strategy,
                "id_column": e.id_column,
                "id_from": e.id_from,
            }
            for e in (mapping.entities or [])
        ],
        "columns": [
            {
                "column_name": c.column_name,
                "role": c.role.value,
                "datatype": c.datatype,
                "attribute_name": c.attribute_name,
                "entity": c.entity,
                "target_type": c.target_type,
                "text_kind": c.text_kind,
            }
            for c in mapping.columns
        ],
        "relationships": rels or None,
    }


@pytest.mark.parametrize("path", FIXTURE_PATHS, ids=lambda p: p.stem)
@pytest.mark.asyncio
async def test_golden_fixture_resolves_full_schema(path, monkeypatch):
    fx = _load(path)
    resolver = CSVResolver(client=None, openrouter_key="")
    _script_recordings(monkeypatch, resolver, fx["recorded"])

    mapping = await resolver.infer_schema(
        fx["headers"], fx["rows"], {}, total_rows=len(fx["rows"]),
    )

    assert _project(mapping) == fx["expected"], (
        f"{path.stem}: resolved schema diverged from the golden expectation"
    )


def test_fixture_suite_is_present_and_representative():
    """The suite must keep covering the representative bands: at least ten
    fixtures, with at least one marker in each decision tier (auto,
    adjudicated-yes, adjudicated-no/omitted, gated-adversarial)."""
    assert len(FIXTURE_PATHS) >= 10
    marked = {
        p.stem
        for p in FIXTURE_PATHS
        for c in _load(p)["expected"]["columns"]
        if c["text_kind"] == "free_text"
    }
    # Auto tier and adjudicated tier both appear among the marked fixtures.
    assert "parliamentary_speeches" in marked
    assert "product_reviews" in marked
    # Unmarked structured-text bands stay unmarked.
    unmarked = {"street_addresses", "org_directory", "inventory_codes"}
    assert not (unmarked & marked)


def test_old_shape_recording_still_auto_marks():
    """Back-compat guard: the parliamentary_speeches recording predates
    ONTA-177 (no ``text_kind`` key anywhere in the recorded REASON output),
    yet the resolved schema carries the marker — the auto tier is
    deterministic and old recordings stay valid."""
    fx = _load(FIXTURE_DIR / "parliamentary_speeches.json")
    assert "text_kind" not in json.dumps(fx["recorded"])
    marked = [
        c["column_name"]
        for c in fx["expected"]["columns"]
        if c["text_kind"] == "free_text"
    ]
    assert marked == ["speech_text"]


def test_adversarial_recordings_are_gated():
    """The profiler-proposes gate: recordings that claim ``free_text`` on
    code/number columns (inventory_codes) or on a relationship-role column
    (event_listings' city) must resolve with those columns unmarked."""
    codes = _load(FIXTURE_DIR / "inventory_codes.json")
    assert "free_text" in json.dumps(codes["recorded"])  # the claim is there…
    assert all(c["text_kind"] is None for c in codes["expected"]["columns"])

    events = _load(FIXTURE_DIR / "event_listings.json")
    city = next(
        c for c in events["expected"]["columns"] if c["column_name"] == "venue_city"
    )
    assert city["role"] == "relationship" and city["text_kind"] is None
