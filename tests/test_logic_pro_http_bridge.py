import types
import unittest

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

    def test_defaults_are_loopback_only(self):
        args = bridge.parse_args([])
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8765)
        self.assertEqual(args.path, "/mcp")
        self.assertEqual(args.instance_id, "bridge")
        self.assertEqual(args.backend, bridge.default_backend())


if __name__ == "__main__":
    unittest.main()
