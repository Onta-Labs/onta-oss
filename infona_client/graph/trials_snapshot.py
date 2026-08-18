"""Load the shipped prebuilt ``trials`` graph (ONTA-544).

Builds ingest-shaped facts from ``examples/trials.csv`` (or a frozen JSON
snapshot) and writes them through :func:`insert_facts` into a GraphStore —
MemoryGraphStore in tests, Neo4j via ``NEO4J_*`` for ``oss_up``.

No LLM. No second write path.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from infona_client.graph.iri import IRI_BASE, ONTO_PRED_PREFIX, TYPE_URI_PREFIX
from infona_client.graph.kg_writer import insert_facts
from infona_client.graph.ontology_queries import entity_uri
from infona_client.graph.queries import kg_graph_uri

DEFAULT_TENANT = "default"
DEFAULT_KG = "trials"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"

_REPO_ROOT = Path(__file__).resolve().parents[2]
PREBUILT_DIR = _REPO_ROOT / "examples" / "prebuilt"
TRIALS_CSV = _REPO_ROOT / "examples" / "trials.csv"
SNAPSHOT_JSON = PREBUILT_DIR / "trials_snapshot.json"


def prebuilt_dir() -> Path:
    override = (os.environ.get("INFONA_PREBUILT_DIR") or "").strip()
    if override:
        return Path(override)
    return PREBUILT_DIR


def snapshot_path() -> Path:
    override = (os.environ.get("INFONA_TRIALS_SNAPSHOT") or "").strip()
    if override:
        return Path(override)
    return prebuilt_dir() / "trials_snapshot.json"


def triples_from_rows(rows: Sequence[dict[str, str]]) -> list[tuple[str, str, str]]:
    """Trial + Sponsor + Indication facts; ``onto/runs`` + ``onto/indication``.

    Same shape as the README hero ingest: Trial--sponsor-->Sponsor is the
    queryable direction; Sponsor--runs-->Trial is the product edge.
    """
    triples: list[tuple[str, str, str]] = []
    seen_sponsors: set[str] = set()
    seen_indications: set[str] = set()
    for row in rows:
        trial_id = (row.get("trial_id") or "").strip()
        trial_name = (row.get("trial") or "").strip()
        sponsor_name = (row.get("sponsor") or "").strip()
        indication_name = (row.get("indication") or "").strip()
        phase = (row.get("phase") or "").strip()
        status = (row.get("status") or "").strip()
        if not trial_id or not trial_name:
            continue
        trial = entity_uri("Trial", trial_id)
        triples.append((trial, RDF_TYPE, f"{TYPE_URI_PREFIX}Trial"))
        triples.append((trial, RDFS_LABEL, trial_name))
        if phase:
            triples.append((trial, f"{TYPE_URI_PREFIX}Trial/attrs/phase", phase))
        if status:
            triples.append((trial, f"{TYPE_URI_PREFIX}Trial/attrs/status", status))
        if sponsor_name:
            sponsor = entity_uri("Sponsor", sponsor_name)
            if sponsor_name not in seen_sponsors:
                seen_sponsors.add(sponsor_name)
                triples.append((sponsor, RDF_TYPE, f"{TYPE_URI_PREFIX}Sponsor"))
                triples.append((sponsor, RDFS_LABEL, sponsor_name))
            triples.append((sponsor, f"{ONTO_PRED_PREFIX}runs", trial))
            triples.append((trial, f"{ONTO_PRED_PREFIX}sponsor", sponsor))
        if indication_name:
            indication = entity_uri("Indication", indication_name)
            if indication_name not in seen_indications:
                seen_indications.add(indication_name)
                triples.append((indication, RDF_TYPE, f"{TYPE_URI_PREFIX}Indication"))
                triples.append((indication, RDFS_LABEL, indication_name))
            triples.append((trial, f"{ONTO_PRED_PREFIX}indication", indication))
    return triples


def load_trial_rows(csv_path: Path | None = None) -> list[dict[str, str]]:
    path = csv_path or TRIALS_CSV
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def triples_from_csv(csv_path: Path | None = None) -> list[tuple[str, str, str]]:
    return triples_from_rows(load_trial_rows(csv_path))


def triples_from_snapshot(path: Path | None = None) -> list[tuple[str, str, str]]:
    data = json.loads((path or snapshot_path()).read_text(encoding="utf-8"))
    raw = data.get("triples") or []
    out: list[tuple[str, str, str]] = []
    for item in raw:
        if isinstance(item, (list, tuple)) and len(item) >= 3:
            out.append((str(item[0]), str(item[1]), str(item[2])))
    return out


def load_triples() -> list[tuple[str, str, str]]:
    """Prefer the frozen snapshot; fall back to ``examples/trials.csv``."""
    snap = snapshot_path()
    if snap.is_file():
        triples = triples_from_snapshot(snap)
        if triples:
            return triples
    if TRIALS_CSV.is_file():
        return triples_from_csv(TRIALS_CSV)
    raise FileNotFoundError(
        f"no prebuilt trials snapshot at {snap} and no {TRIALS_CSV}"
    )


def snapshot_payload(triples: Sequence[tuple[str, str, str]]) -> dict[str, Any]:
    return {
        "version": 1,
        "source": "examples/trials.csv",
        "kg": DEFAULT_KG,
        "triple_count": len(triples),
        "triples": [list(t) for t in triples],
    }


def write_snapshot_json(
    path: Path | None = None,
    *,
    triples: Sequence[tuple[str, str, str]] | None = None,
) -> Path:
    dest = path or snapshot_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    rows = list(triples) if triples is not None else triples_from_csv()
    dest.write_text(
        json.dumps(snapshot_payload(rows), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return dest


async def register_kg(store: Any, tenant_id: str, kg: str, *, n_triples: int) -> None:
    native = getattr(store, "kg_registry_upsert", None)
    if callable(native):
        await native(
            tenant_id,
            kg,
            description="Prebuilt oncology trials sample (cached-plan replay).",
            triple_count=n_triples,
            only_if_absent=False,
        )
        return
    from infona_client.graph.kg_registry import upsert_registered_kg
    from infona_client.graph.store import configure_graph_store

    configure_graph_store(store)
    await upsert_registered_kg(
        tenant_id,
        kg,
        description="Prebuilt oncology trials sample (cached-plan replay).",
        triple_count=n_triples,
        only_if_absent=False,
    )


async def load_trials_snapshot(
    store: Any,
    *,
    tenant_id: str = DEFAULT_TENANT,
    kg: str = DEFAULT_KG,
    triples: Sequence[tuple[str, str, str]] | None = None,
) -> int:
    """Write the prebuilt trials graph through ``insert_facts``. Returns count."""
    bootstrap = getattr(store, "bootstrap_schema", None)
    if callable(bootstrap):
        await bootstrap()
    facts = list(triples) if triples is not None else load_triples()
    if not facts:
        raise ValueError("prebuilt trials snapshot produced no triples")
    graph = kg_graph_uri(tenant_id, kg)
    await insert_facts(None, graph, list(facts), store=store)
    await register_kg(store, tenant_id, kg, n_triples=len(facts))
    return len(facts)


def _resolve_store() -> Any:
    from infona_client.graph.store import get_graph_store

    return get_graph_store()


async def _amain(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Load the shipped prebuilt trials graph (no LLM)."
    )
    parser.add_argument("--tenant", default=os.environ.get("INFONA_TENANT") or DEFAULT_TENANT)
    parser.add_argument("--kg", default=os.environ.get("INFONA_PREBUILT_KG") or DEFAULT_KG)
    parser.add_argument(
        "--dump",
        action="store_true",
        help="Write examples/prebuilt/trials_snapshot.json from trials.csv and exit.",
    )
    parser.add_argument("--snapshot", type=Path, default=None)
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.dump:
        dest = write_snapshot_json(args.snapshot)
        print(f"wrote {dest} ({dest.stat().st_size} bytes)")
        return 0

    store = _resolve_store()
    n = await load_trials_snapshot(store, tenant_id=args.tenant, kg=args.kg)
    print(
        f"loaded prebuilt trials graph: tenant={args.tenant} kg={args.kg} "
        f"triples={n} (no LLM)"
    )
    close = getattr(store, "close", None)
    if callable(close):
        await close()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    sys.exit(main())
