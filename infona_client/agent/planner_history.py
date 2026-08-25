"""Session-transcript helpers for the agent planner.

Implementation sibling of :mod:`infona_client.agent.planner`.
"""
from __future__ import annotations

import re

from infona_client.agent.conversation_store import Turn

# How many recent turns of a (possibly long, history-backed) transcript to feed
# the classifier prompt + accumulate for capability extraction. The store keeps
# a longer tail for the history UI; the prompt only needs the recent context.
_PROMPT_HISTORY_TURNS = 16


def _host():
    """Call-time lookup of the public planner module (monkeypatch surface)."""
    from infona_client.agent import planner as _mod

    return _mod


def _turn_matches_kg(turn: Turn, kg_name: str | None) -> bool:
    """True when ``turn`` belongs to the knowledge graph this request targets.

    ONTA-419. A turn with no recorded ``kg_name`` matches EVERYTHING: transcripts
    persisted before the field existed, and turns made with no KG scope at all,
    keep their pre-change behavior instead of silently disappearing from the
    window. Likewise a request with no ``kg_name`` (tenant-scoped only) sees the
    whole transcript, exactly as before.
    """
    if not turn.kg_name or not kg_name:
        return True
    return turn.kg_name == kg_name


def _same_kg_turns(history: list[Turn] | None, kg_name: str | None) -> list[Turn]:
    """Drop the turns that belong to a DIFFERENT knowledge graph."""
    return [t for t in history or [] if _turn_matches_kg(t, kg_name)]


_QUERY_FOLLOWUP_TURNS = 6


def query_followup_turns(
    history: list[Turn] | None, kg_name: str | None = None
) -> list[dict[str, str]]:
    """Recent same-graph turns for the NL query planner, including answers.

    ``_effective_instruction`` RESETS after an ``answer`` so a new enrich/clean
    ask cannot inherit a finished question's type names. Query follow-ups
    ("what did we talk about?") NEED that finished turn. This window is
    query-only and does not change capability-extraction bleed guards.
    """
    out: list[dict[str, str]] = []
    for t in _same_kg_turns(history, kg_name)[-_QUERY_FOLLOWUP_TURNS:]:
        text = (t.text or "").strip()
        if not text or t.role not in ("user", "assistant"):
            continue
        out.append({"role": t.role, "text": text})
    return out


# A KG name is rendered INTO the classifier's transcript block, which is a
# line-oriented, role-prefixed format. Anything outside this class (most of all a
# newline) could forge a turn the model reads as the user's own words, so the
# label is sanitized at the point of interpolation.
_KG_LABEL_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_-]")
_KG_LABEL_MAX = 64


def _kg_label(name: str | None) -> str:
    """Render a knowledge-graph name safe to interpolate into a prompt.

    Real KG names already match ``^[a-zA-Z0-9_-]+$`` (``graph/kg_writer.py``),
    but the ``/agent`` request body does not enforce that pattern before the name
    reaches here, so an arbitrary string can be stamped onto a turn today. Escape
    at the interpolation point rather than trusting an upstream check to arrive:
    every character outside the safe class becomes ``_`` and the result is
    length-capped. Only the LABEL is rewritten -- ``_turn_matches_kg`` still
    compares the raw values, so scoping is unaffected.
    """
    safe = _KG_LABEL_UNSAFE_RE.sub("_", str(name or ""))
    return safe[:_KG_LABEL_MAX]


def _recent_window(
    history: list[Turn] | None, kg_name: str | None, limit: int = _PROMPT_HISTORY_TURNS
) -> list[Turn]:
    """A bounded transcript tail that always carries this graph's own turns.

    Trimming the raw tail and only THEN scoping by KG (the shape this code had
    before) can leave zero same-graph turns: in a session where the user works on
    graph B, switches to graph A and stays there for ``limit`` turns, B's own
    open ask falls off the end and the accumulation window comes back empty. That
    was equally true pre-ONTA-419, so it is a reach limitation rather than a
    regression, but it is one line to fix now that the KG is known.

    So keep the last ``limit`` turns overall AND the last ``limit`` turns for
    THIS graph, restored to transcript order (bounded at ``2 * limit``). Foreign
    turns still reach :func:`_format_history`, where they are labelled rather
    than dropped; :func:`_open_ask_user_turns` still filters them out.
    """
    turns = list(history or [])
    if len(turns) <= limit:
        return turns
    keep = set(range(len(turns) - limit, len(turns)))
    same = [i for i, t in enumerate(turns) if _turn_matches_kg(t, kg_name)]
    keep.update(same[-limit:])
    return [turns[i] for i in sorted(keep)]


def _format_history(history: list[Turn] | None, kg_name: str | None = None) -> str:
    """Render the prior turns as a transcript block for the classifier prompt.

    Foreign-KG turns (ONTA-419) are LABELLED rather than dropped here. The
    classifier is a semantic router, not a parameter extractor: a genuine
    cross-graph reference ("do the same thing here") is only legible if its
    antecedent is still visible, and silently deleting one half of a
    clarify/reply pair would leave the remaining half reading as a non sequitur.
    Labelling gives the model the fact it needs -- that turn was about another
    graph -- and lets it decide. The accumulation window
    (:func:`_open_ask_user_turns`) takes the opposite, stricter line, because
    there the text is spliced verbatim into a capability's parameter extraction.
    """
    if not history:
        return ""
    lines = []
    saw_foreign = False
    for t in history:
        if t.role == "assistant":
            who = f"Assistant ({t.kind})" if t.kind else "Assistant"
        else:
            who = "User"
        if not _turn_matches_kg(t, kg_name):
            who = f"{who} [on a different knowledge graph: {_kg_label(t.kg_name)}]"
            saw_foreign = True
        text = (t.text or "").strip()
        if text:
            lines.append(f"{who}: {text}")
    if not lines:
        return ""
    # Only spend prompt tokens on the cross-graph preamble when the transcript
    # actually contains a foreign turn; an ordinary single-graph thread renders
    # byte-for-byte as it did before ONTA-419.
    header = "Conversation so far:"
    if saw_foreign:
        header = (
            f"Conversation so far (the latest message targets the "
            f"'{_kg_label(kg_name)}' knowledge graph; turns marked as being on a "
            "different knowledge graph are about other data and usually should "
            "not be continued):"
        )
    return header + "\n" + "\n".join(lines) + "\n\n"


# Assistant reply kinds that COMMIT / resolve an intent, ending the current ask.
# A ``clarify`` is the ONE reply that means "still gathering the parameters of the
# SAME ask", so it does NOT close the accumulation window; everything else
# (``answer`` answered a question, ``plan`` proposed a committed plan, ``result``
# ran one) is a finished, different request whose text must not bleed forward.
_COMMITTED_REPLY_KINDS = frozenset({"answer", "plan", "result"})


def _open_ask_user_turns(
    history: list[Turn] | None, kg_name: str | None = None
) -> list[str]:
    """The user turns belonging to the CURRENT, still-open ask (oldest→newest).

    Walk the transcript backwards collecting user turns, stopping as soon as we
    cross the boundary of a RESOLVED intent — any assistant reply that is not a
    ``clarify`` (see :data:`_COMMITTED_REPLY_KINDS`). A ``clarify`` → answer
    exchange keeps accumulating so the field named an earlier turn survives a
    terse reply ("both"); but once a ``plan`` was proposed or a question
    ``answer``ed, that intent is DONE and its text is dropped from the window so
    a later, unrelated request is not contaminated by it (the session-context
    bleed the planner previously suffered — every prior user turn was replayed).

    ONTA-419: turns made against a DIFFERENT knowledge graph are removed before
    the walk, not merely skipped during it. Removing them first is what makes an
    interleaved session behave: an open clarify on graph B stays open across a
    detour to graph A, and graph A's committed reply no longer closes B's window
    (nor does A's user text get spliced into B's instruction, where it would feed
    a capability a field/type name from a graph that may not even have it). A
    turn with no recorded ``kg_name`` is kept -- see :func:`_turn_matches_kg`.
    """
    out: list[str] = []
    for t in reversed(_same_kg_turns(history, kg_name)):
        if t.role == "assistant":
            # A clarify keeps the window open; anything else closes it.
            if t.kind == "clarify":
                continue
            break
        if t.role == "user" and t.text:
            out.append(t.text)
    out.reverse()
    return out


def _effective_instruction(
    history: list[Turn] | None, message: str, kg_name: str | None = None
) -> str:
    """Accumulate the user's answers so capability extraction sees the full ask.

    A capability's parameter extraction (which field, which attribute, which
    rule) runs on a single string. Feeding it only the latest reply ("I wanna do
    both") loses the field the user named two turns ago — so we DO concatenate
    prior user turns. But only the turns of the CURRENT, still-open ask
    (:func:`_open_ask_user_turns`): a committed plan / answered question RESETS
    the window, so a finished prior request never replays into a new one. The
    current ``message`` is always appended LAST so it dominates. With no
    (in-window) prior turns this is just the message (unchanged single-turn
    behavior). ``kg_name`` scopes the window to the graph this turn targets
    (ONTA-419).
    """
    prior_user = _open_ask_user_turns(history, kg_name)
    if not prior_user:
        return message
    return "\n".join([*prior_user, message])

