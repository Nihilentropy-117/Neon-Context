# Neon-Context

A Python-based MCP (Model Context Protocol) client with OAuth authentication support. This client provides an interactive command-line interface for connecting to MCP servers using streamable HTTP or SSE transport with OAuth 2.0 authentication.

## Features

- **OAuth 2.0 Authentication**: Built-in OAuth flow with automatic browser-based authorization
- **Multiple Transport Types**: Supports both StreamableHTTP and SSE (Server-Sent Events) transports
- **Interactive CLI**: Simple command-line interface for exploring and calling MCP server tools
- **Token Storage**: In-memory token storage with support for token refresh
- **Local Callback Server**: Automatic handling of OAuth callbacks via local HTTP server

## Requirements

- Python 3.7+
- Required packages:
  - `mcp` (Model Context Protocol SDK)

## Installation

```bash
# Clone the repository
git clone <repository-url>
cd Neon-Context

# Install dependencies (using pip)
pip install mcp

# Or using uv
uv pip install mcp
```

## Configuration

The client can be configured using environment variables:

- `MCP_SERVER_PORT`: The port where your MCP server is running (default: `8000`)
- `MCP_TRANSPORT_TYPE`: Transport type to use - either `streamable-http` or `sse` (default: `streamable-http`)

Example:
```bash
export MCP_SERVER_PORT=8000
export MCP_TRANSPORT_TYPE=streamable-http
```

## Usage

### Basic Usage

Run the client:

```bash
python main.py
```

Or if installed as a script:

```bash
./main.py
```

### OAuth Flow

1. When you start the client, it will:
   - Start a local callback server on port 3030
   - Open your default browser for OAuth authorization
   - Wait for you to complete the authorization

2. After authorization:
   - The callback server captures the authorization code
   - The client exchanges it for access tokens
   - Tokens are stored in memory for the session

3. The interactive prompt will appear once connected

### Interactive Commands

Once connected, you can use the following commands:

- **`list`**: List all available tools from the MCP server
- **`call <tool_name> [args]`**: Call a specific tool with optional JSON arguments
- **`quit`**: Exit the client

#### Examples

List available tools:
```
mcp> list
```

Call a tool without arguments:
```
mcp> call get_status
```

Call a tool with JSON arguments:
```
mcp> call search_data {"query": "example", "limit": 10}
```

Exit the client:
```
mcp> quit
```

## Architecture

### Key Components

- **`InMemoryTokenStorage`** (lines 26-43): Implements token storage for OAuth credentials
- **`CallbackHandler`** (lines 46-96): HTTP handler for OAuth callback redirects
- **`CallbackServer`** (lines 99-147): Local HTTP server for capturing OAuth callbacks
- **`SimpleAuthClient`** (lines 150-330): Main client class handling MCP connections and interactions

### OAuth Flow

1. Client initiates connection with OAuth provider
2. Authorization URL opens in browser
3. User authenticates and authorizes
4. OAuth provider redirects to `http://localhost:3030/callback`
5. Callback server captures authorization code
6. Client exchanges code for tokens
7. Tokens are used for subsequent MCP requests

### Transport Support

The client supports two transport types:

- **StreamableHTTP** (default): Uses HTTP streaming for bidirectional communication
- **SSE**: Uses Server-Sent Events for server-to-client communication

## Development

### Code Structure

```
main.py
├── InMemoryTokenStorage      # Token storage implementation
├── CallbackHandler            # OAuth callback HTTP handler
├── CallbackServer             # Local OAuth callback server
├── SimpleAuthClient           # Main MCP client
│   ├── connect()              # Connection management
│   ├── list_tools()           # List available tools
│   ├── call_tool()            # Execute tool calls
│   └── interactive_loop()     # Interactive CLI
└── main()                     # Entry point
```

### Extending the Client

To add custom functionality:

1. **Custom Token Storage**: Implement `TokenStorage` interface for persistent storage
2. **Additional Commands**: Extend `interactive_loop()` method
3. **Custom Auth Flow**: Modify `OAuthClientProvider` configuration

## Security Considerations

- Tokens are stored in memory only (not persisted to disk)
- OAuth callback server runs only during authorization
- Local callback server listens on `localhost` only
- Authorization timeout is set to 5 minutes (300 seconds)

## Troubleshooting

### Connection Issues

If you can't connect to the server:
- Verify the server URL and port are correct
- Check that the MCP server is running
- Ensure OAuth endpoints are properly configured

### OAuth Issues

If OAuth authorization fails:
- Check that callback URL `http://localhost:3030/callback` is registered
- Verify the callback server port (3030) is available
- Look for error messages in the terminal

### Transport Issues

If experiencing transport problems:
- Try switching transport types (SSE vs StreamableHTTP)
- Check server logs for error messages
- Verify timeout settings are appropriate

## License

[Add your license information here]

## Contributing

[Add contributing guidelines here]
