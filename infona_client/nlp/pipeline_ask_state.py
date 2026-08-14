"""Mutable per-request bag for one Cypher /ask turn. Not a public type."""
from __future__ import annotations


class AskCypherState:
    """Attribute bag shared across ``_ask_cypher`` stage mixins.

    Stages read/write fields rather than threading 20-argument tuples. Not part
    of the public ``infona_client.nlp.pipeline`` API.
    """

    __slots__ = ("__dict__",)
