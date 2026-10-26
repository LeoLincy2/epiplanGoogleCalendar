#!/usr/bin/env python3
"""MCP server exposing the my.epitech.eu tools (stdio by default).

    python server.py                    # stdio, for any MCP client
    python server.py --http 8765 [--host H]   # streamable HTTP, default 127.0.0.1:8765 (put auth in front before exposing)
    python server.py --read-only        # do not register the write tools at all
"""
import argparse
import functools

from mcp.server.fastmcp import FastMCP

import tools
from client import ApiError, AuthError


def safe(fn):
    """Turn API/auth failures into a result the LLM can read instead of a protocol error."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except (ApiError, AuthError) as e:
            return tools._error(e)
    return wrapper


def build(read_only=False, host="127.0.0.1", port=8000):
    mcp = FastMCP("epitech-planning", host=host, port=port, instructions=(
        "Tools for the student's my.epitech.eu planning. Dates are Europe/Paris. "
        "Write tools default to dry_run=True; only pass dry_run=False when the rules clearly require the action, "
        "and always give a short `reason`."))
    for fn in tools.READ_TOOLS + ([] if read_only else tools.WRITE_TOOLS):
        mcp.tool()(safe(fn))
    return mcp


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", type=int, metavar="PORT")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--read-only", action="store_true")
    a = ap.parse_args()
    build(a.read_only, host=a.host, port=a.http or 8000).run("streamable-http" if a.http else "stdio")
