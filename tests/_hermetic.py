"""Shared constants for the test suite's live-LLM hermeticity.

Lives in its own module (like `tests/_enrichment_prov_helpers.py`) so that
`tests/conftest.py`, `tests/test_hermetic_llm_env.py`, and the opt-in live
integration tests can all reference one definition. Importing these from
`conftest` instead would execute it a second time under a different module name.

Deliberately imports nothing from `cograph_client`: `conftest` pulls this in
before the package is first imported, and must not trigger that import early.
"""

import os

# Credentials that route code onto a live LLM provider. `nlp/pipeline.py` and the
# `resolver/` modules read the two unprefixed ones straight off os.environ; the
# OMNIX_-prefixed ones reach the same code through the `config.Settings` singleton.
LIVE_PROVIDER_KEY_VARS = (
    "OPENROUTER_API_KEY",
    "CEREBRAS_API_KEY",
    "ANTHROPIC_API_KEY",
    "OMNIX_OPENROUTER_API_KEY",
    "OMNIX_CEREBRAS_API_KEY",
    "OMNIX_ANTHROPIC_API_KEY",
)

# Opt back in to real providers for a deliberate live run.
ALLOW_LIVE_VAR = "ONTA_TEST_ALLOW_LIVE_LLM"

# Set by conftest when it has cleared the vars above. Asserting this — rather than
# only "no key is present" — is what lets the guard fail on a CI runner that never
# had a key to begin with.
HERMETIC_SENTINEL_VAR = "_ONTA_TEST_HERMETIC_LLM"


def live_llm_opted_in() -> bool:
    """True when the run explicitly asked to keep real provider credentials."""
    return os.environ.get(ALLOW_LIVE_VAR) == "1"
