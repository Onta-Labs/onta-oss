"""Drift guard: the committed example bank must reference the LIVE URI namespace.

The bug this prevents: the URI namespace was renamed ``omnix.dev`` →
``graph.onta.sh`` on 2026-04-27 (b7069f0; the deployed graph store was migrated
by a one-shot script in the proprietary parent repo), but the example bank was
never migrated with it. The bank is injected verbatim into every ``/ask``
few-shot prompt (``nlp/example_bank.py::format_examples_for_prompt``), so every
NL→SPARQL generation was being primed with worked examples whose predicates
resolve to nothing in any graph — while the SYSTEM prompt already taught
``graph.onta.sh``, making each prompt self-contradictory.

This file is the bank an OSS checkout or standalone install serves; the hosted
image ships the parent repo's own copy, guarded separately there. Both drifted.

Nothing evicts a stale entry by *content*. ``eval.rebuild_example_bank`` merges
``eval_reports/finetune_pairs.jsonl`` into the bank after every eval run, and
since onta-oss#280's follow-up a pair does refresh the entry it supersedes — so
a re-evaluated KG heals its own namespace drift. But that only fires for a KG
someone actually re-evaluates, the pairs file is gitignored and machine-local,
and until that follow-up the rebuild REPLACED the committed bank with that local
file rather than merging into it. A dev whose local pairs still carry the old
namespace could silently commit a stale bank. This guard is what catches that.

Two layers:
- **Bank** — no retired namespace in the committed JSONL, and every minted URI
  in it matches a live path shape.
- **Normalizer** — ``normalize_sparql`` rewrites a retired-namespace URI the LLM
  echoes back onto the live namespace instead of passing it through.
"""

import json
import re

from infona_client.nlp.example_bank import DEFAULT_BANK_PATH
from infona_client.nlp.validator import LEGACY_ONTO_HOSTS, ONTO_BASE, normalize_sparql

# The minted-URI shapes graph/queries.py + graph/ontology_queries.py actually
# produce. A bank URI outside this set means either a new shape landed without
# updating this guard, or the bank picked up a malformed URI.
LIVE_PREFIXES = (
    "graphs/",      # https://graph.onta.sh/graphs/<tenant>[/kg/<kg>]
    "types/",       # https://graph.onta.sh/types/<Type>[/attrs/<attr>]
    "onto/",        # https://graph.onta.sh/onto/<leaf>  (relationship instance edge)
    "entities/",    # https://graph.onta.sh/entities/<Type>/<id>
    "attr_meta/",   # https://graph.onta.sh/attr_meta/<Type>/<attr>/<suffix>
    "functions/",
    "kgs/",
)

_ONTA_URI_RE = re.compile(re.escape(ONTO_BASE) + r"/([\w/.\-]+)")


def _bank_text() -> str:
    assert DEFAULT_BANK_PATH.exists(), f"example bank missing at {DEFAULT_BANK_PATH}"
    return DEFAULT_BANK_PATH.read_text()


def test_bank_has_no_retired_namespace():
    """No example may reference a namespace that was renamed away."""
    text = _bank_text()
    offenders = [host for host in LEGACY_ONTO_HOSTS if host in text]
    assert not offenders, (
        f"{DEFAULT_BANK_PATH} still references retired URI namespace(s) {offenders}. "
        "These resolve to nothing in any graph, and the bank is injected verbatim "
        "into every /ask few-shot prompt. Rewrite them onto "
        f"{ONTO_BASE}/ (host-only swap — the path shapes are unchanged)."
    )


def test_bank_uris_use_live_path_shapes():
    """Every minted URI in the bank matches a shape the graph layer produces."""
    bad: list[str] = []
    for path in _ONTA_URI_RE.findall(_bank_text()):
        if not path.startswith(LIVE_PREFIXES):
            bad.append(path)
    assert not bad, (
        f"example bank contains {ONTO_BASE} URIs with unknown path shapes: "
        f"{sorted(set(bad))[:10]}"
    )


def test_bank_is_valid_jsonl_with_embeddings():
    """The rewrite must not have disturbed the stored question embeddings."""
    lines = [ln for ln in _bank_text().splitlines() if ln.strip()]
    assert lines, "example bank is empty"
    for line in lines:
        rec = json.loads(line)
        assert rec["question"].strip()
        assert rec["sparql"].strip()
        assert len(rec["embedding"]) == 1536


def test_normalizer_rewrites_retired_namespace():
    """A retired-namespace URI echoed back by the LLM is normalized, not passed through.

    Deliberately uses HYPHENATED tenant, KG and entity names — `demo-tenant`,
    `imdb-movies`, `san-jose`. Those are the realistic shapes (every real tenant
    is hyphenated, and `_safe_id` leaves hyphens intact), and an earlier cut of
    this test used hyphen-free `graphs/t/kg/imdb`, which passed against a regex
    whose character class excluded `-` — asserting coverage the code did not have.
    """
    for host in LEGACY_ONTO_HOSTS:
        out = normalize_sparql(
            f"SELECT ?x FROM <https://{host}/graphs/demo-tenant/kg/imdb-movies> "
            f"WHERE {{ ?x a <https://{host}/types/Film> ; "
            f"<https://{host}/onto/acted-in> <https://{host}/entities/City/san-jose> ; "
            f"<https://{host}/types/Film/attrs/release-year> ?z }}"
        )
        assert host not in out, f"normalize_sparql passed through retired host {host}: {out}"
        assert f"{ONTO_BASE}/graphs/demo-tenant/kg/imdb-movies" in out
        assert f"{ONTO_BASE}/types/Film" in out
        assert f"{ONTO_BASE}/onto/acted-in" in out
        assert f"{ONTO_BASE}/entities/City/san-jose" in out
        assert f"{ONTO_BASE}/types/Film/attrs/release-year" in out


def test_normalizer_still_fixes_bare_type_names():
    """The bare-PascalCase → /types/ fixup applies on the LIVE namespace.

    Regression: the rename updated this branch's *output* string but left its
    matching regex on the retired host, so the fixup silently stopped firing for
    the namespace the LLM actually emits.
    """
    out = normalize_sparql(f"SELECT ?x WHERE {{ ?x a <{ONTO_BASE}/Property> }}")
    assert f"<{ONTO_BASE}/types/Property>" in out


def test_normalizer_leaves_correct_uris_alone():
    query = (
        f"SELECT ?x WHERE {{ ?x a <{ONTO_BASE}/types/Film> ; "
        f"<{ONTO_BASE}/onto/actedIn> <{ONTO_BASE}/entities/Person/ada_lovelace> }}"
    )
    assert normalize_sparql(query) == query
