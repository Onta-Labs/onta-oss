"""ONTA-462 / WS5 — mechanism-level discovery quality regression fixtures.

Hard rules (plan v2, anti-overfit):

* Fixtures test **properties** (role inversion, identity merge, sibling
  non-merge), never "ElevenLabs must be dropped" as the sole assertion.
* Production code under ``infona_client/pipeline/`` and the ensemble skip path
  must not contain incident brand strings as code literals. Tests and fixtures
  are exempt.
* Multi-domain synthetic fixtures are required; the incident TTS batch is an
  optional secondary fixture labeled by structural expect rules.

Integrity + denylist-grep always run. Mechanism execution tests use
``importorskip`` (or soft skip) for modules not yet merged (WS1 / WS2).
"""

from __future__ import annotations

import importlib
import io
import json
import pathlib
import re
import tokenize
from typing import Any, Callable, Optional

import infona_client
import pytest

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

_TESTS_ROOT = pathlib.Path(__file__).resolve().parent
_FIXTURES = _TESTS_ROOT / "fixtures" / "discovery"
_ROLE_FIXTURE = _FIXTURES / "role_inversion_multi_domain.json"
_IDENTITY_FIXTURE = _FIXTURES / "identity_catalog_surface.json"

_PKG_ROOT = pathlib.Path(infona_client.__file__).resolve().parent
_PIPELINE_DIR = _PKG_ROOT / "pipeline"
# Ensemble skip / provider-scope paths (R3 wiring lives here; guard both).
_ENSEMBLE_PATHS = (
    _PKG_ROOT / "agent" / "capabilities" / "web_ingest_cap.py",
    _PKG_ROOT / "web_sources" / "base.py",
)

# Incident brand strings that must never appear as production code literals in
# the discovery quality / ensemble skip surface. Tests and fixtures exempt.
# Keep multi-token / distinctive forms so legitimate prose like "play" does not
# trip the guard.
_INCIDENT_BRAND_LITERALS: tuple[str, ...] = (
    "ElevenLabs",
    "PlayHT",
    "Play.ht",
    "play.ht",
    "Cartesia",
    "cartesia.ai",
)

# Minimum multi-domain coverage required by WS5.
_MIN_ROLE_DOMAINS = 3
_MIN_IDENTITY_DOMAINS = 3


# --------------------------------------------------------------------------- #
# Structural helpers (fixture integrity + property oracles)
# --------------------------------------------------------------------------- #


def _alnum_norm(value: object) -> str:
    """Casefold + strip non-alnum — matches the plan's surface/role identity norm."""
    s = re.sub(r"\s+", " ", str(value if value is not None else "")).strip()
    if not s:
        return ""
    return re.sub(r"[^a-z0-9]+", "", s.casefold())


def _is_catalog_path(key: object) -> bool:
    """True when key looks like ``segment/segment…`` (one or more ``/``, non-empty segs).

    Leading ``@scope/pkg`` (npm) counts: strip a single leading ``@`` then require
    at least two non-empty path segments.
    """
    raw = str(key if key is not None else "").strip()
    if not raw or "/" not in raw:
        return False
    body = raw[1:] if raw.startswith("@") and "/" in raw[1:] else raw
    parts = [p for p in body.split("/") if p.strip()]
    return len(parts) >= 2


def _slug_tail(key: object) -> str:
    raw = str(key if key is not None else "").strip()
    if not raw:
        return ""
    return raw.rsplit("/", 1)[-1]


def _surface_matches_catalog(surface: object, catalog: object) -> bool:
    """Alnum-normalized free-text equals alnum-normalized catalog slug tail."""
    return bool(_alnum_norm(surface)) and _alnum_norm(surface) == _alnum_norm(
        _slug_tail(catalog)
    )


def _load_json(path: pathlib.Path) -> dict:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    assert isinstance(data, dict), f"{path.name}: root must be object"
    return data


def _role_values_in_batch(
    rows: list[dict],
    role_attrs: list[str],
    key_attr: str,
) -> set[str]:
    """Normalized values of role-like attributes across the batch (not the key alone)."""
    out: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        for attr in role_attrs:
            if attr == key_attr:
                continue
            val = row.get(attr)
            n = _alnum_norm(val)
            if n:
                out.add(n)
    return out


def _structural_role_drop_keys(
    rows: list[dict],
    key_attr: str,
    role_attrs: list[str],
) -> set[str]:
    """Keys that the R2 property says must drop: key equals some other row's role value.

    A row is a drop candidate when its own key (alnum-norm) appears as a role-attr
    value on *any* row (typically a richer instance), and it lacks a stronger
    catalog-path identity form than the evidence row. We approximate "stronger"
    as: drop when the key is NOT a catalog path (or when name == provider only).
    """
    role_vals = _role_values_in_batch(rows, role_attrs, key_attr)
    drops: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = row.get(key_attr)
        if key is None or key == "":
            continue
        kn = _alnum_norm(key)
        if not kn or kn not in role_vals:
            continue
        # Catalog-path keys that also equal a role value are rare; keep them if
        # they look like instances (have non-role filled attrs beyond name/provider).
        if _is_catalog_path(key):
            continue
        drops.add(str(key))
    return drops


def _kept_keys_after_structural_role_drop(
    rows: list[dict],
    key_attr: str,
    role_attrs: list[str],
) -> set[str]:
    drops = {_alnum_norm(k) for k in _structural_role_drop_keys(rows, key_attr, role_attrs)}
    kept: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = row.get(key_attr)
        if key is None or key == "":
            continue
        if _alnum_norm(key) in drops:
            continue
        kept.add(str(key))
    return kept


# --------------------------------------------------------------------------- #
# Fixture integrity (always green)
# --------------------------------------------------------------------------- #


def test_role_inversion_fixture_exists_and_multi_domain():
    data = _load_json(_ROLE_FIXTURE)
    batches = data.get("batches")
    assert isinstance(batches, list) and batches, "role fixture needs batches"
    domains = {b.get("domain") for b in batches if isinstance(b, dict)}
    # Optional incident domain does not count toward the multi-domain floor.
    non_optional = {
        b.get("domain")
        for b in batches
        if isinstance(b, dict) and not b.get("optional")
    }
    assert len(non_optional) >= _MIN_ROLE_DOMAINS, (
        f"need ≥{_MIN_ROLE_DOMAINS} non-optional domains, got {sorted(non_optional)}"
    )
    assert domains, "domains must be non-empty"


def test_identity_fixture_exists_and_multi_domain():
    data = _load_json(_IDENTITY_FIXTURE)
    domains = data.get("domains")
    assert isinstance(domains, list) and domains
    non_optional = [
        d for d in domains if isinstance(d, dict) and not d.get("optional")
    ]
    assert len(non_optional) >= _MIN_IDENTITY_DOMAINS, (
        f"need ≥{_MIN_IDENTITY_DOMAINS} non-optional identity domains, "
        f"got {len(non_optional)}"
    )
    names = {d.get("domain") for d in non_optional}
    # Plan: models, packages, datasets (or equivalents) — at least three labels.
    assert len(names) >= _MIN_IDENTITY_DOMAINS


@pytest.mark.parametrize(
    "batch",
    _load_json(_ROLE_FIXTURE)["batches"],
    ids=lambda b: b.get("id", "batch"),
)
def test_role_fixture_batch_structure(batch: dict):
    assert batch.get("id"), "batch id required"
    assert batch.get("domain"), "batch domain required"
    key_attr = batch.get("key_attr") or "name"
    role_attrs = batch.get("role_attrs") or []
    assert isinstance(role_attrs, list) and role_attrs, f"{batch['id']}: role_attrs"
    rows = batch.get("rows") or []
    assert isinstance(rows, list) and len(rows) >= 2, f"{batch['id']}: need ≥2 rows"
    for row in rows:
        assert isinstance(row, dict) and key_attr in row

    expect = batch.get("expect_structural") or {}
    assert expect.get("role_drop_rule"), f"{batch['id']}: role_drop_rule required"
    must_drop = list(expect.get("must_drop_keys") or [])
    must_keep = list(expect.get("must_keep_keys") or [])
    # Structural consistency: fixture labels must match the property oracle.
    oracle_drops = _structural_role_drop_keys(rows, key_attr, role_attrs)
    oracle_drop_norm = {_alnum_norm(k) for k in oracle_drops}
    for k in must_drop:
        assert _alnum_norm(k) in oracle_drop_norm, (
            f"{batch['id']}: must_drop_keys entry {k!r} is not a structural "
            f"role-inversion drop (oracle={sorted(oracle_drops)})"
        )
    oracle_kept = _kept_keys_after_structural_role_drop(rows, key_attr, role_attrs)
    oracle_kept_norm = {_alnum_norm(k) for k in oracle_kept}
    for k in must_keep:
        assert _alnum_norm(k) in oracle_kept_norm, (
            f"{batch['id']}: must_keep_keys entry {k!r} would be dropped by "
            f"structural rule (kept={sorted(oracle_kept)})"
        )
    # Property: must_drop ∩ must_keep is empty (by alnum-norm).
    assert not ({_alnum_norm(k) for k in must_drop} & {_alnum_norm(k) for k in must_keep})


@pytest.mark.parametrize(
    "domain",
    _load_json(_IDENTITY_FIXTURE)["domains"],
    ids=lambda d: d.get("domain", "domain"),
)
def test_identity_fixture_domain_structure(domain: dict):
    assert domain.get("domain")
    key_attr = domain.get("key_attr") or "name"
    merges = domain.get("merge_pairs") or []
    non_merges = domain.get("non_merge_pairs") or []
    assert merges or non_merges, f"{domain['domain']}: need merge and/or non-merge pairs"

    for pair in merges:
        rows = pair.get("rows") or []
        assert len(rows) >= 2, f"{pair.get('id')}: merge pair needs ≥2 rows"
        assert pair.get("expect_merge") is True
        catalog = pair.get("catalog_key")
        surface = pair.get("surface_key")
        if catalog and surface:
            assert _is_catalog_path(catalog), (
                f"{pair.get('id')}: catalog_key must be catalog-path form"
            )
            assert _surface_matches_catalog(surface, catalog), (
                f"{pair.get('id')}: surface {surface!r} must match slug tail of "
                f"{catalog!r} under alnum-norm"
            )
        # At least one row should carry catalog-path identity.
        keys = [r.get(key_attr) for r in rows if isinstance(r, dict)]
        assert any(_is_catalog_path(k) for k in keys), (
            f"{pair.get('id')}: merge pair needs a catalog-path key"
        )

    for pair in non_merges:
        rows = pair.get("rows") or []
        assert len(rows) >= 2, f"{pair.get('id')}: non-merge pair needs ≥2 rows"
        assert pair.get("expect_merge") is False
        keys = [str(r.get(key_attr)) for r in rows if isinstance(r, dict)]
        # Sibling catalog paths: if both are catalog paths, tails must differ OR
        # full paths differ (already true if two distinct strings).
        if all(_is_catalog_path(k) for k in keys):
            assert len(set(keys)) == len(keys), (
                f"{pair.get('id')}: non-merge catalog paths must be distinct"
            )


def test_role_fixture_property_not_brand_only():
    """No batch may assert drops solely via incident brands without structural rule."""
    data = _load_json(_ROLE_FIXTURE)
    for batch in data["batches"]:
        expect = batch.get("expect_structural") or {}
        assert "role_drop_rule" in expect
        # Incident batch must declare the general property.
        if batch.get("id") == "incident_tts_structural" or batch.get("optional"):
            prop = expect.get("property") or expect.get("role_drop_rule")
            assert prop, f"{batch['id']}: optional/incident needs structural property"
            assert "elevenlabs" not in (expect.get("role_drop_rule") or "").casefold()


def test_incident_batch_uses_structural_not_sole_brand_assert():
    """Incident rows OK only when must_drop is explained by role-value equality."""
    data = _load_json(_ROLE_FIXTURE)
    batch = next(
        (b for b in data["batches"] if b.get("id") == "incident_tts_structural"),
        None,
    )
    if batch is None:
        pytest.skip("optional incident batch not present")
    key_attr = batch["key_attr"]
    role_attrs = batch["role_attrs"]
    rows = batch["rows"]
    role_vals = _role_values_in_batch(rows, role_attrs, key_attr)
    for k in batch["expect_structural"]["must_drop_keys"]:
        assert _alnum_norm(k) in role_vals, (
            f"incident must_drop {k!r} must equal another row's role value "
            f"(structural), not a brand denylist"
        )
    # Positive side of the property: every kept key must NOT equal a role value
    # that appears on a different instance (or if it does, it is catalog-path).
    for k in batch["expect_structural"]["must_keep_keys"]:
        if _alnum_norm(k) in role_vals:
            assert _is_catalog_path(k), (
                f"kept key {k!r} equals a role value but is not catalog-path"
            )


def test_fixture_counts_are_nontrivial():
    """WS5 deliverable: reportable multi-domain depth (used by ship notes too)."""
    role = _load_json(_ROLE_FIXTURE)
    ident = _load_json(_IDENTITY_FIXTURE)
    n_role_batches = len(role["batches"])
    n_role_rows = sum(len(b.get("rows") or []) for b in role["batches"])
    n_merge = sum(
        len(d.get("merge_pairs") or []) for d in ident["domains"]
    )
    n_non_merge = sum(
        len(d.get("non_merge_pairs") or []) for d in ident["domains"]
    )
    assert n_role_batches >= 5
    assert n_role_rows >= 20
    assert n_merge >= 5
    assert n_non_merge >= 4


# --------------------------------------------------------------------------- #
# Denylist grep guard (always runs)
# --------------------------------------------------------------------------- #


def _strip_comments_preserve_strings(src: str) -> str:
    """Blank ``#`` comment spans; keep string literals for literal scanning."""
    lines = src.splitlines(keepends=True)
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return src
    for tok in toks:
        if tok.type != tokenize.COMMENT:
            continue
        (srow, scol), (erow, ecol) = tok.start, tok.end
        if srow == erow:
            line = lines[srow - 1]
            lines[srow - 1] = line[:scol] + (" " * (ecol - scol)) + line[ecol:]
        else:
            # Multi-line comments are rare for ``#``; blank remaining span.
            for r in range(srow - 1, erow):
                if r == srow - 1:
                    lines[r] = lines[r][:scol] + "\n"
                elif r == erow - 1:
                    lines[r] = (" " * ecol) + lines[r][ecol:]
                else:
                    lines[r] = "\n"
    return "".join(lines)


def _string_literals(src: str) -> list[str]:
    """Return decoded string-literal values from Python source."""
    import ast

    out: list[str] = []
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return out
    for tok in toks:
        if tok.type != tokenize.STRING:
            continue
        try:
            out.append(ast.literal_eval(tok.string))
        except (ValueError, SyntaxError):
            # Fall back to raw token text for scanning.
            out.append(tok.string)
    return out


def _production_py_files() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    if _PIPELINE_DIR.is_dir():
        files.extend(sorted(_PIPELINE_DIR.rglob("*.py")))
    for p in _ENSEMBLE_PATHS:
        if p.is_file():
            files.append(p)
    return files


def _brand_hits_in_file(path: pathlib.Path) -> list[str]:
    """Return brand literals found as string tokens or bare identifiers in prod code.

    Comments are stripped so narrative docs in comments do not fail the guard;
    string *literals* (the actual denylist mechanism) still fail.
    """
    raw = path.read_text(encoding="utf-8")
    cleaned = _strip_comments_preserve_strings(raw)
    hits: list[str] = []
    # 1) String literals (ast-decoded when possible).
    for lit in _string_literals(cleaned):
        for brand in _INCIDENT_BRAND_LITERALS:
            if brand.casefold() in str(lit).casefold():
                hits.append(f"string-literal contains {brand!r}")
    # 2) Bare code tokens that spell a brand (e.g. set membership on names).
    #    Word-boundary match on comment-stripped source, excluding string contents
    #    already covered above: scan identifier-like runs only.
    code_wo_strings = cleaned
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(cleaned).readline))
        chunks: list[str] = []
        for tok in toks:
            if tok.type == tokenize.STRING:
                chunks.append(" " * (tok.end[1] - tok.start[1] if tok.start[0] == tok.end[0] else 1))
            else:
                chunks.append(tok.string)
        code_wo_strings = "".join(chunks) if chunks else cleaned
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    for brand in _INCIDENT_BRAND_LITERALS:
        # Escape for regex; treat as literal substring with non-alnum boundaries
        # so "PlayHT" matches as a whole token-ish run.
        pat = re.compile(
            rf"(?<![A-Za-z0-9_]){re.escape(brand)}(?![A-Za-z0-9_])",
            re.IGNORECASE,
        )
        if pat.search(code_wo_strings):
            hits.append(f"code token {brand!r}")
    return hits


def test_production_pipeline_has_no_incident_brand_literals():
    """Deny-by-default: no TTS/incident brand strings in pipeline / ensemble skip."""
    files = _production_py_files()
    assert files, "expected production pipeline/ensemble files to scan"
    violations: list[str] = []
    for path in files:
        rel = path.relative_to(_PKG_ROOT)
        for hit in _brand_hits_in_file(path):
            violations.append(f"{rel}: {hit}")
    assert not violations, (
        "Incident brand strings must not appear as production code literals "
        "in infona_client/pipeline/ or ensemble skip paths "
        f"(ONTA-462 denylist guard). Hits:\n  - "
        + "\n  - ".join(violations)
    )


def test_denylist_guard_flags_planted_brand_literal(tmp_path: pathlib.Path):
    """Planted-violation self-test so the guard cannot rot into a no-op."""
    planted = tmp_path / "planted_denylist.py"
    planted.write_text(
        'BRANDS = {"ElevenLabs", "Cartesia"}\n'
        'if name in BRANDS:\n'
        "    drop = True\n",
        encoding="utf-8",
    )
    hits = _brand_hits_in_file(planted)
    assert hits, "guard must detect planted ElevenLabs/Cartesia string literals"
    assert any("ElevenLabs" in h or "Cartesia" in h for h in hits)


def test_denylist_guard_ignores_comments_only(tmp_path: pathlib.Path):
    """Comments mentioning brands are not production denylist logic."""
    planted = tmp_path / "comment_only.py"
    planted.write_text(
        "# Incident motivation mentioned ElevenLabs / Cartesia only in narrative.\n"
        "def score(row):\n"
        "    return 1\n",
        encoding="utf-8",
    )
    hits = _brand_hits_in_file(planted)
    assert hits == [], f"comment-only brands must not trip guard: {hits}"


# --------------------------------------------------------------------------- #
# Mechanism execution (importorskip when WS1 / WS2 not merged)
# --------------------------------------------------------------------------- #


def _try_import_role_gate() -> Optional[Any]:
    try:
        return importlib.import_module("infona_client.pipeline.role_membership_gate")
    except ModuleNotFoundError:
        return None


def _resolve_role_gate_fn(mod: Any) -> Optional[Callable[..., Any]]:
    for name in (
        "screen_role_membership",  # WS2 shipped entrypoint (ONTA-460)
        "apply_role_membership_gate",
        "role_membership_gate",
        "gate_role_membership",
        "apply_gate",
    ):
        fn = getattr(mod, name, None)
        if callable(fn):
            return fn
    return None


def _extract_kept_rows(result: Any) -> list[dict]:
    """Normalize gate/merge return shapes to a list of row dicts."""
    if result is None:
        return []
    if isinstance(result, list):
        return [r for r in result if isinstance(r, dict)]
    if isinstance(result, tuple) and result:
        first = result[0]
        if isinstance(first, list):
            return [r for r in first if isinstance(r, dict)]
    rows = getattr(result, "rows", None)
    if isinstance(rows, list):
        return [r for r in rows if isinstance(r, dict)]
    kept = getattr(result, "kept", None)
    if isinstance(kept, list):
        return [r for r in kept if isinstance(r, dict)]
    return []


def _call_role_gate(
    fn: Callable[..., Any],
    *,
    rows: list[dict],
    key_attr: str,
    role_attrs: list[str],
    plan_attrs: list[str],
    focus_type: Optional[str],
) -> list[dict]:
    """Call the gate with a few signature fallbacks."""
    attempts: list[dict[str, Any]] = [
        {
            "rows": rows,
            "key_attr": key_attr,
            "role_attributes": frozenset(role_attrs),
            "focus_type": focus_type,
        },
        {
            "rows": rows,
            "key_attr": key_attr,
            "role_attrs": role_attrs,
            "plan_attrs": plan_attrs,
            "focus_type": focus_type,
        },
        {
            "rows": rows,
            "key_attr": key_attr,
            "plan_attrs": plan_attrs,
        },
        {
            "rows": list(rows),
            "key_attr": key_attr,
        },
    ]
    last_err: Optional[BaseException] = None
    for kwargs in attempts:
        try:
            return _extract_kept_rows(fn(**kwargs))
        except TypeError as e:
            last_err = e
            continue
    # Positional fallbacks
    for args in (
        (rows, key_attr, plan_attrs),
        (rows, key_attr),
        (rows,),
    ):
        try:
            return _extract_kept_rows(fn(*args))
        except TypeError as e:
            last_err = e
            continue
    raise AssertionError(
        f"role gate callable {fn!r} rejected known signatures; last error: {last_err}"
    )


@pytest.fixture(scope="module")
def role_gate_fn():
    mod = _try_import_role_gate()
    if mod is None:
        pytest.skip(
            "infona_client.pipeline.role_membership_gate not merged yet (WS2)"
        )
    fn = _resolve_role_gate_fn(mod)
    if fn is None:
        pytest.skip(
            "role_membership_gate module present but no apply_* entrypoint exported"
        )
    return fn


@pytest.mark.parametrize(
    "batch",
    [b for b in _load_json(_ROLE_FIXTURE)["batches"] if not b.get("optional")],
    ids=lambda b: b.get("id", "batch"),
)
def test_role_membership_gate_property(role_gate_fn, batch: dict):
    """Property: role-inverted keys are not kept; catalog/instance keys remain."""
    key_attr = batch["key_attr"]
    role_attrs = batch["role_attrs"]
    plan_attrs = batch.get("plan_attrs") or [key_attr, *role_attrs]
    rows = [dict(r) for r in batch["rows"]]
    kept = _call_role_gate(
        role_gate_fn,
        rows=rows,
        key_attr=key_attr,
        role_attrs=role_attrs,
        plan_attrs=plan_attrs,
        focus_type=batch.get("focus_type"),
    )
    kept_norm = {_alnum_norm(r.get(key_attr)) for r in kept}
    expect = batch["expect_structural"]
    for k in expect.get("must_drop_keys") or []:
        assert _alnum_norm(k) not in kept_norm, (
            f"{batch['id']}: structural role drop failed for {k!r}; kept={kept_norm}"
        )
    for k in expect.get("must_keep_keys") or []:
        assert _alnum_norm(k) in kept_norm, (
            f"{batch['id']}: structural keep failed for {k!r}; kept={kept_norm}"
        )


def test_role_membership_gate_incident_structural_property(role_gate_fn):
    """Incident batch: every kept name must not equal another row's provider value."""
    data = _load_json(_ROLE_FIXTURE)
    batch = next(
        (b for b in data["batches"] if b.get("id") == "incident_tts_structural"),
        None,
    )
    if batch is None:
        pytest.skip("optional incident batch not present")
    key_attr = batch["key_attr"]
    role_attrs = batch["role_attrs"]
    rows = [dict(r) for r in batch["rows"]]
    kept = _call_role_gate(
        role_gate_fn,
        rows=rows,
        key_attr=key_attr,
        role_attrs=role_attrs,
        plan_attrs=batch.get("plan_attrs") or [key_attr, *role_attrs],
        focus_type=batch.get("focus_type"),
    )
    # Build role values from the *input* batch (evidence).
    role_vals = _role_values_in_batch(rows, role_attrs, key_attr)
    for r in kept:
        name = r.get(key_attr)
        if not name:
            continue
        if _alnum_norm(name) in role_vals and not _is_catalog_path(name):
            pytest.fail(
                f"kept non-catalog name {name!r} equals a role-attr value in the "
                f"batch — role inversion property violated"
            )


_DEDICATED_IDENTITY_HELPERS = (
    "merge_identity_clusters",
    "merge_catalog_surface_identity",
    "cluster_identity",
    "apply_identity_merge",
    "merge_structural_identity",
)


def _probe_supports_catalog_surface(fn: Callable[..., Any]) -> bool:
    """True when ``fn`` merges catalog-path + surface form of the same slug tail.

    Used to detect WS1 landing as an *extension* of ``merge_near_duplicates``
    rather than a new symbol — without false-positiving on name-only near-dup.
    """
    probe_rows = [
        {"name": "acme/widget-pro", "provider": "acme", "x": "1"},
        {"name": "Widget Pro", "provider": "acme"},
    ]
    try:
        kept = _call_identity_merge(
            fn,
            rows=probe_rows,
            key_attr="name",
            plan_attrs=["name", "provider", "x"],
        )
    except Exception:
        return False
    return len(kept) == 1


def _try_identity_merge_fn() -> Optional[Callable[..., Any]]:
    """Locate structural catalog-path identity merge if WS1 has landed."""
    try:
        dq = importlib.import_module("infona_client.pipeline.discovery_quality")
    except ModuleNotFoundError:
        return None
    for name in _DEDICATED_IDENTITY_HELPERS:
        fn = getattr(dq, name, None)
        if callable(fn):
            return fn
    # WS1 may extend merge_near_duplicates in place — only accept if a probe
    # pair actually merges under catalog↔surface rules.
    near = getattr(dq, "merge_near_duplicates", None)
    if callable(near) and _probe_supports_catalog_surface(near):
        return near
    return None


@pytest.fixture(scope="module")
def identity_merge_fn():
    fn = _try_identity_merge_fn()
    if fn is None:
        pytest.skip(
            "structural catalog↔surface identity merge not available yet (WS1); "
            "name-only near-dup merge is not sufficient"
        )
    return fn


def _call_identity_merge(
    fn: Callable[..., Any],
    *,
    rows: list[dict],
    key_attr: str,
    plan_attrs: list[str],
) -> list[dict]:
    attempts: list[tuple[tuple, dict]] = [
        ((rows, key_attr), {"plan_attrs": plan_attrs}),
        ((rows,), {"key_attr": key_attr, "plan_attrs": plan_attrs}),
        ((rows, key_attr, plan_attrs), {}),
        ((rows, key_attr), {}),
    ]
    last_err: Optional[BaseException] = None
    for args, kwargs in attempts:
        try:
            return _extract_kept_rows(fn(*args, **kwargs))
        except TypeError as e:
            last_err = e
            continue
    raise AssertionError(
        f"identity merge {fn!r} rejected known signatures; last error: {last_err}"
    )


def _identity_cases(expect_merge: bool) -> list[tuple[str, dict, dict]]:
    data = _load_json(_IDENTITY_FIXTURE)
    cases: list[tuple[str, dict, dict]] = []
    for domain in data["domains"]:
        if domain.get("optional"):
            continue
        key = "merge_pairs" if expect_merge else "non_merge_pairs"
        for pair in domain.get(key) or []:
            cases.append((domain["domain"], domain, pair))
    return cases


_IDENTITY_MERGE_CASES = _identity_cases(True)
_IDENTITY_NON_MERGE_CASES = _identity_cases(False)


@pytest.mark.parametrize(
    "domain_name,domain,pair",
    _IDENTITY_MERGE_CASES,
    ids=[p[2].get("id", "pair") for p in _IDENTITY_MERGE_CASES],
)
def test_identity_merge_positive_property(identity_merge_fn, domain_name, domain, pair):
    """Catalog-path + surface form of same slug tail collapse to one survivor."""
    key_attr = domain.get("key_attr") or "name"
    plan_attrs = domain.get("plan_attrs") or [key_attr]
    rows = [dict(r) for r in pair["rows"]]
    kept = _call_identity_merge(
        identity_merge_fn,
        rows=rows,
        key_attr=key_attr,
        plan_attrs=plan_attrs,
    )
    assert len(kept) == 1, (
        f"{pair.get('id')} ({domain_name}): expected merge → 1 row, got {len(kept)}: "
        f"{kept}"
    )
    survivor = kept[0].get(key_attr)
    if pair.get("expect_survivor_form") == "catalog_path":
        catalog = pair.get("catalog_key")
        if catalog:
            assert _is_catalog_path(survivor) or _alnum_norm(survivor) == _alnum_norm(
                catalog
            ), (
                f"{pair.get('id')}: survivor should prefer catalog-path form, "
                f"got {survivor!r}"
            )


@pytest.mark.parametrize(
    "domain_name,domain,pair",
    _IDENTITY_NON_MERGE_CASES,
    ids=[p[2].get("id", "pair") for p in _IDENTITY_NON_MERGE_CASES],
)
def test_identity_sibling_non_merge_property(
    identity_merge_fn, domain_name, domain, pair
):
    """Sibling catalog paths / unrelated surfaces must not merge."""
    key_attr = domain.get("key_attr") or "name"
    plan_attrs = domain.get("plan_attrs") or [key_attr]
    rows = [dict(r) for r in pair["rows"]]
    kept = _call_identity_merge(
        identity_merge_fn,
        rows=rows,
        key_attr=key_attr,
        plan_attrs=plan_attrs,
    )
    assert len(kept) == len(rows), (
        f"{pair.get('id')} ({domain_name}): expected no merge "
        f"({len(rows)} rows stay), got {len(kept)}: {kept}"
    )


def test_identity_merge_importorskip_documents_ws1_gap():
    """``discovery_quality`` is present; dedicated WS1 helpers may still be absent.

    Greppable contract: either a dedicated structural merge symbol exists, or
    ``merge_near_duplicates`` remains as the pre-WS1 baseline (not sufficient
    for catalog↔surface — property tests skip until WS1 lands).
    """
    pytest.importorskip("infona_client.pipeline.discovery_quality")
    dq = importlib.import_module("infona_client.pipeline.discovery_quality")
    dedicated = any(
        callable(getattr(dq, n, None)) for n in _DEDICATED_IDENTITY_HELPERS
    )
    near = callable(getattr(dq, "merge_near_duplicates", None))
    assert dedicated or near, (
        "discovery_quality must export either a structural identity merge helper "
        "or merge_near_duplicates"
    )


def test_role_membership_importorskip_documents_ws2_gap():
    """Explicit importorskip for the WS2 module name."""
    pytest.importorskip("infona_client.pipeline.role_membership_gate")
