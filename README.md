# lionbank-wealth

A ChatGPT app (MCP server) that answers market and wealth-management questions
using only the insights published by HSBC Private Banking at
[privatebanking.hsbc.com](https://www.privatebanking.hsbc.com/). Once connected
to ChatGPT in Developer mode, users enable `lionbank-wealth` from the composer's
tools menu and ask a question directly in chat.

## How it works

The server exposes a single MCP tool, `lionbank_wealth_insight(query)`. It makes one
call to OpenAI's Responses API with the built-in `web_search` tool, restricted via
`filters.allowed_domains` to `privatebanking.hsbc.com`. The model searches that
site, then answers the user's question from what it finds, citing the specific
page(s) used. If nothing relevant exists on the site, it says so instead of
falling back on outside knowledge.

Built with [FastMCP](https://gofastmcp.com) (Python) and served over the MCP
Streamable HTTP transport, which is what lets it run as a normal HTTP container
on Cloud Run.

## Project layout

| File | Purpose |
|---|---|
| `server.py` | The MCP server and the `lionbank_wealth_insight` tool |
| `requirements.txt` | Python dependencies (`fastmcp<3`, `openai`) |
| `Dockerfile` | Container image for Cloud Run |
| `.env.example` | Required environment variables for local runs |

## Configuration

| Variable | Required | Description |
|---|---|---|
| `OPENAI_API_KEY` | yes | OpenAI API key used server-side for search + answering |
| `OPENAI_MODEL` | no (default `gpt-5.6-luna`) | Model used for the Responses API call — must support the `web_search` tool |
| `PORT` | no (default `8080`) | Port the HTTP server binds to (Cloud Run sets this automatically) |

## Run locally

```bash
pip install -r requirements.txt
OPENAI_API_KEY=sk-your-key python server.py
```

The server listens on `http://0.0.0.0:8080/mcp`.

## Test locally

**Quick structural check** (no OpenAI call):
```bash
fastmcp inspect server.py
```

**Call the tool against a running server:**
```bash
fastmcp call http://127.0.0.1:8080/mcp lionbank_wealth_insight query="What is HSBC's outlook on gold?"
```

**Interactive browser UI:**
```bash
fastmcp dev inspector server.py
```

**Test the real ChatGPT connection flow before deploying**, tunnel the local
server with a public HTTPS URL:
```bash
ngrok http 8080
```
then use the printed `https://....ngrok-free.app/mcp` URL as the MCP server URL
when adding the connection in ChatGPT (see below).

## Deploy to Cloud Run

```bash
gcloud config set project YOUR_PROJECT_ID
gcloud services enable run.googleapis.com cloudbuild.googleapis.com secretmanager.googleapis.com

# store the OpenAI key as a secret rather than a plain env var
printf '%s' 'sk-...' | gcloud secrets create openai-api-key --data-file=-

gcloud run deploy lionbank-wealth \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --set-secrets=OPENAI_API_KEY=openai-api-key:latest \
  --set-env-vars=OPENAI_MODEL=gpt-5.6-luna \
  --min-instances=0 \
  --max-instances=2
```

- `--allow-unauthenticated` is required — ChatGPT calls this endpoint directly and
  this server doesn't implement OAuth, so Cloud Run's IAM auth would block it.
- `--min-instances=0` means the service scales to zero when idle (no cost while
  unused), at the cost of a cold start on the first request after idling.
- `--max-instances=2` caps how far it can scale under load, which also caps
  worst-case cost since there's no auth to stop someone from hammering the
  endpoint (see [Known limitations](#known-limitations)).
- The MCP endpoint is the printed Cloud Run service URL + `/mcp`.

Verify the deployment:
```bash
curl -s https://YOUR-SERVICE-URL/mcp -X POST \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'
```

## Connect it in ChatGPT

1. **Settings → Security and login → turn on Developer mode.** (Availability
   depends on account/workspace policy.)
2. Go to [chatgpt.com/plugins](https://chatgpt.com/plugins) and select the
   **+** button to add a server.
3. Enter a user-facing name (`lionbank-wealth`) and description.
4. Under **Connection**, choose the public-endpoint option and enter the MCP
   server URL, including the `/mcp` path: `https://YOUR-SERVICE-URL/mcp`.
5. Select **Create the connection** — ChatGPT connects and lists the tools it
   discovered (`lionbank_wealth_insight`). No authentication step is needed
   since this server doesn't require any.
6. Start a new conversation, open the composer's **tools menu**, enable the
   `lionbank-wealth` connection, and ask your question.

If ChatGPT can't connect, verify the public HTTPS endpoint with MCP Inspector
first (see [Test locally](#test-locally)) before retrying step 5.

This makes the app usable from your own ChatGPT account only (Developer mode).
Making it installable/discoverable for other users requires packaging it as a
plugin and submitting it through OpenAI's review process separately.

## Known limitations

- No auth on the endpoint — anyone with the URL can call it and consume your
  OpenAI quota. `--max-instances=2` caps concurrent scale, but set a usage
  budget/alert on the OpenAI project too — it's the real backstop on cost.
- `web_search` calls have their own OpenAI API cost, separate from model tokens.
