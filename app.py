#!/usr/bin/env python3
"""
Chat Backend — MCP Client
Handles Salesforce OAuth login, Anthropic API key entry, and the MCP tool-call loop.
"""

import json
import os
import secrets
import traceback
from urllib.parse import urlencode

import anthropic
import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from mcp import ClientSession
from mcp.client.sse import sse_client
from pydantic import BaseModel

load_dotenv()

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

MCP_SERVER_URL   = os.getenv("MCP_SERVER_URL", "http://localhost:8000/sse")
SF_CLIENT_ID     = os.getenv("SF_CLIENT_ID")
SF_CLIENT_SECRET = os.getenv("SF_CLIENT_SECRET")
SF_LOGIN_URL     = os.getenv("SF_LOGIN_URL", "https://login.salesforce.com")
SF_CALLBACK_URL  = os.getenv("SF_CALLBACK_URL", "http://localhost:3000/auth/salesforce/callback")

SESSION_COOKIE = "mcp_session"

# In-memory session store: { session_id: { sf_token, sf_instance_url, anthropic_key, oauth_state } }
sessions: dict = {}


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def get_session(request: Request) -> dict | None:
    sid = request.cookies.get(SESSION_COOKIE)
    return sessions.get(sid) if sid else None


def require_session(request: Request, response: Response) -> tuple[str, dict]:
    """Return existing session or create a new one."""
    sid = request.cookies.get(SESSION_COOKIE)
    if sid and sid in sessions:
        return sid, sessions[sid]
    sid = secrets.token_urlsafe(32)
    sessions[sid] = {}
    response.set_cookie(SESSION_COOKIE, sid, httponly=True, samesite="lax")
    return sid, sessions[sid]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def to_dict(obj):
    """Convert Pydantic model or dict to plain dict (for JSON serialization)."""
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "dict"):
        return obj.dict()
    return json.loads(json.dumps(obj, default=str))


def friendly_api_error(e: anthropic.APIStatusError) -> str:
    try:
        msg = e.response.json().get("error", {}).get("message", str(e))
    except Exception:
        msg = str(e)
    if "credit balance is too low" in msg:
        return "Your Anthropic API credit balance is too low. Please add credits at console.anthropic.com → Plans & Billing."
    if e.status_code == 401:
        return "Invalid Anthropic API key. Please re-enter your key."
    if e.status_code == 429:
        return "Anthropic API rate limit hit. Please wait a moment and try again."
    return f"Anthropic API error ({e.status_code}): {msg}"


# ---------------------------------------------------------------------------
# Auth status — used by UI to decide which screen to show
# ---------------------------------------------------------------------------

@app.get("/auth/status")
async def auth_status(request: Request):
    session = get_session(request)
    return {
        "sf_authenticated":     bool(session and session.get("sf_token")),
        "anthropic_configured": bool(session and session.get("anthropic_key")),
    }


# ---------------------------------------------------------------------------
# Salesforce OAuth — Authorization Code Flow
# Step 1: redirect browser to Salesforce login
# ---------------------------------------------------------------------------

@app.get("/auth/salesforce")
async def salesforce_login(request: Request, response: Response):
    sid, session = require_session(request, response)

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

    redirect = RedirectResponse(url=auth_url)
    redirect.set_cookie(SESSION_COOKIE, sid, httponly=True, samesite="lax")
    return redirect


# ---------------------------------------------------------------------------
# Salesforce OAuth — Authorization Code Flow
# Step 2: Salesforce redirects back here with ?code=...
# ---------------------------------------------------------------------------

@app.get("/auth/salesforce/callback")
async def salesforce_callback(request: Request):
    code  = request.query_params.get("code")
    state = request.query_params.get("state")
    error = request.query_params.get("error")

    if error:
        return RedirectResponse(url=f"/?error={error}")

    sid = request.cookies.get(SESSION_COOKIE)
    session = sessions.get(sid)

    if not session:
        return RedirectResponse(url="/?error=session_missing")

    # Validate state to prevent CSRF
    if state != session.get("oauth_state"):
        return RedirectResponse(url="/?error=invalid_state")

    # Exchange authorization code for access token
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

    if resp.status_code != 200:
        err = resp.json().get("error_description", "token_exchange_failed")
        return RedirectResponse(url=f"/?error={err}")

    token_data = resp.json()
    session["sf_token"]        = token_data["access_token"]
    session["sf_instance_url"] = token_data["instance_url"]
    session.pop("oauth_state", None)

    redirect = RedirectResponse(url="/")
    redirect.set_cookie(SESSION_COOKIE, sid, httponly=True, samesite="lax")
    return redirect


# ---------------------------------------------------------------------------
# Anthropic API key — user submits via UI form
# ---------------------------------------------------------------------------

class ApiKeyRequest(BaseModel):
    api_key: str


@app.post("/set-api-key")
async def set_api_key(req: ApiKeyRequest, request: Request):
    session = get_session(request)
    if not session:
        return JSONResponse({"error": "No session found. Please refresh and try again."}, status_code=401)
    if not session.get("sf_token"):
        return JSONResponse({"error": "Please connect Salesforce first."}, status_code=401)

    session["anthropic_key"] = req.api_key.strip()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Logout — clears session and cookie
# ---------------------------------------------------------------------------

@app.post("/auth/logout")
async def logout(request: Request, response: Response):
    sid = request.cookies.get(SESSION_COOKIE)
    if sid:
        sessions.pop(sid, None)
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


# ---------------------------------------------------------------------------
# /chat endpoint — the core MCP client loop
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str


@app.post("/chat")
async def chat(req: ChatRequest, request: Request):
    session = get_session(request)

    if not session or not session.get("sf_token"):
        return {"reply": "You are not connected to Salesforce. Please log in first."}
    if not session.get("anthropic_key"):
        return {"reply": "No Anthropic API key set. Please enter your API key first."}

    sf_token        = session["sf_token"]
    sf_instance_url = session["sf_instance_url"]
    claude          = anthropic.Anthropic(api_key=session["anthropic_key"])

    # Pass SF token to mcp-salesforce via query params so it can call Salesforce as this user
    mcp_url = f"{MCP_SERVER_URL}?sf_token={sf_token}&sf_instance={sf_instance_url}"

    try:
        async with sse_client(url=mcp_url) as (read, write):
            async with ClientSession(read, write) as mcp_session:
                await mcp_session.initialize()

                # ── Step 1: List tools from MCP Server ──────────────────────
                tools_response = await mcp_session.list_tools()
                tools = [
                    {
                        "name":         t.name,
                        "description":  t.description or "",
                        "input_schema": to_dict(t.inputSchema),
                    }
                    for t in tools_response.tools
                ]

                # ── Step 2: Send user message + tools to Claude ──────────────
                messages = [{"role": "user", "content": req.message}]

                system_prompt = """You are a Salesforce AI assistant. You help users query their Salesforce org using natural language.

When answering questions about Salesforce data, always use the run_soql tool to build a dynamic SOQL query based exactly on what the user is asking.
Do NOT use the predefined tools (get_accounts, get_contacts, etc.) unless the user asks for a simple default list with no specific fields or filters.

Rules for building SOQL:
- Only use SELECT statements
- Select only the fields relevant to the user's question — do not always default to the same fields
- Add WHERE clauses, ORDER BY, and LIMIT based on what the user asks
- If the user asks for "all" records, use LIMIT 200 at most
- If the user says "show me X", figure out the right object and fields for X
- Use relationships where needed e.g. Account.Name on Contact, Opportunity

Common Salesforce objects and their key fields:
- Account: Id, Name, Industry, Phone, Website, AnnualRevenue, BillingCity, BillingCountry, Type, OwnerId
- Contact: Id, FirstName, LastName, Email, Phone, Title, Department, AccountId, Account.Name
- Opportunity: Id, Name, StageName, Amount, CloseDate, Probability, AccountId, Account.Name, OwnerId
- Case: Id, CaseNumber, Subject, Status, Priority, Origin, AccountId, Account.Name, ContactId
- Lead: Id, FirstName, LastName, Email, Phone, Company, Status, Industry, LeadSource
- Task: Id, Subject, Status, Priority, ActivityDate, WhoId, WhatId
- User: Id, Name, Email, Title, Department, IsActive
- Organization: Id, Name, OrganizationType, IsSandbox

Example mappings:
- "show me high priority cases" → SELECT Id, CaseNumber, Subject, Status, Priority, Account.Name FROM Case WHERE Priority = 'High' ORDER BY CreatedDate DESC LIMIT 20
- "list contacts at Acme" → SELECT Id, FirstName, LastName, Email, Phone FROM Contact WHERE Account.Name LIKE '%Acme%' LIMIT 20
- "opportunities closing this month" → SELECT Id, Name, StageName, Amount, CloseDate FROM Opportunity WHERE CloseDate = THIS_MONTH ORDER BY CloseDate ASC LIMIT 20
- "who are my top 5 accounts by revenue" → SELECT Id, Name, AnnualRevenue FROM Account WHERE AnnualRevenue != null ORDER BY AnnualRevenue DESC LIMIT 5
"""

                try:
                    response = claude.messages.create(
                        model="claude-sonnet-4-6",
                        max_tokens=1024,
                        system=system_prompt,
                        tools=tools,
                        messages=messages,
                    )

                    # ── Step 3: Tool call loop ────────────────────────────────
                    while response.stop_reason == "tool_use":
                        # Collect ALL tool_use blocks — Claude may call more than one at once
                        tool_blocks = [b for b in response.content if b.type == "tool_use"]

                        tool_results = []
                        for tool_block in tool_blocks:
                            tool_result = await mcp_session.call_tool(
                                tool_block.name,
                                dict(tool_block.input),
                            )
                            result_text = (
                                tool_result.content[0].text
                                if tool_result.content
                                else "No data returned."
                            )
                            tool_results.append({
                                "type":        "tool_result",
                                "tool_use_id": tool_block.id,
                                "content":     result_text,
                            })

                        assistant_content = [
                            to_dict(b) if not isinstance(b, dict) else b
                            for b in response.content
                        ]
                        messages.append({"role": "assistant", "content": assistant_content})
                        messages.append({"role": "user", "content": tool_results})

                        response = claude.messages.create(
                            model="claude-sonnet-4-6",
                            max_tokens=1024,
                            system=system_prompt,
                            tools=tools,
                            messages=messages,
                        )

                    # ── Step 4: Extract final text ──────────────────────────
                    final_text = next(
                        (b.text for b in response.content if hasattr(b, "text")),
                        "Sorry, I could not generate a response.",
                    )
                    return {"reply": final_text}

                except anthropic.APIStatusError as e:
                    return {"reply": friendly_api_error(e)}

    except Exception as e:
        print(traceback.format_exc())
        return {"reply": f"Error: {str(e)}"}


# ---------------------------------------------------------------------------
# Serve the chat UI
# ---------------------------------------------------------------------------

@app.get("/")
async def index():
    return FileResponse("static/index.html")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", 3000))
    uvicorn.run(app, host="0.0.0.0", port=port)
