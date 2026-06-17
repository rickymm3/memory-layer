"""Image and video generation provider abstraction.

Providers:
  image  — openai (DALL-E 3)  |  replicate (FLUX Schnell)  |  none (no-op)
  video  — replicate (Minimax Video-01)                     |  none (no-op)

All provider packages are lazy-imported so the app boots without them installed.
Set IMAGE_PROVIDER / VIDEO_PROVIDER to "auto" (default) for detection by key presence.
"""
from __future__ import annotations

import os
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


# ── Result ────────────────────────────────────────────────────────────────────

class GenerationResult:
    def __init__(
        self,
        url: str | None = None,
        local_path: str | None = None,
        error: str | None = None,
        provider: str = "unknown",
        prompt: str = "",
    ):
        self.url = url
        self.local_path = local_path  # relative to dashboard/static/
        self.error = error
        self.provider = provider
        self.prompt = prompt
        self.ok = error is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "local_path": self.local_path,
            "error": self.error,
            "provider": self.provider,
            "prompt": self.prompt,
            "ok": self.ok,
        }


# ── Image providers ───────────────────────────────────────────────────────────

class BaseImageProvider(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    def available(self) -> bool:
        return True

    @abstractmethod
    def generate(self, prompt: str, size: str = "1024x1024") -> GenerationResult: ...


class NoOpImageProvider(BaseImageProvider):
    @property
    def name(self) -> str:
        return "none"

    @property
    def available(self) -> bool:
        return False

    def generate(self, prompt: str, size: str = "1024x1024") -> GenerationResult:
        return GenerationResult(
            error="No image provider configured. Add OPENAI_API_KEY or REPLICATE_API_TOKEN to .env",
            provider=self.name,
            prompt=prompt,
        )


class DallEProvider(BaseImageProvider):
    def __init__(self, api_key: str):
        self._key = api_key

    @property
    def name(self) -> str:
        return "openai/dall-e-3"

    def generate(self, prompt: str, size: str = "1024x1024") -> GenerationResult:
        try:
            from openai import OpenAI
        except ImportError:
            return GenerationResult(
                error="openai package not installed — run: pip install openai",
                provider=self.name, prompt=prompt,
            )
        try:
            client = OpenAI(api_key=self._key)
            valid_sizes = {"1024x1024", "1792x1024", "1024x1792"}
            safe_size = size if size in valid_sizes else "1024x1024"
            resp = client.images.generate(
                model="dall-e-3", prompt=prompt, size=safe_size, quality="standard", n=1,
            )
            image_url = resp.data[0].url
            local_path = _download_image(image_url)
            return GenerationResult(url=image_url, local_path=local_path,
                                    provider=self.name, prompt=prompt)
        except Exception as exc:
            return GenerationResult(error=str(exc), provider=self.name, prompt=prompt)


class ReplicateImageProvider(BaseImageProvider):
    MODEL = "black-forest-labs/flux-schnell"

    def __init__(self, api_token: str):
        self._token = api_token

    @property
    def name(self) -> str:
        return "replicate/flux-schnell"

    def generate(self, prompt: str, size: str = "1024x1024") -> GenerationResult:
        try:
            import replicate
        except ImportError:
            return GenerationResult(
                error="replicate package not installed — run: pip install replicate",
                provider=self.name, prompt=prompt,
            )
        try:
            os.environ.setdefault("REPLICATE_API_TOKEN", self._token)
            w, h = _parse_size(size)
            output = replicate.run(
                self.MODEL,
                input={"prompt": prompt, "width": w, "height": h, "num_outputs": 1},
            )
            image_url = str(output[0]) if isinstance(output, list) else str(output)
            local_path = _download_image(image_url)
            return GenerationResult(url=image_url, local_path=local_path,
                                    provider=self.name, prompt=prompt)
        except Exception as exc:
            return GenerationResult(error=str(exc), provider=self.name, prompt=prompt)


# ── Video providers ───────────────────────────────────────────────────────────

class BaseVideoProvider(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    def available(self) -> bool:
        return True

    @abstractmethod
    def generate(self, prompt: str) -> GenerationResult: ...


class NoOpVideoProvider(BaseVideoProvider):
    @property
    def name(self) -> str:
        return "none"

    @property
    def available(self) -> bool:
        return False

    def generate(self, prompt: str) -> GenerationResult:
        return GenerationResult(
            error="No video provider configured. Add REPLICATE_API_TOKEN to .env",
            provider=self.name, prompt=prompt,
        )


class ReplicateVideoProvider(BaseVideoProvider):
    MODEL = "minimax/video-01"

    def __init__(self, api_token: str):
        self._token = api_token

    @property
    def name(self) -> str:
        return "replicate/minimax-video-01"

    def generate(self, prompt: str) -> GenerationResult:
        try:
            import replicate
        except ImportError:
            return GenerationResult(
                error="replicate package not installed — run: pip install replicate",
                provider=self.name, prompt=prompt,
            )
        try:
            os.environ.setdefault("REPLICATE_API_TOKEN", self._token)
            output = replicate.run(self.MODEL, input={"prompt": prompt})
            video_url = str(output[0]) if isinstance(output, list) else str(output)
            return GenerationResult(url=video_url, provider=self.name, prompt=prompt)
        except Exception as exc:
            return GenerationResult(error=str(exc), provider=self.name, prompt=prompt)


# ── Factories ─────────────────────────────────────────────────────────────────

def get_image_provider() -> BaseImageProvider:
    from app.config import get_config
    config = get_config()
    mode = os.getenv("IMAGE_PROVIDER", "auto").strip().lower()
    replicate_token = os.getenv("REPLICATE_API_TOKEN", "").strip()

    if mode == "openai" or (mode == "auto" and config.openai_api_key):
        if config.openai_api_key:
            return DallEProvider(config.openai_api_key)
    if mode == "replicate" or (mode == "auto" and replicate_token):
        if replicate_token:
            return ReplicateImageProvider(replicate_token)
    return NoOpImageProvider()


def get_video_provider() -> BaseVideoProvider:
    replicate_token = os.getenv("REPLICATE_API_TOKEN", "").strip()
    mode = os.getenv("VIDEO_PROVIDER", "auto").strip().lower()

    if mode == "replicate" or (mode == "auto" and replicate_token):
        if replicate_token:
            return ReplicateVideoProvider(replicate_token)
    return NoOpVideoProvider()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_size(size: str) -> tuple[int, int]:
    try:
        w, h = size.lower().split("x")
        return int(w), int(h)
    except Exception:
        return 1024, 1024


def _generated_dir() -> Path:
    here = Path(__file__).parent.parent
    d = here / "dashboard" / "static" / "generated"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _download_image(url: str) -> str | None:
    """Download image to dashboard/static/generated/ and return Flask-relative path."""
    try:
        import requests
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        ct = resp.headers.get("content-type", "")
        ext = ".png" if "png" in ct else ".webp" if "webp" in ct else ".jpg"
        filename = f"img_{uuid.uuid4().hex[:12]}{ext}"
        (_generated_dir() / filename).write_bytes(resp.content)
        return f"generated/{filename}"
    except Exception:
        return None


def list_recent_images(limit: int = 24) -> list[str]:
    """Return Flask-relative paths for the most recent saved images, newest first."""
    d = _generated_dir()
    files = sorted(d.glob("img_*"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [f"generated/{p.name}" for p in files[:limit]]
