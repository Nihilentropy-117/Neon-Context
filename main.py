import asyncio
import json
import os
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from mcp.client.auth import (
    AuthorizationRequiredError,
    OAuthClientProvider,
    TokenStorage,
)
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)
from pydantic import AnyUrl, BaseModel, Field
from pydantic_settings import BaseSettings

import httpx

# --- Configuration ---
SERVERS_DATA_DIR = "data"
SERVERS_FILE = os.path.join(SERVERS_DATA_DIR, "servers.json")

class Settings(BaseSettings):
    app_base_url: str = "http://localhost:80"
    class Config:
        env_file = ".env"
        env_file_encoding = 'utf-8'

settings = Settings()


# --- Pydantic Models ---
class MCPServerConfig(BaseModel):
    name: str = Field(..., description="A unique name for this MCP server connection.")
    url: str = Field(..., description="The base URL of the MCP server.")

class MCPServerInfo(BaseModel):
    name: str
    url: str
    session: ClientSession
    read_stream: Any
    write_stream: Any
    auth_provider: Optional[OAuthClientProvider] = None
    class Config:
        arbitrary_types_allowed = True

class MCPServerDetails(BaseModel):
    name: str
    url: str

class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "tool"]
    content: str
    tool_calls: Optional[List[Dict]] = None
    tool_call_id: Optional[str] = None

class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    openrouter_api_key: str = Field(..., description="Your OpenRouter API key.")
    model: str = Field("anthropic/claude-3.5-sonnet", description="The model to use on OpenRouter.")

class ChatResponse(BaseModel):
    type: Literal["message", "oauth_redirect"]
    message: Optional[ChatMessage] = None
    redirect_url: Optional[str] = None
    error: Optional[str] = None


# --- In-Memory State and Storage ---
class InMemoryTokenStorage(TokenStorage):
    def __init__(self):
        self._tokens: Dict[str, OAuthToken] = {}
        self._client_info: Dict[str, OAuthClientInformationFull] = {}
    async def get_tokens(self, server_name: str) -> OAuthToken | None: return self._tokens.get(server_name)
    async def set_tokens(self, server_name: str, tokens: OAuthToken) -> None: self._tokens[server_name] = tokens
    async def get_client_info(self, server_name: str) -> OAuthClientInformationFull | None: return self._client_info.get(server_name)
    async def set_client_info(self, server_name: str, client_info: OAuthClientInformationFull) -> None: self._client_info[server_name] = client_info

g_token_storage = InMemoryTokenStorage()
g_mcp_servers: Dict[str, MCPServerInfo] = {}
g_oauth_flows: Dict[str, asyncio.Future] = {}


# --- Persistence Functions ---
def save_mcp_servers_to_disk():
    os.makedirs(SERVERS_DATA_DIR, exist_ok=True)
    server_configs = [{"name": s.name, "url": s.url} for s in g_mcp_servers.values()]
    with open(SERVERS_FILE, "w") as f:
        json.dump(server_configs, f, indent=2)

async def load_mcp_servers_from_disk():
    if not os.path.exists(SERVERS_FILE):
        return
    try:
        with open(SERVERS_FILE, "r") as f:
            server_configs = json.load(f)
        print(f"Found {len(server_configs)} saved servers. Reconnecting...")
        for config in server_configs:
            await connect_to_mcp_server(MCPServerConfig(**config))
    except (json.JSONDecodeError, FileNotFoundError) as e:
        print(f"Could not load server configurations: {e}")


# --- Core Logic ---
async def connect_to_mcp_server(config: MCPServerConfig) -> str:
    if config.name in g_mcp_servers:
        return f"Server '{config.name}' is already connected."

    try:
        oauth_provider = OAuthClientProvider(
            server_url=config.url,
            client_metadata=OAuthClientMetadata(
                client_name="MCP Chat Backend Client",
                redirect_uris=[AnyUrl(f"{settings.app_base_url}/oauth/callback")],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                scope="user",
            ),
            storage=g_token_storage,
            storage_key=config.name,
            redirect_handler=lambda auth_url: None,
            callback_handler=lambda: handle_callback(oauth_provider.state),
        )

        mcp_endpoint = f"{config.url.rstrip('/')}/mcp"
        client_context = streamablehttp_client(mcp_endpoint, auth=oauth_provider)
        read, write, _ = await client_context.__aenter__()

        session = ClientSession(read, write)
        await session.initialize()

        g_mcp_servers[config.name] = MCPServerInfo(
            name=config.name,
            url=config.url,
            session=session,
            read_stream=read,
            write_stream=write,
            auth_provider=oauth_provider,
        )
        return f"Successfully connected to MCP server '{config.name}'."
    except Exception as e:
        if 'client_context' in locals():
            await client_context.__aexit__(type(e), e, e.__traceback__)
        raise ConnectionError(f"Failed to connect to MCP server at {config.url}: {e}")

async def handle_callback(state: str) -> tuple[str, str | None]:
    future = asyncio.get_event_loop().create_future()
    g_oauth_flows[state] = future
    return await future


# --- FastAPI Application ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Starting MCP Chat Backend...")
    await load_mcp_servers_from_disk()
    yield
    print("Shutting down... disconnecting from MCP servers.")
    for server_name, server_info in list(g_mcp_servers.items()):
        try:
            await server_info.session.shutdown()
        except Exception as e:
            print(f"Error shutting down server {server_name}: {e}")
    g_mcp_servers.clear()

app = FastAPI(title="MCP Chat Backend", lifespan=lifespan)

@app.post("/mcp_servers", status_code=201)
async def add_mcp_server(config: MCPServerConfig):
    try:
        message = await connect_to_mcp_server(config)
        if "already connected" in message:
             raise HTTPException(status_code=409, detail=message)
        save_mcp_servers_to_disk()
        return {"status": "success", "message": message}
    except ConnectionError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.delete("/mcp_servers/{server_name}")
async def remove_mcp_server(server_name: str):
    if server_name not in g_mcp_servers:
        raise HTTPException(status_code=404, detail="Server not found.")
    server_info = g_mcp_servers.pop(server_name)
    try:
        await server_info.session.shutdown()
    except Exception as e:
        print(f"Error during shutdown of {server_name}: {e}")
    save_mcp_servers_to_disk()
    return {"status": "success", "message": f"Disconnected from server '{server_name}'."}

@app.get("/mcp_servers", response_model=List[MCPServerDetails])
async def list_mcp_servers():
    return [MCPServerDetails(name=info.name, url=info.url) for info in g_mcp_servers.values()]

@app.get("/oauth/callback", response_class=HTMLResponse)
async def oauth_callback(code: str, state: str):
    if state in g_oauth_flows:
        future = g_oauth_flows.pop(state)
        future.set_result((code, state))
        return """<html><head><title>Authentication Successful</title></head><body style="font-family: sans-serif; text-align: center; padding: 40px;"><h1>✅ Authentication Successful</h1><p>You can now close this tab and return to your application.</p></body></html>"""
    else:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state.")

@app.post("/chat", response_model=ChatResponse)
async def chat(chat_request: ChatRequest):
    all_tools, tool_to_server_map = [], {}
    for name, server_info in g_mcp_servers.items():
        try:
            tool_list = await server_info.session.list_tools()
            for tool in tool_list.tools:
                prefixed_name = f"{name}__{tool.name}"
                formatted_tool = {"type": "function", "function": {"name": prefixed_name, "description": tool.description, "parameters": tool.inputSchema}}
                all_tools.append(formatted_tool)
                tool_to_server_map[prefixed_name] = server_info
        except Exception as e:
            print(f"Could not fetch tools from server '{name}': {e}")

    llm_response = await get_llm_response(chat_request, all_tools)
    response_message = llm_response.get("choices", [{}])[0].get("message", {})

    if not response_message.get("tool_calls"):
        return ChatResponse(type="message", message=ChatMessage(role="assistant", content=response_message.get("content", "")))

    chat_request.messages.append(ChatMessage(**response_message))

    for tool_call in response_message["tool_calls"]:
        tool_name, tool_args, tool_call_id = tool_call["function"]["name"], json.loads(tool_call["function"]["arguments"]), tool_call["id"]
        if tool_name not in tool_to_server_map:
             chat_request.messages.append(ChatMessage(role="tool", tool_call_id=tool_call_id, content=f"Error: Tool '{tool_name}' not found."))
             continue

        server_info = tool_to_server_map[tool_name]
        original_tool_name = tool_name.split("__", 1)[1]

        try:
            result = await server_info.session.call_tool(original_tool_name, arguments=tool_args)
            content = json.dumps(result.structuredContent) if result.structuredContent else (result.content[0].text if result.content and hasattr(result.content[0], 'text') else "Tool executed.")
            chat_request.messages.append(ChatMessage(role="tool", tool_call_id=tool_call_id, content=content))
        except AuthorizationRequiredError as e:
            return ChatResponse(type="oauth_redirect", redirect_url=str(e.auth_url))
        except Exception as e:
            print(f"Error calling tool {tool_name}: {e}")
            chat_request.messages.append(ChatMessage(role="tool", tool_call_id=tool_call_id, content=f"Error executing tool: {e}"))

    final_llm_response = await get_llm_response(chat_request, all_tools)
    final_message = final_llm_response.get("choices", [{}])[0].get("message", {})
    return ChatResponse(type="message", message=ChatMessage(role="assistant", content=final_message.get("content", "")))

async def get_llm_response(chat_request: ChatRequest, tools: List[Dict]) -> Dict:
    headers = {"Authorization": f"Bearer {chat_request.openrouter_api_key}", "Content-Type": "application/json"}
    payload = {"model": chat_request.model, "messages": [msg.model_dump(exclude_none=True) for msg in chat_request.messages]}
    if tools:
        payload["tools"] = tools

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload, timeout=60)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=e.response.status_code, detail=f"Error from OpenRouter: {e.response.text}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"An unexpected error occurred: {e}")


