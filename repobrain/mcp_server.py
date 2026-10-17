"""MCP server: the same graph tool catalogue the agent uses, for any MCP client.

  python -m repobrain mcp            stdio transport, for Claude Desktop, Cursor and other desktop clients
  python -m repobrain serve          also mounts it over streamable HTTP at /mcp

Each catalogue tool becomes an MCP tool with a typed signature generated from its JSON schema,
so clients get proper argument validation. Results carry the rows plus the evidence ids the rows
rest on, so a client model can cite the same graph nodes the web UI draws.
"""

import inspect
import json
import os
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from . import coord, memory, tools  # noqa: F401  importing coord and memory registers their tools
from .db import get_graph

PY_TYPES: dict[str, Any] = {"string": str, "integer": int, "array": list[str]}
WRITE_TOOLS = {"remember", "draft_triage"}
MCP_USER = os.getenv("REPOBRAIN_MCP_USER", "mcp")  # whose memory recall/remember use over MCP

INSTRUCTIONS = """Repo Brain: a FalkorDB knowledge graph of the {tenant} GitHub org (issues, PRs, reviews,
comments, files, releases and the decisions extracted from discussions). Use search first for concepts,
then the specific tools. Cite the ids in evidence_ids when you name people, tickets or files."""


def _wrap(t: tools.Tool, tenant: str):
    def call(**kwargs: Any) -> str:
        args = {k: v for k, v in kwargs.items() if v is not None}
        result = tools.run(get_graph(tenant), t.name, args, user=MCP_USER)
        return json.dumps({**result.for_llm(), "evidence_ids": list(result.evidence.nodes)[:80]}, ensure_ascii=False)

    params = []
    for name, spec in t.params.items():
        typ, desc = PY_TYPES[spec["type"]], spec.get("description", "")
        required = name in t.required
        annotation = Annotated[typ, Field(description=desc)] if required else Annotated[typ | None, Field(description=desc)]
        params.append(inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, annotation=annotation,
                                        default=inspect.Parameter.empty if required else None))
    call.__signature__ = inspect.Signature(params, return_annotation=str)
    call.__annotations__ = {p.name: p.annotation for p in params} | {"return": str}
    call.__name__, call.__doc__ = t.name, t.description
    return call


def build(tenant: str = "FalkorDB") -> MCPServer:
    server = MCPServer(name="repo-brain", instructions=INSTRUCTIONS.format(tenant=tenant))
    for t in tools.CATALOGUE.values():
        server.add_tool(_wrap(t, tenant), name=t.name, description=t.description,
                        annotations=ToolAnnotations(readOnlyHint=t.name not in WRITE_TOOLS))
    return server
