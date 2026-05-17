import src.llm as llm_mod
from src.llm import call_stage1


class _FakeSettings:
    def __init__(self, temp=0.1):
        self.llm_stage1 = {
            "provider": "mistral",
            "model": "mistral-small",
            "temperature": temp,
            "max_tokens": 100,
            "timeout_seconds": 5,
        }
        self.llm_fallback_chain = []
        self.mistral_api_key = "test-key"


def test_call_stage1_uses_config_temperature_when_no_override(monkeypatch):
    captured = {}

    def fake_call(base_url, api_key, model, system, prompt, temperature, max_tokens, timeout, path, extra_headers):
        captured["temperature"] = temperature
        return "[]"

    monkeypatch.setattr(llm_mod, "_call_openai_compat", fake_call)
    call_stage1("p", "s", _FakeSettings(temp=0.1))
    assert captured["temperature"] == 0.1


def test_call_stage1_override_wins_over_config(monkeypatch):
    captured = {}

    def fake_call(base_url, api_key, model, system, prompt, temperature, max_tokens, timeout, path, extra_headers):
        captured["temperature"] = temperature
        return "[]"

    monkeypatch.setattr(llm_mod, "_call_openai_compat", fake_call)
    call_stage1("p", "s", _FakeSettings(temp=0.1), temperature=0.3)
    assert captured["temperature"] == 0.3


def test_call_stage1_override_zero_is_respected(monkeypatch):
    captured = {}

    def fake_call(base_url, api_key, model, system, prompt, temperature, max_tokens, timeout, path, extra_headers):
        captured["temperature"] = temperature
        return "[]"

    monkeypatch.setattr(llm_mod, "_call_openai_compat", fake_call)
    call_stage1("p", "s", _FakeSettings(temp=0.5), temperature=0.0)
    assert captured["temperature"] == 0.0
