"""Blueprint manifest protocol (INF-563, INF-587).

OSS: the format spec, the v1-frozen schema, and the validator (ADR 0014).
Canonical package is a directory of UTF-8 plain text with ``blueprint.yaml``
at the root. Hosted registry, private extension layers, entitlement, and
paid source bindings are premium. No ``from infona.*``.
"""

from infona_client.blueprints.sample import (
    feeds_freshness_panel,
    sample_section_label,
    surface_label,
)
from infona_client.blueprints.schema import (
    SCHEMA_VERSION,
    UNREPRESENTABLE_FIELD_NAMES,
    BlueprintManifest,
    Sample,
    frozen_json_schema,
)
from infona_client.blueprints.load import load_package, load_sample_section
from infona_client.blueprints.validate import (
    BlueprintValidationError,
    dump_manifest,
    validate_manifest,
    validate_sample,
)
from infona_client.blueprints.versioning import ChangeReport, VersionBump, classify_change

__all__ = [
    "SCHEMA_VERSION",
    "UNREPRESENTABLE_FIELD_NAMES",
    "BlueprintManifest",
    "BlueprintValidationError",
    "ChangeReport",
    "Sample",
    "VersionBump",
    "classify_change",
    "dump_manifest",
    "feeds_freshness_panel",
    "frozen_json_schema",
    "load_package",
    "load_sample_section",
    "sample_section_label",
    "surface_label",
    "validate_manifest",
    "validate_sample",
]
