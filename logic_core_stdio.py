import asyncio
import argparse
from urllib.parse import urlparse

import anyio
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.stdio import stdio_server

from logic_pro_http_bridge import BridgeState, build_server
import logic_manual_mode as manual


async def serve(url='http://127.0.0.1:8765/mcp'):
    with manual.register_worker():
        async with streamable_http_client(url) as streams:
            async with ClientSession(streams[0], streams[1], read_timeout_seconds=180) as session:
                await session.initialize()
                state = BridgeState(session=session, lock=anyio.Lock(), tools=await session.list_tools())
                server = build_server(state, local_ownership=False)
                async with stdio_server() as local:
                    await server.run(*local, server.create_initialization_options())


def main():
    parser = argparse.ArgumentParser(description='STDIO relay to the shared local Logic backend; creates no MIDI ports.')
    parser.add_argument('--url', default='http://127.0.0.1:8765/mcp')
    args = parser.parse_args()
    url = urlparse(args.url)
    if url.scheme != 'http' or url.hostname != '127.0.0.1' or url.username or url.password or url.query or url.fragment or url.path != '/mcp':
        raise SystemExit('The shared backend URL must be local http://127.0.0.1:PORT/mcp')
    asyncio.run(serve(args.url))


if __name__ == '__main__':
    main()
