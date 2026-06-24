#!/usr/bin/env python3
"""Stdin wrapper for the MCP server.

Claude Desktop (Windows) sends blank lines and \r\n line endings over stdio.
FastMCP reads from sys.stdin.buffer (raw bytes) via asyncio, so we must
patch at the buffer level, not sys.stdin (TextIOWrapper).
"""
from __future__ import annotations

import io
import os
import sys

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _BlankLineFilter(io.RawIOBase):
    """Raw IO wrapper that skips blank / whitespace-only lines."""

    def __init__(self, raw: io.RawIOBase) -> None:
        self._raw = raw

    def readable(self) -> bool:
        return True

    def readinto(self, b: bytearray) -> int:
        # FastMCP stdio reads line-by-line via readline(), not readinto(),
        # but implement both for completeness.
        data = self.readline()
        n = len(data)
        b[:n] = data
        return n

    def readline(self, size: int = -1) -> bytes:
        while True:
            line = self._raw.readline()
            if not line:               # EOF
                return line
            if line.strip(b" \t\r\n"):  # non-blank
                return line
            # blank — drop silently

    def read(self, n: int = -1) -> bytes:
        return self._raw.read(n)

    def read1(self, n: int = -1) -> bytes:  # type: ignore[override]
        if hasattr(self._raw, "read1"):
            return self._raw.read1(n)
        return self._raw.read(n)

    def __getattr__(self, name: str):
        return getattr(self._raw, name)


# Replace the raw buffer so asyncio's StreamReader sees filtered bytes.
sys.stdin = io.TextIOWrapper(
    io.BufferedReader(_BlankLineFilter(sys.stdin.buffer.raw)),  # type: ignore[arg-type]
    encoding="utf-8",
    errors="replace",
    line_buffering=False,
)
sys.stdin.buffer  # type: ignore[attr-defined]  # keep attribute accessible

from mcp_server.server import mcp  # noqa: E402
mcp.run(transport="stdio")
