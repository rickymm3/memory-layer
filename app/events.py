"""Commit events: how layers built on the memory core react to writes.

The core never imports the layers built on it. Instead it emits events after
a write, and a layer registers handlers for them. Synapse (posts, discussions,
feed) is one such layer; it registers its handlers when imported.

A process that runs only core code (for example the MCP server) can still
load layers by listing their modules in MEMORY_LAYER_PLUGINS, comma-separated:

    MEMORY_LAYER_PLUGINS=synapse python -m mcp_server.server

Events (all keyword arguments):

    atom_committed   atom_id, source_user_id, scope, visibility, decision,
                     novelty_score, interest_flag
    atom_superseded  atom_id, superseded_by
    turn_reflected   user_msg, answer, committed_atom_ids, source_user_id, route

Handlers run synchronously in registration order. A failing handler is logged
and skipped; it never affects the write that triggered it.
"""
from __future__ import annotations

import importlib
import logging
import os
from collections import defaultdict
from typing import Any, Callable

_log = logging.getLogger(__name__)

Handler = Callable[..., Any]

_handlers: dict[str, list[Handler]] = defaultdict(list)
_plugins_loaded = False


def on(event: str, handler: Handler) -> Handler:
    """Register handler for event. Registering the same handler twice is a no-op."""
    if handler not in _handlers[event]:
        _handlers[event].append(handler)
    return handler


def off(event: str, handler: Handler) -> None:
    if handler in _handlers.get(event, []):
        _handlers[event].remove(handler)


def has_handlers(event: str) -> bool:
    _load_plugins()
    return bool(_handlers.get(event))


def emit(event: str, **payload: Any) -> None:
    _load_plugins()
    for handler in list(_handlers.get(event, [])):
        try:
            handler(**payload)
        except Exception as exc:  # a layer must never break a core write
            _log.warning("event %s: handler %s failed: %s", event, getattr(handler, "__name__", handler), exc)


def _load_plugins() -> None:
    global _plugins_loaded
    if _plugins_loaded:
        return
    _plugins_loaded = True
    for name in filter(None, (p.strip() for p in os.environ.get("MEMORY_LAYER_PLUGINS", "").split(","))):
        try:
            importlib.import_module(name)
        except Exception as exc:
            _log.warning("MEMORY_LAYER_PLUGINS: could not load %s: %s", name, exc)
