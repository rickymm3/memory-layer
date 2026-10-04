"""The memory core must run without Synapse.

Core = app/, mcp_server/, and the scripts they import. Synapse = synapse/,
webapp/, app_main.py. Synapse may import the core; the core may never import
Synapse. These tests fail if a core module grows a Synapse import.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE_DIRS = ["app", "mcp_server", "mcp_server/tools"]
SYNAPSE_ROOTS = {"synapse", "webapp", "app_main"}


def _core_files():
    for d in CORE_DIRS:
        yield from sorted((ROOT / d).glob("*.py"))


def _imported_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def test_no_core_file_imports_synapse():
    offenders = {
        str(p.relative_to(ROOT)): sorted(_imported_roots(p) & SYNAPSE_ROOTS)
        for p in _core_files()
        if _imported_roots(p) & SYNAPSE_ROOTS
    }
    assert offenders == {}, f"core modules import Synapse: {offenders}"


_BLOCKER = """
import importlib.abc, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split('.')[0] in {'synapse', 'webapp', 'app_main'}:
            raise ImportError('core imported Synapse module ' + name)
sys.meta_path.insert(0, Block())
"""


def test_core_imports_and_commits_with_synapse_unavailable(tmp_path):
    """Import every core module and run a full commit with Synapse blocked."""
    modules = [
        ".".join(p.relative_to(ROOT).with_suffix("").parts)
        for p in _core_files()
        if p.name != "__init__.py"
    ]
    script = _BLOCKER + f"""
import importlib, math, os
os.environ.pop('MEMORY_LAYER_PLUGINS', None)
for m in {modules!r}:
    importlib.import_module(m)

from app.sqlite_store import SQLiteStore
from app.commit_pipeline import MemoryCommitPipeline

class Emb:
    def embed_text(self, t):
        seed = hash(t) % (2**31); v = [math.sin(seed + i) for i in range(8)]
        n = math.sqrt(sum(x * x for x in v)); return [x / n for x in v]

class LLM:
    def generate_response(self, prompt, system='', **kw):
        return '{{"decision":"commit","final_memory_text":null,"suggested_visibility":"public"}}'

store = SQLiteStore({str(tmp_path / 'core.db')!r}, ollama_client=Emb())
store.init_db()
d = MemoryCommitPipeline(store=store, llm=LLM()).commit_candidate(
    {{"content": "The billing service stores invoices in Postgres.", "scope": "project:billing"}},
    visibility="public",
)
assert d.committed_atom_id, d.to_dict()
print("ok")
"""
    out = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().endswith("ok")
