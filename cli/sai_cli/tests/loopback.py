"""Loopback servers and protocol recordings shared by CLI integration tests."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from websockets.http11 import Request, Response
from websockets.sync.server import ServerConnection, serve
from websockets.typing import Subprotocol

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@contextmanager
def http_server(handler: type[BaseHTTPRequestHandler]) -> Generator[str]:
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join()


@contextmanager
def graphql_ws(
    handler: Callable[[ServerConnection], None],
    *,
    process_request: Callable[[ServerConnection, Request], Response | None]
    | None = None,
    create_connection: type[ServerConnection] | None = None,
) -> Generator[str]:
    with serve(
        handler,
        "127.0.0.1",
        0,
        subprotocols=[Subprotocol("graphql-transport-ws")],
        process_request=process_request,
        create_connection=create_connection,
    ) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"ws://127.0.0.1:{server.socket.getsockname()[1]}/query"
        finally:
            server.shutdown()
            thread.join()
