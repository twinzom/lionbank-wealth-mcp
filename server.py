import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

import httpx
import uvicorn
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.tools.tool import ToolResult
from openai import OpenAI
from pydantic import BaseModel, Field
from starlette.middleware.cors import CORSMiddleware

load_dotenv()

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.6-luna")
ALLOWED_DOMAIN = "privatebanking.hsbc.com"

client = OpenAI(api_key=OPENAI_API_KEY)

# HSBC's own og:image/twitter:image tags are a generic site-wide logo, not a
# per-article photo, so we pull the article's hero image out of the page body
# instead. This id/class is what HSBC's article template currently renders it
# with — it's a scrape, not a stable API, so any failure just means no image
# rather than a broken response.
_HERO_IMAGE_PATTERN = re.compile(
    r'<img\b[^>]*\bclass="[^"]*smart-image-img[^"]*"[^>]*\bsrc="([^"]+)"'
    r'|<img\b[^>]*\bsrc="([^"]+)"[^>]*\bclass="[^"]*smart-image-img[^"]*"',
    re.IGNORECASE,
)


def _fetch_hero_image(page_url: str) -> str | None:
    try:
        resp = httpx.get(
            page_url,
            timeout=5.0,
            headers={"User-Agent": "Mozilla/5.0"},
            follow_redirects=True,
        )
        resp.raise_for_status()
    except httpx.HTTPError:
        return None

    match = _HERO_IMAGE_PATTERN.search(resp.text)
    if not match:
        return None
    return urljoin(page_url, match.group(1) or match.group(2))


CAMPAIGNS_PATH = os.path.join(os.path.dirname(__file__), "campaigns.json")

with open(CAMPAIGNS_PATH, "r") as _f:
    CAMPAIGNS: list[dict] = json.load(_f)

CAMPAIGNS_BY_ID = {c["id"]: c for c in CAMPAIGNS}

mcp = FastMCP("lionbank-wealth")

SYSTEM_INSTRUCTIONS = f"""You are LionBank Wealth, an assistant that answers questions
using only the market and wealth insights published by HSBC Private Banking at
{ALLOWED_DOMAIN}. Search that site for pages relevant to the user's question. Every
point in your answer must be grounded in a distinct page you found there, with its
exact title and URL. If you cannot find anything relevant on that site, return an
empty points list and use the summary to say so plainly instead of guessing or
falling back on outside knowledge."""


class InsightPoint(BaseModel):
    subtitle: str = Field(
        description="A short, specific headline for this point, under 8 words, "
        "e.g. 'Gold outlook stays constructive'."
    )
    body: str = Field(
        description="1-3 sentence explanation of this point, written for a "
        "private banking client."
    )
    citation_title: str = Field(
        description="The title of the HSBC Private Banking page this point is drawn from."
    )
    citation_url: str = Field(
        description=f"The exact URL of the source page on {ALLOWED_DOMAIN}."
    )


class InsightResponse(BaseModel):
    summary: str = Field(
        description="A 1-2 sentence executive summary answering the user's "
        "question at a glance."
    )
    points: list[InsightPoint] = Field(
        description="3 to 6 key points supporting the summary, each grounded in "
        "a distinct HSBC Private Banking page. Empty if nothing relevant was found."
    )


CAMPAIGN_MATCH_INSTRUCTIONS = """You match a user's wealth/investment question, and the
answer they were just given, against a fixed list of HSBC marketing campaigns. Pick the
single campaign that is most genuinely relevant to what the user is asking about or
what the answer discussed - e.g. a question about interest rates or savings connects to
a time deposit campaign, a question about stock trading connects to a brokerage
commission campaign. Only pick a campaign if there's a clear, specific connection. If
nothing fits well, return null rather than forcing an unrelated match."""


class CampaignMatch(BaseModel):
    campaign_id: str | None = Field(
        description="The id of the single most relevant campaign from the provided "
        "list, or null if none are clearly relevant to the query or answer."
    )


def _match_campaign(query: str, summary: str) -> dict | None:
    if not CAMPAIGNS:
        return None

    catalog = "\n".join(
        f"- id: {c['id']} | {c['title']} — {c['teaser']} "
        f"(relevant when the user asks about {c['relevance_hint']})"
        for c in CAMPAIGNS
    )
    response = client.responses.parse(
        model=OPENAI_MODEL,
        instructions=CAMPAIGN_MATCH_INSTRUCTIONS,
        input=f"User's question:\n{query}\n\nAnswer given:\n{summary}\n\nCampaigns:\n{catalog}",
        text_format=CampaignMatch,
    )
    match = response.output_parsed.campaign_id
    return CAMPAIGNS_BY_ID.get(match) if match else None


WIDGET_URI = "ui://widget/insight-cards.html"

TOOL_META = {
    "openai/outputTemplate": WIDGET_URI,
    "openai/toolInvocation/invoking": "Searching HSBC Private Banking insights…",
    "openai/toolInvocation/invoked": "Found HSBC Private Banking insights",
}

# Lets the widget iframe load the hero images we pull from HSBC's own pages;
# without this the CSP blocks the <img> requests and they just fail to load.
WIDGET_META = {
    "openai/widgetCSP": {
        "resource_domains": [
            f"https://{ALLOWED_DOMAIN}",
            f"https://*.{ALLOWED_DOMAIN}",
        ],
    },
}

WIDGET_HTML = """<div id="app"></div>
<style>
  :root {
    --lbw-bg: #ffffff;
    --lbw-fg: #1a1a1a;
    --lbw-muted: #5c5c5c;
    --lbw-card-bg: #ffffff;
    --lbw-card-border: #e6e6e6;
    --lbw-red: #db0011;
    --lbw-gold: #a9781f;
  }
  :root[data-theme="dark"] {
    --lbw-bg: #17181a;
    --lbw-fg: #f2f2f2;
    --lbw-muted: #b3b3b3;
    --lbw-card-bg: #1f2023;
    --lbw-card-border: #33343a;
    --lbw-red: #db0011;
    --lbw-gold: #d9ac4f;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --lbw-bg: #17181a;
      --lbw-fg: #f2f2f2;
      --lbw-muted: #b3b3b3;
      --lbw-card-bg: #1f2023;
      --lbw-card-border: #33343a;
      --lbw-red: #db0011;
      --lbw-gold: #d9ac4f;
    }
  }
  html, body {
    margin: 0;
    background: var(--lbw-bg);
    color: var(--lbw-fg);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  }
  #app { padding: 4px 2px 8px; }
  .lbw-summary {
    background: var(--lbw-card-bg);
    border: 1px solid var(--lbw-card-border);
    border-left: 4px solid var(--lbw-red);
    border-radius: 10px;
    padding: 12px 14px;
    margin-bottom: 10px;
  }
  .lbw-summary-label {
    font-size: 11px;
    font-weight: 700;
    letter-spacing: .08em;
    text-transform: uppercase;
    color: var(--lbw-red);
    margin-bottom: 4px;
  }
  .lbw-summary-text { font-size: 14px; line-height: 1.45; font-weight: 500; color: var(--lbw-fg); }
  .lbw-empty { font-size: 13px; color: var(--lbw-muted); padding: 4px 2px; }
  .lbw-cards { display: flex; flex-direction: column; gap: 10px; }
  .lbw-card {
    background: var(--lbw-card-bg);
    border: 1px solid var(--lbw-card-border);
    border-left: 4px solid var(--lbw-red);
    border-radius: 10px;
    overflow: hidden;
  }
  .lbw-card-image {
    display: block;
    width: 100%;
    aspect-ratio: 16 / 9;
    object-fit: cover;
    background: var(--lbw-card-border);
  }
  .lbw-card-content { padding: 12px 14px; }
  .lbw-card-subtitle { font-size: 14px; font-weight: 700; margin-bottom: 4px; }
  .lbw-card-body { font-size: 13px; line-height: 1.5; color: var(--lbw-muted); }
  .lbw-card-link {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    margin-top: 8px;
    font-size: 12px;
    font-weight: 700;
    color: var(--lbw-red);
    text-decoration: none;
  }
  .lbw-card-link:hover { text-decoration: underline; }
  .lbw-campaign {
    background: var(--lbw-card-bg);
    border: 1px solid var(--lbw-card-border);
    border-left: 4px solid var(--lbw-gold);
    border-radius: 10px;
    padding: 12px 14px;
    margin-top: 10px;
  }
  .lbw-campaign-header {
    display: flex;
    align-items: center;
    gap: 6px;
    margin-bottom: 6px;
  }
  .lbw-campaign-icon { width: 16px; height: 16px; color: var(--lbw-gold); flex: 0 0 auto; }
  .lbw-campaign-label {
    font-size: 11px;
    font-weight: 700;
    letter-spacing: .08em;
    text-transform: uppercase;
    color: var(--lbw-gold);
  }
  .lbw-campaign-title { font-size: 14px; font-weight: 700; margin-bottom: 4px; }
  .lbw-campaign-teaser { font-size: 13px; color: var(--lbw-fg); margin-bottom: 4px; }
  .lbw-campaign-detail { font-size: 12px; line-height: 1.5; color: var(--lbw-muted); }
  .lbw-campaign-cta {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    margin-top: 8px;
    font-size: 12px;
    font-weight: 700;
    color: var(--lbw-gold);
    text-decoration: none;
  }
  .lbw-campaign-cta:hover { text-decoration: underline; }
</style>
<script>
(function () {
  function escapeHtml(value) {
    var div = document.createElement("div");
    div.textContent = value == null ? "" : String(value);
    return div.innerHTML;
  }

  function escapeAttr(value) {
    return escapeHtml(value).replace(/"/g, "&quot;");
  }

  function render(data) {
    var app = document.getElementById("app");
    if (!data) {
      app.innerHTML = "";
      return;
    }

    var summaryHtml =
      '<div class="lbw-summary">' +
      '<div class="lbw-summary-label">HSBC Private Banking &middot; Insight Summary</div>' +
      '<div class="lbw-summary-text">' + escapeHtml(data.summary) + "</div>" +
      "</div>";

    var points = data.points || [];
    var bodyHtml;
    if (points.length === 0) {
      bodyHtml = '<div class="lbw-empty">No related HSBC Private Banking insights were found.</div>';
    } else {
      bodyHtml =
        '<div class="lbw-cards">' +
        points
          .map(function (point) {
            var image = point.image_url
              ? '<img class="lbw-card-image" src="' + escapeAttr(point.image_url) +
                '" alt="" loading="lazy" referrerpolicy="no-referrer" ' +
                'onerror="this.remove()">'
              : "";
            var link = point.citation_url
              ? '<a class="lbw-card-link" href="' + escapeAttr(point.citation_url) +
                '" target="_blank" rel="noopener noreferrer">Read more: ' +
                escapeHtml(point.citation_title || "Source") + " &rarr;</a>"
              : "";
            return (
              '<div class="lbw-card">' +
              image +
              '<div class="lbw-card-content">' +
              '<div class="lbw-card-subtitle">' + escapeHtml(point.subtitle) + "</div>" +
              '<div class="lbw-card-body">' + escapeHtml(point.body) + "</div>" +
              link +
              "</div>" +
              "</div>"
            );
          })
          .join("") +
        "</div>";
    }

    var campaignHtml = "";
    if (data.campaign) {
      var campaign = data.campaign;
      campaignHtml =
        '<div class="lbw-campaign">' +
        '<div class="lbw-campaign-header">' +
        '<svg class="lbw-campaign-icon" viewBox="0 0 24 24" fill="none" ' +
        'xmlns="http://www.w3.org/2000/svg" aria-hidden="true">' +
        '<path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-3.5 10.9c.6.44.9 1.13.9 1.85V16h5.2v-.25' +
        'c0-.72.3-1.4.9-1.85A6 6 0 0 0 12 3Z" stroke="currentColor" stroke-width="1.6" ' +
        'stroke-linecap="round" stroke-linejoin="round"/></svg>' +
        '<span class="lbw-campaign-label">Offer for you</span>' +
        "</div>" +
        '<div class="lbw-campaign-title">' + escapeHtml(campaign.title) + "</div>" +
        '<div class="lbw-campaign-teaser">' + escapeHtml(campaign.teaser) + "</div>" +
        (campaign.detail
          ? '<div class="lbw-campaign-detail">' + escapeHtml(campaign.detail) + "</div>"
          : "") +
        (campaign.cta_url
          ? '<a class="lbw-campaign-cta" href="' + escapeAttr(campaign.cta_url) +
            '" target="_blank" rel="noopener noreferrer">' +
            escapeHtml(campaign.cta_label || "Learn more") + " &rarr;</a>"
          : "") +
        "</div>";
    }

    app.innerHTML = summaryHtml + bodyHtml + campaignHtml;
  }

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme === "dark" ? "dark" : "light");
  }

  window.addEventListener("openai:set_globals", function (event) {
    var globals = (event && event.detail && event.detail.globals) || {};
    if (globals.theme) applyTheme(globals.theme);
    if (globals.toolOutput) render(globals.toolOutput);
  });

  var api = window.openai;
  if (api) {
    applyTheme(api.theme);
    if (api.toolOutput) render(api.toolOutput);
  }
})();
</script>
"""


@mcp.resource(WIDGET_URI, mime_type="text/html+skybridge", meta=WIDGET_META)
def insight_cards_widget() -> str:
    return WIDGET_HTML


@mcp.tool(meta=TOOL_META)
def lionbank_wealth_insight(query: str) -> ToolResult:
    """Answer a market, economic, or wealth-management question using HSBC Private
    Banking's published insights (privatebanking.hsbc.com). Use this whenever the
    user asks what HSBC thinks, forecasts, or has published about markets, economies,
    investment themes, or wealth strategy."""
    response = client.responses.parse(
        model=OPENAI_MODEL,
        instructions=SYSTEM_INSTRUCTIONS,
        input=query,
        tools=[
            {
                "type": "web_search",
                "filters": {"allowed_domains": [ALLOWED_DOMAIN]},
            }
        ],
        text_format=InsightResponse,
    )
    insight = response.output_parsed

    if insight.points:
        with ThreadPoolExecutor(max_workers=len(insight.points) + 1) as pool:
            hero_images_future = pool.map(
                _fetch_hero_image, [p.citation_url for p in insight.points]
            )
            campaign_future = pool.submit(_match_campaign, query, insight.summary)
            hero_images = list(hero_images_future)
            campaign = campaign_future.result()
    else:
        hero_images = []
        campaign = _match_campaign(query, insight.summary)

    fallback_lines = [insight.summary, ""]
    points_payload = []
    for point, image_url in zip(insight.points, hero_images):
        fallback_lines.append(f"**{point.subtitle}**")
        fallback_lines.append(point.body)
        fallback_lines.append(f"Source: {point.citation_title}")
        fallback_lines.append("")
        points_payload.append({**point.model_dump(), "image_url": image_url})
    if campaign:
        fallback_lines.append(f"Offer for you: {campaign['title']} — {campaign['teaser']}")
    fallback_text = "\n".join(fallback_lines).strip()

    return ToolResult(
        content=fallback_text,
        structured_content={
            "summary": insight.summary,
            "points": points_payload,
            "campaign": campaign,
        },
        meta=TOOL_META,
    )


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
