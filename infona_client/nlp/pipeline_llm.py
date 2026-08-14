"""OpenRouter/Anthropic helpers, JSON salvage, embedding singleton.

Invariant (other agents): product NL→Cypher is always LLM; do not short-circuit
/ask onto deterministic fixtures.
"""
from __future__ import annotations

import json
import os
import re

# Query generation provider config.
# INFONA_LLM_BASE_URL / INFONA_QUERY_BASE_URL let self-hosted OpenAI-compatible
# endpoints (vLLM, Ollama, LiteLLM) serve /ask without a source patch
# (OSS dogfood S8). Fall back to the public OpenRouter host.
def _openrouter_base() -> str:
    return (
        os.environ.get("INFONA_QUERY_BASE_URL")
        or os.environ.get("INFONA_LLM_BASE_URL")
        or os.environ.get("INFONA_OPENROUTER_BASE_URL")
        or "https://openrouter.ai/api/v1"
    ).rstrip("/")


OPENROUTER_BASE = _openrouter_base()


def _default_query_provider() -> str:
    """Prefer explicit env; otherwise pick a provider the process can actually call.

    Historical default was ``cerebras`` + ``llama3.1-8b``. That silently fails
    for the common OSS quickstart that only sets ``OPENROUTER_API_KEY`` —
    /ask returned HTTP 200 with the provider 400 text as the answer
    (OSS dogfood S1/S2/S4/S5). When Cerebras is not configured but OpenRouter
    is, default to OpenRouter.
    """
    explicit = os.environ.get("INFONA_QUERY_PROVIDER")
    if explicit:
        return explicit.strip().lower()
    has_openrouter = bool(
        os.environ.get("OPENROUTER_API_KEY")
        or os.environ.get("INFONA_OPENROUTER_API_KEY")
    )
    has_cerebras = bool(
        os.environ.get("CEREBRAS_API_KEY")
        or os.environ.get("INFONA_CEREBRAS_API_KEY")
    )
    if has_openrouter and not has_cerebras:
        return "openrouter"
    return "cerebras"


def _default_query_model(provider: str | None = None) -> str:
    """Default NL→Cypher model.

    OpenRouter OSS default is OpenAI **gpt-oss-120b** (reasoning / thinking
    model; often served via Cerebras on OpenRouter). Override with
    ``INFONA_QUERY_MODEL``. Direct Cerebras provider uses the bare slug.
    """
    explicit = os.environ.get("INFONA_QUERY_MODEL")
    if explicit:
        return explicit
    prov = (provider or _default_query_provider()).lower()
    if prov == "openrouter":
        return "openai/gpt-oss-120b"
    if prov == "cerebras":
        return "gpt-oss-120b"
    return "gpt-oss-120b"


def _is_reasoning_query_model(model: str | None) -> bool:
    """True for models that spend tokens on chain-of-thought before JSON."""
    m = (model or "").lower()
    if not m:
        return False
    return any(
        tok in m
        for tok in (
            "gpt-oss",
            "o1",
            "o3",
            "o4",
            "reasoning",
            "thinking",
            "deepseek-r1",
            "r1-",
        )
    )


DEFAULT_QUERY_MODEL = _default_query_model()
DEFAULT_QUERY_PROVIDER = _default_query_provider()  # cerebras, openrouter, or anthropic

# Reasoning-budget recovery for gpt-oss-120b (and similar). The model can spend
# its ENTIRE max_completion_tokens on reasoning and return finish_reason=length
# with NO answer content. Retry with a bigger budget; then optionally fall back
# to a non-reasoning path.
CEREBRAS_LENGTH_RECOVERY_TOKENS = int(
    os.environ.get("INFONA_QUERY_LENGTH_RECOVERY_TOKENS", "6144")
)
# OpenRouter reasoning models need headroom for think + JSON Cypher answer.
OPENROUTER_REASONING_MAX_TOKENS = int(
    os.environ.get("INFONA_QUERY_OPENROUTER_MAX_TOKENS", "8192")
)
OPENROUTER_QUERY_TIMEOUT_S = float(
    os.environ.get("INFONA_QUERY_OPENROUTER_TIMEOUT_S", "120")
)


class EmptyLLMResponse(ValueError):
    """Raised when an OpenAI-compatible chat completion carried no usable
    ``content`` — the key is missing entirely, ``null``, or an empty string.

    Subclasses ``ValueError`` (and keeps the exact ``"empty LLM response from
    <provider>"`` message) so every existing ``except ValueError`` /
    ``except Exception`` retry-and-fallback path — and the existing guard tests —
    treat it identically to the old bare ``ValueError``. It additionally carries
    ``finish_reason`` so a caller can distinguish a *reasoning-budget exhaustion*
    (``finish_reason == "length"``: the model spent its whole
    ``max_completion_tokens`` on reasoning and never emitted the answer) — which
    is RECOVERABLE with a bigger budget or a non-reasoning fallback — from an
    ordinary empty/null response.
    """

    def __init__(self, provider: str, finish_reason: str | None = None):
        self.provider = provider
        self.finish_reason = finish_reason
        super().__init__(f"empty LLM response from {provider}")


def _require_message_content(data: dict, provider: str) -> str:
    """Extract ``choices[0].message.content`` from an OpenAI-compatible chat
    completion, raising a clear, typed error when the model returns an
    empty/``None``/ABSENT content.

    Some models intermittently return ``content: null`` (e.g. GLM-5.2 did this
    ~40x in production) or an empty string; a *reasoning* model that exhausts its
    ``max_completion_tokens`` on reasoning (``finish_reason == "length"``) can
    omit the ``content`` key ENTIRELY (Cerebras gpt-oss-120b did this ~11x in
    persona-eval). Callers immediately ``.strip()`` or ``json.loads()`` the
    content, so a null surfaces as an opaque ``AttributeError`` / ``TypeError``,
    and a *missing* key used to surface as a hard ``KeyError('content')`` that flew
    past the retry loop as ``"Could not answer … Last error: 'content'"``. This
    guard turns ALL of those — missing/absent ``choices``/``message``/``content``,
    ``null``, or ``""`` — into one diagnosable :class:`EmptyLLMResponse` (a
    ``ValueError`` subclass) that names the provider and carries the
    ``finish_reason``, so each caller's existing retry/fallback/error path handles
    it uniformly and can RECOVER a truncated reasoning turn. The happy path is
    unchanged: when ``content`` is a normal non-empty string it is returned
    verbatim.
    """
    # Degrade every shape defect (absent choices/message/content, empty list,
    # non-dict payload) to the SAME diagnosable empty-response error rather than
    # trading one raw KeyError/IndexError/TypeError for another.
    try:
        choice = data["choices"][0]
    except (KeyError, IndexError, TypeError):
        choice = {}
    if not isinstance(choice, dict):
        choice = {}
    message = choice.get("message")
    if not isinstance(message, dict):
        message = {}
    content = message.get("content")
    if not content:
        finish_reason = choice.get("finish_reason")
        raise EmptyLLMResponse(provider, finish_reason=finish_reason)
    return content


def _strip_code_fences(text: str) -> str:
    """Drop ```/```json fence lines a model sometimes wraps its JSON in.

    Mirrors the fence-stripping the OpenRouter/Anthropic SPARQL paths already do,
    so the Cerebras path tolerates the same wrapping. A fence-free response is
    returned with only surrounding whitespace stripped (happy path unchanged).
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = "\n".join(
            l for l in stripped.split("\n") if not l.strip().startswith("```")
        )
    return stripped


def _salvage_sparql_field(text: str) -> str:
    """Best-effort extraction of the ``sparql`` string from a MALFORMED JSON blob.

    A reasoning model occasionally truncates its JSON mid-string (an unterminated
    ``"sparql": "SELECT …`` with no closing quote/brace) or otherwise emits JSON
    that ``json.loads`` rejects. Rather than throw the whole (possibly usable)
    query away, walk the characters after ``"sparql":`` honoring JSON escapes and
    stop at the first UNescaped closing quote — or at end-of-string when the model
    was cut off. Returns the recovered query text, or ``""`` when there is no
    ``sparql`` field to recover (which degrades to the empty-query escalation).
    """
    m = re.search(r'"sparql"\s*:\s*"', text)
    if not m:
        return ""
    i, n = m.end(), len(text)
    out: list[str] = []
    escapes = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/"}
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n:
            out.append(escapes.get(text[i + 1], text[i + 1]))
            i += 2
            continue
        if c == '"':
            break  # unescaped closing quote — end of the value
        out.append(c)
        i += 1
    return "".join(out).strip()


def _parse_sparql_gen_json(content: str) -> dict:
    """Tolerantly parse a SPARQL-generation JSON response.

    Well-formed JSON (the happy path) parses byte-identically to
    ``json.loads(content)`` — code fences, if any, are stripped first exactly as
    the OpenRouter path already does. On a JSON parse error (code-fence residue,
    an unterminated/truncated string, trailing prose) it SALVAGES the ``sparql``
    field so a truncated-but-usable query still runs; when nothing can be
    recovered it returns an EMPTY ``sparql`` so the caller's retry loop escalates
    (full ontology + explicit "produce a valid non-empty SELECT" feedback) instead
    of surfacing an uncaught ``JSONDecodeError``.
    """
    stripped = _strip_code_fences(content)
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return {
            "sparql": _salvage_sparql_field(stripped),
            "explanation": "",
            "functions_needed": [],
        }


# Max rows rendered in the plain-text answer before truncating. The old
# hard-coded 20 silently dropped most of a wide "list all ..." result; raise it
# and make it tunable. Truncation is now stated prominently (not buried) AND
# the slice is deterministic because generated SELECTs get a stable ORDER BY.
ANSWER_ROW_CAP = int(os.environ.get("INFONA_ANSWER_ROW_CAP", "100"))

# Embedding service singleton
_embedding_service = None


def _resolve_openrouter_api_key() -> str:
    """OpenRouter key for embeddings / LLM helpers.

    Settings uses ``INFONA_`` prefix (``INFONA_OPENROUTER_API_KEY``). OSS
    quickstart docs often set bare ``OPENROUTER_API_KEY`` — accept both so
    auto ontology embed turns on without a second env rename.
    """
    import os

    from infona_client.config import settings

    return (
        (getattr(settings, "openrouter_api_key", None) or "").strip()
        or (os.environ.get("INFONA_OPENROUTER_API_KEY") or "").strip()
        or (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    )


def get_embedding_service():
    """Lazy-init singleton for the ontology embedding service.

    Returns ``None`` only when no OpenRouter key is configured (cannot embed).
    """
    global _embedding_service
    if _embedding_service is None:
        from infona_client.config import settings

        key = _resolve_openrouter_api_key()
        if key:
            from infona_client.nlp.ontology_embeddings import OntologyEmbeddingService

            _embedding_service = OntologyEmbeddingService(
                openrouter_api_key=key,
                s3_bucket=settings.embeddings_s3_bucket,
                s3_prefix=settings.embeddings_s3_prefix,
            )
    return _embedding_service


def reset_embedding_service_for_tests() -> None:
    """Drop the process singleton (tests only)."""
    global _embedding_service
    _embedding_service = None

