"""Tests for the Sentinel Intelligence API.

Run: .venv/Scripts/python -m pytest -q

The content guards are deliberate. The paid product previously shipped
uncited claims and a fabricated penalty figure, so the citation standard is
enforced here rather than left to good intentions.
"""

import re

import pytest
from fastapi.testclient import TestClient

import main
from main import CATALOG, app

client = TestClient(app)


# ---------------------------------------------------------------------------
# Content guards: what we sell must be sourced
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", sorted(CATALOG))
def test_brief_exists(slug):
    assert main.brief_available(slug), f"{slug}.md is missing, so the route would 503"


@pytest.mark.parametrize("slug", sorted(CATALOG))
def test_brief_carries_at_least_six_sources(slug):
    text = main.load_brief(slug)
    urls = re.findall(r"https?://", text)
    assert len(urls) >= 6, f"{slug}.md has {len(urls)} source links, needs at least 6"


@pytest.mark.parametrize("slug", sorted(CATALOG))
def test_brief_is_dated(slug):
    text = main.load_brief(slug)
    assert re.search(r"\*\*Edition:\*\*\s*\d{4}-\d{2}-\d{2}", text), (
        f"{slug}.md has no machine-readable edition date"
    )


def test_no_penalty_figure_appears_in_two_briefs_at_once():
    """The old briefs cited $43,792 for both the CFPB and the FTC.

    Any dollar figure over four digits that shows up in more than one brief is
    the same failure mode repeating, so fail on the pattern, not the number.
    """
    seen = {}
    for slug in CATALOG:
        for figure in set(re.findall(r"\$[\d,]{6,}", main.load_brief(slug))):
            seen.setdefault(figure, []).append(slug)
    shared = {fig: slugs for fig, slugs in seen.items() if len(slugs) > 1}
    assert not shared, f"Same dollar figure used across briefs: {shared}"


# ---------------------------------------------------------------------------
# Delivery preflight: never charge for what cannot be delivered
# ---------------------------------------------------------------------------


def test_missing_brief_returns_503_not_a_payment_demand(monkeypatch):
    monkeypatch.setattr(main, "brief_available", lambda slug: False)
    r = client.get("/brief/bnpl")
    assert r.status_code == 503
    assert r.status_code != 402


def test_research_without_api_key_returns_503_before_payment(monkeypatch):
    monkeypatch.setattr(main, "ANTHROPIC_API_KEY", "")
    r = client.post("/research", json={"topic": "embedded finance in Brazil"})
    assert r.status_code == 503
    assert "No payment was taken" in r.json()["detail"]


def test_research_with_key_but_no_payment_returns_402(monkeypatch):
    monkeypatch.setattr(main, "ANTHROPIC_API_KEY", "sk-ant-test")
    r = client.post("/research", json={"topic": "embedded finance in Brazil"})
    assert r.status_code == 402


def test_research_rejects_short_topic():
    r = client.post("/research", json={"topic": "abc"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Payment gates
# ---------------------------------------------------------------------------


def test_unpaid_brief_returns_402_with_x402_requirements():
    r = client.get("/brief/bnpl")
    assert r.status_code == 402
    assert "PAYMENT-REQUIRED" in r.headers


def test_unpaid_brief_advertises_card_path_when_stripe_configured(monkeypatch):
    monkeypatch.setattr(main, "STRIPE_SECRET_KEY", "sk_test_x")
    r = client.get("/brief/bnpl")
    assert r.status_code == 402
    assert r.json()["pay_with_card"].endswith("/buy/bnpl")


def test_unknown_brief_returns_404():
    assert client.get("/brief/not-a-real-brief").status_code == 404


def test_buy_without_stripe_configured_returns_503(monkeypatch):
    monkeypatch.setattr(main, "STRIPE_SECRET_KEY", "")
    assert client.get("/buy/bnpl").status_code == 503


def test_buy_unknown_slug_returns_404():
    assert client.get("/buy/not-a-real-brief").status_code == 404


def test_buy_returns_503_not_500_when_stripe_rejects_the_key(monkeypatch):
    """A bad or expired Stripe key must not show a buyer a stack trace."""
    import stripe

    monkeypatch.setattr(main, "STRIPE_SECRET_KEY", "sk_test_invalid")

    def boom(**_):
        raise stripe.error.AuthenticationError("Invalid API Key provided")

    monkeypatch.setattr(stripe.checkout.Session, "create", staticmethod(boom))
    r = client.get("/buy/bnpl", follow_redirects=False)
    assert r.status_code == 503
    assert "No payment was taken" in r.json()["detail"]


def test_local_facilitator_refuses_to_serve_paid_content(monkeypatch):
    """Local mode verifies signatures but moves no funds.

    Serving content there gives the product away, so it must 503 unless
    explicitly allowed.
    """
    monkeypatch.setattr(main, "FACILITATOR_MODE", "local")
    monkeypatch.setattr(main, "ALLOW_LOCAL_FACILITATOR", False)
    r = client.get("/brief/bnpl", headers={"X-PAYMENT": "irrelevant-but-present"})
    assert r.status_code == 503
    assert "does not move funds" in r.json()["detail"]


def test_session_verify_failure_does_not_leak_stripe_internals(monkeypatch):
    """Stripe error strings echo key material. They must not reach the client."""
    import stripe

    monkeypatch.setattr(main, "STRIPE_SECRET_KEY", "sk_test_invalid")

    def boom(*_, **__):
        raise stripe.error.AuthenticationError(
            "Invalid API Key provided: sk_test_********leak"
        )

    monkeypatch.setattr(stripe.checkout.Session, "retrieve", staticmethod(boom))
    r = client.get("/brief/bnpl?session_id=cs_test_bogus")
    assert r.status_code == 402
    assert "sk_test" not in r.text
    assert "Invalid API Key" not in r.text


def _stub_session(monkeypatch, payment_status, slug):
    """Return a REAL stripe object, not a dict.

    An earlier version of this test mocked _verify_stripe_session itself and
    therefore never exercised stripe's object semantics. It missed that
    StripeObject is not a dict subclass in stripe>=15, so session.get(...)
    raised AttributeError and every returning buyer got a 500 after paying.
    """
    import stripe

    monkeypatch.setattr(main, "STRIPE_SECRET_KEY", "sk_test_x")
    session = stripe.checkout.Session.construct_from(
        {
            "id": "cs_test_fake",
            "object": "checkout.session",
            "payment_status": payment_status,
            "metadata": {"slug": slug},
        },
        "sk_test_x",
    )
    monkeypatch.setattr(
        stripe.checkout.Session, "retrieve", staticmethod(lambda *_, **__: session)
    )


def test_paid_session_delivers_the_brief(monkeypatch):
    """The money path. A buyer returning from Stripe must get what they paid for."""
    _stub_session(monkeypatch, "paid", "bnpl")
    r = client.get("/brief/bnpl?session_id=cs_test_fake")
    assert r.status_code == 200
    body = r.json()
    assert body["topic"] == "BNPL & Embedded Finance"
    assert "BNPL" in body["brief"]


def test_stripe_session_path_rejects_unpaid_session(monkeypatch):
    _stub_session(monkeypatch, "unpaid", "bnpl")
    r = client.get("/brief/bnpl?session_id=cs_test_fake")
    assert r.status_code == 402
    assert "not paid" in r.json()["detail"]


def test_paid_session_for_another_brief_is_rejected(monkeypatch):
    """A $2 session for one brief must not unlock a different one."""
    _stub_session(monkeypatch, "paid", "ai-governance")
    r = client.get("/brief/bnpl?session_id=cs_test_fake")
    assert r.status_code == 402
    assert "different resource" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Human and agent surfaces
# ---------------------------------------------------------------------------


def test_landing_page_renders_a_buy_button_when_stripe_configured(monkeypatch):
    monkeypatch.setattr(main, "STRIPE_SECRET_KEY", "sk_test_x")
    body = client.get("/").text
    assert 'href="/buy/bnpl"' in body
    assert "Buy for $2.00" in body


def test_landing_page_says_so_when_checkout_is_unconfigured(monkeypatch):
    monkeypatch.setattr(main, "STRIPE_SECRET_KEY", "")
    body = client.get("/").text
    assert "Card checkout not configured" in body
    assert 'href="/buy/bnpl"' not in body


def test_landing_page_lists_every_catalog_item():
    body = client.get("/").text
    for item in CATALOG.values():
        assert item["title"] in body


def test_health_reports_delivery_and_settlement_truthfully():
    data = client.get("/health").json()
    assert set(data["briefs_available"]) == set(CATALOG)
    assert isinstance(data["settlement_moves_funds"], bool)
    assert "problems" in data
    if not data["settlement_moves_funds"]:
        assert "x402_settlement_does_not_move_funds" in data["problems"]


def test_discovery_hides_research_when_it_cannot_run(monkeypatch):
    monkeypatch.setattr(main, "ANTHROPIC_API_KEY", "")
    paths = [
        r["path"] for r in client.get("/.well-known/x402.json").json()["resources"]
    ]
    assert "/research" not in paths


def test_discovery_lists_available_briefs():
    paths = [
        r["path"] for r in client.get("/.well-known/x402.json").json()["resources"]
    ]
    for slug in CATALOG:
        assert f"/brief/{slug}" in paths
