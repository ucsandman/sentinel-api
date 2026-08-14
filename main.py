"""
Sentinel Intelligence API
Pay-per-brief intelligence service powered by x402 micropayments.
Accepts USDC on Base mainnet. Payments go to Pico's wallet.

Two ways to pay: an x402 X-PAYMENT header (agents) or a paid Stripe checkout
session (humans). Stripe holds the checkout state, so this service needs no
database.

Endpoints:
  GET  /                        Free   landing page with Buy buttons
  GET  /health                  Free   delivery and settlement preflight
  GET  /.well-known/x402.json   Free   discovery, lists only what can be delivered
  GET  /buy/{slug}              Free   redirect to Stripe Checkout
  GET  /brief/{slug}            $2.00  slug is bnpl or ai-governance
  POST /research                $10.00 on-demand brief, body {"topic": "..."}

Two rules enforced here:
  1. Never charge for what cannot be delivered. Paid routes run a delivery
     preflight before the payment gate.
  2. Never serve paid content that cannot be collected for. In local
     facilitator mode no USDC moves, so paid routes 503 unless
     ALLOW_LOCAL_FACILITATOR is set.
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path

# Imported at module level on purpose. If this is missing the app must fail to
# boot, so the deploy fails and the previous version keeps serving. Importing
# it lazily would instead 500 a customer who had already paid.
import markdown

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel

from x402 import x402ResourceServer
from x402.http import (
    HTTPFacilitatorClient,
    FacilitatorConfig,
    CreateHeadersAuthProvider,
)
from x402.mechanisms.evm.eip712 import hash_eip3009_authorization
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.mechanisms.evm.types import ExactEIP3009Payload
from x402.mechanisms.evm.verify import verify_eoa_signature
from x402.schemas import (
    ResourceConfig,
    SupportedResponse,
    SupportedKind,
    VerifyResponse,
    SettleResponse,
    PaymentPayload,
    PaymentRequirements,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

WALLET_ADDRESS = "0xAFAd5fBF0Ad891385019092CE9c2eAd12F912A37"
BASE_MAINNET = "eip155:8453"
CHAIN_ID = 8453
USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
USDC_NAME = "USD Coin"
USDC_VERSION = "2"

CDP_FACILITATOR_URL = "https://api.cdp.coinbase.com/platform/v2/x402"
CDP_HOST = "api.cdp.coinbase.com"
CDP_KEY_FILE = Path(__file__).parent.parent.parent / "credentials" / "cdp_api_key.json"

BRIEFS_DIR = Path(__file__).parent / "briefs"

# Card checkout (Stripe) runs beside x402 so a human with a card can buy.
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")

# On-demand research uses the Anthropic SDK directly. The previous `ant` CLI
# subprocess is not installed on the Render host, so it took payment and 500'd.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
RESEARCH_MODEL = os.environ.get("RESEARCH_MODEL", "claude-haiku-4-5-20251001")

# The local facilitator verifies signatures but cannot move funds on chain.
# Serving paid content in that mode gives the product away, so it must be
# opted into deliberately.
ALLOW_LOCAL_FACILITATOR = os.environ.get("ALLOW_LOCAL_FACILITATOR", "").lower() in (
    "1",
    "true",
    "yes",
)
PENDING_SETTLEMENTS_FILE = Path(__file__).parent / "pending_settlements.jsonl"

# One catalog entry per sellable brief, so routes, pricing, discovery and the
# landing page cannot drift apart.
CATALOG = {
    "bnpl": {
        "slug": "bnpl",
        "title": "BNPL & Embedded Finance",
        "price_usd": "$2.00",
        "price_cents": 200,
        "blurb": "BNPL and embedded finance intelligence: regulatory pulse, market moves, competitive signals.",
    },
    "ai-governance": {
        "slug": "ai-governance",
        "title": "AI Governance & Compliance",
        "price_usd": "$2.00",
        "price_cents": 200,
        "blurb": "AI governance and compliance intelligence: policy developments, enforcement signals, enterprise implications.",
    },
}

app = FastAPI(
    title="Sentinel Intelligence API",
    description="Pay-per-brief fintech and AI governance intelligence. Powered by x402 micropayments on Base.",
    version="4.0.0",
)

# ---------------------------------------------------------------------------
# x402 facilitator setup — CDP (mainnet) with local fallback
# ---------------------------------------------------------------------------


class LocalBaseFacilitator:
    """Fallback facilitator: verifies EIP-712 signatures locally, defers settlement.
    Used when CDP key is not available.
    """

    def __init__(self):
        self._used_nonces: set[str] = set()

    def get_supported(self) -> SupportedResponse:
        return SupportedResponse(
            kinds=[SupportedKind(x402_version=2, scheme="exact", network=BASE_MAINNET)]
        )

    async def verify(
        self, payload: PaymentPayload, requirements: PaymentRequirements
    ) -> VerifyResponse:
        try:
            evm = ExactEIP3009Payload.from_dict(payload.payload)
            auth = evm.authorization
            payer = auth.from_address
            now = int(time.time())
            if int(auth.valid_before) < now + 6:
                return VerifyResponse(
                    is_valid=False, invalid_reason="valid_before_expired", payer=payer
                )
            if int(auth.valid_after) > now:
                return VerifyResponse(
                    is_valid=False, invalid_reason="valid_after_future", payer=payer
                )
            if auth.to.lower() != requirements.pay_to.lower():
                return VerifyResponse(
                    is_valid=False, invalid_reason="recipient_mismatch", payer=payer
                )
            if int(auth.value) < int(requirements.amount):
                return VerifyResponse(
                    is_valid=False, invalid_reason="amount_too_low", payer=payer
                )
            nonce_key = f"{auth.from_address.lower()}:{auth.nonce}"
            if nonce_key in self._used_nonces:
                return VerifyResponse(
                    is_valid=False, invalid_reason="nonce_already_used", payer=payer
                )
            msg_hash = hash_eip3009_authorization(
                auth, CHAIN_ID, USDC_ADDRESS, USDC_NAME, USDC_VERSION
            )
            sig_bytes = bytes.fromhex((evm.signature or "").removeprefix("0x"))
            if not verify_eoa_signature(msg_hash, sig_bytes, payer):
                return VerifyResponse(
                    is_valid=False, invalid_reason="invalid_signature", payer=payer
                )
            self._used_nonces.add(nonce_key)
            return VerifyResponse(is_valid=True, payer=payer)
        except Exception as exc:
            return VerifyResponse(
                is_valid=False,
                invalid_reason="verify_error",
                invalid_message=str(exc)[:200],
                payer="",
            )

    async def settle(self, payload, requirements) -> SettleResponse:
        """Record the signed authorization for later on-chain settlement.

        This mode does NOT move funds. It previously returned success and wrote
        nothing, so the signed authorization was discarded and the paid content
        was served for free. Persist it, or the payment is unrecoverable.
        """
        try:
            record = {
                "recorded_at": datetime.utcnow().isoformat(),
                "payload": payload.payload
                if hasattr(payload, "payload")
                else str(payload),
                "pay_to": requirements.pay_to,
                "amount": requirements.amount,
            }
            with PENDING_SETTLEMENTS_FILE.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except Exception as exc:
            print(f"[x402] FAILED to persist pending settlement: {exc}")
            return SettleResponse(success=False, transaction="")
        return SettleResponse(success=True, transaction="local_deferred")


def pending_settlement_count() -> int:
    """Signed authorizations that have not been settled on chain."""
    if not PENDING_SETTLEMENTS_FILE.exists():
        return 0
    try:
        with PENDING_SETTLEMENTS_FILE.open(encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())
    except OSError:
        return -1


FACILITATOR_INIT_ERROR: str | None = None


def _build_facilitator():
    """Build CDP facilitator if key available, else fall back to local."""
    global FACILITATOR_INIT_ERROR
    # Check env vars first (Render), then credentials file
    # Support both naming conventions (underscores or none)
    key_id = os.environ.get("CDP_API_KEY_ID") or os.environ.get("CDPAPIKEYID")
    key_secret = os.environ.get("CDP_API_KEY_SECRET") or os.environ.get(
        "CDPAPIKEYSECRET"
    )
    if not (key_id and key_secret) and CDP_KEY_FILE.exists():
        data = json.loads(CDP_KEY_FILE.read_text())
        key_id, key_secret = data["id"], data["privateKey"]

    if not key_id:
        FACILITATOR_INIT_ERROR = (
            "no_key_id: CDP_API_KEY_ID and CDPAPIKEYID both missing from env"
        )
        print(f"[x402] {FACILITATOR_INIT_ERROR}")
    elif not key_secret:
        FACILITATOR_INIT_ERROR = "no_key_secret: CDP_API_KEY_SECRET and CDPAPIKEYSECRET both missing from env"
        print(f"[x402] {FACILITATOR_INIT_ERROR}")
    else:
        try:
            from cdp.auth import get_auth_headers, GetAuthHeadersOptions

            def _make_headers() -> dict[str, dict[str, str]]:
                def _h(path: str, method: str = "POST") -> dict[str, str]:
                    return get_auth_headers(
                        GetAuthHeadersOptions(
                            api_key_id=key_id,
                            api_key_secret=key_secret,
                            request_method=method,
                            request_host=CDP_HOST,
                            request_path=path,
                        )
                    )

                return {
                    "verify": _h("/platform/v2/x402/verify"),
                    "settle": _h("/platform/v2/x402/settle"),
                    "supported": _h("/platform/v2/x402/supported", "GET"),
                    "bazaar": _h("/platform/v2/x402/discovery/resources", "GET"),
                }

            fac = HTTPFacilitatorClient(
                FacilitatorConfig(
                    url=CDP_FACILITATOR_URL,
                    auth_provider=CreateHeadersAuthProvider(_make_headers),
                )
            )
            # Quick sanity check
            fac.get_supported()
            print("[x402] Using CDP facilitator (Base mainnet, real settlement)")
            return fac, "cdp"
        except Exception as e:
            FACILITATOR_INIT_ERROR = f"{type(e).__name__}: {str(e)[:300]}"
            print(
                f"[x402] CDP facilitator failed ({FACILITATOR_INIT_ERROR}), falling back to local"
            )

    print(
        "[x402] Using local facilitator (signature verification, deferred settlement)"
    )
    return LocalBaseFacilitator(), "local"


facilitator, FACILITATOR_MODE = _build_facilitator()
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


def stripe_enabled() -> bool:
    return bool(STRIPE_SECRET_KEY)


def _verify_stripe_session(session_id: str, slug: str | None) -> bool:
    """True only when Stripe confirms this session was paid for this resource."""
    import stripe

    stripe.api_key = STRIPE_SECRET_KEY
    try:
        session = stripe.checkout.Session.retrieve(session_id)
    except Exception as exc:
        # Log the detail, return a generic message. Stripe error strings echo
        # back key material and internal state.
        print(f"[stripe] session retrieve failed: {type(exc).__name__}: {exc}")
        raise HTTPException(
            status_code=402, detail="Could not verify that checkout session"
        )
    # stripe.StripeObject is not a dict subclass in stripe>=15, so .get()
    # raises AttributeError. Use attribute access.
    if getattr(session, "payment_status", None) != "paid":
        raise HTTPException(status_code=402, detail="Checkout session is not paid")
    if slug:
        metadata = getattr(session, "metadata", None)
        session_slug = getattr(metadata, "slug", None) if metadata is not None else None
        if session_slug != slug:
            raise HTTPException(
                status_code=402, detail="Checkout session was for a different resource"
            )
    return True


async def require_payment(request: Request, price_usd: str, slug: str | None = None):
    """Allow the request through if it carries a valid payment.

    Two accepted paths: an x402 X-PAYMENT header (agents), or a paid Stripe
    checkout session id (humans). Returns True when paid. Returns a 402
    JSONResponse when payment has not been attempted. Raises 402 when a
    payment was attempted and failed.
    """
    # Card path. Stripe holds the state, so this needs no database.
    session_id = request.query_params.get("session_id")
    if session_id:
        if not stripe_enabled():
            raise HTTPException(
                status_code=503, detail="Card payment is not configured on this server"
            )
        return _verify_stripe_session(session_id, slug)

    # Agent path.
    payment_header = request.headers.get("X-PAYMENT")
    config = payment_config(price_usd)
    requirements = x402_server.build_payment_requirements(config)

    if not payment_header:
        body = {"error": "Payment required", "x402Version": 1}
        if slug and stripe_enabled():
            body["pay_with_card"] = f"{PUBLIC_BASE_URL}/buy/{slug}"
        return JSONResponse(
            status_code=402,
            content=body,
            headers={
                "PAYMENT-REQUIRED": json.dumps([r.model_dump() for r in requirements]),
                "Access-Control-Expose-Headers": "PAYMENT-REQUIRED",
            },
        )

    # The local facilitator cannot settle on chain. Serving paid content in
    # that mode hands the product over for free, so it must be opted into.
    if FACILITATOR_MODE == "local" and not ALLOW_LOCAL_FACILITATOR:
        raise HTTPException(
            status_code=503,
            detail=(
                "x402 settlement is unavailable: the server is running the local "
                "facilitator, which verifies signatures but does not move funds. "
                "No payment was taken. Use the card checkout instead."
            ),
        )

    result = await x402_server.verify_payment(payment_header, requirements[0])
    if not result.is_valid:
        raise HTTPException(
            status_code=402, detail=f"Invalid payment: {result.invalid_reason}"
        )
    return True


# ---------------------------------------------------------------------------
# Brief loader
# ---------------------------------------------------------------------------


def brief_path(name: str) -> Path:
    return BRIEFS_DIR / f"{name}.md"


def brief_available(name: str) -> bool:
    """Preflight. Never take payment for a brief that is not on disk."""
    return brief_path(name).is_file()


def load_brief(name: str) -> str:
    path = brief_path(name)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Brief '{name}' not found")
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Human-facing pages
# ---------------------------------------------------------------------------

BASE_CSS = """
:root {
  --ground:#F4F5F1; --surface:#FFFFFF; --ink:#171A1D; --muted:#656B63;
  --rule:#D8DCD3; --accent:#1D4E58; --accent-ink:#FFFFFF; --sunk:#ECEEE8;
}
@media (prefers-color-scheme: dark) {
  :root {
    --ground:#141715; --surface:#1C201D; --ink:#E7E9E2; --muted:#8E948B;
    --rule:#333933; --accent:#7FB6C0; --accent-ink:#10201F; --sunk:#232823;
  }
}
* { box-sizing:border-box; }
body {
  background:var(--ground); color:var(--ink); margin:0; padding:56px 20px 80px;
  font-family:"Segoe UI",system-ui,-apple-system,sans-serif; line-height:1.62;
}
main { max-width:42rem; margin:0 auto; }
a { color:var(--accent); text-underline-offset:2px; }
a:focus-visible { outline:2px solid var(--ink); outline-offset:2px; }
code { font-family:ui-monospace,Consolas,monospace; font-size:0.9em; }
footer {
  border-top:1px solid var(--rule); margin-top:44px; padding-top:16px;
  color:var(--muted); font-size:0.83rem;
}
"""

BRIEF_CSS = """
h1 { font-size:1.9rem; line-height:1.15; margin:0 0 6px; letter-spacing:-0.015em; }
h2 {
  font-size:1.15rem; margin:36px 0 12px; padding-bottom:6px;
  border-bottom:1px solid var(--rule);
}
p { margin:0 0 14px; }
ul { padding-left:0; list-style:none; margin:0 0 14px; }
li { margin:0 0 16px; padding-left:16px; border-left:2px solid var(--rule); }
li strong:first-child { color:var(--ink); }
hr { border:0; border-top:1px solid var(--rule); margin:32px 0; }
.receipt {
  background:var(--sunk); border-left:3px solid var(--accent);
  padding:12px 16px; margin:0 0 28px; font-size:0.9rem; color:var(--muted);
}
.receipt strong { color:var(--ink); }
"""


LANDING_CSS = """
main { display:flex; flex-direction:column; gap:30px; max-width:40rem; }
h1 { font-size:2rem; margin:0; letter-spacing:-0.015em; }
.sub { color:var(--muted); margin:6px 0 0; }
.product {
  background:var(--surface); border:1px solid var(--rule);
  padding:20px 22px; display:flex; flex-direction:column; gap:10px;
}
.product h2 { font-size:1.15rem; margin:0; }
.product p { margin:0; color:var(--muted); font-size:0.95rem; }
.row { display:flex; flex-wrap:wrap; align-items:center; gap:14px; margin-top:4px; }
a.buy {
  background:var(--accent); color:var(--accent-ink); text-decoration:none;
  padding:10px 20px; font-weight:600; font-size:0.95rem; display:inline-block;
}
a.buy:hover { opacity:0.9; }
.unavailable { color:var(--muted); font-style:italic; font-size:0.92rem; }
.agent, .note { color:var(--muted); font-size:0.83rem; }
footer { margin-top:0; }
"""


def page_shell(title: str, body: str, extra_css: str = "") -> str:
    return f"<title>{title}</title>\n<style>{BASE_CSS}{extra_css}</style>\n{body}"


def wants_html(request: Request) -> bool:
    """True for a browser, False for an agent.

    An agent either sends X-PAYMENT or asks for JSON. Everything else that
    accepts HTML is a person looking at a page.
    """
    if request.headers.get("X-PAYMENT"):
        return False
    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        return False
    return "text/html" in accept


def render_brief_page(slug: str, item: dict) -> str:
    """The page a paying human sees. Previously they got escaped JSON."""
    body_html = markdown.markdown(
        load_brief(slug), extensions=["extra", "sane_lists", "nl2br"]
    )
    return page_shell(
        item["title"],
        f"""<main>
  <div class="receipt">
    <strong>Paid.</strong> {item["price_usd"]} for the {item["title"]} brief.
    Stripe emails your receipt. This page stays available at the link in your
    address bar, so bookmark it if you want to come back.
  </div>
  {body_html}
  <footer>
    Need this as data? Request the same URL with
    <code>Accept: application/json</code>.<br>
    <a href="/">All briefs</a> &middot; Corrections to agent@practicalsystems.io
  </footer>
</main>""",
        BRIEF_CSS,
    )


# ---------------------------------------------------------------------------
# On-demand research via ant CLI
# ---------------------------------------------------------------------------

RESEARCH_SYSTEM_PROMPT = (
    "You are Sentinel Intelligence, a professional fintech and AI governance research service. "
    "Produce a concise, high-signal intelligence brief on the requested topic. "
    "Format: Critical Alerts, Regulatory Pulse, Market Moves, Key Questions. "
    "Be specific, cite real companies and developments, avoid fluff. "
    "State plainly when you are uncertain about a fact rather than asserting it. "
    "Maximum 800 words. No em dashes."
)


def research_available() -> bool:
    """Preflight. Never take payment for research the server cannot generate."""
    return bool(ANTHROPIC_API_KEY)


async def generate_research_brief(topic: str) -> str:
    """Generate a fresh intelligence brief with the Anthropic SDK.

    This used to shell out to an `ant` CLI that is not installed on the Render
    host, so a paid request charged the caller and then returned a 500.
    """
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    try:
        message = await client.messages.create(
            model=RESEARCH_MODEL,
            max_tokens=2000,
            system=RESEARCH_SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": f"Write an intelligence brief on: {topic}"}
            ],
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Research generation failed: {str(exc)[:200]}"
        )

    text = "".join(
        block.text
        for block in message.content
        if getattr(block, "type", None) == "text"
    )
    if not text.strip():
        raise HTTPException(
            status_code=502, detail="Research generation returned no content"
        )
    return text.strip()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def landing():
    cards = []
    for slug, item in CATALOG.items():
        available = brief_available(slug)
        if not available:
            action = '<span class="unavailable">Temporarily unavailable</span>'
        elif stripe_enabled():
            action = (
                f'<a class="buy" href="/buy/{slug}">Buy for {item["price_usd"]}</a>'
            )
        else:
            action = '<span class="unavailable">Card checkout not configured</span>'
        cards.append(f"""
  <section class="product">
    <h2>{item["title"]}</h2>
    <p>{item["blurb"]}</p>
    <div class="row">
      {action}
      <span class="agent">Agents: <code>GET /brief/{slug}</code> with x402, {item["price_usd"]} USDC</span>
    </div>
  </section>""")

    return page_shell(
        "Sentinel Intelligence",
        f"""<main>
  <header>
    <h1>Sentinel Intelligence</h1>
    <p class="sub">Sourced fintech and AI governance briefs. Every claim carries a link.
    Buy one with a card, or pay per call with <a href="https://x402.org">x402</a> USDC on Base.</p>
  </header>
{"".join(cards)}
  <section class="product">
    <h2>On-demand research</h2>
    <p>A fresh brief on any fintech or AI governance topic.</p>
    <div class="row">
      <span class="agent">Agents: <code>POST /research</code> with x402, $10.00 USDC.
      Body <code>{{"topic": "..."}}</code></span>
    </div>
    <p class="note">{"Available now." if research_available() else "Currently unavailable. The endpoint returns 503 and takes no payment."}</p>
  </section>
  <footer>
    <a href="/health">Service status</a> &middot;
    <a href="/.well-known/x402.json">x402 discovery</a><br>
    Sentinel Intelligence by Practical Systems &middot; agent@practicalsystems.io
  </footer>
</main>""",
        LANDING_CSS,
    )


@app.get("/.well-known/x402.json")
async def x402_discovery():
    """x402 service discovery endpoint for AI agents and directories."""
    return JSONResponse(
        content={
            "name": "Sentinel Intelligence API",
            "description": "Pay-per-brief fintech and AI governance intelligence. Curated research briefs on BNPL, embedded finance, and AI compliance.",
            "contact": "agent@practicalsystems.io",
            "network": BASE_MAINNET,
            "asset": USDC_ADDRESS,
            "resources": [
                *[
                    {
                        "path": f"/brief/{slug}",
                        "method": "GET",
                        "description": item["blurb"],
                        "price": item["price_usd"],
                        "scheme": "exact",
                    }
                    for slug, item in CATALOG.items()
                    if brief_available(slug)
                ],
                *(
                    [
                        {
                            "path": "/research",
                            "method": "POST",
                            "description": "On-demand research brief on any topic. Body: {topic: string}",
                            "price": "$10.00",
                            "scheme": "exact",
                        }
                    ]
                    if research_available()
                    else []
                ),
            ],
            "x402Version": 2,
        }
    )


@app.get("/health")
async def health():
    # Can this server actually deliver what it sells?
    briefs_ready = {slug: brief_available(slug) for slug in CATALOG}
    can_sell_briefs = any(briefs_ready.values()) and (
        stripe_enabled() or FACILITATOR_MODE == "cdp"
    )
    settlement_moves_funds = FACILITATOR_MODE == "cdp"

    problems = []
    for slug, ready in briefs_ready.items():
        if not ready:
            problems.append(f"brief_missing:{slug}")
    if not stripe_enabled():
        problems.append("card_checkout_unconfigured")
    if not research_available():
        problems.append("research_unavailable_no_anthropic_key")
    if not settlement_moves_funds:
        problems.append("x402_settlement_does_not_move_funds")

    resp = {
        "status": "ok" if not problems else "degraded",
        "service": "Sentinel Intelligence API",
        "timestamp": datetime.utcnow().isoformat(),
        "wallet": WALLET_ADDRESS,
        "network": BASE_MAINNET,
        "usdc": USDC_ADDRESS,
        "facilitator": FACILITATOR_MODE,
        "version": "4.0.0",
        # Delivery preflight. Each of these gates a paid route before payment.
        "briefs_available": briefs_ready,
        "research_available": research_available(),
        "card_checkout_available": stripe_enabled(),
        "can_sell_briefs": can_sell_briefs,
        # Settlement honesty. In local mode signatures verify but no USDC moves.
        "settlement_moves_funds": settlement_moves_funds,
        "local_facilitator_allowed": ALLOW_LOCAL_FACILITATOR,
        "pending_unsettled_authorizations": pending_settlement_count(),
        "problems": problems,
    }
    if FACILITATOR_INIT_ERROR:
        resp["facilitator_init_error"] = FACILITATOR_INIT_ERROR
    # Expose which env var names were found (not values) to help diagnose key pickup
    resp["env_key_id_found"] = bool(
        os.environ.get("CDP_API_KEY_ID") or os.environ.get("CDPAPIKEYID")
    )
    resp["env_key_secret_found"] = bool(
        os.environ.get("CDP_API_KEY_SECRET") or os.environ.get("CDPAPIKEYSECRET")
    )
    return resp


@app.get("/buy/{slug}")
async def buy(slug: str):
    """Card checkout for humans. Redirects to Stripe's hosted page.

    Uses inline price_data, so it creates nothing in the Stripe account.
    """
    item = CATALOG.get(slug)
    if not item:
        raise HTTPException(status_code=404, detail=f"Unknown brief '{slug}'")
    if not brief_available(slug):
        raise HTTPException(
            status_code=503, detail=f"Brief '{slug}' is not available right now"
        )
    if not stripe_enabled():
        raise HTTPException(
            status_code=503, detail="Card payment is not configured on this server"
        )

    import stripe

    stripe.api_key = STRIPE_SECRET_KEY
    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[
                {
                    "quantity": 1,
                    "price_data": {
                        "currency": "usd",
                        "unit_amount": item["price_cents"],
                        "product_data": {
                            "name": f"Sentinel Intelligence: {item['title']}",
                            "description": item["blurb"],
                        },
                    },
                }
            ],
            metadata={"slug": slug},
            success_url=f"{PUBLIC_BASE_URL}/brief/{slug}?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{PUBLIC_BASE_URL}/",
        )
    except Exception as exc:
        # Never show a buyer a stack trace, and never leak key material.
        print(f"[stripe] checkout session creation failed: {type(exc).__name__}: {exc}")
        raise HTTPException(
            status_code=503,
            detail="Card checkout is temporarily unavailable. No payment was taken.",
        )
    return RedirectResponse(url=session.url, status_code=303)


@app.get("/brief/{slug}")
async def brief(request: Request, slug: str):
    item = CATALOG.get(slug)
    if not item:
        raise HTTPException(status_code=404, detail=f"Unknown brief '{slug}'")

    # Preflight before payment. Confirm delivery is possible first, so a buyer
    # is never charged for something this server cannot hand over.
    if not brief_available(slug):
        raise HTTPException(
            status_code=503, detail=f"Brief '{slug}' is not available right now"
        )

    result = await require_payment(request, item["price_usd"], slug=slug)
    if isinstance(result, JSONResponse):
        return result

    # A person who just paid with a card gets a readable page. An agent gets
    # JSON. Serving escaped JSON to a browser is how the first real buyer was
    # greeted, and it looked broken.
    if wants_html(request):
        return HTMLResponse(render_brief_page(slug, item))

    return JSONResponse(
        content={
            "brief": load_brief(slug),
            "topic": item["title"],
            "timestamp": datetime.utcnow().isoformat(),
        }
    )


class ResearchRequest(BaseModel):
    topic: str


@app.post("/research")
async def research(request: Request, body: ResearchRequest):
    if not body.topic or len(body.topic.strip()) < 5:
        raise HTTPException(
            status_code=400, detail="topic must be at least 5 characters"
        )

    # Preflight before payment. This endpoint used to charge $10 and then fail
    # because its generator was missing on the host.
    if not research_available():
        raise HTTPException(
            status_code=503,
            detail="On-demand research is unavailable: this server has no ANTHROPIC_API_KEY configured. No payment was taken.",
        )

    result = await require_payment(request, "$10.00")
    if isinstance(result, JSONResponse):
        return result
    brief = await generate_research_brief(body.topic)
    return JSONResponse(
        content={
            "brief": brief,
            "topic": body.topic,
            "timestamp": datetime.utcnow().isoformat(),
            "generated_by": "Sentinel Intelligence / Practical Systems",
        }
    )
