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


def test_edgar_phrases_tries_the_whole_topic_then_its_first_three_words():
    """The retry is what took live EDGAR coverage from 2/7 to 5/7 topics.

    A whole topic quoted verbatim often matches nothing, or matches only a
    marginal filing, so the first three words -- still a prefix of what the
    buyer actually asked for -- are always tried too.
    """
    assert main.edgar_phrases("buy now pay later regulation") == [
        "buy now pay later regulation",
        "buy now pay",
    ]


def test_edgar_phrases_does_not_repeat_a_topic_of_three_words_or_fewer():
    assert main.edgar_phrases("open banking") == ["open banking"]
    assert main.edgar_phrases("buy now pay") == ["buy now pay"]


def test_edgar_hit_becomes_a_real_archives_url():
    """Four silent-failure reformats in one URL, plus the dedupe rule.

    The accession loses its dashes, the CIK loses its zero padding, and the
    filename has to be split off the "_id". Get any of them wrong and the
    result still renders as a link, so someone who paid $10 gets a 404 instead
    of an error. This payload is a real EDGAR response, trimmed, and the
    expected URL was confirmed live at 200 OK.

    Dedupe also has to key on the registrant, not the accession: a real
    63-hit live payload had the same ETF trust spread across two different
    accessions for its prospectus exhibits, which accession-only dedupe
    cannot see.
    """
    beyond_meat = {
        "ciks": ["0001655210"],
        "display_names": ["BEYOND MEAT, INC.  (BYND)  (CIK 0001655210)"],
        "adsh": "0001655210-26-000057",
        "form": "8-K",
        "file_date": "2026-08-11",
    }
    same_registrant_different_accession = {
        "ciks": ["0001655210"],
        "display_names": ["BEYOND MEAT, INC.  (BYND)  (CIK 0001655210)"],
        "adsh": "0001655210-26-000060",
        "form": "424B3",
        "file_date": "2026-08-12",
    }
    payload = {
        "hits": {
            "hits": [
                {
                    "_id": "0001655210-26-000057:bynd-20260811.htm",
                    "_source": beyond_meat,
                },
                # Same filing, second matching document. Must be deduped away.
                {"_id": "0001655210-26-000057:ex991press.htm", "_source": beyond_meat},
                # Same registrant, a DIFFERENT accession. Must also be deduped
                # away -- accession-only dedupe would let this one through.
                {
                    "_id": "0001655210-26-000060:prospectus.htm",
                    "_source": same_registrant_different_accession,
                },
                # No colon, so no filename. Must be dropped, never guessed at.
                {
                    "_id": "0001234567-26-000001",
                    "_source": {
                        "ciks": ["0001234567"],
                        "display_names": ["Nope Inc"],
                        "adsh": "0001234567-26-000001",
                        "form": "10-K",
                        "file_date": "2026-08-01",
                    },
                },
            ]
        }
    }
    rows = main.parse_edgar_hits(payload)
    assert len(rows) == 1, f"expected one deduped row, got {rows}"
    assert rows[0]["url"] == (
        "https://www.sec.gov/Archives/edgar/data/1655210/"
        "000165521026000057/bynd-20260811.htm"
    )


def test_fetch_sec_filings_retries_a_thin_result_with_the_shorter_phrase(monkeypatch):
    """The retry is the whole difference between a 1-citation brief and a 6.

    Measured live: "buy now pay later regulation" quoted whole matches one
    filing, its first three words match 62. Nothing else in this suite touches
    fetch_sec_filings, so without this test the retry could be deleted and the
    suite would stay green.
    """
    import asyncio

    import httpx

    def fake_hit(i):
        return {
            "_id": f"000000000{i}-26-00000{i}:f{i}.htm",
            "_source": {
                "ciks": [f"000000000{i}"],
                "display_names": [f"Filer {i}"],
                "adsh": f"000000000{i}-26-00000{i}",
                "form": "8-K",
                "file_date": "2026-08-06",
            },
        }

    asked = []

    class _FakeResponse:
        def __init__(self, count):
            self.count = count

        def raise_for_status(self):
            pass

        def json(self):
            return {"hits": {"hits": [fake_hit(i) for i in range(self.count)]}}

    class _FakeClient:
        def __init__(self, **_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def get(self, _url, params=None):
            asked.append(params["q"])
            return _FakeResponse(1 if len(asked) == 1 else 6)

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    rows = asyncio.run(main.fetch_sec_filings("buy now pay later regulation"))
    assert asked == ['"buy now pay later regulation"', '"buy now pay"']
    assert len(rows) == 6, f"thin first result was not retried, got {rows}"


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
    r = client.get(
        "/brief/bnpl?session_id=cs_test_fake", headers={"Accept": "application/json"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["topic"] == "BNPL & Embedded Finance"
    assert "BNPL" in body["brief"]


def test_browser_gets_a_rendered_page_not_raw_json(monkeypatch):
    """The first real buyer got escaped JSON in a browser. Never again.

    A person paying by card lands here from Stripe with a browser Accept
    header. They must get readable HTML, not a JSON blob full of \\n.
    """
    _stub_session(monkeypatch, "paid", "bnpl")
    r = client.get(
        "/brief/bnpl?session_id=cs_test_fake",
        headers={"Accept": "text/html,application/xhtml+xml"},
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    body = r.text
    assert "<h1>" in body  # markdown was rendered
    assert "\\n" not in body  # no escaped newlines
    assert '{"brief"' not in body  # not a JSON dump
    assert "Paid." in body  # receipt line for the buyer
    assert 'href="https://www.orrick.com' in body or "href=" in body


def test_agent_with_x_payment_still_gets_json(monkeypatch):
    """An agent must never be handed HTML just because it accepts everything."""
    monkeypatch.setattr(main, "FACILITATOR_MODE", "cdp")
    r = client.get("/brief/bnpl", headers={"Accept": "*/*"})
    # No payment supplied, so this is the 402 path, but it must be JSON.
    assert r.status_code == 402
    assert "json" in r.headers["content-type"]


def test_wants_html_logic():
    from starlette.datastructures import Headers

    class Req:
        def __init__(self, headers):
            self.headers = Headers(headers)

    assert main.wants_html(Req({"accept": "text/html"})) is True
    assert main.wants_html(Req({"accept": "application/json"})) is False
    assert main.wants_html(Req({"accept": "*/*"})) is False
    # An agent that sends X-PAYMENT gets JSON even if it accepts HTML.
    assert main.wants_html(Req({"accept": "text/html", "x-payment": "abc"})) is False


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
# /research citation enforcement: the $10 route must be sourced too
# ---------------------------------------------------------------------------


def _stub_anthropic(monkeypatch, text="Stub brief body."):
    """Fake the Anthropic call so citation tests never make a real request."""
    import anthropic

    class _FakeTextBlock:
        type = "text"

        def __init__(self, text):
            self.text = text

    class _FakeMessage:
        def __init__(self, text):
            self.content = [_FakeTextBlock(text)]

    class _FakeMessages:
        async def create(self, **_):
            return _FakeMessage(text)

    class _FakeAsyncAnthropic:
        def __init__(self, **_):
            self.messages = _FakeMessages()

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _FakeAsyncAnthropic)


def test_research_response_carries_real_sec_sources_when_edgar_has_hits(monkeypatch):
    """The $10 route must carry the same citation guarantee as the $2 briefs.

    test_brief_carries_at_least_six_sources enforces >= 6 source links on the
    catalog briefs but is parametrized only over CATALOG, so /research had no
    citation enforcement at all. The Sources section is appended from the
    filings EDGAR actually returned, not from anything the model claims, so
    the link count cannot silently drop to zero the way the old unsourced
    brief did.
    """
    monkeypatch.setattr(main, "ANTHROPIC_API_KEY", "sk-ant-test")
    _stub_anthropic(monkeypatch)

    filings = [
        {
            "company": f"Test Filer {i} Inc.",
            "form": "8-K",
            "filed": "2026-08-06",
            "url": f"https://www.sec.gov/Archives/edgar/data/{1000000 + i}/000000/f{i}.htm",
        }
        for i in range(6)
    ]

    async def fake_fetch(topic):
        return filings

    monkeypatch.setattr(main, "fetch_sec_filings", fake_fetch)
    _stub_session(monkeypatch, "paid", "research")
    r = client.post(
        "/research?session_id=cs_test_fake",
        json={"topic": "buy now pay later regulation"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["sources"] == filings
    urls = re.findall(r"https?://", body["brief"])
    assert len(urls) >= 6, (
        f"research brief has {len(urls)} source links, needs at least 6"
    )


def test_research_degrades_without_500_when_edgar_is_unreachable(monkeypatch):
    """EDGAR being down must never fail a request that has already been paid for."""
    monkeypatch.setattr(main, "ANTHROPIC_API_KEY", "sk-ant-test")
    _stub_anthropic(monkeypatch)

    async def fake_fetch(topic):
        return None

    monkeypatch.setattr(main, "fetch_sec_filings", fake_fetch)
    _stub_session(monkeypatch, "paid", "research")
    r = client.post(
        "/research?session_id=cs_test_fake",
        json={"topic": "embedded finance licensing in Brazil"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["sources"] == []
    assert "unreachable" in body["sources_note"]


def test_research_notes_no_filer_distinctly_from_edgar_being_down(monkeypatch):
    """No filer matching is a different fact from EDGAR being unreachable."""
    monkeypatch.setattr(main, "ANTHROPIC_API_KEY", "sk-ant-test")
    _stub_anthropic(monkeypatch)

    async def fake_fetch(topic):
        return []

    monkeypatch.setattr(main, "fetch_sec_filings", fake_fetch)
    _stub_session(monkeypatch, "paid", "research")
    r = client.post(
        "/research?session_id=cs_test_fake",
        json={"topic": "stablecoin custody rules"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["sources"] == []
    assert "unreachable" not in body["sources_note"]
    assert "No SEC filing" in body["sources_note"]


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
