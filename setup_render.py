#!/usr/bin/env python3
"""Configure the Render service for this repo, then verify it can actually sell.

This script reads secrets. You do not paste them, and nothing prints them.

Secrets are read from the first file that exists:
  1. --secrets <path>
  2. ./.env
  3. ~/.claude/.secrets.env

Keys it looks for:
  RENDER_API_KEY      required. Render dashboard -> Account Settings -> API Keys
  STRIPE_SECRET_KEY   required for card sales. Use sk_live_... to take real money
  ANTHROPIC_API_KEY   optional. Enables POST /research

Usage:
  python setup_render.py                 # dry run, shows what would change
  python setup_render.py --apply         # writes the env vars and redeploys
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

# Values are never printed. Only these names, and whether each was found.
WANTED = ["RENDER_API_KEY", "STRIPE_SECRET_KEY", "ANTHROPIC_API_KEY"]


def load_secrets(explicit: str | None) -> tuple[dict, Path | None]:
    candidates = [Path(explicit)] if explicit else SECRET_CANDIDATES
    for path in candidates:
        if not path.is_file():
            continue
        found = {}
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key in WANTED:
                found[key] = value.strip().strip("'\"")
        # Environment wins, so you can override without editing the file.
        for key in WANTED:
            if os.environ.get(key):
                found[key] = os.environ[key]
        return found, path
    return {key: os.environ[key] for key in WANTED if os.environ.get(key)}, None


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
    else:
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
    ap.add_argument("--secrets", help="path to a file holding the keys")
    ap.add_argument("--service-id", help="Render service id, skips auto-discovery")
    args = ap.parse_args()

    secrets, source = load_secrets(args.secrets)
    print(f"Secrets source: {source or 'environment only'}")
    for key in WANTED:
        value = secrets.get(key, "")
        if not value:
            print(f"  {key:<20} MISSING")
        else:
            mode = ""
            if key == "STRIPE_SECRET_KEY":
                mode = " (TEST mode)" if value.startswith("sk_test") else " (LIVE mode)"
            print(f"  {key:<20} found, {len(value)} chars{mode}")

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
    if secrets.get("STRIPE_SECRET_KEY"):
        updates["STRIPE_SECRET_KEY"] = secrets["STRIPE_SECRET_KEY"]
    if secrets.get("ANTHROPIC_API_KEY"):
        updates["ANTHROPIC_API_KEY"] = secrets["ANTHROPIC_API_KEY"]
    if base_url:
        updates["PUBLIC_BASE_URL"] = base_url

    if not updates:
        return err("nothing to set. No STRIPE_SECRET_KEY or ANTHROPIC_API_KEY found.")

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
    print(f"  deploy {deploy.get('id')} status {deploy.get('status')}")
    print("  waiting for the service to come back up")
    time.sleep(20)
    return verify(base_url) if base_url else 0


def err(message: str) -> int:
    print(f"\nERROR: {message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
