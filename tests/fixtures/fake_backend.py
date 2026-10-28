"""Protocol-only backend for integration tests. Creates no MIDI or Logic connection."""
import asyncio
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server
from mcp_types import ListToolsResult, Tool, CallToolResult, TextContent

# Capability marker checked by the supervisor; this fake has no actual MIDI engine.
LOGIC_PRO_MCP_MIDI_INSTANCE_ID = 'test-only'


async def tools(ctx, params):
    return ListToolsResult(tools=[Tool(name='echo', input_schema={'type': 'object'})])


async def call(ctx, params):
    return CallToolResult(content=[TextContent(text=str(params.arguments))])


async def main():
    server = Server('fake-core', version='3.13.0', on_list_tools=tools, on_call_tool=call)
    async with stdio_server() as streams:
        await server.run(*streams, server.create_initialization_options())


if __name__ == '__main__':
    asyncio.run(main())
