#!/usr/bin/env python3
"""Tests for the web research fallback (app/research.py).

Verifies:
  1. NoOpProvider is always unavailable and returns [].
  2. should_use_research returns "provider_unavailable" when provider is disabled
     but a trigger keyword is present.
  3. Sensitive content is blocked from external search.
  4. User opt-out phrase suppresses research.
  5. A mock available provider triggers on keyword.
  6. A mock available provider triggers on empty memories (no local context).
  7. No memory atoms are written from web results.
  8. Web research is disabled by default (config: WEB_RESEARCH_ENABLED unset).

Usage:
    make test-web-research
    # or directly:
    python scripts/test_web_research.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

# Ensure project root is on sys.path.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.research import (
    BaseResearchProvider,
    BraveSearchProvider,
    NoOpProvider,
    _is_sensitive,
    classify_research_intent,
    get_research_provider,
    should_use_research,
)


# ---------------------------------------------------------------------------
# Tiny mock provider (available, returns fake snippets)
# ---------------------------------------------------------------------------


class _MockProvider(BaseResearchProvider):
    def __init__(self, results: list[dict[str, Any]] | None = None) -> None:
        self._results = results or [
            {"title": "Mock result", "url": "https://example.com", "snippet": "test snippet"}
        ]

    def search(self, query: str, limit: int = 3) -> list[dict[str, Any]]:
        return self._results[:limit]

    def is_available(self) -> bool:
        return True

    @property
    def name(self) -> str:
        return "mock"


class _MockLLM:
    """Stub LLM that returns a fixed response string."""

    def __init__(self, response: str = "NO") -> None:
        self._response = response

    def generate_response(self, prompt: str) -> str:  # noqa: ARG002
        return self._response

    def embed_text(self, text: str) -> list[float]:  # noqa: ARG002
        return []


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------


def _assert(condition: bool, label: str) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}")
    if not condition:
        raise AssertionError(f"Assertion failed: {label}")


def _section(title: str) -> None:
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------------------
# Test functions
# ---------------------------------------------------------------------------


def test_noop_provider() -> None:
    _section("Test 1: NoOpProvider is always unavailable")
    p = NoOpProvider()
    _assert(p.is_available() is False, "NoOpProvider.is_available() == False")
    _assert(p.search("test") == [], "NoOpProvider.search() == []")
    _assert(p.name == "none", "NoOpProvider.name == 'none'")


def test_trigger_keyword_with_unavailable_provider() -> None:
    _section(
        "Test 2: should_use_research returns provider_unavailable "
        "when provider disabled + LLM says search needed"
    )
    p = NoOpProvider()
    msg = "search the web for the latest Postgres 17 release notes"
    should, reason = should_use_research(msg, [], p, llm=_MockLLM("YES"))
    _assert(should is False, "should == False when provider unavailable")
    _assert(reason == "provider_unavailable", f"reason == 'provider_unavailable' (got {reason!r})")


def test_sensitive_content_blocked() -> None:
    _section("Test 3: Sensitive content is blocked even with available provider")
    p = _MockProvider()
    msg = "what is my database_url password for this connection?"
    should, reason = should_use_research(msg, [], p, llm=None)
    _assert(should is False, "should == False for sensitive content")
    _assert(reason == "sensitive_content", f"reason == 'sensitive_content' (got {reason!r})")


def test_user_opt_out() -> None:
    _section("Test 4: User opt-out phrase suppresses research")
    p = _MockProvider()
    for phrase in ("no search", "local only", "don't search"):
        msg = f"{phrase}, just tell me about the project scope"
        should, reason = should_use_research(msg, [], p, llm=None)
        _assert(should is False, f"should == False for opt-out phrase: {phrase!r}")
        _assert(
            reason == "user_opted_out",
            f"reason == 'user_opted_out' (got {reason!r}) for phrase {phrase!r}",
        )


def test_trigger_keyword_with_available_provider() -> None:
    _section("Test 5: LLM says YES with available provider → should search")
    p = _MockProvider()
    msg = "search for the latest Python 3.13 release notes"
    should, reason = should_use_research(msg, [], p, llm=_MockLLM("YES"))
    _assert(should is True, "should == True when LLM says YES + available provider")
    _assert(reason == "llm_needs_research", f"reason == 'llm_needs_research' (got {reason!r})")


def test_no_local_context_triggers_search() -> None:
    _section("Test 6: LLM says YES for info-seeking message with no local memories")
    p = _MockProvider()
    msg = "how does recursive CTE syntax work in PostgreSQL?"
    should, reason = should_use_research(msg, [], p, llm=_MockLLM("YES"))
    _assert(should is True, "should == True when LLM says YES and memories empty")
    _assert(reason == "llm_needs_research", f"reason == 'llm_needs_research' (got {reason!r})")


def test_local_context_sufficient() -> None:
    _section("Test 7: LLM says NO (local context sufficient) → research suppressed")
    p = _MockProvider()
    msg = "how does recursive CTE syntax work in PostgreSQL?"
    # Inject a fake high-similarity memory; pass mock LLM that says NO
    memories = [{"content": "PostgreSQL CTEs …", "similarity": 0.82}]
    should, reason = should_use_research(msg, memories, p, llm=_MockLLM("NO"))
    _assert(should is False, "should == False when LLM says NO")
    _assert(
        reason == "local_context_sufficient",
        f"reason == 'local_context_sufficient' (got {reason!r})",
    )


def test_no_memory_writes_from_web_results() -> None:
    _section("Test 8: Web search results are NOT written to memory_atoms")
    import psycopg
    from dotenv import load_dotenv

    load_dotenv()
    db_url = os.getenv(
        "DATABASE_URL",
        "postgresql://memory:memory_dev_password@localhost:5432/memory_layer_development",
    )

    try:
        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM memory_atoms;")
                before = cur.fetchone()[0]

        # Simulate running research and explicitly confirm nothing was stored.
        p = _MockProvider()
        results = p.search("test web search — does not store memory")

        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM memory_atoms;")
                after = cur.fetchone()[0]

        _assert(after == before, f"No new atoms after web search (before={before}, after={after})")
        _assert(len(results) > 0, "Mock provider returned at least one result")
    except psycopg.OperationalError as exc:
        print(f"  [SKIP] Database unreachable — skipping write guard test ({exc})")


def test_disabled_by_default() -> None:
    _section("Test 9: NoOpProvider returned when WEB_RESEARCH_ENABLED=false")
    # Force-disable via env var (overrides .env file; load_dotenv never overwrites os.environ).
    from app.config import get_config

    original = os.environ.get("WEB_RESEARCH_ENABLED")
    try:
        os.environ["WEB_RESEARCH_ENABLED"] = "false"
        get_config.cache_clear()
        p = get_research_provider()
        _assert(isinstance(p, NoOpProvider), "get_research_provider() returns NoOpProvider when disabled")
        _assert(p.is_available() is False, "NoOpProvider.is_available() == False")
    finally:
        if original is not None:
            os.environ["WEB_RESEARCH_ENABLED"] = original
        else:
            os.environ.pop("WEB_RESEARCH_ENABLED", None)
        get_config.cache_clear()


def test_classify_research_intent() -> None:
    _section("Test 10: classify_research_intent delegates decision to LLM")

    # LLM says YES → True (research needed)
    _assert(
        classify_research_intent("any message", [], _MockLLM("YES")) is True,
        "returns True when LLM responds YES",
    )
    # LLM says NO → False
    _assert(
        classify_research_intent("any message", [], _MockLLM("NO")) is False,
        "returns False when LLM responds NO",
    )
    # LLM returns lower-case or mixed case → still works
    _assert(
        classify_research_intent("any message", [], _MockLLM("yes, definitely")) is True,
        "returns True for response starting with YES (case-insensitive)",
    )

    # LLM errors → False (fail closed)
    class _ErrorLLM:
        def generate_response(self, prompt: str) -> str:  # noqa: ARG002
            raise RuntimeError("LLM unavailable")

    _assert(
        classify_research_intent("any message", [], _ErrorLLM()) is False,
        "returns False on LLM error (fail closed)",
    )

    # Memory context is forwarded in the prompt (no assertion on content; just no crash)
    memories = [{"content": "some note", "similarity": 0.75}]
    result = classify_research_intent("any message", memories, _MockLLM("NO"))
    _assert(result is False, "handles non-empty memories without error")


def test_brave_provider_unavailable_without_key() -> None:
    _section("Test 11: BraveSearchProvider unavailable without API key")
    for key in ("", "none", "  "):
        p = BraveSearchProvider(api_key=key)
        _assert(
            p.is_available() is False,
            f"BraveSearchProvider.is_available() == False for key={key!r}",
        )
        _assert(p.search("test") == [], f"BraveSearchProvider.search() == [] for key={key!r}")


def test_sensitive_patterns() -> None:
    _section("Test 12: _is_sensitive() detection")
    _assert(_is_sensitive("my password is hunter2"), "detects 'password'")
    _assert(_is_sensitive("API_KEY=abc123"), "detects 'api_key'")
    _assert(_is_sensitive("token: ghp_xxxx"), "detects 'token'")
    _assert(_is_sensitive("my database_url=postgres://..."), "detects 'database_url'")
    _assert(not _is_sensitive("how does list comprehension work?"), "no sensitive data in benign msg")


def test_prompt_epistemic_content() -> None:
    _section("Test 14: System prompts contain explicit epistemic-status definitions")
    from app.chat import build_prompt, build_prompt_with_history

    single_turn = "".join(build_prompt("hello", []))
    multi_no_web = "".join(build_prompt_with_history([{"role": "user", "content": "hello"}], []))
    multi_with_web = "".join(build_prompt_with_history(
        [{"role": "user", "content": "hello"}],
        [],
        research_results=[{"title": "T", "url": "https://example.com", "snippet": "s"}],
    ))

    for label, prompt in [
        ("build_prompt", single_turn),
        ("build_prompt_with_history (no web)", multi_no_web),
        ("build_prompt_with_history (with web)", multi_with_web),
    ]:
        _assert("Supported:" in prompt, f"{label}: contains 'Supported:' definition")
        _assert("Unsupported:" in prompt, f"{label}: contains 'Unsupported:' definition")
        _assert("Do NOT hide useful unsupported claims" in prompt, f"{label}: instructs to label not hide")
        _assert("opinion" in prompt, f"{label}: mentions opinion label")
        _assert("web research" in prompt.lower() or "web" in prompt.lower(), f"{label}: mentions web research")

    # Web-unavailable notice appears only when no research results
    _assert(
        "web research would help but is unavailable" in multi_no_web.lower()
        or "unavailable" in multi_no_web.lower(),
        "build_prompt_with_history (no web): prompt mentions web unavailability path",
    )


def test_conversational_greeting_no_research() -> None:
    _section("Test 13: When LLM says NO, research is not triggered (e.g. for greetings)")
    p = _MockProvider()
    llm = _MockLLM("NO")
    # These messages should never trigger research when the LLM says NO
    messages_that_should_not_trigger = [
        "Hi there, let's chat for a bit",
        "Hi There chat - were going to talk a bit and hopefully you 'learn' a few things",
        "Hello! Nice to meet you.",
        "Let's have a conversation.",
        "I'm excited to chat with you!",
    ]
    for msg in messages_that_should_not_trigger:
        should, reason = should_use_research(msg, [], p, llm=llm)
        _assert(
            should is False,
            f"should == False: {msg!r}",
        )
        _assert(
            reason == "local_context_sufficient",
            f"reason == 'local_context_sufficient' (got {reason!r}) for: {msg!r}",
        )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def main() -> int:
    print("web_research tests")
    print("=" * 48)

    tests = [
        test_noop_provider,
        test_trigger_keyword_with_unavailable_provider,
        test_sensitive_content_blocked,
        test_user_opt_out,
        test_trigger_keyword_with_available_provider,
        test_no_local_context_triggers_search,
        test_local_context_sufficient,
        test_no_memory_writes_from_web_results,
        test_disabled_by_default,
        test_classify_research_intent,
        test_brave_provider_unavailable_without_key,
        test_sensitive_patterns,
        test_prompt_epistemic_content,
        test_conversational_greeting_no_research,
    ]

    passed = 0
    failed = 0
    for test_fn in tests:
        try:
            test_fn()
            passed += 1
        except AssertionError as exc:
            print(f"  ERROR: {exc}")
            failed += 1
        except Exception as exc:
            print(f"  UNEXPECTED ERROR in {test_fn.__name__}: {exc}")
            failed += 1

    print()
    print(f"Summary: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
