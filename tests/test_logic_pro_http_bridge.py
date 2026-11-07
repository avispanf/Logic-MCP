import types
import unittest
from unittest.mock import patch
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

    def test_defaults_are_loopback_only(self):
        args = bridge.parse_args([])
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8765)
        self.assertEqual(args.path, "/mcp")
        self.assertEqual(args.instance_id, "bridge")
        self.assertEqual(args.backend, bridge.default_backend())


if __name__ == "__main__":
    unittest.main()
