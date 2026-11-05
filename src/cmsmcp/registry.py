"""Tool registry: profiles, effect classes, compact output, audit, lazy mode.

:class:`CmsMcpServer` overrides ``tool()`` so every existing
``@server.tool(name=..., description=...)`` registration (160+ tools across the
adapter modules) passes through one place that:

1. skips tools whose group is not in the enabled profiles, and act tools when
   actions are disabled;
2. registers the tool as *unstructured* (no ``outputSchema`` in the tool list,
   no duplicate ``structuredContent`` in results);
3. wraps the function so its return value is rendered by
   :mod:`cmsmcp.output` (compact text, budgeted), audited, and noted for taint;
4. in ``tool_mode=lazy`` hides non-core tools behind
   ``cmsmcp_find_tools`` / ``cmsmcp_describe_tool`` / ``cmsmcp_call``.
"""

from __future__ import annotations

import functools
import inspect
import json
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from cmsmcp.config.settings import Settings
from cmsmcp.contracts.results import ToolEnvelope
from cmsmcp.output import budget, render_envelope
from cmsmcp.policy import ACT_EFFECTS, READ_EFFECTS, Effect, Policy
from cmsmcp.tools.common import failure

# Tool-name prefix -> group. Unlisted prefixes are their own group.
_GROUP_ALIASES = {"cern": "core", "cmsmcp": "core"}

# Profiles bundle groups. Any group prefix is also accepted directly.
PROFILES: dict[str, tuple[str, ...]] = {
    "search": ("search",),
    "docs": ("twiki", "cmssearch", "mkdocs", "gocms", "eguide", "websites", "search"),
    "meetings": ("indico",),
    "literature": ("inspire", "cds", "cmsresults", "docdb", "pdf"),
    "analysis": ("glance", "xsdb", "mcm", "das", "hltinfo", "pdf", "rucio"),
    "code": ("gitlab", "github", "cmssdt"),
    "chat": ("mattermost", "discourse", "hypernews"),
    "ops": (
        "cmsonline",
        "dqm",
        "runregistry",
        "jira",
        "servicenow",
        "phonebook",
        "edms",
        "hltinfo",
    ),
    "compute": ("lxplus", "rucio", "grid", "das"),
    "mine": ("mine",),
}
PROFILES["personal"] = ("mine", "mattermost", "gitlab", "jira", "indico", "lxplus")

CORE_GROUP = "core"


def group_of(tool_name: str) -> str:
    prefix = tool_name.split("_", 1)[0]
    return _GROUP_ALIASES.get(prefix, prefix)


def enabled_groups(profiles: Iterable[str]) -> set[str] | None:
    """Groups enabled by ``profiles``; ``None`` means every group."""
    wanted = {p.strip().lower() for p in profiles if p.strip()}
    if not wanted or "all" in wanted:
        return None
    groups = {CORE_GROUP}
    for name in wanted:
        groups.update(PROFILES.get(name, (name,)))
    return groups


@dataclass(frozen=True)
class ToolInfo:
    name: str
    group: str
    effect: Effect
    description: str
    hidden: bool


_SCHEMA_MAPS = frozenset({"properties", "$defs", "definitions", "patternProperties"})


def compact_schema(node: Any) -> Any:
    """Drop pydantic's auto ``title`` keywords (``"title": "Page"``) from a schema.

    Only the keyword is removed; a *property* named ``title`` survives because
    property maps are walked by name, not filtered.
    """
    if isinstance(node, list):
        return [compact_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "title" and isinstance(value, str):
            continue
        if key in _SCHEMA_MAPS and isinstance(value, dict):
            out[key] = {name: compact_schema(sub) for name, sub in value.items()}
        else:
            out[key] = compact_schema(value)
    return out


def _first_sentence(text: str, limit: int = 160) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    match = re.match(r"(.+?[.!?])(\s|$)", text)
    sentence = match.group(1) if match else text
    return sentence if len(sentence) <= limit else sentence[: limit - 1].rstrip() + "…"


class CmsMcpServer(MCPServer):
    """MCPServer whose tools are policy-wrapped and token-efficient."""

    def __init__(self, *args: Any, settings: Settings, policy: Policy, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.cmsmcp_settings = settings
        self.policy = policy
        self.tool_infos: dict[str, ToolInfo] = {}
        self._groups = enabled_groups(settings.profiles)
        self._lazy = settings.tool_mode == "lazy"
        # Lazy mode: non-core tools live on a private server that only
        # cmsmcp_call reaches, so the client's tool list stays tiny.
        self._backstage: MCPServer | None = (
            MCPServer(name="cmsmcp-backstage") if self._lazy else None
        )
        if self._lazy:
            self._register_lazy_tools()

    # ------------------------------------------------------------------ gating
    def group_enabled(self, group: str) -> bool:
        return group == CORE_GROUP or self._groups is None or group in self._groups

    def _admits(self, group: str, effect: Effect) -> bool:
        if effect in ACT_EFFECTS and not self.cmsmcp_settings.actions_enabled:
            return False
        return self.group_enabled(group)

    # ---------------------------------------------------------------- wrapping
    def _wrap(self, fn: Callable[..., Any], name: str, group: str, effect: Effect) -> Callable:
        settings = self.cmsmcp_settings
        policy = self.policy
        label = None if group == CORE_GROUP else group

        @functools.wraps(fn)
        async def wrapped(*args: Any, **kwargs: Any) -> str:
            started = time.monotonic()
            try:
                result = fn(*args, **kwargs)
                if inspect.isawaitable(result):
                    result = await result
            except Exception as exc:  # adapter bug or policy refusal: never crash the session
                result = failure(exc)
            ok = not (isinstance(result, ToolEnvelope) and result.status == "error")
            if ok and effect in READ_EFFECTS and label:
                policy.note_read(group)
            text = render_envelope(
                result,
                output_format=settings.output_format,
                max_chars=settings.max_result_chars,
                label=label if effect in READ_EFFECTS else None,
            )
            audited = {k: v for k, v in kwargs.items() if not isinstance(v, Context)}
            error_code = None
            if isinstance(result, ToolEnvelope) and result.error is not None:
                error_code = str(result.error.code)
            policy.audit(
                name,
                effect,
                audited,
                status="ok" if ok else (error_code or "error"),
                elapsed=time.monotonic() - started,
                extra={"chars": len(text)},
            )
            return text

        return wrapped

    def tool(  # type: ignore[override]  # noqa: PLR0917 - mirrors MCPServer.tool
        self,
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        annotations: ToolAnnotations | None = None,
        icons: list[Any] | None = None,
        meta: dict[str, Any] | None = None,
        structured_output: bool | None = None,  # noqa: ARG002 - always unstructured
        *,
        effect: Effect = Effect.READ,
        group: str | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            tool_name = name or fn.__name__
            tool_group = group or group_of(tool_name)
            if not self._admits(tool_group, effect):
                return fn
            hidden = self._lazy and tool_group != CORE_GROUP and tool_name != "cern_search"
            ann = annotations or ToolAnnotations(
                read_only_hint=effect in READ_EFFECTS,
                destructive_hint=effect == Effect.ACT_EXTERNAL,
                open_world_hint=True,
            )
            target = self._backstage if hidden and self._backstage is not None else self
            MCPServer.add_tool(
                target,
                self._wrap(fn, tool_name, tool_group, effect),
                name=tool_name,
                title=title,
                description=description,
                annotations=ann,
                icons=icons,
                meta=meta,
                structured_output=False,
            )
            registered = target._tool_manager.get_tool(tool_name)
            if registered is not None:
                registered.parameters = compact_schema(registered.parameters)
            self.tool_infos[tool_name] = ToolInfo(
                tool_name, tool_group, effect, description or fn.__doc__ or "", hidden
            )
            return fn

        return decorator

    # --------------------------------------------------------------- lazy mode
    def _register_lazy_tools(self) -> None:
        @self.tool(
            name="cmsmcp_find_tools",
            description=(
                "List cmsmcp tools matching words (name, group, or description), one line "
                "each. Lazy mode keeps ~170 CERN/CMS tools off the tool list: find one "
                "here, read its arguments with cmsmcp_describe_tool, run it with "
                "cmsmcp_call. Empty query lists every group with its tool count."
            ),
        )
        async def cmsmcp_find_tools(
            query: Annotated[str, Field(description="Words to match, e.g. 'indico slides'.")] = "",
            limit: Annotated[int, Field(ge=1, le=100)] = 25,
        ) -> str:
            return self._find_tools(query, limit)

        @self.tool(
            name="cmsmcp_describe_tool",
            description="Full description and argument schema of one cmsmcp tool by exact name.",
        )
        async def cmsmcp_describe_tool(name: str) -> str:
            return await self._describe_tool(name)

        @self.tool(
            name="cmsmcp_call",
            description=(
                "Run one cmsmcp tool by exact name with its arguments object (see "
                "cmsmcp_describe_tool). Same policy, confirmation and output rules as a "
                "direct call."
            ),
        )
        async def cmsmcp_call(
            name: str,
            ctx: Context,
            arguments: Annotated[
                dict[str, Any] | None, Field(description="Tool arguments.")
            ] = None,
        ) -> str:
            return await self._call_hidden(name, arguments or {}, ctx)

    def _hidden_infos(self) -> list[ToolInfo]:
        return [info for info in self.tool_infos.values() if info.hidden]

    def _find_tools(self, query: str, limit: int) -> str:
        infos = self._hidden_infos()
        words = [w for w in re.split(r"\W+", query.lower()) if w]
        if not words:
            counts: dict[str, int] = {}
            for info in infos:
                counts[info.group] = counts.get(info.group, 0) + 1
            return "groups: " + ", ".join(f"{g} ({n})" for g, n in sorted(counts.items()))

        def score(info: ToolInfo) -> int:
            hay = f"{info.name} {info.group} {info.description}".lower()
            return sum(3 if w in info.name else 1 for w in words if w in hay)

        ranked = sorted((i for i in infos if score(i)), key=score, reverse=True)[:limit]
        if not ranked:
            return "No matching tools; try other words or an empty query for the group list."
        return "\n".join(f"{i.name} [{i.effect}]: {_first_sentence(i.description)}" for i in ranked)

    async def _describe_tool(self, name: str) -> str:
        if self._backstage is None:
            return "Lazy mode is off; every tool is already listed."
        for tool in await self._backstage.list_tools():
            if tool.name == name:
                schema = tool.input_schema or {}
                return budget(
                    f"{tool.name}: {tool.description}\nargs: "
                    + json.dumps(schema.get("properties", {}), separators=(",", ":"))
                    + f"\nrequired: {', '.join(schema.get('required', [])) or '-'}",
                    self.cmsmcp_settings.max_result_chars,
                )
        return f"Unknown tool {name!r}; use cmsmcp_find_tools."

    async def _call_hidden(self, name: str, arguments: dict[str, Any], ctx: Context) -> str:
        if self._backstage is None or name not in self.tool_infos:
            return f"ERROR INVALID_INPUT: unknown tool {name!r}; use cmsmcp_find_tools."
        try:
            result = await self._backstage.call_tool(name, arguments, ctx)
        except Exception as exc:  # argument validation errors from the SDK
            return f"ERROR INVALID_INPUT: {exc}"
        content = getattr(result, "content", None) or []
        return "\n".join(getattr(block, "text", "") for block in content) or "ok"
