import os

import uvicorn
from dotenv import load_dotenv
from fastmcp import FastMCP
from openai import OpenAI
from starlette.middleware.cors import CORSMiddleware

load_dotenv()

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.6-luna")
ALLOWED_DOMAIN = "privatebanking.hsbc.com"

client = OpenAI(api_key=OPENAI_API_KEY)

mcp = FastMCP("lionbank-wealth")

SYSTEM_INSTRUCTIONS = f"""You are LionBank Wealth, an assistant that answers questions
using only the market and wealth insights published by HSBC Private Banking at
{ALLOWED_DOMAIN}. Search that site for pages relevant to the user's question, then
answer concisely based on what you find there. Always cite the specific HSBC page(s)
you used (title and URL). If you cannot find anything relevant on that site, say so
plainly instead of guessing or falling back on outside knowledge."""


@mcp.tool
def lionbank_wealth_insight(query: str) -> str:
    """Answer a market, economic, or wealth-management question using HSBC Private
    Banking's published insights (privatebanking.hsbc.com). Use this whenever the
    user asks what HSBC thinks, forecasts, or has published about markets, economies,
    investment themes, or wealth strategy."""
    response = client.responses.create(
        model=OPENAI_MODEL,
        instructions=SYSTEM_INSTRUCTIONS,
        input=query,
        tools=[
            {
                "type": "web_search",
                "filters": {"allowed_domains": [ALLOWED_DOMAIN]},
            }
        ],
    )
    return response.output_text


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app = mcp.http_app()
    # CORS is needed for browser-based MCP clients (e.g. MCP Inspector); ChatGPT
    # itself calls this server-to-server and doesn't require it, but it's
    # harmless to leave on in production. add_middleware() (rather than passing
    # middleware= into http_app()) is required so this runs outermost, ahead of
    # FastMCP's own request-context middleware which otherwise 405s OPTIONS first.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=[
            "mcp-protocol-version",
            "mcp-session-id",
            "Authorization",
            "Content-Type",
        ],
        expose_headers=["mcp-session-id"],
    )
    uvicorn.run(app, host="0.0.0.0", port=port)
