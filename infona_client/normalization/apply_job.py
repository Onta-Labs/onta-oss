"""Run a confirmed normalization rule and DURABLY record how it went.

Apply is genuinely a background job: both entry points — ``POST
/graphs/{tenant}/normalize/rules/{id}/apply`` and the agent's normalize
capability — spawn a detached task and ack immediately (202 / an ``ack``
envelope). That shape is right and is preserved. What was wrong is that the
outcome only ever went to a log line:

    except Exception:
        logger.error("normalize_apply_failed", ...)

The request had already returned, the rule stayed ``confirmed`` forever, and the
user was told nothing. They believed their normalization rule was live; it was
not, and never would be — no error, no state change, no retry signal. That is
the silent failure this module exists to close.

:func:`apply_and_record` is the ONE place that turns an apply attempt into
persisted state, so the two call sites cannot drift on what a failure means:

* success → ``status="applied"``, ``applied_at`` stamped, and ``failed_at`` /
  ``last_error`` CLEARED (an applied rule never shows a stale error).
* failure → ``status="failed"``, ``failed_at`` stamped, ``last_error`` set to a
  capped one-line description. ``applied_at`` is left alone: it still records
  the last apply that actually landed.

``failed`` is deliberately RETRYABLE, not terminal — the apply route accepts a
``failed`` rule exactly as it accepts a ``confirmed`` one, so once the cause is
fixed the user just applies again. Nothing needs manual store surgery.

This is about apply failing for ANY reason. Today the common cause is that
``normalization/execute.py``'s reads are still on the retired SPARQL client
(``SparqlClientRetired`` on the first read for ``strip_emoji`` /
``promote_to_node`` / both ``list_explode`` shapes), but nothing here is
SPARQL-specific: a bad rule, a store outage, or a bug in a future rule type all
land in the same durable, user-visible place.

The call sites keep their own log events (``normalize_apply_*`` for the route,
``agent_normalize_*`` for the capability) — only the PERSISTED outcome is shared.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import structlog

from infona_client.normalization.execute import apply_rule
from infona_client.normalization.rules import (
    MAX_LAST_ERROR_CHARS,
    NormalizationRule,
    NormalizationRuleStore,
)

logger = structlog.stdlib.get_logger("infona.normalization.apply_job")


@dataclass(frozen=True)
class ApplyOutcome:
    """What one apply attempt did, for the caller to log. Never an exception."""

    ok: bool
    summary: dict = field(default_factory=dict)
    error: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def describe_error(exc: BaseException) -> str:
    """One capped line identifying a failure, safe to persist as a literal.

    ``type: message`` rather than a traceback — the traceback still goes to the
    log via ``exc_info``; this is the part a user reads in the Explorer.
    """
    message = str(exc).strip() or exc.__class__.__name__
    return f"{exc.__class__.__name__}: {message}"[:MAX_LAST_ERROR_CHARS]


async def apply_and_record(
    client, tenant_id: str, rule: NormalizationRule
) -> ApplyOutcome:
    """Apply ``rule`` and persist the outcome on it. NEVER raises.

    Runs detached, so raising would only produce an unhandled-task warning.
    Every exit path writes something durable the user can see via
    ``GET /normalize/rules`` (including ``?status=failed``).
    """
    store = NormalizationRuleStore(client)
    try:
        summary = await apply_rule(client, tenant_id, rule)
    except Exception as exc:  # noqa: BLE001 — detached worker, never raise
        error = describe_error(exc)
        try:
            await store.update_status(
                tenant_id, rule.id, "failed", failed_at=_now(), last_error=error
            )
        # The store itself is down — log only; there is nowhere durable left.
        except Exception:
            logger.error(
                "normalize_apply_outcome_unrecordable",
                rule_id=rule.id,
                error=error,
                exc_info=True,
            )
        return ApplyOutcome(ok=False, error=error)

    try:
        await store.update_status(
            tenant_id,
            rule.id,
            "applied",
            applied_at=_now(),
            failed_at=None,
            last_error=None,
        )
    # The apply DID land; only the mark failed. Log it — do not report failure.
    except Exception:
        logger.error(
            "normalize_apply_outcome_unrecordable", rule_id=rule.id, exc_info=True
        )
    return ApplyOutcome(ok=True, summary=summary)
