"""Bounded JSON-RPC transport for the pinned standalone app-server."""

import asyncio
import contextlib
import json
import os
import signal

from .runtime import CodexError, validate_runtime


class CodexConnection:
    def __init__(self, on_request, on_notification, *, request_timeout=60):
        self.on_request = on_request
        self.on_notification = on_notification
        self.request_timeout = request_timeout
        self.pending = {}
        self.counter = 0
        self.process = None
        self.reader = None
        self.handlers = set()
        self.write_lock = asyncio.Lock()
        self.closed = asyncio.get_running_loop().create_future()

    async def start(self, binary, *, cwd, env):
        await asyncio.to_thread(validate_runtime, binary)
        try:
            self.process = await asyncio.create_subprocess_exec(
                binary,
                "--listen",
                "stdio://",
                cwd=cwd,
                env=env,
                start_new_session=os.name == "posix",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=2 * 1024 * 1024,
            )
            self.reader = asyncio.create_task(self.read())
            await self.request(
                "initialize",
                {
                    "clientInfo": {"name": "codex_exec", "version": "0.1.0"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            await self.send({"method": "initialized"})
        except BaseException:
            await self.close()
            raise

    async def send(self, payload):
        if self.closed.done():
            raise self.closed.result()
        try:
            async with self.write_lock:
                self.process.stdin.write((json.dumps(payload) + "\n").encode())
                await self.process.stdin.drain()
        except (BrokenPipeError, ConnectionError) as error:
            raise CodexError("Codex app-server closed its input stream.") from error

    async def request(self, method, params):
        self.counter += 1
        request_id = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            async with asyncio.timeout(self.request_timeout):
                await self.send({"id": request_id, "method": method, "params": params})
                return await future
        except TimeoutError as error:
            raise CodexError(f"Codex request timed out: {method}") from error
        finally:
            self.pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def wait(self, future, timeout):
        completed, _ = await asyncio.wait(
            [future, self.closed],
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if future in completed:
            return future.result()
        if self.closed in completed:
            raise self.closed.result()
        raise CodexError("Codex turn timed out.")

    def fail(self, error):
        if not self.closed.done():
            self.closed.set_result(error)
        for future in self.pending.values():
            if not future.done():
                future.set_exception(error)

    async def respond(self, message):
        try:
            result = await self.on_request(
                message["method"], message.get("params") or {}
            )
            reply = {"id": message["id"], "result": result}
            await self.send(reply)
        except CodexError:
            # Never grant an unexpected shell, network, or approval request.
            with contextlib.suppress(CodexError):
                await self.send(
                    {
                        "id": message["id"],
                        "error": {
                            "code": -32601,
                            "message": "Only Dograh dynamic tools are allowed",
                        },
                    }
                )
        except Exception as error:  # noqa: BLE001 - contain user tool failures at the protocol boundary
            self.fail(
                CodexError(f"Codex request handler failed ({type(error).__name__}).")
            )

    async def read(self):
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise CodexError("Codex sent a malformed protocol message.")
                if message.get("method") and "id" in message:
                    # A slow tool must not block unrelated replies or cancellation.
                    handler = asyncio.create_task(self.respond(message))
                    self.handlers.add(handler)
                    handler.add_done_callback(self.handlers.discard)
                elif "id" in message:
                    future = self.pending.get(message["id"])
                    if future and not future.done():
                        if "error" in message:
                            future.set_exception(
                                CodexError(
                                    "Codex protocol error: "
                                    + str(message["error"].get("message", ""))[:1000]
                                )
                            )
                        else:
                            future.set_result(message.get("result"))
                elif message.get("method"):
                    await self.on_notification(
                        message["method"], message.get("params") or {}
                    )
            self.fail(
                CodexError("Codex app-server exited before the connection was closed.")
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - fail all waiters when the transport breaks
            self.fail(
                CodexError(f"Codex protocol stream failed ({type(error).__name__}).")
            )

    async def close(self):
        handlers = list(self.handlers)
        for task in handlers:
            task.cancel()
        if self.reader:
            self.reader.cancel()
        await asyncio.gather(
            *handlers, *([self.reader] if self.reader else []), return_exceptions=True
        )
        if self.process:
            with contextlib.suppress(ProcessLookupError):
                if os.name == "posix":
                    os.killpg(self.process.pid, signal.SIGTERM)
                elif self.process.returncode is None:
                    self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    if os.name == "posix":
                        os.killpg(self.process.pid, signal.SIGKILL)
                    else:
                        self.process.kill()
                await self.process.wait()
            if os.name == "posix":
                # The parent may exit before helpers that ignored SIGTERM.
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
        self.fail(CodexError("Codex connection is closed."))
