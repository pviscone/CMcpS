"""End-to-end MCP stdio startup and protected-tool integration test."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from cmcps.app import build_server
from cmcps.config.settings import Settings


async def test_stdio_server_lists_tools_and_enforces_auth(tmp_path: Path) -> None:
    env = dict(
        os.environ,
        CMCPS_STATE_DIR=str(tmp_path),
        CMCPS_REQUIRE_SSO="true",
    )
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "cmcps"],
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
    assert status.structured_content["data"]["authenticated"] is False
    assert health.structured_content["error"]["code"] == "AUTH_REQUIRED"
