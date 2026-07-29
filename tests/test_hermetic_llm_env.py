"""Guard: the default test run must stay hermetic w.r.t. LIVE LLM providers.

`nlp/pipeline.py` ends its provider dispatch with a bare
``if self._openrouter_key: return await self._generate_via_openrouter(...)``, and
`resolver/schema_resolver.py` / `resolver/csv_resolver.py` / `resolver/llm_router.py`
read OPENROUTER_API_KEY / CEREBRAS_API_KEY straight off ``os.environ``. So a
provider key sitting in the ambient environment silently REDIRECTS tests off the
Anthropic path they mock and onto a real network call.

That is not hypothetical: before `tests/conftest.py` started clearing these vars, a
dev shell with OPENROUTER_API_KEY exported saw 10 failures the CI runner never did
(test_pipeline.py ×6, test_pipeline_robustness.py, test_layer_aware_closure.py,
test_layered_reads.py, test_resolver_relationships.py) — none of them a product
defect. Most were a live 400; test_resolver_relationships instead failed on a
SUCCESSFUL (billed) completion whose verdict differed from the fixture's
expectation. Conversely the `test` job was green only because the runner exports no
provider key: adding one to the workflow env would have reddened CI with no code
change at all.

`test_conftest_declares_the_run_hermetic` is the test that bites IN CI. The other
two can only fail where a key is actually present, because "no key is visible" is
trivially true on a machine that never had one — deleting conftest's clearing block
would leave those two green on a CI runner. Asserting the sentinel that the
clearing block itself sets closes that hole.
"""

import os

import pytest

from cograph_client.nlp.pipeline import NLQueryPipeline
from tests.conftest import HERMETIC_SENTINEL_VAR, LIVE_PROVIDER_KEY_VARS

_live_run = os.environ.get("ONTA_TEST_ALLOW_LIVE_LLM") == "1"
_live_skip = pytest.mark.skipif(
    _live_run,
    reason="ONTA_TEST_ALLOW_LIVE_LLM=1 deliberately opts into live providers",
)


def test_conftest_declares_the_run_hermetic():
    """conftest actually ran its clearing block (or the run opted out loudly).

    This is the CI-effective guard: it fails on a runner with no keys at all if
    the clearing block is removed, whereas an "is a key absent?" assertion would
    stay vacuously green there.
    """
    if _live_run:
        assert os.environ.get(HERMETIC_SENTINEL_VAR) != "1", (
            f"{HERMETIC_SENTINEL_VAR} set despite ONTA_TEST_ALLOW_LIVE_LLM=1 — "
            "the escape hatch is not being honored"
        )
        return

    assert os.environ.get(HERMETIC_SENTINEL_VAR) == "1", (
        f"{HERMETIC_SENTINEL_VAR} not set — tests/conftest.py did not clear the "
        "live LLM provider credentials, so any key in the ambient environment "
        "will route tests to a real provider past their mocks. See its comment."
    )


@_live_skip
def test_no_ambient_live_provider_key():
    """No live-provider credential is visible to the default test run."""
    present = sorted(var for var in LIVE_PROVIDER_KEY_VARS if os.environ.get(var))
    assert not present, (
        f"{present} present during the test run — tests that mock only the "
        "Anthropic path will silently egress to a live provider instead. "
        "tests/conftest.py should have cleared these; see its comment."
    )


@_live_skip
def test_pipeline_dispatch_has_no_live_provider_key(mock_neptune):
    """A default-constructed pipeline resolves to the mockable Anthropic path.

    Asserts the property the pipeline tests actually depend on, one level below
    the env vars: whatever the config layer does, the constructed pipeline must
    not be holding an OpenRouter/Cerebras key, since either one wins the
    dispatch in `_generate_sparql` and bypasses the tests' mock.
    """
    pipeline = NLQueryPipeline(mock_neptune, "fake-key")

    assert not pipeline._openrouter_key, (
        "pipeline picked up an OpenRouter key — `_generate_sparql` would route "
        "to openrouter.ai and bypass the Anthropic mock"
    )
    assert not pipeline._cerebras_key, (
        "pipeline picked up a Cerebras key — `_generate_sparql` would route to "
        "api.cerebras.ai and bypass the Anthropic mock"
    )
