"""No TYPE_ID mapping mints synthetic keys instead of dropping the table."""

from infona_client.resolver.csv_helpers import _synthetic_key
from infona_client.resolver.csv_resolver import CSVResolver
from infona_client.resolver.models import ColumnMapping, ColumnRole, CSVSchemaMapping


def _value_only_mapping() -> CSVSchemaMapping:
    return CSVSchemaMapping(
        entity_type="LookupRow",
        columns=[
            ColumnMapping(
                column_name="Type", role=ColumnRole.ATTRIBUTE,
                datatype="string", attribute_name="type",
            ),
            ColumnMapping(
                column_name="Value", role=ColumnRole.ATTRIBUTE,
                datatype="string", attribute_name="value",
            ),
            ColumnMapping(
                column_name="Label", role=ColumnRole.ATTRIBUTE,
                datatype="string", attribute_name="label",
            ),
        ],
    )


def test_no_type_id_mints_one_entity_per_valued_row():
    mapping = _value_only_mapping()
    rows = [
        {"Type": "email", "Value": "a@x.test", "Label": "Work"},
        {"Type": "phone", "Value": "555-0100", "Label": "Mobile"},
        {"Type": "web", "Value": "https://ex.test", "Label": "Site"},
    ]
    applied = CSVResolver.apply_mapping(mapping, rows)
    ents = [e for e in applied.entities if e.type_name == "LookupRow"]
    assert len(ents) == 3
    assert applied.rows_dropped == 0
    assert applied.rows_in == 3
    ids = [e.id for e in ents]
    assert len(set(ids)) == 3
    again = CSVResolver.apply_mapping(mapping, rows)
    assert [e.id for e in again.entities] == ids
    owned0 = {"Type": "email", "Value": "a@x.test", "Label": "Work"}
    assert ents[0].id == _synthetic_key("LookupRow", owned0)


def test_no_type_id_empty_row_dropped_and_counted():
    mapping = _value_only_mapping()
    rows = [
        {"Type": "email", "Value": "a@x.test", "Label": "Work"},
        {"Type": "", "Value": "", "Label": ""},
        {"Type": "phone", "Value": "555-0100", "Label": "Mobile"},
    ]
    applied = CSVResolver.apply_mapping(mapping, rows)
    ents = [e for e in applied.entities if e.type_name == "LookupRow"]
    assert len(ents) == 2
    assert applied.rows_dropped == 1
    assert applied.drops_by_entity == {"LookupRow": 1}
