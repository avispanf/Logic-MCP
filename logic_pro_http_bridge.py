#!/usr/bin/env python3
"""Expose one persistent LogicProMCP stdio process over local Streamable HTTP.

Codex starts a fresh process for every STDIO MCP connection. LogicProMCP publishes
CoreMIDI endpoints at startup, so multiple Codex tasks make Logic repeatedly report
changing MIDI input/output counts. This bridge owns exactly one upstream process and
lets any number of local MCP clients share it over HTTP.
"""

from __future__ import annotations

if __name__ == "__main__":
    from logic_manual_mode import wait_for_automatic_mode
    wait_for_automatic_mode()

import argparse
import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel.server import Server
from mcp_types import (
    CallToolRequestParams,
    CallToolResult,
    CompleteRequestParams,
    CompleteResult,
    GetPromptRequestParams,
    GetPromptResult,
    InputRequiredResult,
    ListPromptsResult,
    ListResourceTemplatesResult,
    ListResourcesResult,
    ListToolsResult,
    PaginatedRequestParams,
    ReadResourceRequestParams,
    ReadResourceResult,
)
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

LOG = logging.getLogger("logic-pro-http-bridge")
PATCHED_BACKEND = Path.home() / ".local/bin/LogicProMCP-codex"
BREW_BACKEND = Path("/opt/homebrew/bin/LogicProMCP")


def default_backend() -> str:
    """Prefer the local build that supports stable per-instance MIDI names."""
    if PATCHED_BACKEND.is_file() and os.access(PATCHED_BACKEND, os.X_OK):
        return str(PATCHED_BACKEND)
    return str(BREW_BACKEND)


@dataclass(slots=True)
class BridgeState:
    session: ClientSession
    lock: anyio.Lock
    upstream_name: str
    upstream_version: str
    tool_count: int


def _state(ctx: ServerRequestContext[BridgeState, Any]) -> BridgeState:
    return ctx.lifespan_context


def build_server(state: BridgeState) -> Server[BridgeState]:
    @asynccontextmanager
    async def connection_lifespan(_: Server[BridgeState]):
        # The backend belongs to the process-wide serve() context. Individual HTTP
        # sessions only borrow it and must never create or close another MIDI owner.
        yield state

    async def list_tools(
        ctx: ServerRequestContext[BridgeState, Any], params: PaginatedRequestParams | None
    ) -> ListToolsResult:
        bridge = _state(ctx)
        async with bridge.lock:
            return await bridge.session.list_tools(params=params)

    async def call_tool(
        ctx: ServerRequestContext[BridgeState, Any], params: CallToolRequestParams
    ) -> CallToolResult | InputRequiredResult:
        bridge = _state(ctx)
        async with bridge.lock:
            result = await bridge.session.call_tool(
                params.name,
                params.arguments,
                input_responses=params.input_responses,
                request_state=params.request_state,
                meta=params.meta,
                allow_input_required=True,
            )
        if not isinstance(result, (CallToolResult, InputRequiredResult)):
            raise RuntimeError(f"unsupported claimed tool result: {type(result).__name__}")
        return result

    async def list_resources(
        ctx: ServerRequestContext[BridgeState, Any], params: PaginatedRequestParams | None
    ) -> ListResourcesResult:
        bridge = _state(ctx)
        async with bridge.lock:
            return await bridge.session.list_resources(params=params)

    async def list_resource_templates(
        ctx: ServerRequestContext[BridgeState, Any], params: PaginatedRequestParams | None
    ) -> ListResourceTemplatesResult:
        bridge = _state(ctx)
        async with bridge.lock:
            return await bridge.session.list_resource_templates(params=params)

    async def read_resource(
        ctx: ServerRequestContext[BridgeState, Any], params: ReadResourceRequestParams
    ) -> ReadResourceResult | InputRequiredResult:
        bridge = _state(ctx)
        async with bridge.lock:
            return await bridge.session.read_resource(
                params.uri,
                input_responses=params.input_responses,
                request_state=params.request_state,
                meta=params.meta,
                allow_input_required=True,
            )

    async def list_prompts(
        ctx: ServerRequestContext[BridgeState, Any], params: PaginatedRequestParams | None
    ) -> ListPromptsResult:
        bridge = _state(ctx)
        async with bridge.lock:
            return await bridge.session.list_prompts(params=params)

    async def get_prompt(
        ctx: ServerRequestContext[BridgeState, Any], params: GetPromptRequestParams
    ) -> GetPromptResult | InputRequiredResult:
        bridge = _state(ctx)
        async with bridge.lock:
            return await bridge.session.get_prompt(
                params.name,
                params.arguments,
                input_responses=params.input_responses,
                request_state=params.request_state,
                meta=params.meta,
                allow_input_required=True,
            )

    async def complete(
        ctx: ServerRequestContext[BridgeState, Any], params: CompleteRequestParams
    ) -> CompleteResult:
        bridge = _state(ctx)
        context_arguments = params.context.arguments if params.context else None
        async with bridge.lock:
            return await bridge.session.complete(
                params.ref,
                params.argument.model_dump(mode="python"),
                context_arguments,
            )

    return Server(
        "logic-pro-shared",
        title="Logic Pro MCP (shared)",
        version=state.upstream_version,
        instructions=(
            "Shared local proxy for one persistent LogicProMCP process. Calls are "
            "serialized so GUI and MCU operations cannot overlap across clients."
        ),
        lifespan=connection_lifespan,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
        on_list_resources=list_resources,
        on_list_resource_templates=list_resource_templates,
        on_read_resource=read_resource,
        on_list_prompts=list_prompts,
        on_get_prompt=get_prompt,
        on_completion=complete,
    )


async def serve(args: argparse.Namespace) -> None:
    backend = Path(args.backend).expanduser().resolve()
    if not backend.is_file() or not os.access(backend, os.X_OK):
        raise RuntimeError(f"LogicProMCP backend is not executable: {backend}")

    backend_env = {
        "LOGIC_PRO_MCP_MIDI_INSTANCE_ID": args.instance_id,
        "LOGIC_PRO_MCP_SHARE_DIR": args.share_dir,
    }
    parameters = StdioServerParameters(command=str(backend), env=backend_env)
    async with stdio_client(parameters) as streams:
        async with ClientSession(*streams, read_timeout_seconds=args.tool_timeout) as session:
            initialized = await session.initialize()
            tools = await session.list_tools()
            state = BridgeState(
                session=session,
                lock=anyio.Lock(),
                upstream_name=initialized.server_info.name,
                upstream_version=initialized.server_info.version,
                tool_count=len(tools.tools),
            )
            server = build_server(state)

            async def health(_: Request) -> JSONResponse:
                return JSONResponse(
                    {
                        "ready": True,
                        "backend": state.upstream_name,
                        "version": state.upstream_version,
                        "tools": state.tool_count,
                        "instance_id": args.instance_id,
                        "backend_path": str(backend),
                    }
                )

            app = server.streamable_http_app(
                streamable_http_path=args.path,
                json_response=True,
                host=args.host,
                custom_starlette_routes=[Route("/health", health, methods=["GET"])],
            )
            config = uvicorn.Config(
                app,
                host=args.host,
                port=args.port,
                log_level=args.log_level,
                access_log=False,
            )
            http_server = uvicorn.Server(config)

            async def monitor_backend() -> None:
                while not http_server.should_exit:
                    await anyio.sleep(args.health_interval)
                    try:
                        async with state.lock:
                            await state.session.send_ping()
                    except Exception:
                        LOG.exception("LogicProMCP backend stopped responding")
                        http_server.should_exit = True
                        return

            LOG.info(
                "ready on http://%s:%s%s with %s %s (%s tools)",
                args.host,
                args.port,
                args.path,
                state.upstream_name,
                state.upstream_version,
                state.tool_count,
            )
            async with anyio.create_task_group() as group:
                group.start_soon(monitor_backend)
                await http_server.serve()
                group.cancel_scope.cancel()
            if not http_server.started:
                raise RuntimeError(f"could not listen on {args.host}:{args.port}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--path", default="/mcp")
    parser.add_argument("--backend", default=default_backend())
    parser.add_argument("--instance-id", default="bridge")
    parser.add_argument(
        "--share-dir",
        default="/opt/homebrew/opt/logic-pro-mcp/share/logic-pro-mcp",
    )
    parser.add_argument("--tool-timeout", type=float, default=180.0)
    parser.add_argument("--health-interval", type=float, default=10.0)
    parser.add_argument("--log-level", default="info")
    return parser.parse_args(argv)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(serve(parse_args()))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        LOG.exception("bridge failed: %s", exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
