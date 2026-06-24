from __future__ import annotations

import os
from functools import lru_cache
from typing import Protocol, runtime_checkable

import requests
from dotenv import load_dotenv


@runtime_checkable
class LLMProvider(Protocol):
    """Interface for any LLM backend used by the memory layer.

    Implementations must provide both embed_text and generate_response.
    Chat-only providers (e.g. AnthropicClient) raise NotImplementedError for
    embed_text — use get_embedding_client() to get a provider that supports it.
    """

    def embed_text(self, text: str) -> list[float]: ...
    def generate_response(self, prompt: str, system: str | None = None, json_mode: bool = False) -> str: ...


class AnthropicClient:
    """Chat-only LLM client for the Anthropic Messages API.

    Does NOT implement embed_text — Anthropic has no public embeddings API.
    Use get_embedding_client() for embeddings (falls back to OpenAI or Ollama).

    Configure via .env:
        ANTHROPIC_API_KEY=sk-ant-...
        CHAT_MODEL=claude-opus-4-5      # or claude-3-5-haiku-latest, etc.
        CHAT_PROVIDER=anthropic         # optional if ANTHROPIC_API_KEY is set
    """

    _API_URL = "https://api.anthropic.com/v1/messages"
    _API_VERSION = "2023-06-01"
    _DEFAULT_MAX_TOKENS = 4096

    def __init__(
        self,
        api_key: str,
        chat_model: str = "claude-3-5-haiku-latest",
        timeout_seconds: int = 120,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
    ) -> None:
        self.api_key = api_key
        self.chat_model = chat_model
        self.timeout_seconds = timeout_seconds
        self.max_tokens = max_tokens

    def embed_text(self, text: str) -> list[float]:  # noqa: ARG002
        raise NotImplementedError(
            "AnthropicClient does not support embeddings. "
            "Set EMBEDDING_PROVIDER=openai or EMBEDDING_PROVIDER=ollama."
        )

    def generate_response(self, prompt: str, system: str | None = None, json_mode: bool = False) -> str:
        body: dict = {
            "model": self.chat_model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            body["system"] = system
        # Anthropic follows JSON instructions from the system prompt natively; no API flag needed.
        response = requests.post(
            self._API_URL,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": self._API_VERSION,
                "content-type": "application/json",
            },
            json=body,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        content_blocks = data.get("content", [])
        if not content_blocks:
            raise RuntimeError("Anthropic response has no content blocks")
        text = content_blocks[0].get("text", "")
        if not isinstance(text, str):
            raise RuntimeError("Anthropic response content is not a string")
        return text


class OpenAICompatibleClient:
    """LLM client for any server exposing the OpenAI chat/embeddings API.

    Covers: LM Studio, llama.cpp server, Jan, Ollama /v1/,
    text-generation-webui, and any other OpenAI-compatible local server.

    Configure via .env:
        LLM_PROVIDER=openai_compat
        OPENAI_COMPAT_BASE_URL=http://localhost:1234
        OPENAI_COMPAT_API_KEY=none   # leave as "none" for local servers
        CHAT_MODEL=...               # reuses existing env var
        EMBEDDING_MODEL=...          # reuses existing env var
    """

    def __init__(
        self,
        base_url: str,
        chat_model: str,
        embedding_model: str,
        api_key: str = "none",
        timeout_seconds: int = 120,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.chat_model = chat_model
        self.embedding_model = embedding_model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def embed_text(self, text: str) -> list[float]:
        response = requests.post(
            f"{self.base_url}/v1/embeddings",
            headers=self._headers(),
            json={"model": self.embedding_model, "input": text},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        items = data.get("data", [])
        if not items or "embedding" not in items[0]:
            raise RuntimeError(
                "OpenAI-compat embed response missing data[0].embedding"
            )
        return items[0]["embedding"]

    def generate_response(self, prompt: str, system: str | None = None, json_mode: bool = False) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body: dict = {"model": self.chat_model, "messages": messages, "stream": False}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        response = requests.post(
            f"{self.base_url}/v1/chat/completions",
            headers=self._headers(),
            json=body,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        choices = data.get("choices", [])
        if not choices or "message" not in choices[0]:
            raise RuntimeError(
                "OpenAI-compat chat response missing choices[0].message"
            )
        content = choices[0]["message"].get("content", "")
        if not isinstance(content, str):
            raise RuntimeError(
                "OpenAI-compat chat response has non-string content"
            )
        return content


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _openai_client_from_cfg(cfg) -> OpenAICompatibleClient:  # type: ignore[no-untyped-def]
    return OpenAICompatibleClient(
        base_url="https://api.openai.com/v1",
        chat_model=cfg.chat_model,
        embedding_model=cfg.embedding_model,
        api_key=os.getenv("OPENAI_API_KEY", "").strip(),
    )


def _openai_compat_local_from_cfg(cfg) -> OpenAICompatibleClient:  # type: ignore[no-untyped-def]
    return OpenAICompatibleClient(
        base_url=cfg.openai_compat_base_url,
        chat_model=cfg.chat_model,
        embedding_model=cfg.embedding_model,
        api_key=os.getenv("OPENAI_COMPAT_API_KEY", "none").strip(),
    )


# ---------------------------------------------------------------------------
# Public factories
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def get_chat_client() -> LLMProvider:
    """Return the best available chat LLM client.

    Auto-detection priority (highest -> lowest):
      1. anthropic     -- ANTHROPIC_API_KEY is set
      2. openai        -- OPENAI_API_KEY is set
      3. openai_compat -- OPENAI_COMPAT_BASE_URL is set
      4. ollama        -- default fallback

    Override with CHAT_PROVIDER=anthropic|openai|openai_compat|ollama.
    """
    load_dotenv()
    from app.config import get_config  # noqa: PLC0415

    cfg = get_config()
    override = cfg.chat_provider  # "auto", "anthropic", "openai", "openai_compat", "ollama"
    anthropic_key = cfg.anthropic_api_key
    openai_key = cfg.openai_api_key

    if override == "anthropic":
        if not anthropic_key:
            raise RuntimeError("CHAT_PROVIDER=anthropic requires ANTHROPIC_API_KEY")
        return AnthropicClient(api_key=anthropic_key, chat_model=cfg.chat_model)

    if override == "openai":
        if not openai_key:
            raise RuntimeError("CHAT_PROVIDER=openai requires OPENAI_API_KEY")
        return _openai_client_from_cfg(cfg)

    if override == "openai_compat":
        if not cfg.openai_compat_base_url:
            raise RuntimeError("CHAT_PROVIDER=openai_compat requires OPENAI_COMPAT_BASE_URL")
        return _openai_compat_local_from_cfg(cfg)

    if override == "ollama":
        from app.ollama_client import OllamaClient  # noqa: PLC0415
        return OllamaClient()

    # auto: highest-capability provider wins
    if anthropic_key:
        return AnthropicClient(api_key=anthropic_key, chat_model=cfg.chat_model)
    if openai_key:
        return _openai_client_from_cfg(cfg)
    if cfg.openai_compat_base_url:
        return _openai_compat_local_from_cfg(cfg)
    from app.ollama_client import OllamaClient  # noqa: PLC0415
    return OllamaClient()


@lru_cache(maxsize=1)
def get_embedding_client() -> LLMProvider:
    """Return the best available embedding client.

    Anthropic excluded -- no public embeddings API.
    Priority: openai -> openai_compat -> ollama.
    Override with EMBEDDING_PROVIDER=openai|openai_compat|ollama.

    If CHAT_PROVIDER=anthropic and no embedding provider is configured,
    this falls back to Ollama -- run Ollama locally or set OPENAI_API_KEY.
    """
    load_dotenv()
    from app.config import get_config  # noqa: PLC0415

    cfg = get_config()
    override = cfg.embedding_provider
    openai_key = cfg.openai_api_key

    if override == "openai":
        if not openai_key:
            raise RuntimeError("EMBEDDING_PROVIDER=openai requires OPENAI_API_KEY")
        return _openai_client_from_cfg(cfg)

    if override == "openai_compat":
        if not cfg.openai_compat_base_url:
            raise RuntimeError("EMBEDDING_PROVIDER=openai_compat requires OPENAI_COMPAT_BASE_URL")
        return _openai_compat_local_from_cfg(cfg)

    if override == "ollama":
        from app.ollama_client import OllamaClient  # noqa: PLC0415
        return OllamaClient()

    # auto: OpenAI if key present, else openai_compat, else Ollama
    if openai_key:
        return _openai_client_from_cfg(cfg)
    if cfg.openai_compat_base_url:
        return _openai_compat_local_from_cfg(cfg)
    from app.ollama_client import OllamaClient  # noqa: PLC0415
    return OllamaClient()


@lru_cache(maxsize=1)
def get_llm_client() -> LLMProvider:
    """Backward-compatible alias for get_chat_client().

    New code should call get_chat_client() or get_embedding_client() directly.
    """
    return get_chat_client()
