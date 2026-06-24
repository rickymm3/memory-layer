"""Image and video generation provider abstraction.

Providers:
  image  — comfyui (local, z_image_turbo)  |  openai (DALL-E 3)  |  replicate (FLUX Schnell)  |  none (no-op)
  video  — replicate (Minimax Video-01)                                                         |  none (no-op)

ComfyUI provider connects to a local ComfyUI server (default http://localhost:8188).
Set COMFYUI_URL in .env to override. IMAGE_PROVIDER=comfyui or IMAGE_PROVIDER=auto
(auto picks comfyui first if the server is reachable).
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


class ComfyUIProvider(BaseImageProvider):
    """Local image generation via ComfyUI (z_image_turbo + Qwen 3 text encoder)."""

    # Hard cap to avoid VRAM OOM on constrained GPUs (e.g. RTX 5070 12.8 GB with a 12 GB model).
    # Dimensions are scaled proportionally then rounded to the nearest 64-pixel boundary.
    _MAX_SAFE_DIM = 768
    _ALIGN = 64

    # z_image_turbo workflow in ComfyUI API format (derived from built-in blueprint)
    _WORKFLOW: dict = {
        "66": {"class_type": "UNETLoader",
               "inputs": {"unet_name": "z_image_turbo_bf16.safetensors", "weight_dtype": "default"}},
        "62": {"class_type": "CLIPLoader",
               "inputs": {"clip_name": "qwen_3_4b.safetensors", "type": "lumina2", "device": "default"}},
        "70": {"class_type": "ModelSamplingAuraFlow",
               "inputs": {"model": ["66", 0], "shift": 3.0}},
        "67": {"class_type": "CLIPTextEncode",
               "inputs": {"text": "__PROMPT__", "clip": ["62", 0]}},
        "71": {"class_type": "CLIPTextEncode",
               "inputs": {"text": "", "clip": ["62", 0]}},
        "68": {"class_type": "EmptySD3LatentImage",
               "inputs": {"width": 768, "height": 768, "batch_size": 1}},
        "69": {"class_type": "KSampler",
               "inputs": {"model": ["70", 0], "positive": ["67", 0], "negative": ["71", 0],
                          "latent_image": ["68", 0], "seed": 0, "steps": 20,
                          "cfg": 4.0, "sampler_name": "euler", "scheduler": "normal", "denoise": 1.0}},
        "63": {"class_type": "VAELoader",
               "inputs": {"vae_name": "ae.safetensors"}},
        "65": {"class_type": "VAEDecode",
               "inputs": {"samples": ["69", 0], "vae": ["63", 0]}},
        "100": {"class_type": "SaveImage",
                "inputs": {"images": ["65", 0], "filename_prefix": "memory-layer"}},
    }

    @classmethod
    def _safe_dims(cls, w: int, h: int) -> tuple[int, int]:
        """Scale w×h proportionally to fit within _MAX_SAFE_DIM, aligned to _ALIGN."""
        scale = min(1.0, cls._MAX_SAFE_DIM / max(w, h))
        a = cls._ALIGN
        return (
            max(a, round(w * scale / a) * a),
            max(a, round(h * scale / a) * a),
        )

    def __init__(self, url: str = "http://localhost:8188"):
        self._url = url.rstrip("/")

    @property
    def name(self) -> str:
        return "comfyui/z_image_turbo"

    @classmethod
    def is_running(cls, url: str = "http://localhost:8188") -> bool:
        try:
            import requests as _r
            resp = _r.get(f"{url}/system_stats", timeout=2)
            return resp.status_code == 200
        except Exception:
            return False

    def generate(self, prompt: str, size: str = "1024x1024") -> GenerationResult:
        import copy, time, requests as req
        w, h = _parse_size(size)
        w, h = self._safe_dims(w, h)  # clamp to avoid VRAM OOM
        workflow = copy.deepcopy(self._WORKFLOW)
        workflow["67"]["inputs"]["text"] = prompt
        workflow["68"]["inputs"]["width"] = w
        workflow["68"]["inputs"]["height"] = h
        workflow["69"]["inputs"]["seed"] = int(time.time()) % (2**31)

        try:
            # Submit
            resp = req.post(f"{self._url}/prompt",
                            json={"prompt": workflow}, timeout=10)
            resp.raise_for_status()
            prompt_id = resp.json()["prompt_id"]

            # Poll until done (up to 5 minutes)
            for _ in range(300):
                time.sleep(1)
                try:
                    hist = req.get(f"{self._url}/history/{prompt_id}", timeout=5).json()
                except (req.exceptions.ConnectionError, req.exceptions.Timeout):
                    return GenerationResult(
                        error="ComfyUI stopped responding mid-generation (server crashed or was shut down)",
                        provider=self.name, prompt=prompt,
                    )
                if prompt_id in hist:
                    outputs = hist[prompt_id].get("outputs", {})
                    for node_out in outputs.values():
                        for img in node_out.get("images", []):
                            params = {"filename": img["filename"],
                                      "subfolder": img.get("subfolder", ""),
                                      "type": img.get("type", "output")}
                            img_resp = req.get(f"{self._url}/view",
                                               params=params, timeout=30)
                            img_resp.raise_for_status()
                            local_path = _save_image_bytes(img_resp.content)
                            return GenerationResult(
                                url=f"{self._url}/view?filename={img['filename']}",
                                local_path=local_path,
                                provider=self.name, prompt=prompt,
                            )
                    # outputs present but no images — error
                    err = hist[prompt_id].get("status", {})
                    return GenerationResult(error=f"ComfyUI error: {err}",
                                            provider=self.name, prompt=prompt)
            return GenerationResult(error="ComfyUI timed out after 5 minutes",
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
    comfyui_url = os.getenv("COMFYUI_URL", "http://localhost:8188").strip()
    replicate_token = os.getenv("REPLICATE_API_TOKEN", "").strip()

    if mode == "comfyui":
        return ComfyUIProvider(comfyui_url)
    if mode == "auto" and ComfyUIProvider.is_running(comfyui_url):
        return ComfyUIProvider(comfyui_url)
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


def _sniff_ext(data: bytes) -> str:
    """Return file extension from image magic bytes, defaulting to .png."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".png"


def _save_image_bytes(data: bytes) -> str | None:
    """Save raw image bytes to generated/ dir and return Flask-relative path."""
    try:
        ext = _sniff_ext(data)
        filename = f"img_{uuid.uuid4().hex[:12]}{ext}"
        (_generated_dir() / filename).write_bytes(data)
        return f"generated/{filename}"
    except Exception:
        return None


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
