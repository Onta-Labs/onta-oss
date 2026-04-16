"""Persistent verdict cache for type-matching decisions.

Ensures the same type pairing is never re-judged. JSON file for now,
swappable to DynamoDB by implementing the VerdictStore protocol.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

import structlog

from cograph.resolver.models import MatchVerdict

logger = structlog.stdlib.get_logger("cograph.resolver.cache")


class VerdictEntry:
    __slots__ = ("proposed", "existing", "verdict", "confidence")

    def __init__(self, proposed: str, existing: str, verdict: MatchVerdict, confidence: float):
        self.proposed = proposed
        self.existing = existing
        self.verdict = verdict
        self.confidence = confidence

    def to_dict(self) -> dict:
        return {
            "proposed": self.proposed,
            "existing": self.existing,
            "verdict": self.verdict.value,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, d: dict) -> VerdictEntry:
        return cls(
            proposed=d["proposed"],
            existing=d["existing"],
            verdict=MatchVerdict(d["verdict"]),
            confidence=d["confidence"],
        )


def _cache_key(proposed: str, existing: str) -> str:
    return f"{proposed.lower()}::{existing.lower()}"


class VerdictStore(Protocol):
    """Protocol for verdict storage backends."""

    async def get(self, proposed: str, existing: str) -> VerdictEntry | None: ...
    async def put(self, entry: VerdictEntry) -> None: ...
    async def get_all_for_proposed(self, proposed: str) -> list[VerdictEntry]: ...


class JsonVerdictCache:
    """File-backed verdict cache. Good for single-instance deployments."""

    def __init__(self, path: Path):
        self._path = path
        self._cache: dict[str, VerdictEntry] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
            for item in data:
                entry = VerdictEntry.from_dict(item)
                key = _cache_key(entry.proposed, entry.existing)
                self._cache[key] = entry
            logger.info("verdict_cache_loaded", count=len(self._cache), path=str(self._path))
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("verdict_cache_corrupt", error=str(e), path=str(self._path))

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = [entry.to_dict() for entry in self._cache.values()]
        self._path.write_text(json.dumps(data, indent=2))

    async def get(self, proposed: str, existing: str) -> VerdictEntry | None:
        return self._cache.get(_cache_key(proposed, existing))

    async def put(self, entry: VerdictEntry) -> None:
        key = _cache_key(entry.proposed, entry.existing)
        self._cache[key] = entry
        self._save()
        logger.info(
            "verdict_cached",
            proposed=entry.proposed,
            existing=entry.existing,
            verdict=entry.verdict.value,
        )

    async def get_all_for_proposed(self, proposed: str) -> list[VerdictEntry]:
        prefix = proposed.lower() + "::"
        return [e for k, e in self._cache.items() if k.startswith(prefix)]
