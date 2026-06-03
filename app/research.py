"""Web research abstraction for dashboard chat fallback.

Provides a configurable research provider with a no-op default.
If no provider or API key is configured, research is silently disabled.

Providers
---------
none   — NoOpProvider (default); always unavailable, returns []
brave  — Brave Search API (requires WEB_SEARCH_PROVIDER=brave + WEB_SEARCH_API_KEY)
tavily — Tavily Search API (requires WEB_SEARCH_PROVIDER=tavily + WEB_SEARCH_API_KEY)

Config keys (from .env or environment)
---------------------------------------
WEB_RESEARCH_ENABLED=false   # master switch; opt-in, default off
WEB_SEARCH_PROVIDER=none     # provider identifier
WEB_SEARCH_API_KEY=          # API key for paid providers

SECURITY guardrails
--------------------
- Do not send secrets, connection strings, env values, or project-private data
  to external search providers.
- Raw web search results are never auto-stored as memory atoms.
- Any durable project lesson found via research must go through the existing
  write policy (extract → reconcile → store_memory_auto / propose_memory_signal).
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any

try:
    import requests as _requests

    _REQUESTS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _requests = None  # type: ignore[assignment]
    _REQUESTS_AVAILABLE = False


# ---------------------------------------------------------------------------
# Trigger / suppression patterns
# ---------------------------------------------------------------------------

# Phrases that indicate the user does NOT want web research this turn.
_NO_SEARCH_PHRASES: tuple[str, ...] = (
    "don't search",
    "do not search",
    "no search",
    "no web",
    "local only",
    "offline",
)

# Patterns that indicate sensitive content that must NOT be sent to external providers.
_SENSITIVE_RE = re.compile(
    r"\b(password|secret|api[_\s]?key|token|database_url|connection[_\s]?string"
    r"|private[_\s]?key|auth[_\s]?token|bearer|passphrase|credentials)\b",
    re.IGNORECASE,
)

# Minimum similarity score summarised in the LLM classification prompt context.
_LOCAL_CONTEXT_THRESHOLD = 0.45


def _is_sensitive(text: str) -> bool:
    """Return True if text appears to contain sensitive data."""
    return bool(_SENSITIVE_RE.search(text))


# URL detection — used to auto-fetch pages the user links to.
_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)


def extract_urls(message: str) -> list[str]:
    """Return all HTTP/HTTPS URLs found in *message*."""
    return _URL_RE.findall(message)


def fetch_url_content(url: str, timeout: int = 8) -> "dict | None":
    """Fetch *url* and return ``{title, url, snippet}`` with extracted plain text.

    Strips HTML tags and returns the first 4 000 chars of visible text.
    Returns None on any error or when content is too short to be useful.
    Only fetches http/https URLs; skips sensitive-looking URLs.
    """
    if not _REQUESTS_AVAILABLE:
        return None
    if not url.lower().startswith(("http://", "https://")):
        return None
    if _is_sensitive(url):
        return None
    try:
        resp = _requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; memory-layer/1.0; +research)"},
            timeout=timeout,
            allow_redirects=True,
        )
        resp.raise_for_status()
        ct = resp.headers.get("content-type", "")
        if "text" not in ct and "html" not in ct:
            return None
        text = re.sub(r"<[^>]+>", " ", resp.text)
        text = re.sub(r"&[a-zA-Z0-9#]+;", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        snippet = text[:4000]
        if len(snippet) < 80:
            return None
        return {"title": url, "url": url, "snippet": snippet}
    except Exception:
        return None


def fetch_urls_in_message(message: str) -> list[dict]:
    """Extract all URLs from *message* and fetch their content.

    Returns a list of ``{title, url, snippet}`` dicts — one per successfully
    fetched URL.  Skips URLs that fail or return too little content.
    Capped at 3 URLs per turn to limit latency.
    """
    results: list[dict] = []
    for url in extract_urls(message)[:3]:
        hit = fetch_url_content(url)
        if hit:
            results.append(hit)
    return results


def classify_research_intent(
    user_message: str,
    memories: list[dict],
    llm: Any,
) -> bool:
    """Ask the LLM whether this message requires current external information from the web.

    Returns True (research needed) or False (no research needed).
    On any LLM error, returns False — fail closed.
    """
    if memories:
        best_sim = max(float(m.get("similarity", 0.0)) for m in memories)
        memory_note = (
            f"{len(memories)} relevant stored memories available "
            f"(best similarity score: {best_sim:.2f})"
        )
    else:
        memory_note = "no relevant stored memories available"

    prompt = (
        "Decide whether the user message below requires current external information "
        "from the web to answer accurately.\n\n"
        "Answer YES if the message asks about:\n"
        "- current events, news, recent software releases, or changelogs\n"
        "- live documentation, API specifications, or real-world status\n"
        "- installation steps, setup guides, or how-to instructions for a specific tool or project\n"
        "- something the user explicitly wants looked up, searched, or fetched from the web\n\n"
        "Answer NO if the message is:\n"
        "- a greeting, casual chat, or conversational opener\n"
        "- a question answerable from stored memory or general knowledge\n"
        "- a coding task, instruction, preference, correction, or discussion topic\n"
        "- something the user wants to share or teach, not look up\n\n"
        f"Memory context: {memory_note}\n"
        f"User message: {user_message}\n\n"
        "Reply with one word only: YES or NO."
    )
    try:
        response = llm.generate_response(prompt).strip().upper()
        return response.startswith("YES")
    except Exception:
        return False  # fail closed — do not trigger research if LLM is unavailable


# ---------------------------------------------------------------------------
# Provider interface + implementations
# ---------------------------------------------------------------------------


class BaseResearchProvider(ABC):
    """Abstract base for web research providers."""

    @abstractmethod
    def search(self, query: str, limit: int = 3) -> list[dict[str, Any]]:
        """Return up to *limit* result dicts: [{title, url, snippet}].

        Returns [] on error or when unavailable.
        """

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if the provider is configured and can serve requests."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short provider identifier (e.g. 'brave', 'none')."""


class NoOpProvider(BaseResearchProvider):
    """Disabled / stub provider.

    Used when WEB_RESEARCH_ENABLED=false, WEB_SEARCH_PROVIDER=none, or no API key.
    """

    def search(self, query: str, limit: int = 3) -> list[dict[str, Any]]:
        return []

    def is_available(self) -> bool:
        return False

    @property
    def name(self) -> str:
        return "none"


class BraveSearchProvider(BaseResearchProvider):
    """Brave Search API provider.

    Configuration::

        WEB_SEARCH_PROVIDER=brave
        WEB_SEARCH_API_KEY=<your-brave-api-key>

    Free tier: https://brave.com/search/api/
    """

    _API_URL = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key.strip()

    def is_available(self) -> bool:
        return bool(
            _REQUESTS_AVAILABLE
            and self._api_key
            and self._api_key.lower() not in ("", "none")
        )

    def search(self, query: str, limit: int = 3) -> list[dict[str, Any]]:
        if not self.is_available():
            return []

        try:
            resp = _requests.get(
                self._API_URL,
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip",
                    "X-Subscription-Token": self._api_key,
                },
                params={"q": query, "count": min(limit, 10)},
                timeout=8,
            )
            resp.raise_for_status()
            data = resp.json()
            results: list[dict[str, Any]] = []
            for item in data.get("web", {}).get("results", [])[:limit]:
                results.append(
                    {
                        "title": item.get("title", ""),
                        "url": item.get("url", ""),
                        "snippet": item.get("description", ""),
                    }
                )
            return results
        except Exception:
            return []

    @property
    def name(self) -> str:
        return "brave"


class TavilySearchProvider(BaseResearchProvider):
    """Tavily Search API provider.

    Configuration::

        WEB_SEARCH_PROVIDER=tavily
        WEB_SEARCH_API_KEY=<your-tavily-api-key>

    Sign up: https://tavily.com/
    """

    _API_URL = "https://api.tavily.com/search"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key.strip()

    def is_available(self) -> bool:
        return bool(
            _REQUESTS_AVAILABLE
            and self._api_key
            and self._api_key.lower() not in ("", "none")
        )

    def search(self, query: str, limit: int = 3) -> list[dict[str, Any]]:
        if not self.is_available():
            return []

        try:
            resp = _requests.post(
                self._API_URL,
                json={
                    "api_key": self._api_key,
                    "query": query,
                    "max_results": min(limit, 10),
                },
                timeout=8,
            )
            resp.raise_for_status()
            data = resp.json()
            results: list[dict[str, Any]] = []
            for item in data.get("results", [])[:limit]:
                results.append(
                    {
                        "title": item.get("title", ""),
                        "url": item.get("url", ""),
                        "snippet": item.get("content", ""),
                    }
                )
            return results
        except Exception:
            return []

    @property
    def name(self) -> str:
        return "tavily"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_research_provider() -> BaseResearchProvider:
    """Return the configured research provider, or NoOpProvider if disabled.

    Reads from the app config (WEB_RESEARCH_ENABLED, WEB_SEARCH_PROVIDER,
    WEB_SEARCH_API_KEY).  Never raises.
    """
    from app.config import get_config  # local import to avoid circular at module level

    config = get_config()

    if not config.web_research_enabled:
        return NoOpProvider()

    provider_name = config.web_search_provider.lower().strip()
    api_key = config.web_search_api_key

    if provider_name == "brave":
        return BraveSearchProvider(api_key=api_key)

    if provider_name == "tavily":
        return TavilySearchProvider(api_key=api_key)

    # Future: add SearXNG, DuckDuckGo, etc. here.
    return NoOpProvider()


# ---------------------------------------------------------------------------
# Trigger decision
# ---------------------------------------------------------------------------


def should_use_research(
    user_message: str,
    memories: list[dict],
    provider: BaseResearchProvider,
    llm: Any = None,
) -> tuple[bool, str]:
    """Decide whether to run web research for this conversation turn.

    Parameters
    ----------
    user_message:
        The raw user message text.
    memories:
        Retrieved memory dicts (from MemoryStore.retrieve_memories).
    provider:
        The active research provider (use get_research_provider()).
    llm:
        An LLM client with a ``generate_response(prompt) -> str`` method.
        When provided, ``classify_research_intent`` is called to make the
        decision intelligently.  When None, research is skipped safely
        (useful for tests that only exercise the hard-block paths).

    Returns
    -------
    (should_search, reason)

    reason values
    ~~~~~~~~~~~~~
    ``"sensitive_content"``       — message looks like it contains secrets/keys
    ``"user_opted_out"``          — user explicitly said not to search
    ``"provider_unavailable"``    — LLM says research needed but no provider configured
    ``"llm_needs_research"``      — LLM classified this message as needing web research
    ``"local_context_sufficient"``— LLM (or safe default) says no research needed
    """
    # --- Hard blocks (order matters) ---
    if _is_sensitive(user_message):
        return False, "sensitive_content"

    msg_lower = user_message.lower()

    if any(phrase in msg_lower for phrase in _NO_SEARCH_PHRASES):
        return False, "user_opted_out"

    # --- LLM intent classification ---
    # When no LLM is provided (e.g. isolated unit tests for hard blocks), skip safely.
    if llm is None:
        return False, "local_context_sufficient"

    needs_research = classify_research_intent(user_message, memories, llm)

    if not needs_research:
        return False, "local_context_sufficient"

    if not provider.is_available():
        return False, "provider_unavailable"

    return True, "llm_needs_research"


# ---------------------------------------------------------------------------
# Research result storage
# ---------------------------------------------------------------------------


def store_research_findings(
    research_hits: list[dict],
    topic: str,
    scope: str | None = None,
    pipeline: Any = None,
) -> list[dict]:
    """Commit durable facts from web research hits to the memory store.

    Each hit's snippet is run through the full commit pipeline
    (extract → reconcile → critic → risk gate → write) with
    ``source_type="web_research"`` so provenance is tracked.

    Only non-sensitive snippets are stored.  Returns a list of commit
    event dicts (same shape as ``extract_and_store_from_message``).

    Parameters
    ----------
    research_hits:
        List of ``{title, url, snippet, ...}`` dicts from a search provider.
    topic:
        The query or question that drove the research — used as context_summary.
    scope:
        Optional memory scope (e.g. ``"project:memory-layer"``).
    pipeline:
        An optional pre-constructed ``MemoryCommitPipeline`` instance.
        When None a new pipeline is created.  Callers that are already running
        a pipeline can pass it in to avoid repeated initialisation.
    """
    if not research_hits:
        return []

    from app.commit_pipeline import MemoryCommitPipeline, CommitDecision  # noqa: PLC0415

    if pipeline is None:
        pipeline = MemoryCommitPipeline()

    events: list[dict] = []
    for hit in research_hits:
        snippet = (hit.get("snippet") or "").strip()
        if not snippet or _is_sensitive(snippet):
            continue

        candidate = {
            "content": snippet,
            "context_summary": f"[web research] {topic}: {snippet[:120]}",
            "memory_type": "fact",
            "scope": scope,
            "confidence": 0.6,
            "importance": 0.4,
            "source_key": hit.get("url") or "web_research",
            "source_type": "web_research",
            "should_store": True,
        }
        try:
            decision: CommitDecision = pipeline.commit_candidate(candidate)
            d = decision.to_dict()
            committed = d.get("committed_atom_id") is not None
            if committed:
                events.append({
                    "event": "stored",
                    "memory_atom_id": d["committed_atom_id"],
                    "memory_signal_id": d.get("committed_signal_id"),
                    "content": d.get("final_memory_text") or snippet,
                    "source_url": hit.get("url"),
                })
            elif d.get("write_action") == "reinforce_existing":
                events.append({
                    "event": "reinforced",
                    "atom_id": (
                        d.get("reinforces_atom_ids", [None])[0]
                        if isinstance(d.get("reinforces_atom_ids"), list)
                        else None
                    ),
                    "content": snippet,
                    "source_url": hit.get("url"),
                })
        except Exception:
            pass

    return events
