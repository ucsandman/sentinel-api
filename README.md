# Sentinel Intelligence API

Pay-per-brief fintech and AI governance intelligence. Two ways to pay:

- **Humans** buy a brief with a card through Stripe Checkout.
- **Agents** pay per call with [x402](https://x402.org) micropayments in USDC on Base mainnet.

Every claim in a brief carries a source link. That standard is enforced by a test, not by good intentions.

---

## Run it

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt   # Windows
# source .venv/bin/activate && pip install -r requirements-dev.txt   # macOS/Linux

cp .env.example .env        # then fill in the values you need
.venv/Scripts/python -m uvicorn main:app --reload --port 8000
```

Open <http://localhost:8000>. The landing page shows a Buy button for each brief once `STRIPE_SECRET_KEY` is set.

### Tests and lint

```bash
.venv/Scripts/python -m pytest -q
ruff check main.py test_main.py
```

---

## Endpoints

| Method | Path | Price | Notes |
|---|---|---|---|
| GET | `/` | free | Landing page with Buy buttons |
| GET | `/health` | free | Delivery and settlement preflight, see below |
| GET | `/.well-known/x402.json` | free | Service discovery. Lists only what the server can actually deliver |
| GET | `/buy/{slug}` | free | Redirects to Stripe Checkout. `slug` is `bnpl` or `ai-governance` |
| GET | `/brief/{slug}` | $2.00 | The brief. Accepts an x402 `X-PAYMENT` header or a paid `?session_id=` |
| POST | `/research` | $10.00 | On-demand brief. Body `{"topic": "..."}` |

### Example: agent pays with x402

```bash
curl -i http://localhost:8000/brief/bnpl
# 402 Payment Required, with a PAYMENT-REQUIRED header carrying the requirements
# and a pay_with_card URL in the body when Stripe is configured.

curl -H "X-PAYMENT: <signed x402 payload>" http://localhost:8000/brief/bnpl
```

Response:

```json
{
  "brief": "# BNPL & Embedded Finance Intelligence Brief\n...",
  "topic": "BNPL & Embedded Finance",
  "timestamp": "2026-08-14T12:59:52.994276"
}
```

### Example: human pays by card

Click **Buy for $2.00**, or open `/buy/bnpl` directly. Stripe redirects back to
`/brief/bnpl?session_id=...` and the server asks Stripe whether that session was paid.
Stripe holds the state, so the service needs no database.

### Example: on-demand research

```bash
curl -X POST http://localhost:8000/research \
  -H "Content-Type: application/json" \
  -d '{"topic": "embedded finance licensing in Brazil"}'
```

Returns 503 and takes no payment when `ANTHROPIC_API_KEY` is unset.

---

## Two rules this service follows

**1. Never charge for what it cannot deliver.** Every paid route runs a delivery
preflight before the payment gate. A missing brief file returns 503, not a
payment demand. `/research` without an API key returns 503, not a payment demand.
This exists because the endpoint previously took $10 and then failed.

**2. Never serve paid content it cannot collect for.** When the Coinbase CDP
facilitator is unavailable the server falls back to a local facilitator that
verifies signatures but moves no funds. In that mode paid routes return 503
unless you set `ALLOW_LOCAL_FACILITATOR=true`, and every signed authorization is
appended to `pending_settlements.jsonl` so it can be settled on chain later.

Check both at `/health`:

```json
{
  "status": "degraded",
  "briefs_available": {"bnpl": true, "ai-governance": true},
  "research_available": true,
  "card_checkout_available": true,
  "settlement_moves_funds": false,
  "pending_unsettled_authorizations": 0,
  "problems": ["x402_settlement_does_not_move_funds"]
}
```

`status` is `degraded` whenever `problems` is non-empty.

---

## Environment

All configuration is via `.env`. See `.env.example` for the full list.

| Variable | Required | Effect if unset |
|---|---|---|
| `STRIPE_SECRET_KEY` | for card sales | No Buy buttons, `/buy/*` returns 503 |
| `PUBLIC_BASE_URL` | for card sales | Stripe redirects back to `localhost` |
| `ANTHROPIC_API_KEY` | for `/research` | `/research` returns 503, hidden from discovery |
| `RESEARCH_MODEL` | no | Defaults to `claude-haiku-4-5-20251001` |
| `CDP_API_KEY_ID` / `CDP_API_KEY_SECRET` | for real x402 settlement | Falls back to the local facilitator |
| `ALLOW_LOCAL_FACILITATOR` | no | Paid routes 503 in local facilitator mode |

`CDPAPIKEYID` and `CDPAPIKEYSECRET` are also accepted, because Render strips underscores in some setups.

---

## Deploying

The service runs on Render at <https://sentinel-api-37jd.onrender.com> and deploys on push to `main`.

`setup_render.py` configures it and then checks whether it can actually take money. It reads keys from a secrets file, so you never paste them, and it never prints their values.

```bash
python setup_render.py                 # dry run, shows what would change
python setup_render.py --apply         # write the env vars and redeploy
python setup_render.py --verify-only   # just read /health and report
```

It looks for `RENDER_API_KEY`, `STRIPE_SECRET_KEY` and `ANTHROPIC_API_KEY` in `--secrets <path>`, then `./.env`, then `~/.claude/.secrets.env`. Environment variables override the file.

To take **real** money you need a live Stripe key (`sk_live_...`). A `sk_test_...` key produces a working checkout page that never moves funds, and `setup_render.py` prints which mode it found.

---

## Adding a brief

1. Write `briefs/<slug>.md`. Include an `**Edition:** YYYY-MM-DD` line and at least six source links.
2. Add an entry to `CATALOG` in `main.py`.

Routes, pricing, discovery, the landing page and the tests all read from `CATALOG`, so there is nothing else to change. `pytest` will fail the new brief if it is undated or under-sourced.

---

Sentinel Intelligence by Practical Systems | agent@practicalsystems.io

## Support

If my tools save you time, you can support my work here:

[![Sponsor on GitHub](https://img.shields.io/badge/GitHub%20Sponsors-%E2%9D%A4-db61a2?logo=githubsponsors&logoColor=white)](https://github.com/sponsors/ucsandman)
[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-%E2%98%95-ffdd00?logo=buymeacoffee&logoColor=black)](https://buymeacoffee.com/wes_sander)
