#!/usr/bin/env python3
import importlib
import os
from pathlib import Path
import subprocess
import sys
from dataclasses import dataclass

import psycopg
import requests
from dotenv import load_dotenv


DEFAULT_DATABASE_URL = (
    "postgresql://memory:memory_dev_password@localhost:5432/memory_layer_development"
)
DEFAULT_CHAT_MODEL = "qwen3:8b"
DEFAULT_EMBEDDING_MODEL = "qwen3-embedding:latest"
EXPECTED_EMBEDDING_DIM = 4096

EXPECTED_SIGNAL_COLUMNS = [
    "id",
    "memory_atom_id",
    "parent_signal_id",
    "source_key",
    "source_type",
    "content",
    "memory_type",
    "relationship",
    "created_at",
    "task_run_id",
]

EXPECTED_SIGNAL_INDEXES = [
    "idx_memory_signals_memory_atom_id",
    "idx_memory_signals_parent_signal_id",
    "idx_memory_signals_source_key",
    "idx_memory_signals_source_type",
    "idx_memory_signals_scope",
    "idx_memory_signals_subject",
    "idx_memory_signals_relationship",
    "idx_memory_signals_created_at",
    "idx_memory_signals_task_run_id",
]

EXPECTED_ATOM_AGG_COLUMNS = [
    "support_weight",
    "opposition_weight",
    "disagreement_score",
    "last_recomputed_at",
]

EXPECTED_AUTHORITY_COLUMNS = [
    "authority_status",
    "authority_reviewed_at",
    "authority_reviewer",
]


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str


def detect_windows_host_ip() -> str | None:
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


def resolve_ollama_host() -> tuple[str | None, str]:
    env_host = os.getenv("OLLAMA_HOST", "").strip()
    if env_host:
        return env_host.rstrip("/"), "env"

    detected_ip = detect_windows_host_ip()
    if detected_ip:
        return f"http://{detected_ip}:11434", "auto-detected"

    return None, "missing"


def fmt(result: CheckResult) -> str:
    return f"[{result.status}] {result.name}: {result.detail}"


def main() -> int:
    load_dotenv()

    database_url = os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL).strip()
    chat_model = os.getenv("CHAT_MODEL", DEFAULT_CHAT_MODEL).strip()
    embedding_model = os.getenv("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL).strip()
    ollama_host, ollama_source = resolve_ollama_host()

    print("Memory Layer Environment Doctor")
    print("=" * 32)
    print(f"DATABASE_URL: {database_url}")
    print(f"OLLAMA_HOST: {ollama_host or 'UNSET'} ({ollama_source})")
    print(f"CHAT_MODEL: {chat_model}")
    print(f"EMBEDDING_MODEL: {embedding_model}")
    print(f"EXPECTED_EMBEDDING_DIM: {EXPECTED_EMBEDDING_DIM}")
    print()

    results: list[CheckResult] = []

    # ── Anthropic API key ──────────────────────────────────────────────────────
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if anthropic_key:
        results.append(CheckResult(
            "ANTHROPIC_API_KEY",
            "PASS",
            f"set ({len(anthropic_key)} chars) — make reflect --model claude-haiku-4-5-20251001 available",
        ))
    else:
        results.append(CheckResult(
            "ANTHROPIC_API_KEY",
            "WARN",
            "not set in .env — make reflect will fail with non-Anthropic CHAT_MODEL. "
            "Add ANTHROPIC_API_KEY=sk-ant-... to your .env to enable lesson extraction.",
        ))

    # ── CHAT_MODEL structured-output check ────────────────────────────────────
    _STRUCTURED_OK = ("claude-", "gpt-", "o1-", "o3-", "gemini-")
    if any(chat_model.lower().startswith(p) for p in _STRUCTURED_OK):
        results.append(CheckResult(
            "CHAT_MODEL structured output",
            "PASS",
            f"{chat_model} supports strict JSON output (make reflect works)",
        ))
    else:
        results.append(CheckResult(
            "CHAT_MODEL structured output",
            "WARN",
            f"{chat_model!r} does not reliably return JSON — make reflect will fail. "
            "Set CHAT_MODEL=claude-haiku-4-5-20251001 (requires ANTHROPIC_API_KEY) "
            "or pass --model claude-haiku-4-5-20251001 per invocation.",
        ))

    ollama_ok = False
    ollama_tags: list[dict] = []
    embedding_sample: list[float] | None = None

    if not ollama_host:
        results.append(
            CheckResult(
                "Ollama reachable",
                "FAIL",
                "OLLAMA_HOST not set and auto-detection failed",
            )
        )
    else:
        try:
            resp = requests.get(f"{ollama_host}/api/version", timeout=10)
            resp.raise_for_status()
            payload = resp.json()
            version = payload.get("version", "unknown")
            ollama_ok = True
            results.append(
                CheckResult("Ollama reachable", "PASS", f"version={version}")
            )
        except Exception as exc:
            results.append(CheckResult("Ollama reachable", "FAIL", str(exc)))

    if ollama_ok:
        try:
            tags_resp = requests.get(f"{ollama_host}/api/tags", timeout=20)
            tags_resp.raise_for_status()
            ollama_tags = tags_resp.json().get("models", [])
            model_names = {
                item.get("name") for item in ollama_tags if isinstance(item, dict)
            }
            model_names |= {
                item.get("model") for item in ollama_tags if isinstance(item, dict)
            }
            has_chat = chat_model in model_names
            has_embed = embedding_model in model_names

            results.append(
                CheckResult(
                    f"{chat_model} available",
                    "PASS" if has_chat else "FAIL",
                    f"found={has_chat}",
                )
            )
            results.append(
                CheckResult(
                    f"{embedding_model} available",
                    "PASS" if has_embed else "FAIL",
                    f"found={has_embed}",
                )
            )
        except Exception as exc:
            results.append(CheckResult(f"{chat_model} available", "FAIL", str(exc)))
            results.append(
                CheckResult(f"{embedding_model} available", "FAIL", str(exc))
            )
    else:
        results.append(
            CheckResult(f"{chat_model} available", "FAIL", "skipped: Ollama unreachable")
        )
        results.append(
            CheckResult(
                f"{embedding_model} available", "FAIL", "skipped: Ollama unreachable"
            )
        )

    if ollama_ok and ollama_host:
        try:
            embed_resp = requests.post(
                f"{ollama_host}/api/embed",
                json={
                    "model": embedding_model,
                    "input": "healthcheck: embedding dimension verification",
                },
                timeout=120,
            )
            embed_resp.raise_for_status()
            embed_data = embed_resp.json()
            embeddings = embed_data.get("embeddings", [])
            if not embeddings or not isinstance(embeddings[0], list):
                raise ValueError("embed response missing embeddings[0]")
            embedding_sample = embeddings[0]
            dim = len(embedding_sample)
            ok = dim == EXPECTED_EMBEDDING_DIM
            results.append(
                CheckResult(
                    "embedding dimension = 4096",
                    "PASS" if ok else "FAIL",
                    f"dimension={dim}",
                )
            )
        except Exception as exc:
            results.append(CheckResult("embedding dimension = 4096", "FAIL", str(exc)))
    else:
        results.append(
            CheckResult(
                "embedding dimension = 4096",
                "FAIL",
                "skipped: Ollama unreachable",
            )
        )

    db_ok = False
    db_conn: psycopg.Connection | None = None
    try:
        db_conn = psycopg.connect(database_url, connect_timeout=5)
        db_ok = True
        with db_conn.cursor() as cur:
            cur.execute("SELECT 1;")
            cur.fetchone()
        results.append(CheckResult("Postgres reachable", "PASS", "connection established"))
    except Exception as exc:
        results.append(CheckResult("Postgres reachable", "FAIL", str(exc)))

    def run_scalar(query: str, params: tuple = ()):
        if not db_conn:
            raise RuntimeError("db connection unavailable")
        with db_conn.cursor() as cur:
            cur.execute(query, params)
            row = cur.fetchone()
            return row[0] if row else None

    if db_ok and db_conn:
        try:
            vector_installed = bool(
                run_scalar("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector');")
            )
            pgcrypto_installed = bool(
                run_scalar(
                    "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pgcrypto');"
                )
            )
            memory_atoms_exists = bool(
                run_scalar("SELECT to_regclass('public.memory_atoms') IS NOT NULL;")
            )

            embedding_type = run_scalar(
                """
                SELECT format_type(a.atttypid, a.atttypmod)
                FROM pg_attribute a
                JOIN pg_class c ON c.oid = a.attrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relname = 'memory_atoms'
                  AND a.attname = 'embedding'
                  AND a.attnum > 0
                  AND NOT a.attisdropped;
                """
            )

            has_hnsw = bool(
                run_scalar(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM pg_indexes
                        WHERE schemaname = 'public'
                          AND tablename = 'memory_atoms'
                          AND indexdef ILIKE '%%USING hnsw%%'
                          AND indexdef ILIKE '%%embedding%%'
                          AND indexdef ILIKE '%%vector_cosine_ops%%'
                    );
                    """
                )
            )

            results.append(
                CheckResult(
                    "vector extension installed",
                    "PASS" if vector_installed else "FAIL",
                    f"found={vector_installed}",
                )
            )
            results.append(
                CheckResult(
                    "pgcrypto extension installed",
                    "PASS" if pgcrypto_installed else "FAIL",
                    f"found={pgcrypto_installed}",
                )
            )
            results.append(
                CheckResult(
                    "memory_atoms exists",
                    "PASS" if memory_atoms_exists else "FAIL",
                    f"found={memory_atoms_exists}",
                )
            )

            # Migration 014 changed the column to dimension-flexible 'vector'
            # (no explicit dim) to support multiple embedding models.
            # Accept both 'vector' and 'vector(N)' as passing.
            embedding_ok = embedding_type == f"vector({EXPECTED_EMBEDDING_DIM})" or embedding_type == "vector"
            results.append(
                CheckResult(
                    "embedding column = vector(4096)",
                    "PASS" if embedding_ok else "FAIL",
                    f"found={embedding_type}",
                )
            )

            # HNSW is intentionally absent: pgvector HNSW limit is 2000 dims,
            # and our embeddings are 4096-dim. Exact cosine search is correct here.
            results.append(
                CheckResult(
                    "HNSW index exists",
                    "WARN",
                    "Not created by design — pgvector HNSW max is 2000 dims, embeddings are 4096. Exact search used.",
                )
            )

            # --- memory_signals checks ---
            memory_signals_exists = bool(
                run_scalar("SELECT to_regclass('public.memory_signals') IS NOT NULL;")
            )
            results.append(
                CheckResult(
                    "memory_signals exists",
                    "PASS" if memory_signals_exists else "FAIL",
                    f"found={memory_signals_exists}",
                )
            )

            if memory_signals_exists:
                missing_cols = [
                    col
                    for col in EXPECTED_SIGNAL_COLUMNS
                    if not bool(
                        run_scalar(
                            """
                            SELECT EXISTS (
                                SELECT 1
                                FROM pg_attribute a
                                JOIN pg_class c ON c.oid = a.attrelid
                                JOIN pg_namespace n ON n.oid = c.relnamespace
                                WHERE n.nspname = 'public'
                                  AND c.relname = 'memory_signals'
                                  AND a.attname = %s
                                  AND a.attnum > 0
                                  AND NOT a.attisdropped
                            );
                            """,
                            (col,),
                        )
                    )
                ]
                results.append(
                    CheckResult(
                        "memory_signals columns",
                        "PASS" if not missing_cols else "FAIL",
                        "all present" if not missing_cols else f"missing: {', '.join(missing_cols)}",
                    )
                )

                missing_indexes = [
                    idx
                    for idx in EXPECTED_SIGNAL_INDEXES
                    if not bool(
                        run_scalar(
                            """
                            SELECT EXISTS (
                                SELECT 1
                                FROM pg_indexes
                                WHERE schemaname = 'public'
                                  AND tablename = 'memory_signals'
                                  AND indexname = %s
                            );
                            """,
                            (idx,),
                        )
                    )
                ]
                results.append(
                    CheckResult(
                        "memory_signals indexes",
                        "PASS" if not missing_indexes else "FAIL",
                        "all present" if not missing_indexes else f"missing: {', '.join(missing_indexes)}",
                    )
                )
            else:
                results.append(
                    CheckResult(
                        "memory_signals columns",
                        "FAIL",
                        "skipped: memory_signals table missing",
                    )
                )
                results.append(
                    CheckResult(
                        "memory_signals indexes",
                        "FAIL",
                        "skipped: memory_signals table missing",
                    )
                )

            # --- Phase 4: memory_atoms aggregation column checks ---
            missing_agg_cols = [
                col
                for col in EXPECTED_ATOM_AGG_COLUMNS
                if not bool(
                    run_scalar(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM pg_attribute a
                            JOIN pg_class c ON c.oid = a.attrelid
                            JOIN pg_namespace n ON n.oid = c.relnamespace
                            WHERE n.nspname = 'public'
                              AND c.relname = 'memory_atoms'
                              AND a.attname = %s
                              AND a.attnum > 0
                              AND NOT a.attisdropped
                        );
                        """,
                        (col,),
                    )
                )
            ]
            results.append(
                CheckResult(
                    "memory_atoms aggregation columns",
                    "PASS" if not missing_agg_cols else "FAIL",
                    "all present" if not missing_agg_cols else f"missing: {', '.join(missing_agg_cols)} — run db/migrations/003_add_signal_aggregation.sql",
                )
            )

            missing_authority_cols = [
                col
                for col in EXPECTED_AUTHORITY_COLUMNS
                if not bool(
                    run_scalar(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM pg_attribute a
                            JOIN pg_class c ON c.oid = a.attrelid
                            JOIN pg_namespace n ON n.oid = c.relnamespace
                            WHERE n.nspname = 'public'
                              AND c.relname = 'memory_atoms'
                              AND a.attname = %s
                              AND a.attnum > 0
                              AND NOT a.attisdropped
                        );
                        """,
                        (col,),
                    )
                )
            ]
            results.append(
                CheckResult(
                    "memory_atoms authority columns",
                    "PASS" if not missing_authority_cols else "FAIL",
                    "all present" if not missing_authority_cols else (
                        f"missing: {', '.join(missing_authority_cols)} — "
                        "run db/migrations/046_add_authority_status.sql"
                    ),
                )
            )

            # --- Phase 7: task_runs table ---
            task_runs_exists = bool(
                run_scalar("SELECT to_regclass('public.task_runs') IS NOT NULL;")
            )
            results.append(
                CheckResult(
                    "task_runs exists",
                    "PASS" if task_runs_exists else "FAIL",
                    "found" if task_runs_exists else "missing — run db/migrations/005_add_task_runs.sql",
                )
            )

            # --- Sprint 2: atom relations graph ---
            atom_relations_exists = bool(
                run_scalar("SELECT to_regclass('public.memory_atom_relations') IS NOT NULL;")
            )
            results.append(
                CheckResult(
                    "memory_atom_relations exists",
                    "PASS" if atom_relations_exists else "FAIL",
                    "found" if atom_relations_exists else "missing — run db/migrations/015_add_atom_relations.sql",
                )
            )

            # --- Phase 0: context size tracking ---
            context_size_log_exists = bool(
                run_scalar("SELECT to_regclass('public.context_size_log') IS NOT NULL;")
            )
            results.append(
                CheckResult(
                    "context_size_log exists",
                    "PASS" if context_size_log_exists else "FAIL",
                    "found" if context_size_log_exists else "missing — run db/migrations/016_add_context_size_log.sql",
                )
            )

        except Exception as exc:
            results.append(CheckResult("vector extension installed", "FAIL", str(exc)))
            results.append(CheckResult("pgcrypto extension installed", "FAIL", str(exc)))
            results.append(CheckResult("memory_atoms exists", "FAIL", str(exc)))
            results.append(CheckResult("embedding column = vector(4096)", "FAIL", str(exc)))
            results.append(CheckResult("HNSW index exists", "FAIL", str(exc)))
            results.append(CheckResult("memory_signals exists", "FAIL", str(exc)))
            results.append(CheckResult("memory_signals columns", "FAIL", str(exc)))
            results.append(CheckResult("memory_signals indexes", "FAIL", str(exc)))
            results.append(CheckResult("memory_atoms aggregation columns", "FAIL", str(exc)))
            results.append(CheckResult("memory_atoms authority columns", "FAIL", str(exc)))
            results.append(CheckResult("task_runs exists", "FAIL", str(exc)))
    else:
        results.append(
            CheckResult("vector extension installed", "FAIL", "skipped: Postgres unreachable")
        )
        results.append(
            CheckResult(
                "pgcrypto extension installed", "FAIL", "skipped: Postgres unreachable"
            )
        )
        results.append(
            CheckResult("memory_atoms exists", "FAIL", "skipped: Postgres unreachable")
        )
        results.append(
            CheckResult(
                "embedding column = vector(4096)",
                "FAIL",
                "skipped: Postgres unreachable",
            )
        )
        results.append(
            CheckResult("HNSW index exists", "FAIL", "skipped: Postgres unreachable")
        )
        results.append(
            CheckResult("memory_signals exists", "FAIL", "skipped: Postgres unreachable")
        )
        results.append(
            CheckResult("memory_signals columns", "FAIL", "skipped: Postgres unreachable")
        )
        results.append(
            CheckResult("memory_signals indexes", "FAIL", "skipped: Postgres unreachable")
        )
        results.append(
            CheckResult(
                "memory_atoms aggregation columns", "FAIL", "skipped: Postgres unreachable"
            )
        )
        results.append(
            CheckResult(
                "memory_atoms authority columns", "FAIL", "skipped: Postgres unreachable"
            )
        )
        results.append(
            CheckResult("task_runs exists", "FAIL", "skipped: Postgres unreachable")
        )

    baseline_ok = False
    baseline_detail = "skipped"
    if db_ok and db_conn and embedding_sample is not None:
        try:
            embed_literal = "[" + ",".join(str(v) for v in embedding_sample) + "]"
            with db_conn.transaction(force_rollback=True):
                with db_conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO memory_atoms (content, memory_type, scope, embedding_model, embedding)
                        VALUES (%s, %s, %s, %s, %s::vector)
                        RETURNING id;
                        """,
                        (
                            "doctor baseline memory row",
                            "fact",
                            "doctor-check",
                            embedding_model,
                            embed_literal,
                        ),
                    )
                    inserted_id = cur.fetchone()[0]

                    cur.execute(
                        """
                        SELECT id, 1 - (embedding <=> %s::vector) AS similarity
                        FROM memory_atoms
                        ORDER BY embedding <=> %s::vector
                        LIMIT 1;
                        """,
                        (embed_literal, embed_literal),
                    )
                    best = cur.fetchone()

            if best and best[0] == inserted_id:
                baseline_ok = True
                baseline_detail = f"inserted_id={inserted_id}, top_match_similarity={best[1]:.6f}"
            elif best:
                baseline_detail = (
                    f"inserted_id={inserted_id}, top_match_id={best[0]}, similarity={best[1]:.6f}"
                )
            else:
                baseline_detail = f"inserted_id={inserted_id}, no retrieval rows"
        except Exception as exc:
            baseline_detail = str(exc)
    elif embedding_sample is None:
        baseline_detail = "skipped: embedding not available"
    else:
        baseline_detail = "skipped: Postgres unreachable"

    results.append(
        CheckResult(
            "baseline insert/retrieve works",
            "PASS" if baseline_ok else "FAIL",
            baseline_detail,
        )
    )

    # --- mcp_server package checks ---
    # Add project root so mcp_server is importable when this script is run directly
    _project_root = str(Path(__file__).resolve().parent.parent)
    if _project_root not in sys.path:
        sys.path.insert(0, _project_root)

    for symbol, module_path in [
        ("mcp_server", "mcp_server"),
        ("get_memory_health", "mcp_server.tools.health"),
        ("search_memories", "mcp_server.tools.search"),
        ("get_memory_by_id", "mcp_server.tools.get"),
        ("extract_memory_candidates", "mcp_server.tools.extract"),
        ("reconcile_candidate", "mcp_server.tools.reconcile"),
        ("store_memory_auto", "mcp_server.tools.store_auto"),
        ("propose_memory_signal", "mcp_server.tools.propose_signal"),
        ("store_memory_approved", "mcp_server.tools.store_approved"),
        ("get_project_context", "mcp_server.tools.project_context"),
        ("get_recent_memories", "mcp_server.tools.recent"),
        ("compute_atom_weights", "app.signal_aggregator"),
        ("recompute_memory_atom", "mcp_server.tools.recompute"),
        ("get_atom_signals", "mcp_server.tools.get_signals"),
        ("assess_task_readiness", "app.task_readiness"),
        ("assess_task_readiness_handler", "mcp_server.tools.assess_readiness"),
    ]:
        try:
            mod = importlib.import_module(module_path)
            if symbol != module_path:
                _ = getattr(mod, symbol)
            results.append(CheckResult(f"{symbol} importable", "PASS", "ok"))
        except (ImportError, AttributeError) as exc:
            results.append(CheckResult(f"{symbol} importable", "FAIL", str(exc)))

    # --- scripts/show_memory.py exists ---
    show_memory_path = Path(__file__).resolve().parent / "show_memory.py"
    results.append(
        CheckResult(
            "show_memory.py exists",
            "PASS" if show_memory_path.is_file() else "FAIL",
            str(show_memory_path) if show_memory_path.is_file() else "missing",
        )
    )

    # --- scripts/recompute_all_atoms.py exists ---
    recompute_all_path = Path(__file__).resolve().parent / "recompute_all_atoms.py"
    results.append(
        CheckResult(
            "recompute_all_atoms.py exists",
            "PASS" if recompute_all_path.is_file() else "FAIL",
            str(recompute_all_path) if recompute_all_path.is_file() else "missing",
        )
    )

    # --- web research config sanity check ---
    web_enabled = os.getenv("WEB_RESEARCH_ENABLED", "false").strip().lower() in ("true", "1", "yes")
    web_provider = os.getenv("WEB_SEARCH_PROVIDER", "none").strip().lower()
    web_key = os.getenv("WEB_SEARCH_API_KEY", "").strip()
    if web_enabled and web_provider != "none" and not web_key:
        results.append(
            CheckResult(
                "web research config",
                "WARN",
                f"WEB_RESEARCH_ENABLED=true + WEB_SEARCH_PROVIDER={web_provider!r} "
                "but WEB_SEARCH_API_KEY is empty — provider will act as no-op",
            )
        )
    elif web_enabled and web_provider == "none":
        results.append(
            CheckResult(
                "web research config",
                "WARN",
                "WEB_RESEARCH_ENABLED=true but WEB_SEARCH_PROVIDER=none — "
                "no search provider active; set WEB_SEARCH_PROVIDER=brave (or other) "
                "and WEB_SEARCH_API_KEY",
            )
        )
    else:
        results.append(
            CheckResult(
                "web research config",
                "PASS",
                "disabled (default)" if not web_enabled else f"provider={web_provider!r}",
            )
        )

    print("Results")
    print("-" * 32)
    for item in results:
        print(fmt(item))

    passed_count = sum(1 for r in results if r.status == "PASS")
    warn_count = sum(1 for r in results if r.status == "WARN")
    failed = [r for r in results if r.status == "FAIL"]

    print()
    print(f"Summary: {passed_count} PASS, {warn_count} WARN, {len(failed)} FAIL")

    if db_conn is not None:
        db_conn.close()

    if failed:
        print("Overall: FAIL")
        return 1

    print("Overall: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
