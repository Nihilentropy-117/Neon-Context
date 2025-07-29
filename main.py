#!/usr/bin/env python3
"""
Simple MCP client example with OAuth authentication support.

This client connects to an MCP server using streamable HTTP transport with OAuth.

"""

import asyncio
import os
import threading
import time
import webbrowser
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken


class InMemoryTokenStorage(TokenStorage):
    """Simple in-memory token storage implementation."""

    def __init__(self):
        self._tokens: OAuthToken | None = None
        self._client_info: OAuthClientInformationFull | None = None

    async def get_tokens(self) -> OAuthToken | None:
        return self._tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._tokens = tokens

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self._client_info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self._client_info = client_info


class CallbackHandler(BaseHTTPRequestHandler):
    """Simple HTTP handler to capture OAuth callback."""

    def __init__(self, request, client_address, server, callback_data):
        """Initialize with callback data storage."""
        self.callback_data = callback_data
        super().__init__(request, client_address, server)

    def do_GET(self):
        """Handle GET request from OAuth redirect."""
        parsed = urlparse(self.path)
        query_params = parse_qs(parsed.query)

        if "code" in query_params:
            self.callback_data["authorization_code"] = query_params["code"][0]
            self.callback_data["state"] = query_params.get("state", [None])[0]
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"""
            <html>
            <body>
                <h1>Authorization Successful!</h1>
                <p>You can close this window and return to the terminal.</p>
                <script>setTimeout(() => window.close(), 2000);</script>
            </body>
            </html>
            """)
        elif "error" in query_params:
            self.callback_data["error"] = query_params["error"][0]
            self.send_response(400)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(
                f"""
            <html>
            <body>
                <h1>Authorization Failed</h1>
                <p>Error: {query_params["error"][0]}</p>
                <p>You can close this window and return to the terminal.</p>
            </body>
            </html>
            """.encode()
            )
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        """Suppress default logging."""
        pass


class CallbackServer:
    """Simple server to handle OAuth callbacks."""

    def __init__(self, port=3000):
        self.port = port
        self.server = None
        self.thread = None
        self.callback_data = {"authorization_code": None, "state": None, "error": None}

    def _create_handler_with_data(self):
        """Create a handler class with access to callback data."""
        callback_data = self.callback_data

        class DataCallbackHandler(CallbackHandler):
            def __init__(self, request, client_address, server):
                super().__init__(request, client_address, server, callback_data)

        return DataCallbackHandler

    def start(self):
        """Start the callback server in a background thread."""
        handler_class = self._create_handler_with_data()
        self.server = HTTPServer(("localhost", self.port), handler_class)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        print(f"🖥️  Started callback server on http://localhost:{self.port}")

    def stop(self):
        """Stop the callback server."""
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self.thread:
            self.thread.join(timeout=1)

    def wait_for_callback(self, timeout=300):
        """Wait for OAuth callback with timeout."""
        start_time = time.time()
        while time.time() - start_time < timeout:
            if self.callback_data["authorization_code"]:
                return self.callback_data["authorization_code"]
            elif self.callback_data["error"]:
                raise Exception(f"OAuth error: {self.callback_data['error']}")
            time.sleep(0.1)
        raise Exception("Timeout waiting for OAuth callback")

    def get_state(self):
        """Get the received state parameter."""
        return self.callback_data["state"]


class SimpleAuthClient:
    """Simple MCP client with auth support."""

    def __init__(self, server_url: str, transport_type: str = "streamable-http"):
        self.server_url = server_url
        self.transport_type = transport_type
        self.session: ClientSession | None = None

    async def connect(self):
        """Connect to the MCP server."""
        print(f"🔗 Attempting to connect to {self.server_url}...")

        try:
            callback_server = CallbackServer(port=3030)
            callback_server.start()

            async def callback_handler() -> tuple[str, str | None]:
                """Wait for OAuth callback and return auth code and state."""
                print("⏳ Waiting for authorization callback...")
                try:
                    auth_code = callback_server.wait_for_callback(timeout=300)
                    return auth_code, callback_server.get_state()
                finally:
                    callback_server.stop()

            client_metadata_dict = {
                "client_name": "Simple Auth Client",
                "redirect_uris": ["http://localhost:3030/callback"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "client_secret_post",
            }

            async def _default_redirect_handler(authorization_url: str) -> None:
                """Default redirect handler that opens the URL in a browser."""
                print(f"Opening browser for authorization: {authorization_url}")
                webbrowser.open(authorization_url)

            # Create OAuth authentication handler using the new interface
            oauth_auth = OAuthClientProvider(
                server_url=self.server_url.replace("/mcp", ""),
                client_metadata=OAuthClientMetadata.model_validate(client_metadata_dict),
                storage=InMemoryTokenStorage(),
                redirect_handler=_default_redirect_handler,
                callback_handler=callback_handler,
            )

            # Create transport with auth handler based on transport type
            if self.transport_type == "sse":
                print("📡 Opening SSE transport connection with auth...")
                async with sse_client(
                    url=self.server_url,
                    auth=oauth_auth,
                    timeout=60,
                ) as (read_stream, write_stream):
                    await self._run_session(read_stream, write_stream, None)
            else:
                print("📡 Opening StreamableHTTP transport connection with auth...")
                async with streamablehttp_client(
                    url=self.server_url,
                    auth=oauth_auth,
                    timeout=timedelta(seconds=60),
                ) as (read_stream, write_stream, get_session_id):
                    await self._run_session(read_stream, write_stream, get_session_id)

        except Exception as e:
            print(f"❌ Failed to connect: {e}")
            import traceback

            traceback.print_exc()

    async def _run_session(self, read_stream, write_stream, get_session_id):
        """Run the MCP session with the given streams."""
        print("🤝 Initializing MCP session...")
        async with ClientSession(read_stream, write_stream) as session:
            self.session = session
            print("⚡ Starting session initialization...")
            await session.initialize()
            print("✨ Session initialization complete!")

            print(f"\n✅ Connected to MCP server at {self.server_url}")
            if get_session_id:
                session_id = get_session_id()
                if session_id:
                    print(f"Session ID: {session_id}")

            # Run interactive loop
            await self.interactive_loop()

    async def list_tools(self):
        """List available tools from the server."""
        if not self.session:
            print("❌ Not connected to server")
            return

        try:
            result = await self.session.list_tools()
            if hasattr(result, "tools") and result.tools:
                print("\n📋 Available tools:")
                for i, tool in enumerate(result.tools, 1):
                    print(f"{i}. {tool.name}")
                    if tool.description:
                        print(f"   Description: {tool.description}")
                    print()
            else:
                print("No tools available")
        except Exception as e:
            print(f"❌ Failed to list tools: {e}")

    async def call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None):
        """Call a specific tool."""
        if not self.session:
            print("❌ Not connected to server")
            return

        try:
            result = await self.session.call_tool(tool_name, arguments or {})
            print(f"\n🔧 Tool '{tool_name}' result:")
            if hasattr(result, "content"):
                for content in result.content:
                    if content.type == "text":
                        print(content.text)
                    else:
                        print(content)
            else:
                print(result)
        except Exception as e:
            print(f"❌ Failed to call tool '{tool_name}': {e}")

    async def interactive_loop(self):
        """Run interactive command loop."""
        print("\n🎯 Interactive MCP Client")
        print("Commands:")
        print("  list - List available tools")
        print("  call <tool_name> [args] - Call a tool")
        print("  quit - Exit the client")
        print()

        while True:
            try:
                command = input("mcp> ").strip()

                if not command:
                    continue

                if command == "quit":
                    break

                elif command == "list":
                    await self.list_tools()

                elif command.startswith("call "):
                    parts = command.split(maxsplit=2)
                    tool_name = parts[1] if len(parts) > 1 else ""

                    if not tool_name:
                        print("❌ Please specify a tool name")
                        continue

                    # Parse arguments (simple JSON-like format)
                    arguments = {}
                    if len(parts) > 2:
                        import json

                        try:
                            arguments = json.loads(parts[2])
                        except json.JSONDecodeError:
                            print("❌ Invalid arguments format (expected JSON)")
                            continue

                    await self.call_tool(tool_name, arguments)

                else:
                    print("❌ Unknown command. Try 'list', 'call <tool_name>', or 'quit'")

            except KeyboardInterrupt:
                print("\n\n👋 Goodbye!")
                break
            except EOFError:
                break


async def main():
    """Main entry point."""
    # Default server URL - can be overridden with environment variable
    # Most MCP streamable HTTP servers use /mcp as the endpoint
    server_url = os.getenv("MCP_SERVER_PORT", 8000)
    transport_type = os.getenv("MCP_TRANSPORT_TYPE", "streamable-http")
    server_url = (
        f"http://localhost:{server_url}/mcp"
        if transport_type == "streamable-http"
        else f"http://localhost:{server_url}/sse"
    )

    print("🚀 Simple MCP Auth Client")
    print(f"Connecting to: {server_url}")
    print(f"Transport type: {transport_type}")

    # Start connection flow - OAuth will be handled automatically
    client = SimpleAuthClient(server_url, transport_type)
    await client.connect()


def cli():
    """CLI entry point for uv script."""
    asyncio.run(main())


if __name__ == "__main__":
    cli()
import asyncio
import json
import logging
import os
import shutil
from contextlib import AsyncExitStack
from typing import Any

import httpx
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


class Configuration:
    """Manages configuration and environment variables for the MCP client."""

    def __init__(self) -> None:
        """Initialize configuration with environment variables."""
        self.load_env()
        self.api_key = os.getenv("LLM_API_KEY")

    @staticmethod
    def load_env() -> None:
        """Load environment variables from .env file."""
        load_dotenv()

    @staticmethod
    def load_config(file_path: str) -> dict[str, Any]:
        """Load server configuration from JSON file.

        Args:
            file_path: Path to the JSON configuration file.

        Returns:
            Dict containing server configuration.

        Raises:
            FileNotFoundError: If configuration file doesn't exist.
            JSONDecodeError: If configuration file is invalid JSON.
        """
        with open(file_path, "r") as f:
            return json.load(f)

    @property
    def llm_api_key(self) -> str:
        """Get the LLM API key.

        Returns:
            The API key as a string.

        Raises:
            ValueError: If the API key is not found in environment variables.
        """
        if not self.api_key:
            raise ValueError("LLM_API_KEY not found in environment variables")
        return self.api_key


class Server:
    """Manages MCP server connections and tool execution."""

    def __init__(self, name: str, config: dict[str, Any]) -> None:
        self.name: str = name
        self.config: dict[str, Any] = config
        self.stdio_context: Any | None = None
        self.session: ClientSession | None = None
        self._cleanup_lock: asyncio.Lock = asyncio.Lock()
        self.exit_stack: AsyncExitStack = AsyncExitStack()

    async def initialize(self) -> None:
        """Initialize the server connection."""
        command = shutil.which("npx") if self.config["command"] == "npx" else self.config["command"]
        if command is None:
            raise ValueError("The command must be a valid string and cannot be None.")

        server_params = StdioServerParameters(
            command=command,
            args=self.config["args"],
            env={**os.environ, **self.config["env"]} if self.config.get("env") else None,
        )
        try:
            stdio_transport = await self.exit_stack.enter_async_context(stdio_client(server_params))
            read, write = stdio_transport
            session = await self.exit_stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self.session = session
        except Exception as e:
            logging.error(f"Error initializing server {self.name}: {e}")
            await self.cleanup()
            raise

    async def list_tools(self) -> list[Any]:
        """List available tools from the server.

        Returns:
            A list of available tools.

        Raises:
            RuntimeError: If the server is not initialized.
        """
        if not self.session:
            raise RuntimeError(f"Server {self.name} not initialized")

        tools_response = await self.session.list_tools()
        tools = []

        for item in tools_response:
            if isinstance(item, tuple) and item[0] == "tools":
                tools.extend(Tool(tool.name, tool.description, tool.inputSchema, tool.title) for tool in item[1])

        return tools

    async def execute_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        retries: int = 2,
        delay: float = 1.0,
    ) -> Any:
        """Execute a tool with retry mechanism.

        Args:
            tool_name: Name of the tool to execute.
            arguments: Tool arguments.
            retries: Number of retry attempts.
            delay: Delay between retries in seconds.

        Returns:
            Tool execution result.

        Raises:
            RuntimeError: If server is not initialized.
            Exception: If tool execution fails after all retries.
        """
        if not self.session:
            raise RuntimeError(f"Server {self.name} not initialized")

        attempt = 0
        while attempt < retries:
            try:
                logging.info(f"Executing {tool_name}...")
                result = await self.session.call_tool(tool_name, arguments)

                return result

            except Exception as e:
                attempt += 1
                logging.warning(f"Error executing tool: {e}. Attempt {attempt} of {retries}.")
                if attempt < retries:
                    logging.info(f"Retrying in {delay} seconds...")
                    await asyncio.sleep(delay)
                else:
                    logging.error("Max retries reached. Failing.")
                    raise

    async def cleanup(self) -> None:
        """Clean up server resources."""
        async with self._cleanup_lock:
            try:
                await self.exit_stack.aclose()
                self.session = None
                self.stdio_context = None
            except Exception as e:
                logging.error(f"Error during cleanup of server {self.name}: {e}")


class Tool:
    """Represents a tool with its properties and formatting."""

    def __init__(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        title: str | None = None,
    ) -> None:
        self.name: str = name
        self.title: str | None = title
        self.description: str = description
        self.input_schema: dict[str, Any] = input_schema

    def format_for_llm(self) -> str:
        """Format tool information for LLM.

        Returns:
            A formatted string describing the tool.
        """
        args_desc = []
        if "properties" in self.input_schema:
            for param_name, param_info in self.input_schema["properties"].items():
                arg_desc = f"- {param_name}: {param_info.get('description', 'No description')}"
                if param_name in self.input_schema.get("required", []):
                    arg_desc += " (required)"
                args_desc.append(arg_desc)

        # Build the formatted output with title as a separate field
        output = f"Tool: {self.name}\n"

        # Add human-readable title if available
        if self.title:
            output += f"User-readable title: {self.title}\n"

        output += f"""Description: {self.description}
Arguments:
{chr(10).join(args_desc)}
"""

        return output


class LLMClient:
    """Manages communication with the LLM provider."""

    def __init__(self, api_key: str) -> None:
        self.api_key: str = api_key

    def get_response(self, messages: list[dict[str, str]]) -> str:
        """Get a response from the LLM.

        Args:
            messages: A list of message dictionaries.

        Returns:
            The LLM's response as a string.

        Raises:
            httpx.RequestError: If the request to the LLM fails.
        """
        url = "https://api.groq.com/openai/v1/chat/completions"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        payload = {
            "messages": messages,
            "model": "meta-llama/llama-4-scout-17b-16e-instruct",
            "temperature": 0.7,
            "max_tokens": 4096,
            "top_p": 1,
            "stream": False,
            "stop": None,
        }

        try:
            with httpx.Client() as client:
                response = client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                data = response.json()
                return data["choices"][0]["message"]["content"]

        except httpx.RequestError as e:
            error_message = f"Error getting LLM response: {str(e)}"
            logging.error(error_message)

            if isinstance(e, httpx.HTTPStatusError):
                status_code = e.response.status_code
                logging.error(f"Status code: {status_code}")
                logging.error(f"Response details: {e.response.text}")

            return f"I encountered an error: {error_message}. Please try again or rephrase your request."


class ChatSession:
    """Orchestrates the interaction between user, LLM, and tools."""

    def __init__(self, servers: list[Server], llm_client: LLMClient) -> None:
        self.servers: list[Server] = servers
        self.llm_client: LLMClient = llm_client

    async def cleanup_servers(self) -> None:
        """Clean up all servers properly."""
        for server in reversed(self.servers):
            try:
                await server.cleanup()
            except Exception as e:
                logging.warning(f"Warning during final cleanup: {e}")

    async def process_llm_response(self, llm_response: str) -> str:
        """Process the LLM response and execute tools if needed.

        Args:
            llm_response: The response from the LLM.

        Returns:
            The result of tool execution or the original response.
        """
        import json

        def _clean_json_string(json_string: str) -> str:
            """Remove ```json ... ``` or ``` ... ``` wrappers if the LLM response is fenced."""
            import re

            pattern = r"^```(?:\s*json)?\s*(.*?)\s*```$"
            return re.sub(pattern, r"\1", json_string, flags=re.DOTALL | re.IGNORECASE).strip()

        try:
            tool_call = json.loads(_clean_json_string(llm_response))
            if "tool" in tool_call and "arguments" in tool_call:
                logging.info(f"Executing tool: {tool_call['tool']}")
                logging.info(f"With arguments: {tool_call['arguments']}")

                for server in self.servers:
                    tools = await server.list_tools()
                    if any(tool.name == tool_call["tool"] for tool in tools):
                        try:
                            result = await server.execute_tool(tool_call["tool"], tool_call["arguments"])

                            if isinstance(result, dict) and "progress" in result:
                                progress = result["progress"]
                                total = result["total"]
                                percentage = (progress / total) * 100
                                logging.info(f"Progress: {progress}/{total} ({percentage:.1f}%)")

                            return f"Tool execution result: {result}"
                        except Exception as e:
                            error_msg = f"Error executing tool: {str(e)}"
                            logging.error(error_msg)
                            return error_msg

                return f"No server found with tool: {tool_call['tool']}"
            return llm_response
        except json.JSONDecodeError:
            return llm_response

    async def start(self) -> None:
        """Main chat session handler."""
        try:
            for server in self.servers:
                try:
                    await server.initialize()
                except Exception as e:
                    logging.error(f"Failed to initialize server: {e}")
                    await self.cleanup_servers()
                    return

            all_tools = []
            for server in self.servers:
                tools = await server.list_tools()
                all_tools.extend(tools)

            tools_description = "\n".join([tool.format_for_llm() for tool in all_tools])

            system_message = (
                "You are a helpful assistant with access to these tools:\n\n"
                f"{tools_description}\n"
                "Choose the appropriate tool based on the user's question. "
                "If no tool is needed, reply directly.\n\n"
                "IMPORTANT: When you need to use a tool, you must ONLY respond with "
                "the exact JSON object format below, nothing else:\n"
                "{\n"
                '    "tool": "tool-name",\n'
                '    "arguments": {\n'
                '        "argument-name": "value"\n'
                "    }\n"
                "}\n\n"
                "After receiving a tool's response:\n"
                "1. Transform the raw data into a natural, conversational response\n"
                "2. Keep responses concise but informative\n"
                "3. Focus on the most relevant information\n"
                "4. Use appropriate context from the user's question\n"
                "5. Avoid simply repeating the raw data\n\n"
                "Please use only the tools that are explicitly defined above."
            )

            messages = [{"role": "system", "content": system_message}]

            while True:
                try:
                    user_input = input("You: ").strip().lower()
                    if user_input in ["quit", "exit"]:
                        logging.info("\nExiting...")
                        break

                    messages.append({"role": "user", "content": user_input})

                    llm_response = self.llm_client.get_response(messages)
                    logging.info("\nAssistant: %s", llm_response)

                    result = await self.process_llm_response(llm_response)

                    if result != llm_response:
                        messages.append({"role": "assistant", "content": llm_response})
                        messages.append({"role": "system", "content": result})

                        final_response = self.llm_client.get_response(messages)
                        logging.info("\nFinal response: %s", final_response)
                        messages.append({"role": "assistant", "content": final_response})
                    else:
                        messages.append({"role": "assistant", "content": llm_response})

                except KeyboardInterrupt:
                    logging.info("\nExiting...")
                    break

        finally:
            await self.cleanup_servers()


async def main() -> None:
    """Initialize and run the chat session."""
    config = Configuration()
    server_config = config.load_config("servers_config.json")
    servers = [Server(name, srv_config) for name, srv_config in server_config["mcpServers"].items()]
    llm_client = LLMClient(config.llm_api_key)
    chat_session = ChatSession(servers, llm_client)
    await chat_session.start()


if __name__ == "__main__":
    asyncio.run(main())
#!/usr/bin/env python3
"""
Simple MCP client example with OAuth authentication support.

This client connects to an MCP server using streamable HTTP transport with OAuth.

"""

import asyncio
import os
import threading
import time
import webbrowser
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken


class InMemoryTokenStorage(TokenStorage):
    """Simple in-memory token storage implementation."""

    def __init__(self):
        self._tokens: OAuthToken | None = None
        self._client_info: OAuthClientInformationFull | None = None

    async def get_tokens(self) -> OAuthToken | None:
        return self._tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._tokens = tokens

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self._client_info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self._client_info = client_info


class CallbackHandler(BaseHTTPRequestHandler):
    """Simple HTTP handler to capture OAuth callback."""

    def __init__(self, request, client_address, server, callback_data):
        """Initialize with callback data storage."""
        self.callback_data = callback_data
        super().__init__(request, client_address, server)

    def do_GET(self):
        """Handle GET request from OAuth redirect."""
        parsed = urlparse(self.path)
        query_params = parse_qs(parsed.query)

        if "code" in query_params:
            self.callback_data["authorization_code"] = query_params["code"][0]
            self.callback_data["state"] = query_params.get("state", [None])[0]
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"""
            <html>
            <body>
                <h1>Authorization Successful!</h1>
                <p>You can close this window and return to the terminal.</p>
                <script>setTimeout(() => window.close(), 2000);</script>
            </body>
            </html>
            """)
        elif "error" in query_params:
            self.callback_data["error"] = query_params["error"][0]
            self.send_response(400)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(
                f"""
            <html>
            <body>
                <h1>Authorization Failed</h1>
                <p>Error: {query_params["error"][0]}</p>
                <p>You can close this window and return to the terminal.</p>
            </body>
            </html>
            """.encode()
            )
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        """Suppress default logging."""
        pass


class CallbackServer:
    """Simple server to handle OAuth callbacks."""

    def __init__(self, port=3000):
        self.port = port
        self.server = None
        self.thread = None
        self.callback_data = {"authorization_code": None, "state": None, "error": None}

    def _create_handler_with_data(self):
        """Create a handler class with access to callback data."""
        callback_data = self.callback_data

        class DataCallbackHandler(CallbackHandler):
            def __init__(self, request, client_address, server):
                super().__init__(request, client_address, server, callback_data)

        return DataCallbackHandler

    def start(self):
        """Start the callback server in a background thread."""
        handler_class = self._create_handler_with_data()
        self.server = HTTPServer(("localhost", self.port), handler_class)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        print(f"🖥️  Started callback server on http://localhost:{self.port}")

    def stop(self):
        """Stop the callback server."""
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self.thread:
            self.thread.join(timeout=1)

    def wait_for_callback(self, timeout=300):
        """Wait for OAuth callback with timeout."""
        start_time = time.time()
        while time.time() - start_time < timeout:
            if self.callback_data["authorization_code"]:
                return self.callback_data["authorization_code"]
            elif self.callback_data["error"]:
                raise Exception(f"OAuth error: {self.callback_data['error']}")
            time.sleep(0.1)
        raise Exception("Timeout waiting for OAuth callback")

    def get_state(self):
        """Get the received state parameter."""
        return self.callback_data["state"]


class SimpleAuthClient:
    """Simple MCP client with auth support."""

    def __init__(self, server_url: str, transport_type: str = "streamable-http"):
        self.server_url = server_url
        self.transport_type = transport_type
        self.session: ClientSession | None = None

    async def connect(self):
        """Connect to the MCP server."""
        print(f"🔗 Attempting to connect to {self.server_url}...")

        try:
            callback_server = CallbackServer(port=3030)
            callback_server.start()

            async def callback_handler() -> tuple[str, str | None]:
                """Wait for OAuth callback and return auth code and state."""
                print("⏳ Waiting for authorization callback...")
                try:
                    auth_code = callback_server.wait_for_callback(timeout=300)
                    return auth_code, callback_server.get_state()
                finally:
                    callback_server.stop()

            client_metadata_dict = {
                "client_name": "Simple Auth Client",
                "redirect_uris": ["http://localhost:3030/callback"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "client_secret_post",
            }

            async def _default_redirect_handler(authorization_url: str) -> None:
                """Default redirect handler that opens the URL in a browser."""
                print(f"Opening browser for authorization: {authorization_url}")
                webbrowser.open(authorization_url)

            # Create OAuth authentication handler using the new interface
            oauth_auth = OAuthClientProvider(
                server_url=self.server_url.replace("/mcp", ""),
                client_metadata=OAuthClientMetadata.model_validate(client_metadata_dict),
                storage=InMemoryTokenStorage(),
                redirect_handler=_default_redirect_handler,
                callback_handler=callback_handler,
            )

            # Create transport with auth handler based on transport type
            if self.transport_type == "sse":
                print("📡 Opening SSE transport connection with auth...")
                async with sse_client(
                    url=self.server_url,
                    auth=oauth_auth,
                    timeout=60,
                ) as (read_stream, write_stream):
                    await self._run_session(read_stream, write_stream, None)
            else:
                print("📡 Opening StreamableHTTP transport connection with auth...")
                async with streamablehttp_client(
                    url=self.server_url,
                    auth=oauth_auth,
                    timeout=timedelta(seconds=60),
                ) as (read_stream, write_stream, get_session_id):
                    await self._run_session(read_stream, write_stream, get_session_id)

        except Exception as e:
            print(f"❌ Failed to connect: {e}")
            import traceback

            traceback.print_exc()

    async def _run_session(self, read_stream, write_stream, get_session_id):
        """Run the MCP session with the given streams."""
        print("🤝 Initializing MCP session...")
        async with ClientSession(read_stream, write_stream) as session:
            self.session = session
            print("⚡ Starting session initialization...")
            await session.initialize()
            print("✨ Session initialization complete!")

            print(f"\n✅ Connected to MCP server at {self.server_url}")
            if get_session_id:
                session_id = get_session_id()
                if session_id:
                    print(f"Session ID: {session_id}")

            # Run interactive loop
            await self.interactive_loop()

    async def list_tools(self):
        """List available tools from the server."""
        if not self.session:
            print("❌ Not connected to server")
            return

        try:
            result = await self.session.list_tools()
            if hasattr(result, "tools") and result.tools:
                print("\n📋 Available tools:")
                for i, tool in enumerate(result.tools, 1):
                    print(f"{i}. {tool.name}")
                    if tool.description:
                        print(f"   Description: {tool.description}")
                    print()
            else:
                print("No tools available")
        except Exception as e:
            print(f"❌ Failed to list tools: {e}")

    async def call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None):
        """Call a specific tool."""
        if not self.session:
            print("❌ Not connected to server")
            return

        try:
            result = await self.session.call_tool(tool_name, arguments or {})
            print(f"\n🔧 Tool '{tool_name}' result:")
            if hasattr(result, "content"):
                for content in result.content:
                    if content.type == "text":
                        print(content.text)
                    else:
                        print(content)
            else:
                print(result)
        except Exception as e:
            print(f"❌ Failed to call tool '{tool_name}': {e}")

    async def interactive_loop(self):
        """Run interactive command loop."""
        print("\n🎯 Interactive MCP Client")
        print("Commands:")
        print("  list - List available tools")
        print("  call <tool_name> [args] - Call a tool")
        print("  quit - Exit the client")
        print()

        while True:
            try:
                command = input("mcp> ").strip()

                if not command:
                    continue

                if command == "quit":
                    break

                elif command == "list":
                    await self.list_tools()

                elif command.startswith("call "):
                    parts = command.split(maxsplit=2)
                    tool_name = parts[1] if len(parts) > 1 else ""

                    if not tool_name:
                        print("❌ Please specify a tool name")
                        continue

                    # Parse arguments (simple JSON-like format)
                    arguments = {}
                    if len(parts) > 2:
                        import json

                        try:
                            arguments = json.loads(parts[2])
                        except json.JSONDecodeError:
                            print("❌ Invalid arguments format (expected JSON)")
                            continue

                    await self.call_tool(tool_name, arguments)

                else:
                    print("❌ Unknown command. Try 'list', 'call <tool_name>', or 'quit'")

            except KeyboardInterrupt:
                print("\n\n👋 Goodbye!")
                break
            except EOFError:
                break


async def main():
    """Main entry point."""
    # Default server URL - can be overridden with environment variable
    # Most MCP streamable HTTP servers use /mcp as the endpoint
    server_url = os.getenv("MCP_SERVER_PORT", 8000)
    transport_type = os.getenv("MCP_TRANSPORT_TYPE", "streamable-http")
    server_url = (
        f"http://localhost:{server_url}/mcp"
        if transport_type == "streamable-http"
        else f"http://localhost:{server_url}/sse"
    )

    print("🚀 Simple MCP Auth Client")
    print(f"Connecting to: {server_url}")
    print(f"Transport type: {transport_type}")

    # Start connection flow - OAuth will be handled automatically
    client = SimpleAuthClient(server_url, transport_type)
    await client.connect()


def cli():
    """CLI entry point for uv script."""
    asyncio.run(main())


if __name__ == "__main__":
    cli()
import asyncio
import json
import logging
import os
import shutil
from contextlib import AsyncExitStack
from typing import Any

import httpx
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


class Configuration:
    """Manages configuration and environment variables for the MCP client."""

    def __init__(self) -> None:
        """Initialize configuration with environment variables."""
        self.load_env()
        self.api_key = os.getenv("LLM_API_KEY")

    @staticmethod
    def load_env() -> None:
        """Load environment variables from .env file."""
        load_dotenv()

    @staticmethod
    def load_config(file_path: str) -> dict[str, Any]:
        """Load server configuration from JSON file.

        Args:
            file_path: Path to the JSON configuration file.

        Returns:
            Dict containing server configuration.

        Raises:
            FileNotFoundError: If configuration file doesn't exist.
            JSONDecodeError: If configuration file is invalid JSON.
        """
        with open(file_path, "r") as f:
            return json.load(f)

    @property
    def llm_api_key(self) -> str:
        """Get the LLM API key.

        Returns:
            The API key as a string.

        Raises:
            ValueError: If the API key is not found in environment variables.
        """
        if not self.api_key:
            raise ValueError("LLM_API_KEY not found in environment variables")
        return self.api_key


class Server:
    """Manages MCP server connections and tool execution."""

    def __init__(self, name: str, config: dict[str, Any]) -> None:
        self.name: str = name
        self.config: dict[str, Any] = config
        self.stdio_context: Any | None = None
        self.session: ClientSession | None = None
        self._cleanup_lock: asyncio.Lock = asyncio.Lock()
        self.exit_stack: AsyncExitStack = AsyncExitStack()

    async def initialize(self) -> None:
        """Initialize the server connection."""
        command = shutil.which("npx") if self.config["command"] == "npx" else self.config["command"]
        if command is None:
            raise ValueError("The command must be a valid string and cannot be None.")

        server_params = StdioServerParameters(
            command=command,
            args=self.config["args"],
            env={**os.environ, **self.config["env"]} if self.config.get("env") else None,
        )
        try:
            stdio_transport = await self.exit_stack.enter_async_context(stdio_client(server_params))
            read, write = stdio_transport
            session = await self.exit_stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self.session = session
        except Exception as e:
            logging.error(f"Error initializing server {self.name}: {e}")
            await self.cleanup()
            raise

    async def list_tools(self) -> list[Any]:
        """List available tools from the server.

        Returns:
            A list of available tools.

        Raises:
            RuntimeError: If the server is not initialized.
        """
        if not self.session:
            raise RuntimeError(f"Server {self.name} not initialized")

        tools_response = await self.session.list_tools()
        tools = []

        for item in tools_response:
            if isinstance(item, tuple) and item[0] == "tools":
                tools.extend(Tool(tool.name, tool.description, tool.inputSchema, tool.title) for tool in item[1])

        return tools

    async def execute_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        retries: int = 2,
        delay: float = 1.0,
    ) -> Any:
        """Execute a tool with retry mechanism.

        Args:
            tool_name: Name of the tool to execute.
            arguments: Tool arguments.
            retries: Number of retry attempts.
            delay: Delay between retries in seconds.

        Returns:
            Tool execution result.

        Raises:
            RuntimeError: If server is not initialized.
            Exception: If tool execution fails after all retries.
        """
        if not self.session:
            raise RuntimeError(f"Server {self.name} not initialized")

        attempt = 0
        while attempt < retries:
            try:
                logging.info(f"Executing {tool_name}...")
                result = await self.session.call_tool(tool_name, arguments)

                return result

            except Exception as e:
                attempt += 1
                logging.warning(f"Error executing tool: {e}. Attempt {attempt} of {retries}.")
                if attempt < retries:
                    logging.info(f"Retrying in {delay} seconds...")
                    await asyncio.sleep(delay)
                else:
                    logging.error("Max retries reached. Failing.")
                    raise

    async def cleanup(self) -> None:
        """Clean up server resources."""
        async with self._cleanup_lock:
            try:
                await self.exit_stack.aclose()
                self.session = None
                self.stdio_context = None
            except Exception as e:
                logging.error(f"Error during cleanup of server {self.name}: {e}")


class Tool:
    """Represents a tool with its properties and formatting."""

    def __init__(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        title: str | None = None,
    ) -> None:
        self.name: str = name
        self.title: str | None = title
        self.description: str = description
        self.input_schema: dict[str, Any] = input_schema

    def format_for_llm(self) -> str:
        """Format tool information for LLM.

        Returns:
            A formatted string describing the tool.
        """
        args_desc = []
        if "properties" in self.input_schema:
            for param_name, param_info in self.input_schema["properties"].items():
                arg_desc = f"- {param_name}: {param_info.get('description', 'No description')}"
                if param_name in self.input_schema.get("required", []):
                    arg_desc += " (required)"
                args_desc.append(arg_desc)

        # Build the formatted output with title as a separate field
        output = f"Tool: {self.name}\n"

        # Add human-readable title if available
        if self.title:
            output += f"User-readable title: {self.title}\n"

        output += f"""Description: {self.description}
Arguments:
{chr(10).join(args_desc)}
"""

        return output


class LLMClient:
    """Manages communication with the LLM provider."""

    def __init__(self, api_key: str) -> None:
        self.api_key: str = api_key

    def get_response(self, messages: list[dict[str, str]]) -> str:
        """Get a response from the LLM.

        Args:
            messages: A list of message dictionaries.

        Returns:
            The LLM's response as a string.

        Raises:
            httpx.RequestError: If the request to the LLM fails.
        """
        url = "https://api.groq.com/openai/v1/chat/completions"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        payload = {
            "messages": messages,
            "model": "meta-llama/llama-4-scout-17b-16e-instruct",
            "temperature": 0.7,
            "max_tokens": 4096,
            "top_p": 1,
            "stream": False,
            "stop": None,
        }

        try:
            with httpx.Client() as client:
                response = client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                data = response.json()
                return data["choices"][0]["message"]["content"]

        except httpx.RequestError as e:
            error_message = f"Error getting LLM response: {str(e)}"
            logging.error(error_message)

            if isinstance(e, httpx.HTTPStatusError):
                status_code = e.response.status_code
                logging.error(f"Status code: {status_code}")
                logging.error(f"Response details: {e.response.text}")

            return f"I encountered an error: {error_message}. Please try again or rephrase your request."


class ChatSession:
    """Orchestrates the interaction between user, LLM, and tools."""

    def __init__(self, servers: list[Server], llm_client: LLMClient) -> None:
        self.servers: list[Server] = servers
        self.llm_client: LLMClient = llm_client

    async def cleanup_servers(self) -> None:
        """Clean up all servers properly."""
        for server in reversed(self.servers):
            try:
                await server.cleanup()
            except Exception as e:
                logging.warning(f"Warning during final cleanup: {e}")

    async def process_llm_response(self, llm_response: str) -> str:
        """Process the LLM response and execute tools if needed.

        Args:
            llm_response: The response from the LLM.

        Returns:
            The result of tool execution or the original response.
        """
        import json

        def _clean_json_string(json_string: str) -> str:
            """Remove ```json ... ``` or ``` ... ``` wrappers if the LLM response is fenced."""
            import re

            pattern = r"^```(?:\s*json)?\s*(.*?)\s*```$"
            return re.sub(pattern, r"\1", json_string, flags=re.DOTALL | re.IGNORECASE).strip()

        try:
            tool_call = json.loads(_clean_json_string(llm_response))
            if "tool" in tool_call and "arguments" in tool_call:
                logging.info(f"Executing tool: {tool_call['tool']}")
                logging.info(f"With arguments: {tool_call['arguments']}")

                for server in self.servers:
                    tools = await server.list_tools()
                    if any(tool.name == tool_call["tool"] for tool in tools):
                        try:
                            result = await server.execute_tool(tool_call["tool"], tool_call["arguments"])

                            if isinstance(result, dict) and "progress" in result:
                                progress = result["progress"]
                                total = result["total"]
                                percentage = (progress / total) * 100
                                logging.info(f"Progress: {progress}/{total} ({percentage:.1f}%)")

                            return f"Tool execution result: {result}"
                        except Exception as e:
                            error_msg = f"Error executing tool: {str(e)}"
                            logging.error(error_msg)
                            return error_msg

                return f"No server found with tool: {tool_call['tool']}"
            return llm_response
        except json.JSONDecodeError:
            return llm_response

    async def start(self) -> None:
        """Main chat session handler."""
        try:
            for server in self.servers:
                try:
                    await server.initialize()
                except Exception as e:
                    logging.error(f"Failed to initialize server: {e}")
                    await self.cleanup_servers()
                    return

            all_tools = []
            for server in self.servers:
                tools = await server.list_tools()
                all_tools.extend(tools)

            tools_description = "\n".join([tool.format_for_llm() for tool in all_tools])

            system_message = (
                "You are a helpful assistant with access to these tools:\n\n"
                f"{tools_description}\n"
                "Choose the appropriate tool based on the user's question. "
                "If no tool is needed, reply directly.\n\n"
                "IMPORTANT: When you need to use a tool, you must ONLY respond with "
                "the exact JSON object format below, nothing else:\n"
                "{\n"
                '    "tool": "tool-name",\n'
                '    "arguments": {\n'
                '        "argument-name": "value"\n'
                "    }\n"
                "}\n\n"
                "After receiving a tool's response:\n"
                "1. Transform the raw data into a natural, conversational response\n"
                "2. Keep responses concise but informative\n"
                "3. Focus on the most relevant information\n"
                "4. Use appropriate context from the user's question\n"
                "5. Avoid simply repeating the raw data\n\n"
                "Please use only the tools that are explicitly defined above."
            )

            messages = [{"role": "system", "content": system_message}]

            while True:
                try:
                    user_input = input("You: ").strip().lower()
                    if user_input in ["quit", "exit"]:
                        logging.info("\nExiting...")
                        break

                    messages.append({"role": "user", "content": user_input})

                    llm_response = self.llm_client.get_response(messages)
                    logging.info("\nAssistant: %s", llm_response)

                    result = await self.process_llm_response(llm_response)

                    if result != llm_response:
                        messages.append({"role": "assistant", "content": llm_response})
                        messages.append({"role": "system", "content": result})

                        final_response = self.llm_client.get_response(messages)
                        logging.info("\nFinal response: %s", final_response)
                        messages.append({"role": "assistant", "content": final_response})
                    else:
                        messages.append({"role": "assistant", "content": llm_response})

                except KeyboardInterrupt:
                    logging.info("\nExiting...")
                    break

        finally:
            await self.cleanup_servers()


async def main() -> None:
    """Initialize and run the chat session."""
    config = Configuration()
    server_config = config.load_config("servers_config.json")
    servers = [Server(name, srv_config) for name, srv_config in server_config["mcpServers"].items()]
    llm_client = LLMClient(config.llm_api_key)
    chat_session = ChatSession(servers, llm_client)
    await chat_session.start()


if __name__ == "__main__":
    asyncio.run(main())
