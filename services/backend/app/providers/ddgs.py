"""Local web search for the fallback research agent (section 9): DuckDuckGo results through `ddgs`, no key.

Results are untrusted data. A blocked or failed search returns an error the agent sees, and the agent then works from
the row alone and marks the lead "no trigger found" (outage table, section 5).
"""

import asyncio
import logging

log = logging.getLogger(__name__)
MAX_RESULTS = 8


def _search(query):
    from ddgs import DDGS

    return DDGS().text(query, max_results=MAX_RESULTS, safesearch="moderate")


async def search(query):
    """Up to 8 results as [{title, url, snippet}], or {"error": reason}."""
    query = " ".join(str(query or "").split())[:300]
    if len(query) < 2:
        return {"error": "Give a search query"}
    try:
        found = await asyncio.wait_for(asyncio.to_thread(_search, query), timeout=20)
    except Exception as exc:
        log.warning("Web search failed: %s", type(exc).__name__)
        return {"error": f"Search unavailable ({type(exc).__name__}); use the row and pages you can read"}
    return [
        {
            "title": str(r.get("title", ""))[:200],
            "url": str(r.get("href", ""))[:500],
            "snippet": str(r.get("body", ""))[:400],
        }
        for r in found or []
        if r.get("href")
    ]
