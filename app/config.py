import os
import subprocess
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv


DEFAULT_DATABASE_URL = (
    "postgresql://memory:memory_dev_password@localhost:5432/memory_layer_development"
)
DEFAULT_CHAT_MODEL = "qwen3:8b"
DEFAULT_EMBEDDING_MODEL = "qwen3-embedding:latest"


def _detect_windows_host_ip() -> str | None:
    try:
        result = subprocess.run(
            ["ip", "route"],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return None

    for line in result.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) >= 3 and parts[0] == "default" and parts[1] == "via":
            return parts[2]
    return None


def _resolve_ollama_host() -> str | None:
    env_host = os.getenv("OLLAMA_HOST", "").strip()
    if env_host:
        return env_host.rstrip("/")

    detected_ip = _detect_windows_host_ip()
    if detected_ip:
        return f"http://{detected_ip}:11434"
    return None


DEFAULT_MEMORY_RETRIEVAL_THRESHOLD = 0.60


@dataclass(frozen=True)
class AppConfig:
    database_url: str
    ollama_host: str | None
    chat_model: str
    embedding_model: str
    openai_compat_base_url: str | None
    openai_compat_api_key: str
    anthropic_api_key: str | None
    openai_api_key: str | None
    chat_provider: str
    embedding_provider: str
    memory_retrieval_threshold: float
    web_research_enabled: bool
    web_search_provider: str
    web_search_api_key: str
    history_window_size: int
    adaptive_retrieval_enabled: bool
    adaptive_retrieval_thresholds: tuple[float, ...]


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    load_dotenv()

    return AppConfig(
        database_url=os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL).strip(),
        ollama_host=_resolve_ollama_host(),
        chat_model=os.getenv("CHAT_MODEL", DEFAULT_CHAT_MODEL).strip(),
        embedding_model=os.getenv("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL).strip(),
        openai_compat_base_url=os.getenv("OPENAI_COMPAT_BASE_URL", "").strip() or None,
        openai_compat_api_key=os.getenv("OPENAI_COMPAT_API_KEY", "none").strip(),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip() or None,
        openai_api_key=os.getenv("OPENAI_API_KEY", "").strip() or None,
        chat_provider=os.getenv("CHAT_PROVIDER", "auto").strip().lower(),
        embedding_provider=os.getenv("EMBEDDING_PROVIDER", "auto").strip().lower(),
        memory_retrieval_threshold=float(
            os.getenv("MEMORY_RETRIEVAL_THRESHOLD", DEFAULT_MEMORY_RETRIEVAL_THRESHOLD)
        ),
        web_research_enabled=os.getenv("WEB_RESEARCH_ENABLED", "false").strip().lower()
        in ("true", "1", "yes"),
        web_search_provider=os.getenv("WEB_SEARCH_PROVIDER", "none").strip(),
        web_search_api_key=os.getenv("WEB_SEARCH_API_KEY", "").strip(),
        history_window_size=int(os.getenv("HISTORY_WINDOW_SIZE", "6")),
        adaptive_retrieval_enabled=os.getenv("ADAPTIVE_RETRIEVAL_ENABLED", "true").strip().lower()
        in ("true", "1", "yes"),
        adaptive_retrieval_thresholds=tuple(
            float(t.strip())
            for t in os.getenv("ADAPTIVE_RETRIEVAL_THRESHOLDS", "0.60,0.40,0.20").split(",")
        ),
    )
