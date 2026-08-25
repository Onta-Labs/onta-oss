"""Hermetic: eval judge is a DeepSeek reasoning model and thinking is requested."""
from __future__ import annotations

import pytest

from infona_client.eval_llm import _assistant_text, _llm_call
from infona_client.eval_models import EVAL_MAX_TOKENS, EVAL_MODEL, EVAL_REASONING


def test_default_eval_model_is_latest_deepseek_reasoning():
    assert EVAL_MODEL == "deepseek/deepseek-v4-pro-0813"
    assert EVAL_REASONING.get("enabled") is True
    assert EVAL_REASONING.get("effort") == "high"
    assert EVAL_MAX_TOKENS >= 16384


def test_assistant_text_prefers_content_over_reasoning():
    text = _assistant_text({
        "choices": [{
            "message": {
                "content": '{"verdict": "correct"}',
                "reasoning": "I compared 16 to 16.",
            }
        }]
    })
    assert text == '{"verdict": "correct"}'


def test_assistant_text_strips_think_tags():
    text = _assistant_text({
        "choices": [{
            "message": {
                "content": "<think>scratch</think>\n{\"verdict\": \"wrong\"}",
            }
        }]
    })
    assert text == '{"verdict": "wrong"}'


def test_assistant_text_falls_back_to_reasoning():
    text = _assistant_text({
        "choices": [{
            "message": {
                "content": "",
                "reasoning": '{"verdict": "correct"}',
            }
        }]
    })
    assert text == '{"verdict": "correct"}'


def test_assistant_text_empty_raises():
    with pytest.raises(ValueError, match="empty LLM response"):
        _assistant_text({"choices": [{"message": {"content": ""}, "finish_reason": "length"}]})


def test_parse_json_extracts_object_from_prose():
    from infona_client.eval_llm import _parse_json

    data = _parse_json('scratch notes\n{"verdict": "correct", "n": 1}\ntrailing')
    assert data == {"verdict": "correct", "n": 1}


@pytest.mark.asyncio
async def test_llm_call_sends_reasoning_payload(monkeypatch):
    captured: dict = {}

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": '{"ok": true}'}}]}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["json"] = json
            return FakeResp()

    monkeypatch.setattr("infona_client.eval_llm.httpx.AsyncClient", FakeClient)
    text = await _llm_call(prompt="score this", api_key="k", json_mode=True)
    assert text == '{"ok": true}'
    body = captured["json"]
    assert body["model"] == "deepseek/deepseek-v4-pro-0813"
    assert body["reasoning"] == {"enabled": True, "effort": "high", "exclude": True}
    assert body["response_format"] == {"type": "json_object"}
    assert body["max_tokens"] >= 16384
    assert captured["timeout"] >= 600
