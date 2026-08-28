"""Blueprint manifest protocol — frozen v1 (INF-563) + seed (INF-566) + export (INF-565).

A Blueprint is the **means** to acquire and maintain a domain graph. It is
not the graph. This package is the inspectable schema, validator, the
Clinical Trials seed under ``seeds/clinical-trials/``, and the workspace →
directory export path.

Range classification reuses ``classify_attr_range``. Export reads the
ontology slice through ``graph/ontology_queries.schema_types_for_kg``.
The public loader (``load_blueprint_package``) is the PyYAML path so
human-authored seed YAML and export-written YAML both parse. Install is
a later ticket.

Boundary: OSS protocol. Stdlib + pydantic + PyYAML + ``infona_client.*``
only — no ``from infona.*``. Not a hosted registry.
"""

from infona_client.blueprint.models import (
    ALLOWED_TOP_LEVEL_KEYS,
    FORBIDDEN_TOP_LEVEL_KEYS,
    SCHEMA_STATUS,
    SCHEMA_VERSION,
    SAMPLE_MAX_BYTES,
    SAMPLE_MAX_ENTITIES,
    BlueprintManifest,
    dumps_blueprint,
    parse_blueprint,
)
from infona_client.blueprint.semver import (
    ACQUISITION_REVISION_CHANGES,
    SEMVER_MAJOR,
    SEMVER_MINOR,
    SEMVER_PATCH,
    classify_manifest_change,
)
from infona_client.blueprint.load import (
    find_manifest,
    load_blueprint_package,
    validate_blueprint_package,
)
from infona_client.blueprint.validate import validate_blueprint
from infona_client.blueprint.export import (
    BlueprintExport,
    ExportOptions,
    export_blueprint,
)
from infona_client.blueprint.package import (
    dumps_blueprint_yaml,
    write_blueprint_package,
)
from infona_client.blueprint.redact import ExportRedactionError

__all__ = [
    "ALLOWED_TOP_LEVEL_KEYS",
    "FORBIDDEN_TOP_LEVEL_KEYS",
    "SCHEMA_STATUS",
    "SCHEMA_VERSION",
    "SAMPLE_MAX_BYTES",
    "SAMPLE_MAX_ENTITIES",
    "ACQUISITION_REVISION_CHANGES",
    "SEMVER_MAJOR",
    "SEMVER_MINOR",
    "SEMVER_PATCH",
    "BlueprintManifest",
    "classify_manifest_change",
    "dumps_blueprint",
    "find_manifest",
    "load_blueprint_package",
    "parse_blueprint",
    "validate_blueprint",
    "validate_blueprint_package",
    "BlueprintExport",
    "ExportOptions",
    "ExportRedactionError",
    "dumps_blueprint_yaml",
    "export_blueprint",
    "write_blueprint_package",
]
