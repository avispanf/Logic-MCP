#!/usr/bin/env python3
"""One persistent HTTP supervisor for one restartable LogicProMCP backend."""
from __future__ import annotations
import argparse
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import fcntl
import logging
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import anyio
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.lowlevel.server import Server
from mcp.server.context import ServerRequestContext
from mcp_types import (CallToolRequestParams, CallToolResult, InputRequiredResult,
    ListToolsResult, ListResourcesResult, ListResourceTemplatesResult, ListPromptsResult,
    PaginatedRequestParams, ReadResourceRequestParams, ReadResourceResult,
    GetPromptRequestParams, GetPromptResult, CompleteRequestParams, CompleteResult)
from starlette.responses import JSONResponse
from starlette.routing import Route
import logic_control as control
import logic_manual_mode as manual

LOG = logging.getLogger("logic-pro-http-bridge")
PATCHED_BACKEND = Path.home() / ".local/bin/LogicProMCP-codex"


def default_backend():
    configured = os.environ.get("LOGIC_MCP_BACKEND")
    bundled = Path(__file__).resolve().parent / "bin/LogicProMCP"
    if getattr(sys, "frozen", False):
        bundled = Path(sys.executable).resolve().parent.parent / "bin/LogicProMCP"
    installed = manual.STATE_DIR / "runtime/bin/LogicProMCP"
    return str(configured or next((p for p in (bundled, installed, PATCHED_BACKEND) if p.is_file()), bundled))


@dataclass
class BridgeState:
    session: ClientSession | None
    lock: anyio.Lock
    upstream_name: str = "logic-pro-mcp"
    upstream_version: str = "3.13.0"
    tool_count: int = 0
    tools: ListToolsResult = field(default_factory=lambda: ListToolsResult(tools=[]))
    last_ping: float = 0
    error: str | None = None
    capabilities: dict = field(default_factory=dict)


def _owner(meta):
    return meta.model_dump().get("logic-mcp-owner") if meta else None


def build_server(state):
    @asynccontextmanager
    async def lifespan(_):
        yield state

    async def dispatch(method, *args, owner=None, **kwargs):
        initial = control.epoch()
        control.require_automatic()
        async with state.lock:
            if state.session is None:
                raise RuntimeError("Logic backend is offline or starting. Check /health and retry a new request.")
            with control.operation(owner=owner, expected_epoch=initial):
                return await getattr(state.session, method)(*args, **kwargs)

    async def list_tools(ctx: ServerRequestContext, params: PaginatedRequestParams | None) -> ListToolsResult:
        if manual.STATE_FILE.exists() or state.session is None:
            return state.tools
        return await dispatch("list_tools", params=params)

    async def call_tool(ctx: ServerRequestContext, params: CallToolRequestParams) -> CallToolResult | InputRequiredResult:
        return await dispatch("call_tool", params.name, params.arguments, owner=_owner(params.meta),
            input_responses=params.input_responses, request_state=params.request_state,
            meta=params.meta, allow_input_required=True)

    async def list_resources(ctx: ServerRequestContext, params: PaginatedRequestParams | None) -> ListResourcesResult:
        return await dispatch("list_resources", params=params)

    async def list_templates(ctx: ServerRequestContext, params: PaginatedRequestParams | None) -> ListResourceTemplatesResult:
        return await dispatch("list_resource_templates", params=params)

    async def read_resource(ctx: ServerRequestContext, params: ReadResourceRequestParams) -> ReadResourceResult | InputRequiredResult:
        return await dispatch("read_resource", params.uri, owner=_owner(params.meta),
            input_responses=params.input_responses, request_state=params.request_state,
            meta=params.meta, allow_input_required=True)

    async def list_prompts(ctx: ServerRequestContext, params: PaginatedRequestParams | None) -> ListPromptsResult:
        return await dispatch("list_prompts", params=params)

    async def get_prompt(ctx: ServerRequestContext, params: GetPromptRequestParams) -> GetPromptResult | InputRequiredResult:
        return await dispatch("get_prompt", params.name, params.arguments, owner=_owner(params.meta),
            input_responses=params.input_responses, request_state=params.request_state,
            meta=params.meta, allow_input_required=True)

    async def complete(ctx: ServerRequestContext, params: CompleteRequestParams) -> CompleteResult:
        return await dispatch("complete", params.ref, params.argument.model_dump(mode="python"),
            params.context.arguments if params.context else None)

    return Server("logic-pro-shared", title="Logic MCP", version="0.2.0",
        instructions="Shared Logic backend. Manual mode rejects work; re-enabling accepts new requests only.",
        lifespan=lifespan, on_list_tools=list_tools, on_call_tool=call_tool,
        on_list_resources=list_resources, on_list_resource_templates=list_templates,
        on_read_resource=read_resource, on_list_prompts=list_prompts,
        on_get_prompt=get_prompt, on_completion=complete)


def validate_backend(path):
    if not path.is_file() or not os.access(path, os.X_OK):
        raise RuntimeError(f"Missing backend: {path}. Run the Logic MCP installer.")
    # Reject the unpatched Homebrew binary: same version does not imply the same capabilities.
    if b"LOGIC_PRO_MCP_MIDI_INSTANCE_ID" not in path.read_bytes():
        raise RuntimeError("Backend lacks deterministic MIDI namespace support. Install the supported build.")


async def backend_loop(state, args):
    parameters = StdioServerParameters(command=str(Path(args.backend).expanduser().resolve()),
        env={"LOGIC_PRO_MCP_MIDI_INSTANCE_ID": args.instance_id,
             "LOGIC_MCP_STATE_DIR": str(manual.STATE_DIR),
             "LOGIC_PRO_MCP_SHARE_DIR": args.share_dir})
    while True:
        if manual.STATE_FILE.exists():
            state.session = None
            await anyio.sleep(0.2)
            continue
        try:
            with control.operation():
                validate_backend(Path(parameters.command))
            async with gated_backend(parameters) as streams:
                async with ClientSession(*streams, read_timeout_seconds=args.tool_timeout) as session:
                    with anyio.fail_after(20):
                        initialized = await session.initialize()
                        if initialized.server_info.version != "3.13.0":
                            raise RuntimeError("Unsupported backend protocol version; expected 3.13.0")
                        tools = await session.list_tools()
                    if manual.STATE_FILE.exists():
                        continue
                    state.upstream_name = initialized.server_info.name
                    state.upstream_version = initialized.server_info.version
                    state.tools, state.tool_count = tools, len(tools.tools)
                    manual._write(manual.STATE_DIR / "tools.json", tools.model_dump(mode="json"))
                    state.last_ping, state.error = time.monotonic(), None
                    state.session = session
                    while not manual.STATE_FILE.exists():
                        await anyio.sleep(0.2)
                        if time.monotonic() - state.last_ping > args.health_interval and not state.lock.locked():
                            async with state.lock:
                                with anyio.fail_after(5):
                                    await session.send_ping()
                                    if any(tool.name == "logic_system" for tool in state.tools.tools):
                                        try:
                                            with control.operation():
                                                report = await session.call_tool("logic_system", {"command": "health", "params": {}})
                                            payload = getattr(report, "structured_content", None)
                                            if not isinstance(payload, dict):
                                                payload = next((json.loads(block.text) for block in report.content if getattr(block, "text", "").lstrip().startswith("{")), {})
                                            state.capabilities = payload
                                        except RuntimeError:
                                            pass  # Another client holds the complete audit lease.
                                state.last_ping = time.monotonic()
                    # Withdraw the old session before cleanup; no queued command may reuse it.
                    state.session = None
        except Exception as exc:
            state.error = str(exc)
            LOG.warning("backend unavailable: %s", exc)
        finally:
            state.session = None
        await anyio.sleep(1)


@asynccontextmanager
async def gated_backend(parameters):
    # Serialize native process creation with the controller's manual-mode barrier.
    manager = stdio_client(parameters)
    with (manual.STATE_DIR / "manual-mode.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Mode transition in progress")
        control.require_automatic()
        streams = await manager.__aenter__()
    try:
        yield streams
    except BaseException:
        import sys
        if not await manager.__aexit__(*sys.exc_info()):
            raise
    else:
        await manager.__aexit__(None, None, None)


async def serve(args):
    if args.host not in ("127.0.0.1", "::1"):
        raise RuntimeError("Logic MCP accepts loopback bindings only")
    manual.STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (manual.STATE_DIR / "supervisor.lock").open("a") as singleton:
        try:
            fcntl.flock(singleton, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("A Logic MCP supervisor already owns this installation")
        # Check the port before creating MIDI endpoints.
        with socket.socket(socket.AF_INET6 if args.host == "::1" else socket.AF_INET) as probe:
            probe.bind((args.host, args.port))
        state = BridgeState(session=None, lock=anyio.Lock())
        try:
            state.tools = ListToolsResult.model_validate_json((manual.STATE_DIR / "tools.json").read_text())
            state.tool_count = len(state.tools.tools)
        except (OSError, ValueError):
            pass

        async def health(_):
            paused = manual.STATE_FILE.exists()
            backend_ready = state.session is not None and (state.lock.locked() or time.monotonic() - state.last_ping < 30)
            running = False
            if not paused and backend_ready:
                running = await anyio.to_thread.run_sync(lambda: subprocess.run(
                    ["/usr/bin/pgrep", "-x", "Logic Pro"], capture_output=True, timeout=3).returncode == 0)
            status = "manual" if paused else "offline" if not backend_ready else "logic-closed" if not running else "ready"
            if status == "ready" and any(tool.name == "logic_system" for tool in state.tools.tools):
                permissions = state.capabilities.get("permissions", {})
                mcu = state.capabilities.get("mcu", {})
                if not state.capabilities:
                    status = "checking"
                elif permissions.get("accessibility") is False:
                    status = "permissions-required"
                elif mcu.get("registeredAsDevice") is False:
                    status = "mcu-setup-required"
            return JSONResponse({"service": "logic-mcp", "ready": status == "ready", "state": status,
                "backend_ready": backend_ready and not paused, "instance_id": args.instance_id,
                "tools": state.tool_count, "version": state.upstream_version,
                "backend_path": args.backend, "error": state.error})

        app = build_server(state).streamable_http_app(host=args.host, json_response=True,
            streamable_http_path=args.path, custom_starlette_routes=[Route("/health", health)])
        http = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port,
            log_level=args.log_level, access_log=False, timeout_graceful_shutdown=3))
        with manual.register_supervisor():
            async with anyio.create_task_group() as group:
                group.start_soon(backend_loop, state, args)
                await http.serve()
                group.cancel_scope.cancel()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--path", default="/mcp")
    parser.add_argument("--backend", default=default_backend())
    parser.add_argument("--instance-id", default="bridge")
    parser.add_argument("--share-dir", default=str(Path(default_backend()).parent.parent / "share/logic-pro-mcp"))
    parser.add_argument("--tool-timeout", type=float, default=180)
    parser.add_argument("--health-interval", type=float, default=10)
    parser.add_argument("--log-level", default="info")
    return parser.parse_args(argv)


def main():
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(serve(parse_args()))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        LOG.error("supervisor failed: %s", exc)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
