#!/usr/bin/env python3
"""
Chat Backend — MCP Client
Receives messages from the chat window, calls Claude API,
executes tools on the Salesforce MCP Server, returns the answer.
"""

import json
import os
import traceback

import anthropic
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from mcp import ClientSession
from mcp.client.sse import sse_client
from pydantic import BaseModel


def friendly_api_error(e: anthropic.APIStatusError) -> str:
    """Convert Anthropic API errors into readable messages."""
    try:
        msg = e.response.json().get("error", {}).get("message", str(e))
    except Exception:
        msg = str(e)
    if "credit balance is too low" in msg:
        return "Your Anthropic API credit balance is too low. Please add credits at console.anthropic.com → Plans & Billing."
    if e.status_code == 401:
        return "Invalid Anthropic API key. Check your ANTHROPIC_API_KEY in .env."
    if e.status_code == 429:
        return "Anthropic API rate limit hit. Please wait a moment and try again."
    return f"Anthropic API error ({e.status_code}): {msg}"

load_dotenv()

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

claude         = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
MCP_SERVER_URL = os.getenv("MCP_SERVER_URL")  # e.g. http://localhost:8000/sse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def to_dict(obj):
    """Convert a Pydantic model or dict to a plain dict (for JSON serialization)."""
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "dict"):
        return obj.dict()
    # fallback: round-trip through JSON
    return json.loads(json.dumps(obj, default=str))


# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str


# ---------------------------------------------------------------------------
# /chat endpoint — the core MCP client loop
# ---------------------------------------------------------------------------

@app.post("/chat")
async def chat(req: ChatRequest):
    try:
        async with sse_client(url=MCP_SERVER_URL) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # ── Step 1: List tools from MCP Server ──────────────────────
                tools_response = await session.list_tools()
                tools = [
                    {
                        "name": t.name,
                        "description": t.description or "",
                        "input_schema": to_dict(t.inputSchema),
                    }
                    for t in tools_response.tools
                ]

                # ── Step 2: Send user message + tools to Claude ──────────────
                messages = [{"role": "user", "content": req.message}]

                try:
                    response = claude.messages.create(
                        model="claude-sonnet-4-6",
                        max_tokens=1024,
                        tools=tools,
                        messages=messages,
                    )

                    # ── Step 3: Tool call loop ────────────────────────────────
                    while response.stop_reason == "tool_use":
                        tool_block = next(
                            b for b in response.content if b.type == "tool_use"
                        )

                        # Execute the tool on the MCP Server
                        tool_result = await session.call_tool(
                            tool_block.name,
                            dict(tool_block.input),
                        )

                        result_text = (
                            tool_result.content[0].text
                            if tool_result.content
                            else "No data returned."
                        )

                        # Serialize assistant response blocks for the API
                        assistant_content = [
                            to_dict(b) if not isinstance(b, dict) else b
                            for b in response.content
                        ]

                        messages.append({"role": "assistant", "content": assistant_content})
                        messages.append({
                            "role": "user",
                            "content": [{
                                "type": "tool_result",
                                "tool_use_id": tool_block.id,
                                "content": result_text,
                            }],
                        })

                        response = claude.messages.create(
                            model="claude-sonnet-4-6",
                            max_tokens=1024,
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
        print(traceback.format_exc())   # full error in your terminal
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
