"""End-to-end MCP stdio startup and protected-tool integration test."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from cmsmcp.app import build_server
from cmsmcp.config.settings import Settings


def envelope(result: Any) -> dict[str, Any]:
    """Parse the compact-JSON tool result text (tests run with output_format=json)."""
    return json.loads(result.content[0].text)


async def test_stdio_server_lists_tools_and_enforces_auth(tmp_path: Path) -> None:
    env = dict(
        os.environ,
        CMSMCP_STATE_DIR=str(tmp_path),
        CMSMCP_REQUIRE_SSO="true",
    )
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "cmsmcp"],
        env=env,
        cwd=Path.cwd(),
    )

    async with asyncio.timeout(30):
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools = await session.list_tools()
                status = await session.call_tool("cern_auth_status", {})
                health = await session.call_tool("cern_health", {})

    expected = {tool.name for tool in await build_server(Settings()).list_tools()}
    assert {tool.name for tool in tools.tools} == expected
    assert envelope(status)["data"]["authenticated"] is False
    assert envelope(health)["error"]["code"] == "AUTH_REQUIRED"
