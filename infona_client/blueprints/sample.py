"""INF-587 sample-dataset surface contract.

The validator owns size, timestamp, origin, and the ``is_sample`` marker.
This module owns the *read-side* rule: a sample must never present as
current. Explorer and the public Blueprint page do not yet render installed
sample rows (INF-558 is a fake-door catalog). When those surfaces land they
MUST call :func:`surface_label` and MUST NOT feed
:func:`feeds_freshness_panel`. Do not invent a second caption.

A sample that renders with a healthy green freshness light is a lie told by
the one panel the rest of the pitch depends on (INF-587 / INF-571).
"""

from __future__ import annotations

from datetime import date

from infona_client.blueprints.schema import Sample


def surface_label(captured_at: date) -> str:
    """Caption every surface must use for sample-derived rows or answers.

    Always starts with ``sample`` and carries the capture date as data, not
    as README prose.
    """

    return f"sample, captured {captured_at.isoformat()}"


def sample_section_label(sample: Sample) -> str:
    return surface_label(sample.captured_at)


def feeds_freshness_panel() -> bool:
    """Sample rows must not feed the maintenance/verification freshness
    panel. Structural: there is no code path that returns True."""

    return False


def is_marked_sample(sample: Sample) -> bool:
    return sample.kind == "sample" and all(
        entity.is_sample is True for entity in sample.entities
    )
