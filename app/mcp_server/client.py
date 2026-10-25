"""Tool client used by agents. Same interface for both modes:

    async with ToolClient() as tools:
        result = await tools.call("get_technical_indicators", symbol="SBIN")

* MCP_MODE=stdio     -> starts app.mcp_server.server as a subprocess and talks real MCP (default)
* MCP_MODE=inprocess -> calls the same functions directly (fast, used by tests)

Agents may only call tools in their allow-list (see each agent.py): `tools.for_agent(["a", "b"])`.
"""
from __future__ import annotations

import asyncio
import json
import sys
from contextlib import AsyncExitStack
from typing import Any

from app.mcp_server.registry import TOOLS
from app.mcp_server.tools.common import error
from app.service.logger import get_logger, log_event, timed
from app.service.settings import ROOT, get_settings

log = get_logger(__name__)


class ToolClient:
    def __init__(self, mode: str | None = None):
        self.mode = (mode or get_settings().mcp_mode).lower()
        self._stack: AsyncExitStack | None = None
        self._session = None

    async def __aenter__(self) -> "ToolClient":
        if self.mode == "stdio":
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            self._stack = AsyncExitStack()
            params = StdioServerParameters(command=sys.executable, args=["-m", "app.mcp_server.server"], cwd=str(ROOT),
                                           env={**__import__("os").environ, "PYTHONPATH": str(ROOT)})
            read, write = await self._stack.enter_async_context(stdio_client(params))
            self._session = await self._stack.enter_async_context(ClientSession(read, write))
            await self._session.initialize()
            names = {t.name for t in (await self._session.list_tools()).tools}
            missing = set(TOOLS) - names
            if missing:
                raise RuntimeError(f"MCP server is missing tools: {sorted(missing)}")
            log.info("MCP server connected (%d tools)", len(names))
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._stack:
            await self._stack.aclose()

    async def call(self, tool: str, **args: Any) -> dict[str, Any]:
        """Call a tool. Always returns the standard envelope {status, source, data, ...}; never raises."""
        args = {k: v for k, v in args.items() if v is not None}
        with timed() as t:
            try:
                if self.mode == "inprocess":
                    result = await asyncio.to_thread(TOOLS[tool], **args)
                else:
                    resp = await self._session.call_tool(tool, args)
                    text = "".join(getattr(c, "text", "") for c in resp.content)
                    result = json.loads(text) if text else error(tool, "empty MCP response")
                    if resp.isError:
                        result = error(tool, text)
            except Exception as exc:  # noqa: BLE001
                result = error(tool, exc)
        log.info("tool %s %s -> %s (%.0f ms)", tool, args, result.get("status"), t["ms"])
        log_event("tool_call", tool=tool, args=args, status=result.get("status"), ms=t["ms"], source=result.get("source"),
                  message=result.get("message"))
        return result

    def for_agent(self, allowed: list[str]) -> "AgentTools":
        unknown = set(allowed) - set(TOOLS)
        if unknown:
            raise ValueError(f"unknown tools {sorted(unknown)}")
        return AgentTools(self, allowed)


class AgentTools:
    """A view of the client restricted to the tools an agent is allowed to use."""

    def __init__(self, client: ToolClient, allowed: list[str]):
        self._client, self.allowed = client, set(allowed)

    async def call(self, tool: str, **args: Any) -> dict[str, Any]:
        if tool not in self.allowed:
            raise PermissionError(f"tool '{tool}' is not allowed for this agent (allowed: {sorted(self.allowed)})")
        return await self._client.call(tool, **args)
