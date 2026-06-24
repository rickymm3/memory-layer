from __future__ import annotations

import requests

from app.config import get_config


class OllamaClient:
    def __init__(self, timeout_seconds: int = 120) -> None:
        cfg = get_config()
        if not cfg.ollama_host:
            raise RuntimeError(
                "OLLAMA_HOST is not set and WSL auto-detection failed. "
                "Set OLLAMA_HOST, or set LLM_PROVIDER=openai_compat with "
                "OPENAI_COMPAT_BASE_URL to use a different local LLM server."
            )
        self.base_url = cfg.ollama_host
        self.chat_model = cfg.chat_model
        self.embedding_model = cfg.embedding_model
        self.timeout_seconds = timeout_seconds

    def embed_text(self, text: str) -> list[float]:
        response = requests.post(
            f"{self.base_url}/api/embed",
            json={"model": self.embedding_model, "input": text},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        embeddings = data.get("embeddings", [])
        if not embeddings or not isinstance(embeddings[0], list):
            raise RuntimeError("Ollama embed response missing embeddings[0]")
        return embeddings[0]

    def generate_response(self, prompt: str, system: str | None = None, json_mode: bool = False) -> str:
        payload = {"model": self.chat_model, "prompt": prompt, "stream": False}
        if system:
            payload["system"] = system
        if json_mode:
            payload["format"] = "json"
        response = requests.post(
            f"{self.base_url}/api/generate",
            json=payload,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        text = data.get("response", "")
        if not isinstance(text, str):
            raise RuntimeError("Ollama generate response missing 'response' text")
        return text
