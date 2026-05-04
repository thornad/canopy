"""Minimal stdio MCP client for Canopy.

Launches MCP servers as subprocesses and speaks newline-delimited JSON-RPC 2.0
over stdin/stdout. Each ``MCPClient`` manages one server; ``MCPRegistry`` is a
module-level singleton that multiplexes across all configured servers and
exposes a combined tool catalogue to the chat loop.

Tool names are namespaced as ``{server}__{tool}`` on the wire so multiple
servers can expose identically-named tools without colliding.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
from typing import Any, Optional

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "2024-11-05"
NAMESPACE_SEP = "__"
# tools/call can be slow (vector search, fresh uv venv, first-run model load).
# Match the oMLX chat proxy's 300s ceiling so long-running tools don't fail
# faster in Canopy than in other MCP clients.
REQUEST_TIMEOUT = 300.0
INIT_TIMEOUT = 60.0


class MCPError(Exception):
    pass


class MCPClient:
    """One stdio MCP server subprocess."""

    def __init__(
        self,
        name: str,
        command: str,
        args: Optional[list[str]] = None,
        env: Optional[dict[str, str]] = None,
    ):
        self.name = name
        self.command = command
        self.args = args or []
        self.env = env or {}
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._tools: list[dict] = []
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self) -> None:
        if self.running:
            return
        merged_env = {**os.environ, **self.env}
        self._proc = await asyncio.create_subprocess_exec(
            self.command,
            *self.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged_env,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        try:
            await asyncio.wait_for(self._initialize(), timeout=INIT_TIMEOUT)
            await self._refresh_tools()
        except Exception:
            await self.stop()
            raise

    async def stop(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.returncode is None:
                try:
                    self._proc.stdin.close()  # type: ignore[union-attr]
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    self._proc.terminate()
                    try:
                        await asyncio.wait_for(self._proc.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        self._proc.kill()
        finally:
            if self._reader_task:
                self._reader_task.cancel()
            if self._stderr_task:
                self._stderr_task.cancel()
            self._proc = None
            self._reader_task = None
            self._stderr_task = None
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(MCPError(f"{self.name}: server stopped"))
            self._pending.clear()

    async def _read_loop(self) -> None:
        """Read chunks and split on newlines ourselves.

        Avoids asyncio.StreamReader.readline()'s default 64 KB per-line limit,
        which breaks MCP servers that return large tool results (RAG search,
        file dumps). The MCP framing is line-delimited JSON, so we just need
        to accumulate bytes and flush complete lines.
        """
        assert self._proc and self._proc.stdout
        buf = bytearray()
        try:
            while True:
                chunk = await self._proc.stdout.read(65536)
                if not chunk:
                    break
                buf.extend(chunk)
                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        break
                    line = bytes(buf[:nl])
                    del buf[: nl + 1]
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line.decode("utf-8"))
                    except json.JSONDecodeError:
                        log.debug("%s: non-JSON line: %r", self.name, line[:200])
                        continue
                    msg_id = msg.get("id")
                    if msg_id is not None and msg_id in self._pending:
                        fut = self._pending.pop(msg_id)
                        if not fut.done():
                            fut.set_result(msg)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("%s: reader crashed: %s", self.name, e)
            # Fail any in-flight requests so callers don't hang forever.
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(MCPError(f"{self.name}: reader crashed: {e}"))
            self._pending.clear()

    async def _drain_stderr(self) -> None:
        """Drain the child's stderr to debug log.

        Without this, a chatty MCP server (npm warnings, progress logs) will
        eventually fill the stderr pipe buffer (~16-64 KB on macOS) and block
        on its next write — including the JSON-RPC response on stdout — so
        tool calls hang until REQUEST_TIMEOUT with no output.
        """
        assert self._proc and self._proc.stderr
        buf = bytearray()
        try:
            while True:
                chunk = await self._proc.stderr.read(65536)
                if not chunk:
                    break
                buf.extend(chunk)
                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        break
                    line = bytes(buf[:nl])
                    del buf[: nl + 1]
                    if line.strip():
                        log.debug("%s [stderr]: %s", self.name,
                                  line.decode("utf-8", errors="replace").rstrip())
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug("%s: stderr drain ended: %s", self.name, e)

    async def _send(self, method: str, params: Optional[dict] = None) -> Any:
        if not self.running:
            raise MCPError(f"{self.name}: not running")
        req_id = self._next_id
        self._next_id += 1
        msg = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            msg["params"] = params
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut
        data = (json.dumps(msg) + "\n").encode("utf-8")
        assert self._proc and self._proc.stdin
        self._proc.stdin.write(data)
        await self._proc.stdin.drain()
        try:
            response = await asyncio.wait_for(fut, timeout=REQUEST_TIMEOUT)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise MCPError(f"{self.name}: {method} timed out")
        if "error" in response:
            err = response["error"]
            raise MCPError(f"{self.name}: {err.get('message', err)}")
        return response.get("result")

    async def _notify(self, method: str, params: Optional[dict] = None) -> None:
        if not self.running:
            return
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        data = (json.dumps(msg) + "\n").encode("utf-8")
        assert self._proc and self._proc.stdin
        self._proc.stdin.write(data)
        await self._proc.stdin.drain()

    async def _initialize(self) -> None:
        await self._send(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "canopy", "version": "0.1.0"},
            },
        )
        await self._notify("notifications/initialized")

    async def _refresh_tools(self) -> None:
        result = await self._send("tools/list")
        self._tools = result.get("tools", []) if result else []

    @property
    def tools(self) -> list[dict]:
        return self._tools

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        async with self._lock:
            result = await self._send(
                "tools/call",
                {"name": tool_name, "arguments": arguments},
            )
        if not result:
            return ""
        # MCP returns {content: [{type: "text", text: "..."}], isError?: bool}
        parts = []
        for item in result.get("content", []):
            if item.get("type") == "text":
                parts.append(item.get("text", ""))
            else:
                parts.append(json.dumps(item))
        text = "\n".join(parts)
        if result.get("isError"):
            text = f"[error] {text}"
        return text


class MCPRegistry:
    """Manages all enabled MCP servers and exposes a unified tool list.

    Clients are started lazily on first use and kept alive for the server
    lifetime — restart is only triggered by explicit config changes so that
    chat requests don't pay subprocess startup cost per turn.
    """

    def __init__(self):
        self._clients: dict[str, MCPClient] = {}
        self._lock = asyncio.Lock()

    async def sync(self, configs: list[dict]) -> dict[str, str]:
        """Start/stop clients to match ``configs``. Returns per-server status."""
        status: dict[str, str] = {}
        wanted = {c["name"]: c for c in configs if c.get("enabled")}
        async with self._lock:
            # Stop removed/disabled
            for name in list(self._clients):
                if name not in wanted:
                    await self._clients[name].stop()
                    del self._clients[name]
            # Start new/changed
            for name, cfg in wanted.items():
                existing = self._clients.get(name)
                if existing and self._same_config(existing, cfg):
                    status[name] = "running" if existing.running else "stopped"
                    continue
                if existing:
                    await existing.stop()
                client = MCPClient(
                    name=name,
                    command=cfg["command"],
                    args=cfg.get("args") or [],
                    env=cfg.get("env") or {},
                )
                try:
                    await client.start()
                    self._clients[name] = client
                    status[name] = "running"
                except Exception as e:
                    status[name] = f"error: {e}"
                    log.warning("MCP %s failed to start: %s", name, e)
        return status

    @staticmethod
    def _same_config(client: MCPClient, cfg: dict) -> bool:
        return (
            client.command == cfg["command"]
            and client.args == (cfg.get("args") or [])
            and client.env == (cfg.get("env") or {})
        )

    async def stop_all(self) -> None:
        async with self._lock:
            for client in self._clients.values():
                await client.stop()
            self._clients.clear()

    def tool_specs(self) -> list[dict]:
        """Return OpenAI-compatible tool specs for all running servers."""
        specs = []
        for client in self._clients.values():
            if not client.running:
                continue
            for tool in client.tools:
                specs.append(
                    {
                        "type": "function",
                        "function": {
                            "name": f"{client.name}{NAMESPACE_SEP}{tool['name']}",
                            "description": tool.get("description", ""),
                            "parameters": tool.get("inputSchema") or {"type": "object", "properties": {}},
                        },
                    }
                )
        return specs

    async def call(self, namespaced_name: str, arguments: dict) -> str:
        if NAMESPACE_SEP not in namespaced_name:
            raise MCPError(f"invalid tool name: {namespaced_name}")
        server, _, tool = namespaced_name.partition(NAMESPACE_SEP)
        client = self._clients.get(server)
        if client is None or not client.running:
            raise MCPError(f"server '{server}' not running")
        return await client.call_tool(tool, arguments)

    def server_status(self) -> dict[str, dict]:
        return {
            name: {
                "running": c.running,
                "tool_count": len(c.tools),
                "tools": [t["name"] for t in c.tools],
            }
            for name, c in self._clients.items()
        }


registry = MCPRegistry()


def parse_args_string(s: str) -> list[str]:
    """Parse a shell-style args string. Empty → []."""
    s = (s or "").strip()
    if not s:
        return []
    return shlex.split(s)


def parse_env_string(s: str) -> dict[str, str]:
    """Parse KEY=VALUE lines into a dict. Empty → {}."""
    out: dict[str, str] = {}
    for line in (s or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out
