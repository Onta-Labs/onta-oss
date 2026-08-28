"""Blueprint manifest protocol — frozen v1 (INF-563) + seed package (INF-566).

A Blueprint is the **means** to acquire and maintain a domain graph. It is
not the graph. This package is the inspectable schema + validator, plus the
Clinical Trials seed under ``seeds/clinical-trials/``.

It is **not** an ontology reader and does not apply, export, or install
(INF-565). Range classification and type/attribute leaf checks call the
existing catalog helpers (``classify_attr_range``, ``_validate_type_leaf``,
``_validate_attr_leaf``). Do not add a graph-read path here.

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
]
