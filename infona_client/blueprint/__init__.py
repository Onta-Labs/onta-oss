"""Blueprint protocol — validator, export, install, fork, overlay (INF-563 / 565 / 575 / 579 / 578).

A Blueprint is the **means** to acquire and maintain a domain graph. It is
not the graph. This package is the inspectable schema, the Clinical
Trials seed, workspace → directory export, install / inspect /
uninstall, fork / lineage, private overlay / non-clobber update, and
post-install first-run (credentials → acquire → first answer).
Range checks reuse catalog helpers — do not add a second ontology reader.

Boundary: OSS protocol. Stdlib + pydantic + PyYAML + ``infona_client.*``
only — no ``from infona.*``. Not a hosted registry. BYOK only.
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
from infona_client.blueprint.install import (
    inspect_blueprint,
    install_blueprint,
    list_installed_blueprints,
    uninstall_blueprint,
)
from infona_client.blueprint.first_run import (
    acquire_condition_set,
    run_first_run,
)
from infona_client.blueprint.fork import fork_blueprint
from infona_client.blueprint.layer import extend_blueprint, update_blueprint

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
    "extend_blueprint",
    "find_manifest",
    "fork_blueprint",
    "acquire_condition_set",
    "inspect_blueprint",
    "install_blueprint",
    "list_installed_blueprints",
    "run_first_run",
    "load_blueprint_package",
    "parse_blueprint",
    "uninstall_blueprint",
    "update_blueprint",
    "validate_blueprint",
    "validate_blueprint_package",
    "BlueprintExport",
    "ExportOptions",
    "ExportRedactionError",
    "dumps_blueprint_yaml",
    "export_blueprint",
    "write_blueprint_package",
]
