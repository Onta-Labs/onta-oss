"""Tests for CSV schema inference and deterministic mapping."""

import pytest

from cograph_client.resolver.csv_resolver import (
    CSVResolver,
    _rank_sample_rows,
    _safe_id,
    _snake_case,
)
from cograph_client.resolver.models import (
    ColumnMapping,
    ColumnRole,
    CSVSchemaMapping,
    EntityRelationSpec,
    EntitySpec,
)


# Three PMS-style rows: each packs a guest (Person), a reservation, and a
# property. Two reservations are for the same guest (John Smith) at the same
# property; the third is a different guest at a different property.
_PMS_ROWS = [
    {"reservation_id": "R1", "property_id": "HTL-NYC-01", "property_name": "Grand NYC",
     "check_in_date": "2026-06-01", "total_charges_usd": "1200", "status": "CHECKED_OUT",
     "guest_first_name": "John", "guest_last_name": "Smith",
     "guest_email": "john.smith@gmail.com", "guest_phone": "+1 212 555 0001"},
    {"reservation_id": "R2", "property_id": "HTL-NYC-01", "property_name": "Grand NYC",
     "check_in_date": "2026-07-01", "total_charges_usd": "800", "status": "BOOKED",
     "guest_first_name": "John", "guest_last_name": "Smith",
     "guest_email": "john.smith@gmail.com", "guest_phone": "+1 212 555 0001"},
    {"reservation_id": "R3", "property_id": "HTL-LON-01", "property_name": "London Park",
     "check_in_date": "2026-06-15", "total_charges_usd": "950", "status": "CHECKED_OUT",
     "guest_first_name": "Sara", "guest_last_name": "Khan",
     "guest_email": "sara.khan@gmail.com", "guest_phone": "+44 20 555 0002"},
]


def _pms_multi_mapping() -> CSVSchemaMapping:
    def col(name, entity, attr=None, dt="string", role=ColumnRole.ATTRIBUTE, target=None):
        return ColumnMapping(column_name=name, role=role, datatype=dt,
                             attribute_name=attr or name, target_type=target, entity=entity)
    return CSVSchemaMapping(
        entity_type="",
        entities=[
            EntitySpec(name="guest", type_name="Person", id_from=["guest_email"]),
            EntitySpec(name="reservation", type_name="Reservation", id_column="reservation_id"),
            EntitySpec(name="property", type_name="Property", id_column="property_id"),
        ],
        relationships=[
            EntityRelationSpec(subject="reservation", predicate="made_by", object="guest"),
            EntityRelationSpec(subject="reservation", predicate="at_property", object="property"),
        ],
        columns=[
            col("guest_first_name", "guest", "first_name"),
            col("guest_last_name", "guest", "last_name"),
            col("guest_email", "guest", "email"),
            col("guest_phone", "guest", "phone"),
            col("check_in_date", "reservation", "check_in_date", dt="date"),
            col("total_charges_usd", "reservation", "total_charges_usd", dt="float"),
            col("status", "reservation", "status"),
            col("property_name", "property", "name"),
        ],
    )


class TestMultiEntityMapping:
    def test_expands_one_row_into_three_types(self):
        entities, rels = CSVResolver.apply_mapping(_pms_multi_mapping(), _PMS_ROWS)
        by_type: dict[str, list] = {}
        for e in entities:
            by_type.setdefault(e.type_name, []).append(e)
        assert set(by_type) == {"Person", "Reservation", "Property"}
        # 3 reservations (one per row), 2 properties (deduped), 2 guests (deduped).
        assert len(by_type["Reservation"]) == 3
        assert len(by_type["Property"]) == 2
        assert len(by_type["Person"]) == 2

    def test_reservation_attributes_land_on_reservation(self):
        entities, _ = CSVResolver.apply_mapping(_pms_multi_mapping(), _PMS_ROWS)
        res = next(e for e in entities if e.type_name == "Reservation" and e.id == "R1")
        attr_names = {a.name for a in res.attributes}
        assert {"check_in_date", "total_charges_usd", "status"} <= attr_names
        # Guest fields must NOT be on the reservation.
        assert "email" not in attr_names and "first_name" not in attr_names

    def test_person_carries_er_signals(self):
        entities, _ = CSVResolver.apply_mapping(_pms_multi_mapping(), _PMS_ROWS)
        person = next(e for e in entities if e.type_name == "Person")
        attr_names = {a.name for a in person.attributes}
        assert {"first_name", "last_name", "email", "phone"} <= attr_names

    def test_inter_entity_edges_point_at_real_ids(self):
        entities, rels = CSVResolver.apply_mapping(_pms_multi_mapping(), _PMS_ROWS)
        res_ids = {e.id for e in entities if e.type_name == "Reservation"}
        prop_ids = {e.id for e in entities if e.type_name == "Property"}
        made_by = [r for r in rels if r.predicate == "made_by"]
        at_prop = [r for r in rels if r.predicate == "at_property"]
        assert len(made_by) == 3 and len(at_prop) == 3
        # Edge endpoints are the real entity ids (not stubs).
        assert all(r.source_id in res_ids for r in at_prop)
        assert all(r.target_id in prop_ids for r in at_prop)

    def test_property_dedup_merges_attrs_not_duplicates(self):
        entities, _ = CSVResolver.apply_mapping(_pms_multi_mapping(), _PMS_ROWS)
        nyc = [e for e in entities if e.type_name == "Property" and e.id == "HTL-NYC-01"]
        assert len(nyc) == 1
        assert any(a.value == "Grand NYC" for a in nyc[0].attributes)

    def test_skips_entity_with_missing_key(self):
        rows = _PMS_ROWS + [{"reservation_id": "R4", "property_id": "",
                             "guest_email": "x@y.com", "guest_first_name": "X"}]
        entities, rels = CSVResolver.apply_mapping(_pms_multi_mapping(), rows)
        # R4 has no property → Property not created, at_property edge skipped,
        # but the reservation + guest + made_by edge still exist.
        assert any(e.id == "R4" for e in entities if e.type_name == "Reservation")
        assert len([r for r in rels if r.predicate == "at_property"]) == 3

    def test_legacy_single_entity_unaffected(self):
        # entities=None → legacy path, byte-for-byte behavior.
        mapping = CSVSchemaMapping(
            entity_type="Listing",
            columns=[
                ColumnMapping(column_name="address", role=ColumnRole.TYPE_ID, datatype="string"),
                ColumnMapping(column_name="price", role=ColumnRole.ATTRIBUTE, datatype="integer"),
            ],
        )
        entities, _ = CSVResolver.apply_mapping(mapping, [{"address": "1 Main", "price": "500"}])
        assert len(entities) == 1 and entities[0].type_name == "Listing"


class TestSafeId:
    def test_basic(self):
        assert _safe_id("hello world") == "hello_world"

    def test_special_chars(self):
        assert _safe_id("123 Main St, #4") == "123_Main_St___4"

    def test_truncation(self):
        long = "a" * 300
        assert len(_safe_id(long)) == 200

    def test_empty(self):
        assert _safe_id("") == "unknown"


class TestSnakeCase:
    def test_basic(self):
        assert _snake_case("Hello World") == "hello_world"

    def test_camel(self):
        assert _snake_case("listingPrice") == "listingprice"

    def test_special(self):
        assert _snake_case("Bed/Bath Count") == "bed_bath_count"


class TestApplyMapping:
    def _make_mapping(self):
        return CSVSchemaMapping(
            entity_type="Property",
            columns=[
                ColumnMapping(column_name="address", role=ColumnRole.TYPE_ID, datatype="string"),
                ColumnMapping(column_name="price", role=ColumnRole.ATTRIBUTE, datatype="integer", attribute_name="price"),
                ColumnMapping(column_name="bedrooms", role=ColumnRole.ATTRIBUTE, datatype="integer", attribute_name="bedrooms"),
                ColumnMapping(column_name="city", role=ColumnRole.RELATIONSHIP, target_type="City", datatype="string", attribute_name="city"),
            ],
        )

    def test_basic_mapping(self):
        mapping = self._make_mapping()
        rows = [
            {"address": "123 Main St", "price": "500000", "bedrooms": "3", "city": "Austin"},
            {"address": "456 Oak Ave", "price": "350000", "bedrooms": "2", "city": "Dallas"},
        ]
        entities, rels = CSVResolver.apply_mapping(mapping, rows)

        # 2 property entities + 2 city stub entities
        assert len(entities) == 4
        property_entities = [e for e in entities if e.type_name == "Property"]
        city_entities = [e for e in entities if e.type_name == "City"]
        assert len(property_entities) == 2
        assert len(city_entities) == 2

        # 2 relationships (property → city)
        assert len(rels) == 2
        assert all(r.predicate == "city" for r in rels)

    def test_attributes_mapped(self):
        mapping = self._make_mapping()
        rows = [{"address": "123 Main St", "price": "500000", "bedrooms": "3", "city": "Austin"}]
        entities, _ = CSVResolver.apply_mapping(mapping, rows)

        prop = next(e for e in entities if e.type_name == "Property")
        attr_names = {a.name for a in prop.attributes}
        assert "price" in attr_names
        assert "bedrooms" in attr_names

    def test_empty_rows(self):
        mapping = self._make_mapping()
        entities, rels = CSVResolver.apply_mapping(mapping, [])
        assert entities == []
        assert rels == []

    def test_empty_id_gets_synthetic_key(self):
        # COG-51 / ADR 0003 §2 inverted this contract (formerly
        # test_skips_empty_id): an empty natural key with non-empty owned
        # values used to silently drop the row; it now mints a deterministic
        # content-hash synthetic key so the row is conserved.
        mapping = self._make_mapping()
        rows = [{"address": "", "price": "100", "bedrooms": "1", "city": "Austin"}]
        applied = CSVResolver.apply_mapping(mapping, rows)
        property_entities = [e for e in applied.entities if e.type_name == "Property"]
        assert len(property_entities) == 1
        assert applied.rows_dropped == 0
        # The synthetic id is a content hash, not derived from the empty key.
        assert property_entities[0].id != "unknown"

    def test_type_id_value_also_an_attribute(self):
        # ADR 0003 §2 "key consumed, not kept": the key column's value must
        # land as a regular attribute too, not just as URI/label material.
        mapping = self._make_mapping()
        rows = [{"address": "123 Main St", "price": "500000", "bedrooms": "3", "city": "Austin"}]
        entities, _ = CSVResolver.apply_mapping(mapping, rows)
        prop = next(e for e in entities if e.type_name == "Property")
        assert any(a.name == "address" and a.value == "123 Main St" for a in prop.attributes)

    def test_deduplicates_relationship_targets(self):
        mapping = self._make_mapping()
        rows = [
            {"address": "123 Main", "price": "500000", "bedrooms": "3", "city": "Austin"},
            {"address": "456 Oak", "price": "350000", "bedrooms": "2", "city": "Austin"},
        ]
        entities, rels = CSVResolver.apply_mapping(mapping, rows)

        city_entities = [e for e in entities if e.type_name == "City"]
        # Austin should only appear once as a stub entity
        assert len(city_entities) == 1


# --- COG-51 / ADR 0003 §2: row conservation -------------------------------
#
# Deterministic fixture mirroring the production catalog shape that exposed
# the silent-drop bug: 1000 rows, key column exactly 75% complete (production
# was 74.7%), one low-cardinality dimension column (300 distinct / 1000 rows
# = 0.3 card_ratio), one per-row-unique name column. No randomness anywhere —
# synthetic keys must reproduce across batches and re-runs.


def _catalog_rows(n: int = 1000) -> list[dict[str, str]]:
    return [
        {
            "item_key": f"KEY-{i:04d}" if i % 4 != 3 else "",
            "item_name": f"Item {i:04d}",
            "dimension_code": f"D{i % 300:03d}",
        }
        for i in range(n)
    ]


def _catalog_mapping() -> CSVSchemaMapping:
    return CSVSchemaMapping(
        entity_type="Item",
        columns=[
            ColumnMapping(column_name="item_key", role=ColumnRole.TYPE_ID,
                          datatype="string", attribute_name="item_key"),
            ColumnMapping(column_name="item_name", role=ColumnRole.ATTRIBUTE,
                          datatype="string", attribute_name="item_name"),
            ColumnMapping(column_name="dimension_code", role=ColumnRole.ATTRIBUTE,
                          datatype="string", attribute_name="dimension_code"),
        ],
    )


class TestRowConservationSingleEntity:
    """Input rows are never silently dropped (ADR 0003 §2, single-entity path)."""

    def test_catalog_shape_conserves_all_1000_rows(self):
        applied = CSVResolver.apply_mapping(_catalog_mapping(), _catalog_rows(1000))
        items = [e for e in applied.entities if e.type_name == "Item"]
        assert len(items) == 1000
        assert applied.rows_in == 1000
        assert applied.rows_dropped == 0
        assert applied.drops_by_entity == {}

    def test_key_attribute_present_exactly_when_source_value_exists(self):
        applied = CSVResolver.apply_mapping(_catalog_mapping(), _catalog_rows(1000))
        items = [e for e in applied.entities if e.type_name == "Item"]
        with_key = [e for e in items if any(a.name == "item_key" for a in e.attributes)]
        # Exactly the 750 rows whose key cell is non-empty carry the attribute.
        assert len(with_key) == 750
        keyed = next(e for e in items if e.id == "KEY-0000")
        assert any(a.name == "item_key" and a.value == "KEY-0000" for a in keyed.attributes)

    def test_two_batch_ingest_produces_identical_ids(self):
        rows = _catalog_rows(1000)
        single = CSVResolver.apply_mapping(_catalog_mapping(), rows)
        batch1 = CSVResolver.apply_mapping(_catalog_mapping(), rows[:500])
        batch2 = CSVResolver.apply_mapping(_catalog_mapping(), rows[500:])
        batched_ids = [e.id for e in batch1.entities] + [e.id for e in batch2.entities]
        assert [e.id for e in single.entities] == batched_ids
        # Re-running the same batch is idempotent.
        again = CSVResolver.apply_mapping(_catalog_mapping(), rows[:500])
        assert [e.id for e in again.entities] == [e.id for e in batch1.entities]

    def test_identical_keyless_rows_collapse_to_one_id(self):
        dup = {"item_key": "", "item_name": "Same", "dimension_code": "D001"}
        applied = CSVResolver.apply_mapping(_catalog_mapping(), [dict(dup), dict(dup)])
        # Content-hash determinism makes true duplicates share one id — the
        # graph collapses them on URI. Neither row is dropped.
        assert len({e.id for e in applied.entities}) == 1
        assert applied.rows_dropped == 0

    def test_all_empty_row_skipped_and_accounted(self):
        rows = [
            {"item_key": "KEY-1", "item_name": "One", "dimension_code": "D1"},
            {"item_key": "", "item_name": "", "dimension_code": ""},
            {"item_key": "  ", "item_name": " ", "dimension_code": ""},  # whitespace = empty
        ]
        applied = CSVResolver.apply_mapping(_catalog_mapping(), rows)
        assert len(applied.entities) == 1
        assert applied.rows_in == 3
        assert applied.rows_dropped == 2
        assert applied.drops_by_entity == {"Item": 2}

    def test_unmapped_columns_do_not_affect_synthetic_key(self):
        # Only owned (mapped) columns feed the content hash.
        base = {"item_key": "", "item_name": "Same", "dimension_code": "D1"}
        with_extra = dict(base, unmapped_noise="zzz")
        a = CSVResolver.apply_mapping(_catalog_mapping(), [base])
        b = CSVResolver.apply_mapping(_catalog_mapping(), [with_extra])
        assert a.entities[0].id == b.entities[0].id

    def test_legacy_tuple_unpacking_still_works(self):
        # AppliedMapping iterates as the legacy (entities, relationships) pair.
        entities, rels = CSVResolver.apply_mapping(_catalog_mapping(), _catalog_rows(8))
        assert isinstance(entities, list) and isinstance(rels, list)
        assert len(entities) == 8


class TestRowConservationMultiEntity:
    """ADR 0003 §2 invariants on the multi-entity path."""

    def test_synthetic_key_when_natural_key_empty_but_attrs_present(self):
        rows = _PMS_ROWS + [
            {"reservation_id": "", "property_id": "HTL-PAR-01", "property_name": "Paris Centre",
             "check_in_date": "2026-08-01", "total_charges_usd": "640", "status": "BOOKED",
             "guest_first_name": "Ana", "guest_last_name": "Lima",
             "guest_email": "ana.lima@gmail.com", "guest_phone": "+33 1 555 0003"},
        ]
        applied = CSVResolver.apply_mapping(_pms_multi_mapping(), rows)
        reservations = [e for e in applied.entities if e.type_name == "Reservation"]
        # The keyless reservation is conserved under a synthetic key.
        assert len(reservations) == 4
        assert applied.rows_dropped == 0
        synthetic = next(e for e in reservations if e.id not in {"R1", "R2", "R3"})
        # Edges reference the synthetic id (no orphaned endpoints).
        made_by = [r for r in applied.relationships if r.predicate == "made_by"]
        assert any(r.source_id == synthetic.id for r in made_by)
        # Deterministic: re-applying the same rows mints the same ids.
        again = CSVResolver.apply_mapping(_pms_multi_mapping(), rows)
        assert {e.id for e in again.entities} == {e.id for e in applied.entities}

    def test_key_column_value_emitted_as_attribute(self):
        applied = CSVResolver.apply_mapping(_pms_multi_mapping(), _PMS_ROWS)
        res = next(e for e in applied.entities if e.type_name == "Reservation" and e.id == "R1")
        assert any(a.name == "reservation_id" and a.value == "R1" for a in res.attributes)
        prop = next(e for e in applied.entities if e.type_name == "Property" and e.id == "HTL-NYC-01")
        assert any(a.name == "property_id" and a.value == "HTL-NYC-01" for a in prop.attributes)

    def test_id_column_also_mapped_as_column_not_duplicated(self):
        # When the id_column is routed to its entity as a regular column, the
        # value appears exactly once, under the mapped attribute name.
        mapping = _pms_multi_mapping()
        mapping.columns.append(ColumnMapping(
            column_name="reservation_id", role=ColumnRole.ATTRIBUTE,
            datatype="string", attribute_name="confirmation_code", entity="reservation",
        ))
        applied = CSVResolver.apply_mapping(mapping, _PMS_ROWS)
        res = next(e for e in applied.entities if e.type_name == "Reservation" and e.id == "R1")
        hits = [a for a in res.attributes if a.value == "R1"]
        assert [a.name for a in hits] == ["confirmation_code"]

    def test_all_empty_entity_counted_but_row_not_dropped(self):
        # R4 mints a reservation + guest but its property is all-empty: the
        # skipped property instance is accounted, the row is NOT dropped.
        rows = _PMS_ROWS + [{"reservation_id": "R4", "property_id": "",
                             "guest_email": "x@y.com", "guest_first_name": "X"}]
        applied = CSVResolver.apply_mapping(_pms_multi_mapping(), rows)
        assert applied.rows_dropped == 0
        assert applied.drops_by_entity == {"property": 1}

    def test_fully_empty_row_dropped_and_counted(self):
        empty = {k: "" for k in _PMS_ROWS[0]}
        applied = CSVResolver.apply_mapping(_pms_multi_mapping(), _PMS_ROWS + [empty])
        assert applied.rows_in == 4
        assert applied.rows_dropped == 1
        # Every entity spec was all-empty on that row.
        assert applied.drops_by_entity == {"guest": 1, "reservation": 1, "property": 1}


class TestRankSampleRows:
    def test_picks_dense_rows_first(self):
        sparse = [{"slug": f"s{i}", "url": f"u{i}", "name": "", "bio": "", "email": ""} for i in range(12)]
        dense = [
            {"slug": f"d{i}", "url": f"u{i}", "name": f"n{i}", "bio": f"b{i}", "email": f"e{i}@x"}
            for i in range(5)
        ]
        ranked = _rank_sample_rows(sparse + dense)
        # All 5 dense rows should land in the top 5
        assert all(r["name"] != "" for r in ranked[:5])

    def test_stable_on_ties(self):
        rows = [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}, {"a": "5", "b": "6"}]
        ranked = _rank_sample_rows(rows)
        # All scored equally — order preserved
        assert ranked == rows

    def test_does_not_mutate_input(self):
        rows = [{"a": ""}, {"a": "x"}]
        original = list(rows)
        _rank_sample_rows(rows)
        assert rows == original

    def test_treats_whitespace_as_empty(self):
        rows = [{"a": "   ", "b": ""}, {"a": "x", "b": "y"}]
        ranked = _rank_sample_rows(rows)
        assert ranked[0]["a"] == "x"

    def test_handles_none_values(self):
        rows = [{"a": None, "b": None}, {"a": "x", "b": "y"}]
        ranked = _rank_sample_rows(rows)
        assert ranked[0]["a"] == "x"


class TestInferSchemaRetry:
    @pytest.mark.asyncio
    async def test_retries_on_validation_error(self, monkeypatch):
        from unittest.mock import AsyncMock

        resolver = CSVResolver(client=None, openrouter_key="")
        valid_data = {
            "entity_type": "Mentor",
            "columns": [
                {"column_name": "slug", "role": "type_id", "datatype": "string"},
                {"column_name": "name", "role": "attribute", "datatype": "string", "attribute_name": "name"},
            ],
        }
        # First call: malformed key shape (raises KeyError in _build_mapping); second: valid
        bad_data = {"entity_type_oops": "Mentor", "columns": []}

        call_log: list[float] = []

        async def fake_call(user_content: str, temperature: float = 0.0):
            call_log.append(temperature)
            return bad_data if len(call_log) == 1 else valid_data

        monkeypatch.setattr(resolver, "_call_llm", fake_call)

        mapping = await resolver.infer_schema(
            headers=["slug", "name"],
            sample_rows=[{"slug": "s", "name": "n"}],
            existing_types={},
            total_rows=1,
        )
        assert mapping.entity_type == "Mentor"
        assert call_log == [0.0, 0.3]

    @pytest.mark.asyncio
    async def test_propagates_when_retry_also_fails(self, monkeypatch):
        resolver = CSVResolver(client=None, openrouter_key="")
        bad_data = {"entity_type_oops": "Mentor", "columns": []}

        async def fake_call(user_content: str, temperature: float = 0.0):
            return bad_data

        monkeypatch.setattr(resolver, "_call_llm", fake_call)

        with pytest.raises(KeyError):
            await resolver.infer_schema(
                headers=["slug"],
                sample_rows=[{"slug": "s"}],
                existing_types={},
                total_rows=1,
            )


class TestBatchedInsertTriples:
    def test_batching(self):
        from cograph_client.graph.queries import batched_insert_triples

        triples = [(f"s{i}", "p", "o") for i in range(1200)]
        batches = batched_insert_triples("https://g", triples, batch_size=500)
        assert len(batches) == 3  # 500 + 500 + 200
        assert "INSERT DATA" in batches[0]

    def test_empty(self):
        from cograph_client.graph.queries import batched_insert_triples
        assert batched_insert_triples("https://g", []) == []

    def test_small(self):
        from cograph_client.graph.queries import batched_insert_triples
        triples = [("s", "p", "o")]
        batches = batched_insert_triples("https://g", triples)
        assert len(batches) == 1
