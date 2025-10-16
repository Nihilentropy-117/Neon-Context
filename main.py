import asyncio
import json
import logging
import os
import re
import threading
import time
import webbrowser
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from dotenv import load_dotenv
from mcp import types
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

CONFIG_PATH = Path(__file__).resolve().parent / "servers_config.json"
TOKENS_DIR = Path(__file__).resolve().parent / "tokens"
CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 3030


class Configuration:
    """Manages environment variables for the MCP client."""

    def __init__(self) -> None:
        self.load_env()
        self.api_key = os.getenv("LLM_API_KEY")
        self.model = os.getenv("LLM_MODEL", "anthropic/claude-3.5-sonnet")

    @staticmethod
    def load_env() -> None:
        load_dotenv()

    @property
    def llm_api_key(self) -> str:
        if not self.api_key:
            return "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"
            # raise ValueError("LLM_API_KEY not found in environment variables")
        return self.api_key

    @property
    def llm_model(self) -> str:
        return self.model


@dataclass
class ServerRecord:
    server_id: str
    url: str
    transport: str = "streamable-http"
    name: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "server_id": self.server_id,
            "url": self.url,
            "transport": self.transport,
            "name": self.name,
        }


def derive_server_id(url: str, existing_ids: set[str]) -> str:
    parsed = urlparse(url)
    base = parsed.netloc or parsed.path or "server"
    path_part = parsed.path.strip("/").replace("/", "_")
    if path_part:
        base = f"{base}_{path_part}"
    base = base.lower()
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", base).strip("-") or "server"

    candidate = cleaned
    suffix = 2
    while candidate in existing_ids:
        candidate = f"{cleaned}-{suffix}"
        suffix += 1

    return candidate


class PersistentServerStore:
    """Persists MCP server configuration to disk."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"servers": []}
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except json.JSONDecodeError as exc:
            logging.warning("Failed to parse %s: %s", self.path, exc)
            return {"servers": []}

        servers = data.get("servers")
        if isinstance(servers, list):
            return {"servers": servers}
        return {"servers": []}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as handle:
            json.dump(self._data, handle, indent=2)

    def list_records(self) -> list[ServerRecord]:
        return [ServerRecord(**entry) for entry in self._data["servers"]]

    def upsert(self, record: ServerRecord) -> None:
        for index, entry in enumerate(self._data["servers"]):
            if entry.get("server_id") == record.server_id:
                self._data["servers"][index] = record.as_dict()
                self.save()
                return
        self._data["servers"].append(record.as_dict())
        self.save()

    def remove(self, server_id: str) -> None:
        before = len(self._data["servers"])
        self._data["servers"] = [entry for entry in self._data["servers"] if entry.get("server_id") != server_id]
        if len(self._data["servers"]) != before:
            self.save()


class FileTokenStorage(TokenStorage):
    """Stores OAuth tokens and client info for a single server."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    async def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}

        def _load() -> dict[str, Any]:
            with self.path.open("r", encoding="utf-8") as handle:
                return json.load(handle)

        try:
            return await asyncio.to_thread(_load)
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError as exc:
            logging.warning("Failed to parse token file %s: %s", self.path, exc)
            try:
                self.path.unlink()
            except OSError:
                pass
            return {}

    async def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        def _dump() -> None:
            tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
            with tmp_path.open("w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
            tmp_path.replace(self.path)

        await asyncio.to_thread(_dump)

    async def get_tokens(self) -> OAuthToken | None:
        async with self._lock:
            data = await self._read()
            token_data = data.get("tokens")
            if token_data:
                return OAuthToken.model_validate(token_data)
            return None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        async with self._lock:
            data = await self._read()
            data["tokens"] = tokens.model_dump(mode="json")
            await self._write(data)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        async with self._lock:
            data = await self._read()
            client_info = data.get("client_info")
            if client_info:
                return OAuthClientInformationFull.model_validate(client_info)
            return None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        async with self._lock:
            data = await self._read()
            data["client_info"] = client_info.model_dump(mode="json")
            await self._write(data)

    async def clear(self) -> None:
        async with self._lock:
            if self.path.exists():
                self.path.unlink()


class CallbackHandler(BaseHTTPRequestHandler):
    """Handles OAuth callbacks and stores results in shared state."""

    def __init__(self, request, client_address, server, callback_data):
        self.callback_data = callback_data
        super().__init__(request, client_address, server)

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        parsed = urlparse(self.path)
        query_params = {key: values[0] for key, values in parse_qs(parsed.query).items()}

        if "code" in query_params:
            self.callback_data["authorization_code"] = query_params["code"]
            self.callback_data["state"] = query_params.get("state")
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"""
                <html>
                <body>
                    <h1>Authorization Successful!</h1>
                    <p>You can close this window and return to the terminal.</p>
                    <script>setTimeout(() => window.close(), 2000);</script>
                </body>
                </html>
                """
            )
        elif "error" in query_params:
            self.callback_data["error"] = query_params["error"]
            self.send_response(400)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(
                f"""
                <html>
                <body>
                    <h1>Authorization Failed</h1>
                    <p>Error: {query_params['error']}</p>
                    <p>You can close this window and return to the terminal.</p>
                </body>
                </html>
                """.encode()
            )
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003 - match base signature
        pass


class CallbackServer:
    """Runs a temporary HTTP server to receive OAuth callbacks."""

    def __init__(self, host: str = CALLBACK_HOST, port: int = CALLBACK_PORT) -> None:
        self.host = host
        self.port = port
        self.server: HTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.callback_data: dict[str, Any] = {"authorization_code": None, "state": None, "error": None}

    def _create_handler(self):
        callback_data = self.callback_data

        class DataCallbackHandler(CallbackHandler):
            def __init__(self, request, client_address, server):
                super().__init__(request, client_address, server, callback_data)

        return DataCallbackHandler

    def start(self) -> None:
        handler_class = self._create_handler()
        self.server = HTTPServer((self.host, self.port), handler_class)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        logging.info("Started OAuth callback server on http://%s:%s", self.host, self.port)

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self.thread:
            self.thread.join(timeout=1)
            self.thread = None

    def wait_for_callback(self, timeout: float = 300.0) -> str:
        start_time = time.time()
        while time.time() - start_time < timeout:
            if self.callback_data.get("authorization_code"):
                return self.callback_data["authorization_code"]
            if self.callback_data.get("error"):
                raise RuntimeError(f"OAuth error: {self.callback_data['error']}")
            time.sleep(0.1)
        raise TimeoutError("Timeout waiting for OAuth callback")

    def get_state(self) -> str | None:
        return self.callback_data.get("state")


class Tool:
    def __init__(self, name: str, description: str, input_schema: dict[str, Any], provider: str, title: str | None = None) -> None:
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.provider = provider
        self.title = title

    def format_for_llm(self) -> str:
        args_desc = []
        properties = self.input_schema.get("properties") if isinstance(self.input_schema, dict) else None
        required = set(self.input_schema.get("required", [])) if isinstance(self.input_schema, dict) else set()
        if isinstance(properties, dict):
            for param_name, param_info in properties.items():
                segment = f"- {param_name}: {param_info.get('description', 'No description')}"
                if param_name in required:
                    segment += " (required)"
                args_desc.append(segment)

        args_text = "\n".join(args_desc) if args_desc else "- No arguments"
        title_line = f"User-readable title: {self.title}\n" if self.title else ""

        return (
            f"Tool: {self.name}\n"
            f"Provided by: {self.provider}\n"
            f"{title_line}Description: {self.description}\n"
            f"Arguments:\n{args_text}\n"
        )

    def to_openrouter_spec(self) -> dict[str, Any]:
        schema: dict[str, Any] = {}
        if isinstance(self.input_schema, dict):
            schema = dict(self.input_schema)
        if not schema:
            schema = {"type": "object", "properties": {}}
        schema.setdefault("type", "object")
        if schema.get("type") != "object":
            schema = {"type": "object", "properties": {"_": schema}, "required": []}
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description[:1024] if self.description else "",
                "parameters": schema,
            },
        }


class HostedServer:
    """Manages a hosted MCP server connection over streamable HTTP with optional OAuth."""

    def __init__(self, record: ServerRecord, token_storage: FileTokenStorage) -> None:
        self.record = record
        self.token_storage = token_storage
        self.exit_stack = AsyncExitStack()
        self.session: ClientSession | None = None
        self.initialize_result: types.InitializeResult | None = None
        self.session_id: str | None = None
        self._get_session_id = None

    @property
    def display_name(self) -> str:
        if self.initialize_result and self.initialize_result.serverInfo:
            info = self.initialize_result.serverInfo
            if info.title:
                return info.title
            if info.name:
                return info.name
        if self.record.name:
            return self.record.name
        parsed = urlparse(self.record.url)
        return parsed.netloc or self.record.server_id

    def _base_url(self) -> str:
        url = self.record.url.rstrip("/")
        if url.endswith("/mcp") or url.endswith("/sse"):
            return url.rsplit("/", 1)[0]
        return url

    async def initialize(self) -> None:
        existing_tokens = await self.token_storage.get_tokens()
        if existing_tokens:
            await self._connect_with_oauth()
        else:
            try:
                await self._connect_with_oauth()
            except Exception as oauth_error:
                logging.info(
                    "OAuth flow failed for %s, attempting unauthenticated connection: %s",
                    self.record.url,
                    oauth_error,
                )
                await self.cleanup()
                await self._connect(auth=None)
        self.record.name = self.display_name

    async def _connect(self, auth: OAuthClientProvider | None) -> None:
        self.exit_stack = AsyncExitStack()
        try:
            transport = await self.exit_stack.enter_async_context(
                streamablehttp_client(
                    url=self.record.url,
                    auth=auth,
                    timeout=timedelta(seconds=60),
                )
            )
            read, write, get_session_id = transport
            session = await self.exit_stack.enter_async_context(ClientSession(read, write))
            self.initialize_result = await session.initialize()
            self.session = session
            self._get_session_id = get_session_id
            if get_session_id:
                try:
                    self.session_id = get_session_id()
                except Exception:
                    self.session_id = None
        except asyncio.CancelledError as cancel:
            task = asyncio.current_task()
            if task and hasattr(task, "uncancel"):
                task.uncancel()
            with suppress(Exception):
                await self.exit_stack.aclose()
            raise RuntimeError("Connection attempt cancelled") from cancel
        except Exception:
            with suppress(Exception):
                await self.exit_stack.aclose()
            raise

    async def _connect_with_oauth(self) -> None:
        callback_server = CallbackServer(host=CALLBACK_HOST, port=CALLBACK_PORT)
        callback_server.start()

        async def callback_handler() -> tuple[str, str | None]:
            logging.info("Waiting for OAuth callback...")
            try:
                auth_code = await asyncio.to_thread(callback_server.wait_for_callback)
                return auth_code, callback_server.get_state()
            finally:
                callback_server.stop()

        async def redirect_handler(authorization_url: str) -> None:
            logging.info("Opening browser for authorization: %s", authorization_url)
            await asyncio.to_thread(webbrowser.open, authorization_url)

        metadata = OAuthClientMetadata.model_validate(
            {
                "client_name": "Simple Chatbot",
                "redirect_uris": [f"http://{CALLBACK_HOST}:{CALLBACK_PORT}/callback"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "client_secret_post",
            }
        )

        oauth_provider = OAuthClientProvider(
            server_url=self._base_url(),
            client_metadata=metadata,
            storage=self.token_storage,
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
        )

        try:
            await self._connect(auth=oauth_provider)
        finally:
            callback_server.stop()

    async def list_tools(self) -> list[Tool]:
        if not self.session:
            raise RuntimeError(f"Server {self.record.server_id} not initialized")
        result = await self.session.list_tools()
        tools = []
        for tool in result.tools:
            if hasattr(tool.inputSchema, "model_dump"):
                schema = tool.inputSchema.model_dump()
            elif isinstance(tool.inputSchema, dict):
                schema = tool.inputSchema
            elif tool.inputSchema is None:
                schema = {}
            else:
                schema = json.loads(tool.inputSchema)
            tools.append(
                Tool(
                    name=tool.name,
                    description=tool.description or "No description provided",
                    input_schema=schema,
                    provider=self.display_name,
                    title=tool.title,
                )
            )
        return tools

    async def execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        if not self.session:
            raise RuntimeError(f"Server {self.record.server_id} not initialized")
        return await self.session.call_tool(tool_name, arguments)

    async def cleanup(self) -> None:
        try:
            await self.exit_stack.aclose()
        finally:
            self.session = None
            self.initialize_result = None
            self._get_session_id = None
            self.session_id = None


class ServerManager:
    """Coordinates hosted MCP servers, tokens, and persistence."""

    def __init__(self, store: PersistentServerStore, tokens_dir: Path) -> None:
        self.store = store
        self.tokens_dir = tokens_dir
        self.tokens_dir.mkdir(parents=True, exist_ok=True)
        self.active_servers: dict[str, HostedServer] = {}

    def _token_path(self, server_id: str) -> Path:
        return self.tokens_dir / f"{server_id}.json"

    def records(self) -> list[ServerRecord]:
        return self.store.list_records()

    def connected_servers(self) -> list[HostedServer]:
        return list(self.active_servers.values())

    def _normalize_url(self, url: str) -> str:
        cleaned = url.strip()
        cleaned = cleaned.rstrip(",;")
        if not cleaned:
            raise ValueError("URL cannot be empty")
        parsed = urlparse(cleaned)
        if not parsed.scheme:
            cleaned = "https://" + cleaned
        return cleaned

    async def initialize_all(self) -> None:
        records = self.records()
        for record in records:
            if record.server_id in self.active_servers:
                continue
            try:
                await self._connect_record(record)
            except Exception as exc:
                logging.error("Failed to initialize %s: %s", record.url, exc)

    async def _connect_record(self, record: ServerRecord) -> HostedServer:
        storage = FileTokenStorage(self._token_path(record.server_id))
        server = HostedServer(record, storage)
        await server.initialize()
        record.name = server.display_name
        self.store.upsert(record)
        self.active_servers[record.server_id] = server
        return server

    async def add_server(self, url: str) -> HostedServer:
        normalized_url = self._normalize_url(url)
        existing_urls = {record.url for record in self.records()}
        if normalized_url in existing_urls:
            raise ValueError("Server already configured")

        existing_ids = {record.server_id for record in self.records()}
        server_id = derive_server_id(normalized_url, existing_ids)
        record = ServerRecord(server_id=server_id, url=normalized_url)
        server = await self._connect_record(record)
        logging.info("Added server %s (%s)", server.display_name, record.url)
        return server

    async def remove_server(self, server_id: str) -> None:
        server = self.active_servers.pop(server_id, None)
        if server:
            await server.cleanup()
        self.store.remove(server_id)
        token_path = self._token_path(server_id)
        if token_path.exists():
            token_path.unlink()
        logging.info("Removed server %s", server_id)

    async def refresh_tool_cache(self) -> dict[str, list[Tool]]:
        cache: dict[str, list[Tool]] = {}
        for server in self.connected_servers():
            try:
                cache[server.record.server_id] = await server.list_tools()
            except Exception as exc:
                logging.error("Failed to list tools for %s: %s", server.record.url, exc)
        return cache

    async def shutdown(self) -> None:
        for server in list(self.active_servers.values()):
            try:
                await server.cleanup()
            except Exception as exc:
                logging.warning("Error while shutting down %s: %s", server.record.url, exc)
        self.active_servers.clear()


class LLMClient:
    """Thin wrapper around the OpenRouter chat completions API with tool calling."""

    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model
        self._client = httpx.AsyncClient(
            base_url="https://openrouter.ai/api/v1",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/yourusername/mcp-chatbot",
                "X-Title": "MCP Chatbot",
            },
            timeout=60.0,
        )

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], str | None]:
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
        }
        response = await self._client.post("/chat/completions", json=payload)
        response.raise_for_status()
        data = response.json()
        choice = data["choices"][0]
        return choice["message"], choice.get("finish_reason")

    async def close(self) -> None:
        await self._client.aclose()


class ChatSession:
    def __init__(self, server_manager: ServerManager, llm_client: LLMClient) -> None:
        self.server_manager = server_manager
        self.llm_client = llm_client
        self.tool_cache: dict[str, list[Tool]] = {}
        self.tool_to_server: dict[str, HostedServer] = {}
        self.tool_specs: list[dict[str, Any]] = []
        self.messages: list[dict[str, Any]] = []
        self.system_message: str = ""

    async def refresh_tools(self) -> None:
        self.tool_cache = await self.server_manager.refresh_tool_cache()
        self.tool_to_server.clear()
        all_tools: list[Tool] = []
        for server_id, server_tools in self.tool_cache.items():
            server = self.server_manager.active_servers.get(server_id)
            if not server:
                continue
            for tool in server_tools:
                if tool.name not in self.tool_to_server:
                    self.tool_to_server[tool.name] = server
            all_tools.extend(server_tools)

        self.tool_specs = [tool.to_openrouter_spec() for tool in all_tools]
        self.system_message = self._build_system_prompt(all_tools)
        system_entry = {"role": "system", "content": self.system_message}
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0] = system_entry
        else:
            self.messages.insert(0, system_entry)

    @staticmethod
    def _build_system_prompt(tools: list[Tool]) -> str:
        if tools:
            catalog = "\n".join(tool.format_for_llm() for tool in tools)
        else:
            catalog = "No MCP tools are currently configured. Respond directly without using tools."
        return (
            "You are a helpful assistant. Use the available MCP tools when they will help the user. "
            "Always wait for tool results before giving a final answer.\n\n"
            "Available tools:\n"
            f"{catalog}"
        )

    async def open_mcp_menu(self) -> None:
        while True:
            print("\n--- MCP Server Manager ---")
            records = self.server_manager.records()
            if records:
                for index, record in enumerate(records, start=1):
                    status = "connected" if record.server_id in self.server_manager.active_servers else "disconnected"
                    name = record.name or record.server_id
                    print(f"{index}. {name} ({record.url}) - {status}")
            else:
                print("No servers configured.")

            print("\nOptions:")
            print("  1. Add server")
            print("  2. Remove server")
            print("  3. Refresh tool list")
            print("  4. Back to chat")

            choice = input("mcp> ").strip().lower()
            if choice in {"4", "back", "b"}:
                break
            if choice == "1" or choice == "add":
                url = input("Enter MCP server URL: ").strip()
                if not url:
                    print("No URL provided.")
                    continue
                try:
                    await self.server_manager.add_server(url)
                    await self.refresh_tools()
                    print("Server added successfully.")
                except BaseException as exc:
                    if isinstance(exc, KeyboardInterrupt):
                        raise
                    if isinstance(exc, asyncio.CancelledError):
                        task = asyncio.current_task()
                        if task and hasattr(task, "uncancel"):
                            task.uncancel()
                    print(f"Failed to add server: {exc}")
            elif choice == "2" or choice == "remove":
                if not records:
                    print("No servers to remove.")
                    continue
                selection = input("Enter server number to remove: ").strip()
                if not selection:
                    continue
                try:
                    index = int(selection) - 1
                    if not (0 <= index < len(records)):
                        print("Selection out of range.")
                        continue
                    server_id = records[index].server_id
                    await self.server_manager.remove_server(server_id)
                    await self.refresh_tools()
                    print("Server removed.")
                except ValueError:
                    print("Invalid selection.")
                except Exception as exc:
                    print(f"Failed to remove server: {exc}")
            elif choice == "3" or choice == "refresh":
                await self.refresh_tools()
                print("Tool list refreshed.")
            else:
                print("Unknown option.")

    @staticmethod
    def _format_tool_result(server: HostedServer, tool_name: str, result: types.CallToolResult) -> str:
        lines = []
        if getattr(result, "isError", False):
            lines.append("Tool reported an error:")
        for item in result.content:
            item_type = getattr(item, "type", None)
            if item_type == "text" and hasattr(item, "text"):
                lines.append(item.text)
            elif isinstance(item, dict):
                if item.get("type") == "text" and "text" in item:
                    lines.append(item["text"])
                else:
                    lines.append(json.dumps(item, indent=2))
            else:
                lines.append(str(item))
        if result.structuredContent:
            lines.append(json.dumps(result.structuredContent, indent=2))
        output = "\n".join(line for line in lines if line).strip()
        if not output:
            output = "<no content>"
        return f"Tool `{tool_name}` from `{server.display_name}` returned:\n{output}"

    @staticmethod
    def _message_content_to_text(content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            chunks: list[str] = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    chunks.append(part.get("text", ""))
                else:
                    chunks.append(json.dumps(part))
            return "\n".join(chunk for chunk in chunks if chunk)
        return str(content)

    async def _process_tool_calls(self, tool_calls: list[dict[str, Any]]) -> None:
        for call in tool_calls:
            function_data = call.get("function", {}) or {}
            tool_name = function_data.get("name", "")
            arguments_payload = function_data.get("arguments", "{}")
            try:
                arguments = json.loads(arguments_payload or "{}")
            except json.JSONDecodeError:
                logging.warning("Failed to parse arguments for tool %s: %s", tool_name, arguments_payload)
                arguments = {}

            server = self.tool_to_server.get(tool_name)
            if not server:
                error_text = f"Tool '{tool_name}' is not available."
                payload = {"error": error_text}
                print(f"\n[Tool] {error_text}\n")
                self.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id"),
                        "name": tool_name,
                        "content": json.dumps(payload),
                    }
                )
                continue

            try:
                result = await server.execute_tool(tool_name, arguments)
                payload = result.model_dump()
                display_text = self._format_tool_result(server, tool_name, result)
            except Exception as exc:
                logging.error("Error executing tool %s: %s", tool_name, exc)
                payload = {"error": str(exc)}
                display_text = f"Tool `{tool_name}` failed: {exc}"

            print(f"\n[Tool] {display_text}\n")
            self.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "name": tool_name,
                    "content": json.dumps(payload),
                }
            )

    async def _handle_user_message(self, user_input: str) -> None:
        self.messages.append({"role": "user", "content": user_input})

        while True:
            try:
                message, finish_reason = await self.llm_client.complete(self.messages, self.tool_specs)
            except httpx.HTTPError as exc:
                logging.error("Failed to contact OpenRouter: %s", exc)
                print("\nAssistant: I couldn't reach the language model service. Please try again in a moment.")
                break
            self.messages.append(message)

            tool_calls = message.get("tool_calls") or []
            if tool_calls:
                await self._process_tool_calls(tool_calls)
                continue

            assistant_text = self._message_content_to_text(message.get("content"))
            if assistant_text:
                print(f"\nAssistant: {assistant_text}")
            if finish_reason != "tool_calls":
                break

    async def start(self) -> None:
        try:
            await self.server_manager.initialize_all()
            await self.refresh_tools()

            while True:
                try:
                    user_input = input("You: ").strip()
                except EOFError:
                    logging.info("Input stream closed. Exiting chat.")
                    break
                except KeyboardInterrupt:
                    print("\nExiting...")
                    break

                if not user_input:
                    continue

                if user_input.lower() in {"quit", "exit"}:
                    logging.info("Exiting...")
                    break

                if user_input.lower() == "/mcp":
                    await self.open_mcp_menu()
                    continue

                await self._handle_user_message(user_input)
        finally:
            await self.llm_client.close()
            await self.server_manager.shutdown()


async def main() -> None:
    config = Configuration()
    store = PersistentServerStore(CONFIG_PATH)
    manager = ServerManager(store, TOKENS_DIR)
    llm_client = LLMClient(config.llm_api_key, config.llm_model)
    chat_session = ChatSession(manager, llm_client)
    await chat_session.start()


if __name__ == "__main__":
    asyncio.run(main())

