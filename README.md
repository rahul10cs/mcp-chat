# mcp-chat

A **FastAPI chat application** that acts as an MCP (Model Context Protocol) client. It connects to the Salesforce MCP server, passes live Salesforce tools to Claude, and serves a browser-based chat UI so you can query your Salesforce org in plain English.

**Part of a two-repo project:**
- **This repo** — the Chat UI + Claude API backend (presentation layer)
- [mcp-salesforce](https://github.com/rahul10cs/mcp-salesforce) — the Salesforce MCP Server (data layer)

---

## Full System Architecture

```
┌─────────────────────────────────────────────────────────┐
│                      Browser                            │
│   static/index.html — Chat UI (HTML + vanilla JS)       │
│                                                         │
│   User types:  "Show me my accounts"                    │
│         │  POST /chat  { "message": "..." }             │
└─────────┼───────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────┐
│              THIS REPO — app.py (port 3000)             │
│                                                         │
│  Step 1: Open SSE connection to mcp-salesforce          │
│  Step 2: List available Salesforce tools                │
│  Step 3: Send user message + tools to Claude API        │
│  Step 4: If Claude calls a tool → forward to MCP server │
│  Step 5: Send tool result back to Claude                │
│  Step 6: Return Claude's final answer to browser        │
└─────────┬───────────────────────────────────────────────┘
          │  SSE  (http://localhost:8000/sse)
          ▼
┌─────────────────────────────────────────────────────────┐
│         mcp-salesforce — server.py (port 8000)          │
│   Exposes: get_accounts, get_contacts, get_opportunities │
│            get_cases, run_soql, get_org_info             │
└─────────┬───────────────────────────────────────────────┘
          │  OAuth Client Credentials
          ▼
┌─────────────────────────────────────────────────────────┐
│                   Salesforce Org                        │
│              Live CRM data via REST API                 │
└─────────────────────────────────────────────────────────┘
```

---

## Project Structure

```
mcp-chat/
├── app.py              # FastAPI backend — MCP client loop + Claude API calls
├── static/
│   └── index.html      # Chat UI — pure HTML/CSS/JS, no framework
├── requirements.txt    # Python dependencies
├── render.yaml         # Deployment config for Render.com
├── .env.example        # Template for environment variables
└── .gitignore
```

---

## Step-by-Step Request Flow

Here is exactly what happens from the moment a user presses **Send** to when the reply appears on screen.

---

### Step 1 — User types and hits Send (browser → `index.html`)

```javascript
// static/index.html
async function sendMessage() {
    const text = inputEl.value.trim();
    addMessage("user", text);   // show message in chat immediately
    addTyping();                // show "Thinking..." bubble

    const res = await fetch("/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text }),   // send to backend
    });
    const data = await res.json();
    addMessage("bot", data.reply);   // display Claude's answer
}
```

The browser sends a `POST /chat` request with the user's message as JSON to `app.py`.

`Enter` key is also wired to send (without Shift):
```javascript
function handleKey(e) {
    if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
    }
}
```

---

### Step 2 — FastAPI receives the request (`app.py`)

```python
class ChatRequest(BaseModel):
    message: str

@app.post("/chat")
async def chat(req: ChatRequest):
    ...
```

FastAPI validates the incoming JSON against the `ChatRequest` model. If `message` is missing or malformed, it returns a 422 automatically.

---

### Step 3 — Open SSE connection to the MCP Server

```python
async with sse_client(url=MCP_SERVER_URL) as (read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()
```

`MCP_SERVER_URL` points to `http://localhost:8000/sse` (or the deployed Render URL).

- `sse_client` opens an HTTP connection and keeps it alive via **Server-Sent Events**
- `ClientSession` wraps the connection with the MCP protocol (handshake, message framing)
- `session.initialize()` performs the MCP handshake — server and client exchange capabilities

---

### Step 4 — Fetch available tools from the MCP Server

```python
tools_response = await session.list_tools()
tools = [
    {
        "name": t.name,
        "description": t.description or "",
        "input_schema": to_dict(t.inputSchema),
    }
    for t in tools_response.tools
]
```

The MCP server returns the full list of tools it exposes (get_accounts, get_contacts, etc.) with their names, descriptions, and input schemas. These are converted into the format Claude's API expects for tool use.

---

### Step 5 — Send user message + tools to Claude API

```python
messages = [{"role": "user", "content": req.message}]

response = claude.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=1024,
    tools=tools,        # Claude now knows what tools are available
    messages=messages,
)
```

Claude receives:
- The user's message
- The full list of Salesforce tools with their schemas

Claude then decides: can I answer this from my own knowledge, or do I need to call a tool?

---

### Step 6 — Tool call loop (if Claude needs Salesforce data)

If Claude needs to query Salesforce, it responds with `stop_reason = "tool_use"`:

```python
while response.stop_reason == "tool_use":
    # Extract which tool Claude wants to call
    tool_block = next(b for b in response.content if b.type == "tool_use")

    # Execute the tool on the MCP Server
    tool_result = await session.call_tool(
        tool_block.name,       # e.g. "get_accounts"
        dict(tool_block.input) # e.g. { "limit": 10 }
    )

    result_text = tool_result.content[0].text   # plain text result from Salesforce
```

Then the conversation is extended with Claude's tool call and the result:

```python
    messages.append({"role": "assistant", "content": assistant_content})
    messages.append({
        "role": "user",
        "content": [{
            "type": "tool_result",
            "tool_use_id": tool_block.id,
            "content": result_text,    # Salesforce data as text
        }],
    })

    # Call Claude again with the tool result
    response = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        tools=tools,
        messages=messages,
    )
```

This loop continues until `stop_reason` is no longer `"tool_use"` — meaning Claude has all the data it needs and is ready to write a final answer. Claude may call multiple tools in sequence if the query requires it.

---

### Step 7 — Extract and return Claude's final answer

```python
final_text = next(
    (b.text for b in response.content if hasattr(b, "text")),
    "Sorry, I could not generate a response.",
)
return {"reply": final_text}
```

The final text block from Claude's response is extracted and returned as JSON to the browser. The browser's `sendMessage()` function receives it and calls `addMessage("bot", data.reply)` to display it.

---

### Step 8 — Error handling

Two layers of error handling prevent crashes from reaching the user:

```python
# Inner: catches Anthropic API errors (bad key, low credits, rate limit)
except anthropic.APIStatusError as e:
    return {"reply": friendly_api_error(e)}

# Outer: catches everything else (MCP connection failure, network errors)
except Exception as e:
    print(traceback.format_exc())
    return {"reply": f"Error: {str(e)}"}
```

`friendly_api_error()` converts raw API error codes into human-readable messages:
```python
def friendly_api_error(e: anthropic.APIStatusError) -> str:
    if "credit balance is too low" in msg:
        return "Your Anthropic API credit balance is too low..."
    if e.status_code == 401:
        return "Invalid Anthropic API key..."
    if e.status_code == 429:
        return "Anthropic API rate limit hit..."
```

---

## Chat UI — `static/index.html`

The UI is plain HTML + CSS + vanilla JavaScript — no framework, no build step.

| Element | What It Does |
|---|---|
| `.chat-container` | Full-page centered card layout |
| `.messages` | Scrollable message history |
| `.bubble.user` | Blue right-aligned bubble for user messages |
| `.bubble.bot` | Grey left-aligned bubble for assistant replies |
| `#typing` | "Thinking..." indicator shown while waiting for response |
| `.suggestions` | Quick-fill buttons (e.g. "Show me top 10 accounts") |
| `textarea` | Input field — Enter to send, Shift+Enter for newline |

The UI is served by FastAPI as a static file:
```python
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def index():
    return FileResponse("static/index.html")
```

---

## Local Setup

### Prerequisites
- Python 3.10+
- [mcp-salesforce](https://github.com/rahul10cs/mcp-salesforce) running on port 8000
- Anthropic API key from [console.anthropic.com](https://console.anthropic.com)

### Install & Run

```bash
# Clone the repo
git clone https://github.com/rahul10cs/mcp-chat.git
cd mcp-chat

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Copy and fill in credentials
cp .env.example .env
# Edit .env with your ANTHROPIC_API_KEY and MCP_SERVER_URL

# Start the chat server
python app.py
```

Open `http://localhost:3000` in your browser.

### Start Order

Always start the MCP server first, then the chat server:

```bash
# Terminal 1 — MCP Server
cd mcp-salesforce
TRANSPORT=sse .venv/bin/python server.py

# Terminal 2 — Chat App
cd mcp-chat
.venv/bin/python app.py
```

---

## Environment Variables

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Your Anthropic API key from console.anthropic.com |
| `MCP_SERVER_URL` | URL of the running MCP server e.g. `http://localhost:8000/sse` |
| `PORT` | Port for this server (default `3000`) |

---

## Example Queries

| Query | Tool Called |
|---|---|
| "Show me my accounts" | `get_accounts` |
| "Find contacts with last name Smith" | `get_contacts` |
| "List Closed Won opportunities" | `get_opportunities` |
| "Show open cases" | `get_cases` |
| "What org am I connected to?" | `get_org_info` |
| "SELECT Name FROM Lead LIMIT 5" | `run_soql` |

---

## Deploying to Render

A `render.yaml` is included. Push this repo to GitHub, connect it to [Render.com](https://render.com), and set the environment variables in the Render dashboard. Set `MCP_SERVER_URL` to the deployed URL of [mcp-salesforce](https://github.com/rahul10cs/mcp-salesforce).

---

## Dependencies

```
fastapi>=0.104.0        # Web framework + request routing
uvicorn>=0.24.0         # ASGI server
anthropic>=0.34.0       # Claude API client
mcp>=1.0.0              # Model Context Protocol SDK (client side)
python-dotenv>=1.0.0    # Loads .env files
httpx>=0.25.0           # Async HTTP client (used internally by MCP SDK)
httpx-sse>=0.4.0        # SSE support for httpx
```
