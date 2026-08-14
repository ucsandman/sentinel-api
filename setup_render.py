#!/usr/bin/env python3
"""Configure the Render service for this repo, then verify it can actually sell.

This script reads secrets. You do not paste them, and nothing prints them.

Secrets are read from the first file that exists:
  1. --secrets <path>
  2. ./.env
  3. ~/.claude/.secrets.env

Keys it looks for:
  RENDER_API_KEY      required. Render dashboard -> Account Settings -> API Keys
  STRIPE_SECRET_KEY   required for card sales. Must be a SECRET key (sk_ or rk_).
                      A publishable key (pk_) cannot create checkout sessions.
                      Aliases accepted: STRIPE_API_KEY, STRIPE_SECRET, STRIPE_SK
  ANTHROPIC_API_KEY   optional. Enables POST /research

Usage:
  python setup_render.py --diagnose      # which STRIPE_* names exist, and their type
  python setup_render.py                 # dry run, shows what would change
  python setup_render.py --apply         # write the env vars and redeploy
  python setup_render.py --verify-only   # just check what the live service reports
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = "sentinel-api"
RENDER_API = "https://api.render.com/v1"

SECRET_CANDIDATES = [
    Path(".env"),
    Path.home() / ".claude" / ".secrets.env",
]

# The first alias that holds a usable secret key wins.
STRIPE_ALIASES = [
    "STRIPE_SECRET_KEY",
    "STRIPE_API_KEY",
    "STRIPE_SECRET",
    "STRIPE_SK",
    "STRIPE_LIVE_SECRET_KEY",
    "STRIPE_TEST_SECRET_KEY",
]
WANTED = ["RENDER_API_KEY", "ANTHROPIC_API_KEY"] + STRIPE_ALIASES


def key_kind(value: str) -> str:
    """Classify a Stripe key by prefix. Never returns any of the value."""
    for prefix, label in (
        ("pk_live_", "PUBLISHABLE live - CANNOT be used here"),
        ("pk_test_", "PUBLISHABLE test - CANNOT be used here"),
        ("sk_live_", "secret LIVE - takes real money"),
        ("sk_test_", "secret test - no real money moves"),
        ("rk_live_", "restricted LIVE - takes real money"),
        ("rk_test_", "restricted test - no real money moves"),
        ("whsec_", "webhook signing secret - not an API key"),
    ):
        if value.startswith(prefix):
            return label
    return "unrecognized prefix"


def read_env_file(path: Path) -> dict:
    out = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip("'\"")
    return out


def find_secrets_file(explicit: str | None) -> Path | None:
    for path in [Path(explicit)] if explicit else SECRET_CANDIDATES:
        if path.is_file():
            return path
    return None


def diagnose(path: Path | None) -> int:
    """Report which Stripe-ish names exist and what type each is. No values."""
    print("Stripe keys visible to this script\n")
    seen = []
    if path:
        print(f"  file: {path}")
        for name, value in read_env_file(path).items():
            if "STRIPE" in name.upper():
                seen.append((name, value, "file"))
    for name, value in os.environ.items():
        if "STRIPE" in name.upper():
            seen.append((name, value, "environment"))

    if not seen:
        print("\n  No STRIPE_* names found anywhere.")
    for name, value, where in seen:
        print(f"\n  {name}  ({where})")
        print(f"    type   {key_kind(value)}")
        print(f"    length {len(value)} chars")

    usable = [n for n, v, _ in seen if v.startswith(("sk_", "rk_"))]
    print("\n" + "-" * 60)
    if usable:
        print(f"Usable secret key found: {usable[0]}")
        if usable[0] not in STRIPE_ALIASES:
            print(f"  Rename it to STRIPE_SECRET_KEY, or add '{usable[0]}' to")
            print("  STRIPE_ALIASES in this script.")
        return 0

    print("No usable Stripe SECRET key found.")
    print("\nA publishable key (pk_) only works in a browser. Creating a checkout")
    print("session and reading its payment status are server-side operations that")
    print("require a secret key. This service never uses a publishable key at all,")
    print("because Stripe Checkout is a hosted redirect.")
    print("\nGet one at https://dashboard.stripe.com/apikeys")
    print("Safer option: create a RESTRICTED key (rk_live_) there with")
    print("  Checkout Sessions: write")
    print("and give it nothing else. Then add to your secrets file:")
    print("  STRIPE_SECRET_KEY=rk_live_...")
    return 1


def load_secrets(explicit: str | None) -> tuple[dict, Path | None]:
    path = find_secrets_file(explicit)
    found = read_env_file(path) if path else {}
    merged = {k: v for k, v in found.items() if k in WANTED}
    for key in WANTED:
        if os.environ.get(key):
            merged[key] = os.environ[key]

    # Collapse the Stripe aliases down to one canonical name.
    # Live keys win. A test key is only ever a deliberate fallback, because
    # pushing one to production shows buyers a TEST MODE checkout page that
    # rejects real cards.
    candidates = [
        (alias, merged.get(alias, ""))
        for alias in STRIPE_ALIASES
        if merged.get(alias, "").startswith(("sk_", "rk_"))
    ]
    live = [c for c in candidates if c[1].startswith(("sk_live_", "rk_live_"))]
    chosen = (live or candidates or [(None, "")])[0]
    if chosen[0]:
        merged["STRIPE_SECRET_KEY"] = chosen[1]
        merged["_stripe_alias"] = chosen[0]
    return merged, path


def check_stripe_key(value: str) -> tuple[bool, str]:
    """Confirm the key can do the one thing this service needs."""
    req = urllib.request.Request(
        "https://api.stripe.com/v1/checkout/sessions?limit=1",
        headers={"Authorization": f"Bearer {value}"},
    )
    try:
        urllib.request.urlopen(req, timeout=30).read()
        return True, "key works and can read checkout sessions"
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()[:200]
        return False, f"HTTP {exc.code}: {body}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def api(token: str, method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(
        f"{RENDER_API}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:300]
        raise SystemExit(f"Render API {method} {path} failed: {exc.code} {detail}")


def find_service(token: str) -> dict:
    services = api(token, "GET", "/services?limit=100")
    rows = [s.get("service", s) for s in services]
    matches = [s for s in rows if REPO in (s.get("repo") or "")]
    if not matches:
        print("No Render service is connected to this repo. Services on the account:")
        for s in rows:
            print(f"  - {s.get('name')}  id={s.get('id')}  repo={s.get('repo')}")
        raise SystemExit(
            "\nConnect the repo in the Render dashboard first, or pass --service-id."
        )
    if len(matches) > 1:
        print("More than one service matches this repo:")
        for s in matches:
            print(f"  - {s.get('name')}  id={s.get('id')}")
        raise SystemExit("Pass --service-id to choose one.")
    return matches[0]


def service_url(service: dict) -> str:
    details = service.get("serviceDetails") or {}
    return (details.get("url") or "").rstrip("/")


def verify(base_url: str) -> int:
    """Read /health and say plainly whether this thing can take money."""
    print(f"\nVerifying {base_url}/health")
    data = None
    for attempt in range(12):
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=30) as resp:
                data = json.loads(resp.read().decode())
                break
        except Exception:
            if attempt == 11:
                print("  could not reach /health")
                return 1
            time.sleep(10)
    if data is None:
        return 1

    print(f"  version               {data.get('version')}")
    print(f"  status                {data.get('status')}")
    print(f"  briefs available      {data.get('briefs_available')}")
    print(f"  card checkout         {data.get('card_checkout_available')}")
    print(f"  research available    {data.get('research_available')}")
    print(f"  settlement moves USDC {data.get('settlement_moves_funds')}")
    if data.get("problems"):
        print(f"  problems              {data['problems']}")

    if data.get("card_checkout_available"):
        print(f"\n  A human can now buy: {base_url}/buy/bnpl")
        print("  Buy one yourself and refund it before sending anyone else there.")
        return 0
    print("\n  Card checkout is still OFF. No human can pay yet.")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually write the changes")
    ap.add_argument("--verify-only", action="store_true")
    ap.add_argument("--diagnose", action="store_true", help="which Stripe keys exist")
    ap.add_argument(
        "--allow-test-key",
        action="store_true",
        help="permit pushing a sk_test_/rk_test_ key to production",
    )
    ap.add_argument("--secrets", help="path to a file holding the keys")
    ap.add_argument("--service-id", help="Render service id, skips auto-discovery")
    args = ap.parse_args()

    if args.diagnose:
        return diagnose(find_secrets_file(args.secrets))

    secrets, source = load_secrets(args.secrets)
    print(f"Secrets source: {source or 'environment only'}")

    stripe_key = secrets.get("STRIPE_SECRET_KEY", "")
    for name in ("RENDER_API_KEY", "ANTHROPIC_API_KEY"):
        value = secrets.get(name, "")
        print(
            f"  {name:<20} {'found, ' + str(len(value)) + ' chars' if value else 'MISSING'}"
        )
    if stripe_key:
        alias = secrets.get("_stripe_alias", "STRIPE_SECRET_KEY")
        print(f"  {'STRIPE_SECRET_KEY':<20} found via {alias}, {key_kind(stripe_key)}")
        ok, detail = check_stripe_key(stripe_key)
        print(f"  {'':<20} {'OK: ' if ok else 'REJECTED: '}{detail}")
        if not ok:
            return err("the Stripe key does not work. Not writing it to production.")
        if stripe_key.startswith(("sk_test_", "rk_test_")):
            print("\n  This is a TEST key. On a public site it shows buyers a Stripe")
            print("  page marked TEST MODE that rejects real cards.")
            if args.apply and not args.allow_test_key:
                return err(
                    "refusing to put a TEST Stripe key on production.\n"
                    "  Add STRIPE_SECRET_KEY=sk_live_... to your secrets file,\n"
                    "  or pass --allow-test-key if you really want test mode live."
                )
    else:
        print(f"  {'STRIPE_SECRET_KEY':<20} MISSING")
        print("\n  Run: python setup_render.py --diagnose")
        print("  A publishable key (pk_) will not work. This needs sk_ or rk_.")

    token = secrets.get("RENDER_API_KEY")
    if not token:
        return err("RENDER_API_KEY not found. Add it to your secrets file.")

    if args.service_id:
        service = api(token, "GET", f"/services/{args.service_id}")
    else:
        service = find_service(token)
    base_url = service_url(service)
    print(f"\nService: {service.get('name')}  id={service.get('id')}")
    print(f"URL:     {base_url or '(none reported)'}")

    if args.verify_only:
        return verify(base_url) if base_url else err("service reports no URL")

    updates = {}
    if stripe_key:
        updates["STRIPE_SECRET_KEY"] = stripe_key
    if secrets.get("ANTHROPIC_API_KEY"):
        updates["ANTHROPIC_API_KEY"] = secrets["ANTHROPIC_API_KEY"]
    if base_url:
        updates["PUBLIC_BASE_URL"] = base_url

    if not updates:
        return err("nothing to set.")

    print("\nWould set on the service:")
    for key in updates:
        shown = updates[key] if key == "PUBLIC_BASE_URL" else "<hidden>"
        print(f"  {key} = {shown}")

    if not args.apply:
        print("\nDry run. Nothing changed. Re-run with --apply to write these.")
        return 0

    for key, value in updates.items():
        api(token, "PUT", f"/services/{service['id']}/env-vars/{key}", {"value": value})
        print(f"  set {key}")

    print("\nTriggering deploy...")
    deploy = api(token, "POST", f"/services/{service['id']}/deploys", {})
    print(f"  deploy {deploy.get('id') or '(queued)'}")
    print("  waiting for the service to come back up")
    time.sleep(30)
    return verify(base_url) if base_url else 0


def err(message: str) -> int:
    print(f"\nERROR: {message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
