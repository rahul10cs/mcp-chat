# mcp-chat

A **FastAPI chat application** that acts as an MCP (Model Context Protocol) client. It handles Salesforce OAuth login, collects an Anthropic API key, connects to the Salesforce MCP server, and serves a browser-based chat UI so users can query their Salesforce org in plain English.

**Part of a two-repo project:**
- **This repo** — the Chat UI + Claude API backend (presentation layer)
- [mcp-salesforce](https://github.com/rahul10cs/mcp-salesforce) — the Salesforce MCP Server (data layer)

---

## Full System Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                           Browser                                │
│   static/index.html — 3-screen UI (SF Login → API Key → Chat)   │
│                                                                  │
│   Screen 1: "Login with Salesforce" button                       │
│   Screen 2: Anthropic API key input form                         │
│   Screen 3: Chat interface                                       │
│                                                                  │
│   On chat send:  POST /chat  { "message": "..." }                │
└──────────────────────┬───────────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────────────┐
│              THIS REPO — app.py (port 3000)                      │
│                                                                  │
│  Auth endpoints:                                                 │
│    GET  /auth/salesforce          → redirect to SF OAuth         │
│    GET  /auth/salesforce/callback → exchange code for token      │
│    POST /set-api-key              → store Anthropic key          │
│    GET  /auth/status              → check login state            │
│    POST /auth/logout              → clear session                │
│                                                                  │
│  Chat endpoint:                                                  │
│    POST /chat                                                    │
│      1. Read SF token + Anthropic key from session               │
│      2. Open SSE connection to mcp-salesforce (with SF token)    │
│      3. List Salesforce tools                                    │
│      4. Send message + tools to Claude API                       │
│      5. Execute tool calls → forward to mcp-salesforce           │
│      6. Return Claude's final answer                             │
└──────────────────────┬───────────────────────────────────────────┘
                       │  GET /sse?sf_token=<token>&sf_instance=<url>
                       ▼
┌──────────────────────────────────────────────────────────────────┐
│         mcp-salesforce — server.py (port 8000)                   │
│   Receives user's SF token via URL param                         │
│   Exposes: get_accounts, get_contacts, get_opportunities         │
│            get_cases, run_soql, get_org_info                     │
└──────────────────────┬───────────────────────────────────────────┘
                       │  REST API  (Bearer <sf_token>)
                       ▼
┌──────────────────────────────────────────────────────────────────┐
│                   Salesforce Org                                 │
│              Live CRM data via REST API                          │
└──────────────────────────────────────────────────────────────────┘
```

---

## Login Flow — 3 Screens

When the browser opens `http://localhost:3000`, the UI checks `/auth/status` and shows the correct screen:

```
Browser loads /
      │
      ▼
GET /auth/status
      │
      ├── sf_authenticated: false  →  Screen 1: Salesforce Login
      │
      ├── sf_authenticated: true
      │   anthropic_configured: false  →  Screen 2: API Key Input
      │
      └── both true  →  Screen 3: Chat
```

### Screen 1 — Salesforce OAuth (Authorization Code Flow)

```
User clicks "Login with Salesforce"
      │
      ▼
GET /auth/salesforce
  → generates state token (CSRF protection)
  → stores state in session
  → redirects browser to:
    https://login.salesforce.com/services/oauth2/authorize
      ?response_type=code
      &client_id=<SF_CLIENT_ID>
      &redirect_uri=http://localhost:3000/auth/salesforce/callback
      &state=<random>
      &scope=api+refresh_token
      │
      ▼ (user logs in on Salesforce)
      │
      ▼
GET /auth/salesforce/callback?code=<auth_code>&state=<state>
  → validates state (prevents CSRF)
  → exchanges code for token:
      POST https://login.salesforce.com/services/oauth2/token
        grant_type=authorization_code
        code=<auth_code>
        client_id=<SF_CLIENT_ID>
        client_secret=<SF_CLIENT_SECRET>
        redirect_uri=<callback>
  → stores { sf_token, sf_instance_url } in session
  → redirects to /  →  shows Screen 2
```

**Code responsible:**

```python
# app.py — Step 1: redirect to Salesforce
@app.get("/auth/salesforce")
async def salesforce_login(request: Request, response: Response):
    state = secrets.token_urlsafe(16)
    session["oauth_state"] = state
    params = {
        "response_type": "code",
        "client_id":     SF_CLIENT_ID,
        "redirect_uri":  SF_CALLBACK_URL,
        "state":         state,
        "scope":         "api refresh_token",
    }
    auth_url = f"{SF_LOGIN_URL}/services/oauth2/authorize?{urlencode(params)}"
    return RedirectResponse(url=auth_url)

# app.py — Step 2: Salesforce redirects back here
@app.get("/auth/salesforce/callback")
async def salesforce_callback(request: Request):
    code  = request.query_params.get("code")
    state = request.query_params.get("state")
    # validate state, exchange code for token...
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{SF_LOGIN_URL}/services/oauth2/token",
            data={
                "grant_type":    "authorization_code",
                "code":          code,
                "client_id":     SF_CLIENT_ID,
                "client_secret": SF_CLIENT_SECRET,
                "redirect_uri":  SF_CALLBACK_URL,
            },
        )
    token_data = resp.json()
    session["sf_token"]        = token_data["access_token"]
    session["sf_instance_url"] = token_data["instance_url"]
    return RedirectResponse(url="/")
```

### Screen 2 — Anthropic API Key

Anthropic does not offer an OAuth login flow — there is no "Login with Anthropic" equivalent. The Anthropic API uses API keys only, so the user pastes their key from [console.anthropic.com](https://console.anthropic.com) → API Keys.

```
User pastes API key → clicks Save
      │
      ▼
POST /set-api-key  { "api_key": "sk-ant-..." }
  → stores anthropic_key in session
  → redirects to Screen 3: Chat
```

**Code responsible:**

```python
@app.post("/set-api-key")
async def set_api_key(req: ApiKeyRequest, request: Request):
    session = get_session(request)
    session["anthropic_key"] = req.api_key.strip()
    return {"ok": True}
```

### Screen 3 — Chat

The full chat interface. A "Disconnect" button in the header calls `POST /auth/logout` which clears the session cookie and redirects back to Screen 1.

---

## Session Management

Sessions are stored in an **in-memory Python dict** keyed by a random session token stored in a browser cookie.

```python
# app.py
SESSION_COOKIE = "mcp_session"
sessions: dict = {}   # { session_id: { sf_token, sf_instance_url, anthropic_key } }

def get_session(request: Request) -> dict | None:
    sid = request.cookies.get(SESSION_COOKIE)
    return sessions.get(sid) if sid else None
```

Each session holds:
| Key | Value |
|---|---|
| `sf_token` | Salesforce access token from OAuth callback |
| `sf_instance_url` | Salesforce org URL (e.g. `https://orgfarm-xxx.my.salesforce.com`) |
| `anthropic_key` | Anthropic API key entered by the user |
| `oauth_state` | Temporary CSRF token (removed after callback) |

> **Note:** In-memory sessions are lost when the server restarts. For production, use Redis or a database-backed session store.

---

## Chat Request Flow (Screen 3)

Once logged in, every chat message goes through this flow:

### Step 1 — User types and hits Send

```javascript
// static/index.html
async function sendMessage() {
    const text = inputEl.value.trim();
    addMessage("user", text);   // show in chat immediately
    addTyping();                // show "Thinking..."

    const res = await fetch("/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text }),
    });
    const data = await res.json();
    removeTyping();
    addMessage("bot", data.reply);
}
```

---

### Step 2 — FastAPI reads session credentials

```python
@app.post("/chat")
async def chat(req: ChatRequest, request: Request):
    session = get_session(request)   # ← read session cookie

    sf_token        = session["sf_token"]         # user's Salesforce access token
    sf_instance_url = session["sf_instance_url"]  # user's org URL
    claude          = anthropic.Anthropic(api_key=session["anthropic_key"])
```

---

### Step 3 — Open SSE connection to mcp-salesforce, passing SF token

> **This is where mcp-chat calls mcp-salesforce.** See [mcp-salesforce → Per-Session Token Injection](https://github.com/rahul10cs/mcp-salesforce#per-session-token-injection) for how the server receives and stores it.

```python
# Pass the user's SF token via URL query params so mcp-salesforce can call Salesforce as this user
mcp_url = f"{MCP_SERVER_URL}?sf_token={sf_token}&sf_instance={sf_instance_url}"

async with sse_client(url=mcp_url) as (read, write):   # ← GET /sse?sf_token=...
    async with ClientSession(read, write) as mcp_session:
        await mcp_session.initialize()   # MCP handshake
```

---

### Step 4 — Fetch available tools

> See [mcp-salesforce → Step 3: List Tools](https://github.com/rahul10cs/mcp-salesforce#step-3--client-lists-available-tools).

```python
tools_response = await mcp_session.list_tools()   # ← POST /messages/?session_id=<id>  { "method": "tools/list" }
tools = [
    {
        "name":         t.name,
        "description":  t.description or "",
        "input_schema": to_dict(t.inputSchema),
    }
    for t in tools_response.tools
]
```

---

### Step 5 — Send message + tools to Claude API

```python
messages = [{"role": "user", "content": req.message}]

response = claude.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=1024,
    tools=tools,      # Claude now knows what Salesforce tools exist
    messages=messages,
)
```

Claude decides: answer from knowledge, or call a tool?

---

### Step 6 — Tool call loop

If Claude responds with `stop_reason = "tool_use"`:

```json
{
  "stop_reason": "tool_use",
  "content": [
    { "type": "text", "text": "Let me look that up." },
    { "type": "tool_use", "id": "toolu_01...", "name": "get_accounts", "input": { "limit": 10 } }
  ]
}
```

Claude may call **multiple tools at once** (e.g. "show accounts and cases" → both called in one response):

```python
while response.stop_reason == "tool_use":
    tool_blocks = [b for b in response.content if b.type == "tool_use"]

    tool_results = []
    for tool_block in tool_blocks:
        # ↓ This calls mcp-salesforce — see https://github.com/rahul10cs/mcp-salesforce#step-5--mcp-client-sends-the-tool-call-to-this-server
        tool_result = await mcp_session.call_tool(   # ← POST /messages/?session_id=<id>  { "method": "tools/call" }
            tool_block.name,
            dict(tool_block.input),
        )
        tool_results.append({
            "type":        "tool_result",
            "tool_use_id": tool_block.id,   # must match Claude's tool_use id
            "content":     tool_result.content[0].text,
        })

    # ALL tool results must go in one user message — API rejects split results
    messages.append({"role": "assistant", "content": [to_dict(b) for b in response.content]})
    messages.append({"role": "user", "content": tool_results})

    response = claude.messages.create(model="claude-sonnet-4-6", max_tokens=1024, tools=tools, messages=messages)
```

Message history after one tool call:
```
messages = [
  { "role": "user",      "content": "show me my accounts" },
  { "role": "assistant", "content": [ <text>, <tool_use: get_accounts> ] },
  { "role": "user",      "content": [ <tool_result: "Edge Communications | ..."> ] }
]
```

---

### Step 7 — Return final answer

```python
final_text = next(
    (b.text for b in response.content if hasattr(b, "text")),
    "Sorry, I could not generate a response.",
)
return {"reply": final_text}   # → browser renders it in chat
```

---

### Step 8 — Error handling

```python
# Inner: Anthropic API errors (bad key, low credits, rate limit)
except anthropic.APIStatusError as e:
    return {"reply": friendly_api_error(e)}

# Outer: everything else (MCP connection failure, network error)
except Exception as e:
    print(traceback.format_exc())
    return {"reply": f"Error: {str(e)}"}
```

---

## Chat UI — `static/index.html`

Pure HTML + CSS + vanilla JS, no framework. Uses a state machine to switch between screens:

```javascript
async function init() {
    const res  = await fetch("/auth/status");
    const data = await res.json();

    if (!data.sf_authenticated)       showScreen("sf-login");   // Screen 1
    else if (!data.anthropic_configured) showScreen("api-key"); // Screen 2
    else                              showScreen("chat");        // Screen 3
}
```

| Screen | ID | Shown when |
|---|---|---|
| Salesforce Login | `screen-sf-login` | Not logged into Salesforce |
| API Key Input | `screen-api-key` | Salesforce connected, no Anthropic key |
| Chat | `screen-chat` | Both configured |

The UI is served by FastAPI:
```python
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def index():
    return FileResponse("static/index.html")
```

---

## Project Structure

```
mcp-chat/
├── app.py              # FastAPI backend — OAuth, sessions, MCP client loop
├── static/
│   └── index.html      # 3-screen UI: SF login → API key → chat
├── requirements.txt    # Python dependencies
├── render.yaml         # Deployment config for Render.com
├── .env.example        # Template for environment variables
└── .gitignore
```

---

## Local Setup

### Prerequisites
- Python 3.10+
- [mcp-salesforce](https://github.com/rahul10cs/mcp-salesforce) running on port 8000
- A Salesforce Connected App with Authorization Code flow + callback URL configured

### Connected App Setup in Salesforce
1. **Setup → App Manager → New Connected App**
2. Enable OAuth Settings → tick **Authorization Code and Credentials Flow**
3. Add Callback URL: `http://localhost:3000/auth/salesforce/callback`
4. Add scopes: `api`, `refresh_token`
5. Uncheck **Require Proof Key for Code Exchange (PKCE)**
6. Save and copy **Consumer Key** and **Consumer Secret**

### Install & Run

```bash
git clone https://github.com/rahul10cs/mcp-chat.git
cd mcp-chat

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Fill in SF_CLIENT_ID, SF_CLIENT_SECRET in .env

python app.py
```

Open `http://localhost:3000`.

### Start Order

```bash
# Terminal 1 — MCP Server (must start first)
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
| `SF_CLIENT_ID` | Connected App Consumer Key |
| `SF_CLIENT_SECRET` | Connected App Consumer Secret |
| `SF_LOGIN_URL` | Salesforce login URL (default: `https://login.salesforce.com`) |
| `SF_CALLBACK_URL` | OAuth callback URL (default: `http://localhost:3000/auth/salesforce/callback`) |
| `MCP_SERVER_URL` | URL of the running MCP server (default: `http://localhost:8000/sse`) |
| `PORT` | Port for this server (default: `3000`) |

> **ANTHROPIC_API_KEY** is not in `.env` — users enter it via the UI.

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

A `render.yaml` is included. Set the environment variables in the Render dashboard and update `SF_CALLBACK_URL` to your deployed URL. Set `MCP_SERVER_URL` to the deployed URL of [mcp-salesforce](https://github.com/rahul10cs/mcp-salesforce).

---

## Dependencies

```
fastapi>=0.104.0        # Web framework + request routing
uvicorn>=0.24.0         # ASGI server
anthropic>=0.34.0       # Claude API client
mcp>=1.0.0              # Model Context Protocol SDK (client side)
python-dotenv>=1.0.0    # Loads .env files
httpx>=0.25.0           # Async HTTP client — OAuth token exchange + MCP SDK
httpx-sse>=0.4.0        # SSE support for httpx
```
