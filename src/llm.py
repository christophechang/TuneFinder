"""
LLM cascade pattern — two-stage.

Stage 1 (extraction/classification):
  Primary: Mistral Small
  All providers use the OpenAI-compatible /v1/chat/completions endpoint,
  except Gemini which uses /chat/completions under its v1beta OpenAI-compat base URL.
  Sleep 1s between failures. Raise RuntimeError if all exhausted.

Stage 2 (report generation):
  Primary: OpenRouter / DeepSeek
"""
import re
import time

import requests

from src.logger import get_logger

logger = get_logger(__name__)

_THINK_RE = re.compile(r'<think(?:ing)?>.*?</think(?:ing)?>', re.DOTALL | re.IGNORECASE)


def _strip_thinking(text: str) -> str:
    """Remove inline thinking blocks emitted by reasoning models."""
    return _THINK_RE.sub('', text).strip()


_PROVIDER_BASE_URLS = {
    "mistral": "https://api.mistral.ai",
    "groq": "https://api.groq.com/openai",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "openrouter": "https://openrouter.ai/api",
}

# Non-standard paths (default is /v1/chat/completions)
_PROVIDER_PATHS = {
    "gemini": "/chat/completions",
}

_API_KEY_ATTRS = {
    "mistral": "mistral_api_key",
    "groq": "groq_api_key",
    "gemini": "gemini_api_key",
    "openrouter": "openrouter_api_key",
}


def _call_openai_compat(
    base_url: str,
    api_key: str,
    model: str,
    system: str,
    prompt: str,
    temperature: float,
    max_tokens: int,
    timeout: int,
    path: str = "/v1/chat/completions",
) -> str:
    url = f"{base_url.rstrip('/')}{path}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    return _strip_thinking(content)


def call_stage1(prompt: str, system: str, settings) -> str:
    """
    Cheap, fast extraction/classification via cascade chain.
    Chain order: llm.stage1 (primary) then each entry in llm.fallback_chain.
    Providers with no API key are silently skipped.
    Sleeps 1s between failures. Raises RuntimeError if all exhausted.
    """
    stage1_cfg = settings.llm_stage1
    temperature = stage1_cfg.get("temperature", 0.1)
    max_tokens = stage1_cfg.get("max_tokens", 4096)
    timeout = stage1_cfg.get("timeout_seconds", 60)

    # Primary first, then fallback chain.
    # Fallback entries only specify provider+model; they inherit temperature/max_tokens/timeout
    # from the primary config above — intentional to keep fallback config minimal.
    chain = [
        {"provider": stage1_cfg.get("provider", "mistral"), "model": stage1_cfg.get("model", "")}
    ] + settings.llm_fallback_chain

    last_error = None

    for entry in chain:
        provider = entry.get("provider", "")
        model = entry.get("model", "")

        try:
            logger.info(f"[llm.stage1] Trying {provider} ({model})")

            if provider in _PROVIDER_BASE_URLS:
                api_key = getattr(settings, _API_KEY_ATTRS.get(provider, ""), "")
                if not api_key:
                    logger.debug(f"[llm.stage1] Skipping {provider} — no API key set")
                    continue
                path = _PROVIDER_PATHS.get(provider, "/v1/chat/completions")
                result = _call_openai_compat(
                    base_url=_PROVIDER_BASE_URLS[provider],
                    path=path,
                    api_key=api_key,
                    model=model,
                    system=system,
                    prompt=prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout=timeout,
                )

            else:
                logger.warning(f"[llm.stage1] Unknown provider in config: {provider} — skipping")
                continue

            logger.info(f"[llm.stage1] Success via: {provider}")
            return result

        except Exception as e:
            logger.warning(f"[llm.stage1] {provider} failed: {e}")
            last_error = e
            time.sleep(1)

    raise RuntimeError(f"All Stage 1 LLM providers exhausted. Last error: {last_error}")


def call_stage2(prompt: str, system: str, settings) -> str:
    """
    Report generation via cascade chain.
    Chain order: llm.stage2 (primary) then each entry in llm.stage2_fallback_chain.
    Providers with no API key are silently skipped.
    Sleeps 1s between failures. Raises RuntimeError if all exhausted.
    """
    stage2_cfg = settings.llm_stage2
    max_tokens = stage2_cfg.get("max_tokens", 4096)
    temperature = stage2_cfg.get("temperature", 0.2)
    timeout = stage2_cfg.get("timeout_seconds", 90)

    # Fallback entries inherit temperature/max_tokens/timeout from primary — intentional.
    chain = [
        {"provider": stage2_cfg.get("provider", "openrouter"), "model": stage2_cfg.get("model", "deepseek/deepseek-chat")}
    ] + settings.llm_stage2_fallback_chain

    last_error = None

    for entry in chain:
        provider = entry.get("provider", "")
        model = entry.get("model", "")

        try:
            logger.info(f"[llm.stage2] Trying {provider} ({model})")

            if provider in _PROVIDER_BASE_URLS:
                api_key = getattr(settings, _API_KEY_ATTRS.get(provider, ""), "")
                if not api_key:
                    logger.debug(f"[llm.stage2] Skipping {provider} — no API key set")
                    continue
                path = _PROVIDER_PATHS.get(provider, "/v1/chat/completions")
                result = _call_openai_compat(
                    base_url=_PROVIDER_BASE_URLS[provider],
                    path=path,
                    api_key=api_key,
                    model=model,
                    system=system,
                    prompt=prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout=timeout,
                )

            else:
                logger.warning(f"[llm.stage2] Unknown provider in config: {provider} — skipping")
                continue

            logger.info(f"[llm.stage2] Success via: {provider}")
            return result

        except Exception as e:
            logger.warning(f"[llm.stage2] {provider} failed: {e}")
            last_error = e
            time.sleep(1)

    raise RuntimeError(f"All Stage 2 LLM providers exhausted. Last error: {last_error}")
