"""MCP stdio with a bounded drain on stdin EOF and process signals."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from concurrent.futures import Future, ThreadPoolExecutor
from queue import SimpleQueue
from threading import Thread, Timer
from typing import Any

from anyio import (
    AsyncFile,
    CancelScope,
    create_memory_object_stream,
    create_task_group,
    move_on_after,
    sleep_forever,
)
from mcp import types
from mcp.server.lowlevel import Server
from mcp.shared.message import SessionMessage

from ..core import log

SHUTDOWN_GRACE = 1.0
SHUTDOWN_REPLY_TIMEOUT = 0.5
SHUTDOWN_EXIT_TIMEOUT = SHUTDOWN_GRACE + SHUTDOWN_REPLY_TIMEOUT + 0.5
SHUTDOWN_ERROR = "Sai MCP is shutting down. If you submitted a trade, check its status before retrying."


class StdioInput(AsyncFile[str]):
    """Read stdin without making event loop shutdown wait for the next line."""

    def __init__(self) -> None:
        # The reader owns its file, so a blocked read cannot hold a lock on
        # Python's standard streams when the interpreter exits.
        super().__init__(
            os.fdopen(os.dup(sys.stdin.fileno()), encoding="utf-8", errors="replace")
        )
        self.requests: SimpleQueue[Future[str] | None] = SimpleQueue()
        Thread(target=self.read_lines, daemon=True, name="mcp-stdin").start()

    def read_lines(self) -> None:
        with self.wrapped as stdin:
            while (request := self.requests.get()) is not None:
                if request.set_running_or_notify_cancel():
                    try:
                        request.set_result(stdin.readline())
                    except OSError as exc:
                        request.set_exception(exc)

    async def readline(self) -> str:
        request: Future[str] = Future()
        self.requests.put(request)
        return await asyncio.wrap_future(request)

    def stop(self) -> None:
        self.requests.put(None)


async def run_stdio(server: Server[Any, Any], *, standalone: bool) -> None:
    """Keep the session open until accepted requests have sent their replies."""
    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()
    finished = asyncio.Event()
    drained = asyncio.Event()
    drained.set()
    pending: set[types.RequestId] = set()
    output_lock = asyncio.Lock()
    # Keeper calls can fill the default pool. Replies still need a worker.
    output = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mcp-stdout")
    read_send, read = create_memory_object_stream[SessionMessage | Exception](0)
    write, write_receive = create_memory_object_stream[SessionMessage](0)
    request_scope = CancelScope()

    def begin_shutdown() -> None:
        if stopping.is_set():
            return
        stopping.set()
        if standalone:
            # Covers workers left behind by a client cancellation and a
            # pipe whose peer stops reading. An ordinary process exit wins.
            watchdog = Timer(SHUTDOWN_EXIT_TIMEOUT, os._exit, args=(0,))
            watchdog.daemon = True
            watchdog.start()

    def terminate(_signum: int, _frame: object) -> None:
        if not stopping.is_set():
            loop.call_soon_threadsafe(begin_shutdown)

    def write_line(line: str) -> None:
        sys.stdout.buffer.write((line + "\n").encode("utf-8"))
        sys.stdout.buffer.flush()

    async def emit(message: types.JSONRPCMessage) -> None:
        root = message.root
        request_id = (
            root.id
            if isinstance(root, (types.JSONRPCResponse, types.JSONRPCError))
            else None
        )
        async with output_lock:
            if (
                stopping.is_set()
                and request_id is not None
                and request_id not in pending
            ):
                return
            await loop.run_in_executor(
                output,
                write_line,
                root.model_dump_json(by_alias=True, exclude_none=True),
            )
            if request_id is not None:
                pending.discard(request_id)
                if not pending:
                    drained.set()

    async def forward_replies() -> None:
        async with write_receive:
            async for message in write_receive:
                await emit(message.message)
        await finished.wait()
        if not stopping.is_set():
            group.cancel_scope.cancel()

    async def forward_requests(stdin: StdioInput) -> None:
        async with read_send:
            while True:
                line = await stdin.readline()
                if not line:
                    begin_shutdown()
                if stopping.is_set():
                    # Keep the SDK session open while it sends outstanding replies.
                    await sleep_forever()
                try:
                    message = types.JSONRPCMessage.model_validate_json(line)
                except ValueError as exc:
                    await read_send.send(exc)
                    continue
                if isinstance(message.root, types.JSONRPCRequest):
                    pending.add(message.root.id)
                    drained.clear()
                await read_send.send(SessionMessage(message))

    previous_int = signal.signal(signal.SIGINT, terminate)
    previous_term = signal.signal(signal.SIGTERM, terminate)
    try:
        stdin = StdioInput()
        try:
            async with create_task_group() as group:

                async def serve_requests() -> None:
                    try:
                        with request_scope:
                            await server.run(
                                read, write, server.create_initialization_options()
                            )
                    finally:
                        finished.set()

                group.start_soon(serve_requests)
                group.start_soon(forward_requests, stdin)
                group.start_soon(forward_replies)
                await stopping.wait()
                with move_on_after(SHUTDOWN_GRACE):
                    await drained.wait()
                if pending:
                    request_scope.cancel()
                    try:
                        with move_on_after(SHUTDOWN_REPLY_TIMEOUT):
                            for request_id in tuple(pending):
                                await emit(
                                    types.JSONRPCMessage(
                                        types.JSONRPCError(
                                            jsonrpc="2.0",
                                            id=request_id,
                                            error=types.ErrorData(
                                                code=types.INTERNAL_ERROR,
                                                message=SHUTDOWN_ERROR,
                                            ),
                                        )
                                    )
                                )
                    finally:
                        # Python cannot cancel an HTTP call already running in a
                        # thread. Exit after flushing replies so those workers
                        # cannot delay shutdown or continue after it.
                        if standalone:
                            log.flush(0.2)
                            os._exit(0)
                group.cancel_scope.cancel()
        finally:
            stdin.stop()
    finally:
        output.shutdown(wait=False, cancel_futures=True)
        # The watchdog also covers executor cleanup after the stdio session
        # returns. Escalation signals must not interrupt that bounded cleanup.
        if not (standalone and stopping.is_set()):
            signal.signal(signal.SIGTERM, previous_term)
            signal.signal(signal.SIGINT, previous_int)
