"""E04/C014: research is bounded official discovery, not a homepage assumption."""

import pytest
from app.providers.inference import InferenceResult
from app.research.fetch import SafeFetcher
from test_remediation_privacy import principal


@pytest.mark.asyncio
@pytest.mark.parametrize("link_variant", ["services", "duplicates", "foreign"])
async def test_official_discovery_bounds(workspace, app, monkeypatch, link_variant):
    service, p = app.state.service, principal(workspace)
    monkeypatch.setattr(service.settings, "simulation", False)
    domain = "prepared.example.test"
    visits, inputs = [], []
    fact = "The company provides maintenance for commercial refrigeration systems."

    async def fetch(fetcher, url):
        assert fetcher.allowed_domains == {domain}
        assert url.startswith(f"https://{domain}"), "No foreign company may supply personalization"
        visits.append(url)
        path = url.removeprefix(f"https://{domain}")
        links = [
            {"url": f"https://{domain}/{name}", "text": name.title()}
            for name in ("services", "about", "locations", "product", "contact")
        ]
        if link_variant == "duplicates":
            links = [*links, *links, {"url": f"https://{domain}/services#details", "text": "Services"}]
        if link_variant == "foreign":
            links.insert(0, {"url": "https://unrelated.example.test/services", "text": "Services"})
        return {
            "url": url,
            "title": "Prepared Company",
            "text": fact if path == "/services" else "Prepared Company official information.",
            "links": links,
        }

    class Extract:
        async def complete(self, instructions, context, schema):
            inputs.append(context)
            sources = [s for s in context["sources"] if s["url"].endswith("/services")]
            return InferenceResult(
                {"facts": [{"statement": fact, "quote": fact, "source_id": s["id"]} for s in sources]},
                {"cost": 0.001},
                "discovery-receipt",
            )

    monkeypatch.setattr(SafeFetcher, "fetch", fetch)
    monkeypatch.setattr(service, "inference", lambda *args: Extract())
    await service.research_company(p, domain, {"company": "Prepared Company", "domain": domain})
    assert f"https://{domain}/services" in visits, (
        "The useful evidence is on an official linked services page"
    )
    assert 1 < len(visits) <= 3 and len(visits) == len(set(visits))
    assert len(inputs) == 1
    with service.db.transaction(False) as s:
        facts = [r.data for r in service.scoped(s, "research_facts", p) if r.data.get("domain") == domain]
        assert any(r["statement"] == fact and r["source_url"].endswith("/services") for r in facts)
        assert all(r["entity_domain"] == domain and r["status"] == "supported" for r in facts)


@pytest.mark.asyncio
async def test_missing_search_capability_keeps_truthful_generic_script(workspace, app, monkeypatch):
    service, p = app.state.service, principal(workspace)
    seen = []
    monkeypatch.setattr(service.settings, "simulation", False)
    monkeypatch.setattr(service, "inference", lambda *args: None)

    async def forbidden(*args, **kwargs):
        seen.append(args)
        raise AssertionError("No configured search provider authorizes a new search service")

    monkeypatch.setattr(SafeFetcher, "fetch", forbidden)
    lead = workspace["client"].get("/v1/workspace", headers=workspace["headers"]).json()["leads"][0]
    await service.prepare(p, [lead["id"]])
    packet = service.packet(p, lead["id"])
    assert packet["script"]["fallback"] is True
    assert not packet["research"]["facts"]
    assert not seen
