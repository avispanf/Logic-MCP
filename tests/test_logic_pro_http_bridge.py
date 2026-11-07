import types
import unittest
from unittest.mock import AsyncMock, patch
from pathlib import Path
import tempfile
import logic_manual_mode as manual

import anyio
from mcp_types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)

import logic_pro_http_bridge as bridge


class FakeSession:
    def __init__(self):
        self.calls = []

    async def list_tools(self, *, params=None):
        self.calls.append(("list_tools", params))
        return ListToolsResult(
            tools=[
                Tool(
                    name="logic_system",
                    description="health",
                    input_schema={"type": "object"},
                )
            ]
        )

    async def call_tool(self, name, arguments, **kwargs):
        self.calls.append(("call_tool", name, arguments, kwargs))
        return CallToolResult(content=[TextContent(text="ok")], is_error=False)


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for key, value in (("STATE_DIR", Path(directory.name)), ("STATE_FILE", Path(directory.name) / "manual.json")):
            patched = patch.object(manual, key, value)
            patched.start()
            self.addCleanup(patched.stop)
        self.session = FakeSession()
        self.state = bridge.BridgeState(
            session=self.session,
            lock=anyio.Lock(),
            upstream_name="logic-pro-mcp",
            upstream_version="3.13.0",
            tool_count=10,
        )
        self.server = bridge.build_server(self.state)
        self.context = types.SimpleNamespace(lifespan_context=self.state)

    async def test_list_tools_is_forwarded_without_schema_reconstruction(self):
        entry = self.server.get_request_handler("tools/list")
        self.assertIsNotNone(entry)
        params = PaginatedRequestParams(cursor="next")

        result = await entry.handler(self.context, params)

        self.assertEqual([tool.name for tool in result.tools], ["logic_system"])
        self.assertEqual(self.session.calls, [("list_tools", params)])

    async def test_tool_call_forwards_arguments_and_returns_upstream_result(self):
        entry = self.server.get_request_handler("tools/call")
        self.assertIsNotNone(entry)
        params = CallToolRequestParams(
            name="logic_system",
            arguments={"command": "health", "params": {}},
        )

        result = await entry.handler(self.context, params)

        self.assertFalse(result.is_error)
        self.assertEqual(result.content[0].text, "ok")
        _, name, arguments, kwargs = self.session.calls[0]
        self.assertEqual(name, "logic_system")
        self.assertEqual(arguments, {"command": "health", "params": {}})
        self.assertTrue(kwargs["allow_input_required"])

    async def test_tool_call_reads_sdk_mapping_metadata(self):
        entry = self.server.get_request_handler("tools/call")
        params = CallToolRequestParams(name="logic_system", arguments={}, meta={"logic-mcp-owner": "audit-token"})
        with patch.object(bridge.control, "operation") as operation:
            await entry.handler(self.context, params)
        self.assertEqual(operation.call_args.kwargs["owner"], "audit-token")
        self.assertEqual(self.session.calls[0][3]["meta"], {"logic-mcp-owner": "audit-token"})

    async def test_schema_cache_is_available_without_entering_audit_lease(self):
        self.state.tools = ListToolsResult(tools=[Tool(name="cached", input_schema={"type": "object"})])
        entry = self.server.get_request_handler("tools/list")
        with patch.object(bridge.control, "operation", side_effect=AssertionError("Must not touch backend")):
            result = await entry.handler(self.context, None)
        self.assertEqual([tool.name for tool in result.tools], ["cached"])
        self.assertEqual(self.session.calls, [])

    async def test_slow_diagnostic_does_not_recreate_backend(self):
        entered, completed = [], []
        async def slow_health(*args):
            await asyncio.sleep(10)
        fake = types.SimpleNamespace(
            initialize=AsyncMock(return_value=types.SimpleNamespace(
                server_info=types.SimpleNamespace(version="3.13.0", name="fake"))),
            list_tools=AsyncMock(return_value=ListToolsResult(tools=[Tool(name="logic_system", input_schema={"type": "object"})])),
            send_ping=AsyncMock(), call_tool=AsyncMock(side_effect=slow_health))
        @asynccontextmanager
        async def transport(*args):
            entered.append(args[0])
            yield None, None
        @asynccontextmanager
        async def session(*args, **kwargs):
            yield fake
        original_probe = bridge.probe_backend
        async def quick_probe(state):
            await original_probe(state, timeout=.01)
            completed.append(True)
        args = types.SimpleNamespace(backend="/fake/backend", instance_id="bridge", share_dir="/fake", tool_timeout=1, health_interval=0)
        with patch.object(bridge, "gated_backend", transport), patch.object(bridge, "ClientSession", session), \
                patch.object(bridge, "validate_backend"), patch.object(bridge, "probe_backend", quick_probe):
            task = asyncio.create_task(bridge.backend_loop(self.state, args))
            try:
                with anyio.fail_after(3):
                    while len(completed) < 2:
                        await asyncio.sleep(.01)
                self.assertEqual(len(entered), 1)
                self.assertEqual(entered[0].env["LOGIC_PRO_MCP_MIDI_INSTANCE_ID"], "bridge")
                self.assertEqual(entered[0].env["LOGIC_PRO_MCP_TRACK_SELECT_MCU_FIRST"], "1")
                self.assertIs(self.state.session, fake)
                self.assertIn("preserving", self.state.error)
                self.assertEqual(self.state.capabilities, {})
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

    async def test_ping_timeout_preserves_session(self):
        session = types.SimpleNamespace(send_ping=AsyncMock(side_effect=TimeoutError))
        self.state.session = session
        await bridge.probe_backend(self.state)
        self.assertIs(self.state.session, session)
        self.assertIn("ping timed out", self.state.error)

    async def test_health_probe_recovers_without_replacing_session(self):
        payload = {"permissions": {"accessibility": True}, "mcu": {"registered_as_device": False}}
        session = types.SimpleNamespace(send_ping=AsyncMock(), call_tool=AsyncMock(side_effect=[
            TimeoutError(), CallToolResult(content=[], structured_content=payload)]))
        self.state.session = session
        self.state.tools = ListToolsResult(tools=[Tool(name="logic_system", input_schema={"type": "object"})])
        await bridge.probe_backend(self.state)
        self.assertEqual(self.state.capabilities, {})
        self.assertIn("timed out", self.state.error)
        await bridge.probe_backend(self.state)
        self.assertIs(self.state.session, session)
        self.assertEqual(self.state.capabilities, payload)
        self.assertIsNone(self.state.error)

    async def test_transport_failure_is_not_silently_ignored(self):
        self.state.session = types.SimpleNamespace(send_ping=AsyncMock(side_effect=ConnectionError("closed")))
        with self.assertRaises(ConnectionError):
            await bridge.probe_backend(self.state)

    def test_readiness_uses_reported_mcu_fields_and_fails_closed(self):
        permissions = dict.fromkeys(("accessibility", "automation_granted", "automation_system_events", "post_event_access"), True)
        mcu = {"registered_as_device": True, "connected": True, "feedback_stale": False}
        self.assertEqual(bridge.capability_readiness({"permissions": permissions, "mcu": mcu}), "ready")
        cases = (({}, "checking"),
                 ({**mcu, "registered_as_device": False}, "mcu-setup-required"),
                 ({**mcu, "connected": False}, "mcu-feedback-required"),
                 ({**mcu, "feedback_stale": True}, "mcu-feedback-stale"))
        for value, expected in cases:
            with self.subTest(mcu=value):
                self.assertEqual(bridge.capability_readiness({"permissions": permissions, "mcu": value}), expected)
        self.assertEqual(bridge.capability_readiness({}), "checking")
        for malformed in (None, {"permissions": None}, {"permissions": permissions, "mcu": []}):
            self.assertEqual(bridge.capability_readiness(malformed), "checking")
        self.assertEqual(bridge.capability_readiness({"permissions": {**permissions, "accessibility": False}, "mcu": mcu}), "permissions-required")

    def test_defaults_are_loopback_only(self):
        args = bridge.parse_args([])
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8765)
        self.assertEqual(args.path, "/mcp")
        self.assertEqual(args.instance_id, "bridge")
        self.assertEqual(args.backend, bridge.default_backend())


if __name__ == "__main__":
    unittest.main()
import asyncio
from contextlib import asynccontextmanager
