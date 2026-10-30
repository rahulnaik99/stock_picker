"""The ONE MCP server. Every agent talks to it through app.mcp_server.client.

Run standalone (e.g. to inspect with an MCP client):  python -m app.mcp_server.server
The app also starts it automatically as a stdio subprocess when MCP_MODE=stdio.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import sys

from mcp.server.fastmcp import FastMCP

from app.mcp_server.registry import TOOLS

mcp = FastMCP("stock-scoring-tools")


def _register() -> None:
    for name, fn in TOOLS.items():
        @functools.wraps(fn)
        async def wrapper(*args, _fn=fn, **kwargs):  # tools are blocking (requests/yfinance): run in a thread
            return await asyncio.to_thread(_fn, *args, **kwargs)

        mcp.tool(name=name, description=(fn.__doc__ or name).strip())(wrapper)


_register()

if __name__ == "__main__":
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)  # stdout is the MCP channel
    mcp.run(transport="stdio")
