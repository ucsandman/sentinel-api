"""
Sentinel Intelligence API
Pay-per-brief intelligence service powered by x402 micropayments.
Accepts USDC on Base mainnet. Payments go to Pico's wallet.

Endpoints:
  GET  /                    Free — landing page
  GET  /health              Free — health check
  GET  /brief/bnpl          $2.00 USDC — BNPL & Embedded Finance brief
  GET  /brief/ai-governance $2.00 USDC — AI Governance & Compliance brief
  POST /research            $10.00 USDC — On-demand research brief (any topic)
"""

import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

WALLET_ADDRESS = "0xAFAd5fBF0Ad891385019092CE9c2eAd12F912A37"
BASE_MAINNET = "eip155:8453"
CHAIN_ID = 8453
USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

CDP_FACILITATOR_URL = "https://api.cdp.coinbase.com/platform/v2/x402"
CDP_HOST = "api.cdp.coinbase.com"
CDP_KEY_FILE = Path(__file__).parent.parent.parent / "credentials" / "cdp_api_key.json"

BRIEFS_DIR = Path(__file__).parent / "briefs"

app = FastAPI(
    title="Sentinel Intelligence API",
    description="Pay-per-brief fintech and AI governance intelligence. Powered by x402 micropayments on Base.",
    version="3.0.0",
)

# ---------------------------------------------------------------------------
# CDP facilitator setup (real mainnet settlement via Coinbase)
# ---------------------------------------------------------------------------

from x402.schemas import ResourceConfig
from x402 import x402ResourceServer
from x402.http import HTTPFacilitatorClient, FacilitatorConfig, CreateHeadersAuthProvider
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from cdp.auth import get_auth_headers, GetAuthHeadersOptions


def _load_cdp_key() -> tuple[str, str]:
    """Load CDP API key from credentials file or environment."""
    # Prefer env vars (works on Render)
    key_id = os.environ.get("CDP_API_KEY_ID")
    key_secret = os.environ.get("CDP_API_KEY_SECRET")
    if key_id and key_secret:
        return key_id, key_secret
    # Fall back to local credentials file
    if CDP_KEY_FILE.exists():
        data = json.loads(CDP_KEY_FILE.read_text())
        return data["id"], data["privateKey"]
    raise RuntimeError("CDP API key not found. Set CDP_API_KEY_ID and CDP_API_KEY_SECRET env vars.")


CDP_KEY_ID, CDP_KEY_SECRET = _load_cdp_key()


def _make_cdp_auth_headers() -> dict[str, dict[str, str]]:
    """Generate fresh CDP JWT auth headers for each x402 endpoint."""
    def _h(path: str, method: str = "POST") -> dict[str, str]:
        return get_auth_headers(GetAuthHeadersOptions(
            api_key_id=CDP_KEY_ID,
            api_key_secret=CDP_KEY_SECRET,
            request_method=method,
            request_host=CDP_HOST,
            request_path=path,
        ))
    return {
        "verify": _h("/platform/v2/x402/verify"),
        "settle": _h("/platform/v2/x402/settle"),
        "supported": _h("/platform/v2/x402/supported", "GET"),
        "bazaar": _h("/platform/v2/x402/discovery/resources", "GET"),
    }


facilitator = HTTPFacilitatorClient(
    FacilitatorConfig(
        url=CDP_FACILITATOR_URL,
        auth_provider=CreateHeadersAuthProvider(_make_cdp_auth_headers),
    )
)
x402_server = x402ResourceServer(facilitator)
x402_server.register(BASE_MAINNET, ExactEvmServerScheme())
x402_server.initialize()


def payment_config(price_usd: str) -> ResourceConfig:
    return ResourceConfig(
        scheme="exact",
        network=BASE_MAINNET,
        pay_to=WALLET_ADDRESS,
        price=price_usd,
    )


async def require_payment(request: Request, price_usd: str) -> bool:
    """Check x402 payment. Returns True if paid, raises 402 if not."""
    payment_header = request.headers.get("X-PAYMENT")
    if not payment_header:
        config = payment_config(price_usd)
        requirements = x402_server.build_payment_requirements(config)
        return JSONResponse(
            status_code=402,
            content={"error": "Payment required", "x402Version": 1},
            headers={
                "PAYMENT-REQUIRED": json.dumps([r.model_dump() for r in requirements]),
                "Access-Control-Expose-Headers": "PAYMENT-REQUIRED",
            },
        )
    config = payment_config(price_usd)
    requirements = x402_server.build_payment_requirements(config)
    result = await x402_server.verify_payment(payment_header, requirements[0])
    if not result.is_valid:
        raise HTTPException(status_code=402, detail=f"Invalid payment: {result.invalid_reason}")
    return True


# ---------------------------------------------------------------------------
# Brief loader
# ---------------------------------------------------------------------------

def load_brief(name: str) -> str:
    path = BRIEFS_DIR / f"{name}.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Brief '{name}' not found")
    return path.read_text()


# ---------------------------------------------------------------------------
# On-demand research via ant CLI
# ---------------------------------------------------------------------------

async def generate_research_brief(topic: str) -> str:
    """Use ant CLI to generate a fresh intelligence brief on any topic."""
    system_prompt = (
        "You are Sentinel Intelligence, a professional fintech and AI governance research service. "
        "Produce a concise, high-signal intelligence brief on the requested topic. "
        "Format: Critical Alerts, Regulatory Pulse, Market Moves, Key Questions. "
        "Be specific, cite real companies and developments, avoid fluff. "
        "Maximum 800 words. No em dashes."
    )
    user_prompt = f"Write an intelligence brief on: {topic}"

    cmd = [
        "ant", "messages", "create",
        "--model", "claude-haiku-4-5",
        "--max-tokens", "2000",
        "--system", system_prompt,
        "--message", json.dumps({"role": "user", "content": user_prompt}),
        "--transform", "content.0.text",
        "--raw-output",
    ]

    env = {**os.environ, "PATH": "/workspace/bin:/usr/local/bin:/usr/bin:/bin"}
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"Research generation failed: {stderr.decode()[:200]}",
        )
    return stdout.decode().strip()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def landing():
    return """
<!DOCTYPE html>
<html>
<head>
  <title>Sentinel Intelligence API</title>
  <style>
    body { font-family: system-ui; max-width: 700px; margin: 60px auto; padding: 0 20px; color: #111; }
    h1 { font-size: 1.8em; margin-bottom: 4px; }
    .sub { color: #555; margin-bottom: 32px; }
    .endpoint { background: #f5f5f5; border-radius: 8px; padding: 16px 20px; margin-bottom: 16px; }
    .method { display: inline-block; background: #111; color: #fff; padding: 2px 8px; border-radius: 4px; font-size: 0.85em; font-family: monospace; margin-right: 8px; }
    .path { font-family: monospace; font-size: 1em; }
    .price { float: right; font-weight: bold; color: #2d6a4f; }
    .free { float: right; font-weight: bold; color: #888; }
    p { color: #444; margin: 6px 0 0 0; font-size: 0.92em; }
    .wallet { background: #f0f7f0; border: 1px solid #b7dfb7; padding: 12px 16px; border-radius: 6px; font-family: monospace; font-size: 0.85em; word-break: break-all; }
  </style>
</head>
<body>
  <h1>Sentinel Intelligence API</h1>
  <p class="sub">Pay-per-brief fintech and AI governance intelligence. Powered by <a href="https://x402.org">x402</a> micropayments on Base.</p>

  <div class="endpoint">
    <span class="method">GET</span><span class="path">/brief/bnpl</span>
    <span class="price">$2.00 USDC</span>
    <p>BNPL and embedded finance intelligence: regulatory pulse, market moves, competitive signals.</p>
  </div>

  <div class="endpoint">
    <span class="method">GET</span><span class="path">/brief/ai-governance</span>
    <span class="price">$2.00 USDC</span>
    <p>AI governance and compliance intelligence: policy developments, enforcement signals, enterprise implications.</p>
  </div>

  <div class="endpoint">
    <span class="method">POST</span><span class="path">/research</span>
    <span class="price">$10.00 USDC</span>
    <p>On-demand research brief on any fintech or AI topic. Body: <code>{"topic": "your topic here"}</code></p>
  </div>

  <div class="endpoint">
    <span class="method">GET</span><span class="path">/health</span>
    <span class="free">Free</span>
    <p>Service status.</p>
  </div>

  <br>
  <p><strong>Payment:</strong> All paid endpoints use <a href="https://x402.org">x402</a>. Send USDC on Base mainnet.
  No account required - your wallet is your identity.</p>
  <br>
  <p><strong>Receiving wallet (Base mainnet):</strong></p>
  <div class="wallet">0xAFAd5fBF0Ad891385019092CE9c2eAd12F912A37</div>
  <br>
  <p style="color:#888; font-size:0.85em;">Sentinel Intelligence by Practical Systems | agent@practicalsystems.io</p>
</body>
</html>
"""


@app.get("/.well-known/x402.json")
async def x402_discovery():
    """x402 service discovery endpoint for AI agents and directories."""
    return JSONResponse(content={
        "name": "Sentinel Intelligence API",
        "description": "Pay-per-brief fintech and AI governance intelligence. Curated research briefs on BNPL, embedded finance, and AI compliance.",
        "contact": "agent@practicalsystems.io",
        "network": BASE_MAINNET,
        "asset": USDC_ADDRESS,
        "resources": [
            {
                "path": "/brief/bnpl",
                "method": "GET",
                "description": "BNPL and embedded finance intelligence brief.",
                "price": "$2.00",
                "scheme": "exact",
            },
            {
                "path": "/brief/ai-governance",
                "method": "GET",
                "description": "AI governance and compliance intelligence brief.",
                "price": "$2.00",
                "scheme": "exact",
            },
            {
                "path": "/research",
                "method": "POST",
                "description": "On-demand research brief on any topic. Body: {topic: string}",
                "price": "$10.00",
                "scheme": "exact",
            },
        ],
        "x402Version": 2,
    })


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "Sentinel Intelligence API",
        "timestamp": datetime.utcnow().isoformat(),
        "wallet": WALLET_ADDRESS,
        "network": BASE_MAINNET,
        "usdc": USDC_ADDRESS,
        "facilitator": "cdp",
        "version": "3.0.0",
    }


@app.get("/brief/bnpl")
async def brief_bnpl(request: Request):
    result = await require_payment(request, "$2.00")
    if isinstance(result, JSONResponse):
        return result
    content = load_brief("bnpl")
    return JSONResponse(content={
        "brief": content,
        "topic": "BNPL & Embedded Finance",
        "timestamp": datetime.utcnow().isoformat(),
    })


@app.get("/brief/ai-governance")
async def brief_ai_governance(request: Request):
    result = await require_payment(request, "$2.00")
    if isinstance(result, JSONResponse):
        return result
    content = load_brief("ai-governance")
    return JSONResponse(content={
        "brief": content,
        "topic": "AI Governance & Compliance",
        "timestamp": datetime.utcnow().isoformat(),
    })


class ResearchRequest(BaseModel):
    topic: str


@app.post("/research")
async def research(request: Request, body: ResearchRequest):
    if not body.topic or len(body.topic.strip()) < 5:
        raise HTTPException(status_code=400, detail="topic must be at least 5 characters")
    result = await require_payment(request, "$10.00")
    if isinstance(result, JSONResponse):
        return result
    brief = await generate_research_brief(body.topic)
    return JSONResponse(content={
        "brief": brief,
        "topic": body.topic,
        "timestamp": datetime.utcnow().isoformat(),
        "generated_by": "Sentinel Intelligence / Practical Systems",
    })
