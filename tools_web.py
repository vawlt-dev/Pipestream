# =============================================================================
# tools_web.py — Web Search and Scraping Tools
# =============================================================================
# Uses DuckDuckGo for search — no API key needed!
#
# Note: DuckDuckGo can be rate-limited if you hammer it.
# For production, consider Tavily or SerpAPI.
# =============================================================================

import requests
from bs4 import BeautifulSoup
from ddgs import DDGS
from urllib.parse import urljoin, urlparse

import time

# =============================================================================
# WEB SEARCH
# =============================================================================

def _ddgs_search(query: str, max_results: int) -> list[dict]:
    """Shared DDGS call backing both web_search() and web_search_structured()."""
    with DDGS() as ddgs:
        return list(ddgs.text(query, region="nz-en", max_results=max_results))


def web_search_structured(query: str, max_results: int = 5) -> list[dict]:
    """
    Same DuckDuckGo search as web_search(), returned as structured rows
    instead of a pretty-printed string — lets callers (explore_topic) grab
    a URL directly without re-parsing formatted text.
    Returns [{"title", "url", "snippet"}, ...], or [] on error/no results.
    """
    print(f"\n  \033[36m[WEB SEARCH] query={query!r}  max_results={max_results}\033[0m", flush=True)
    t0 = time.time()
    try:
        results = _ddgs_search(query, max_results)
        rows = [
            {
                "title":   r.get("title", "No title"),
                "url":     r.get("href", r.get("link", "")),
                "snippet": r.get("body", r.get("snippet", "")),
            }
            for r in results
        ]
        for i, row in enumerate(rows, 1):
            print(f"  \033[36m[WEB SEARCH]  {i}. {row['title']}\033[0m", flush=True)
            print(f"  \033[36m             {row['url']}\033[0m", flush=True)
        print(f"  \033[36m[WEB SEARCH] Done — {len(rows)} results ({time.time()-t0:.1f}s)\033[0m", flush=True)
        return rows
    except Exception as e:
        print(f"  \033[36m[WEB SEARCH] ERROR: {e}\033[0m", flush=True)
        return []


def web_search(query: str, max_results: int = 5) -> str:
    """
    Search the web using DuckDuckGo.
    Returns a formatted string of results with titles, URLs, and snippets.
    """
    rows = web_search_structured(query, max_results)
    if not rows:
        return f"No results found for: {query}"
    return "\n".join(f"**{r['title']}**\n{r['url']}\n{r['snippet']}\n" for r in rows)

# =============================================================================
# URL SCRAPING
# =============================================================================

_SKIP_LINK_SCHEMES = ("mailto:", "tel:", "javascript:")


def _extract_links(soup: BeautifulSoup, base_url: str, cap: int = 20) -> list[dict]:
    """
    Outbound links from the page, in document order — deliberately extracted
    from the FULL soup before any nav/footer/header stripping, since nav is
    exactly where useful "explore more" links live. Resolves relative hrefs
    via urljoin, drops mailto:/tel:/javascript:/bare-fragment hrefs, dedupes
    by URL, caps to `cap` candidates.
    """
    seen: set[str] = set()
    links: list[dict] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#") or href.lower().startswith(_SKIP_LINK_SCHEMES):
            continue
        abs_url = urljoin(base_url, href)
        if not urlparse(abs_url).scheme.startswith("http") or abs_url in seen:
            continue
        seen.add(abs_url)
        text = a.get_text(strip=True)[:80]
        links.append({"text": text or abs_url, "url": abs_url})
        if len(links) >= cap:
            break
    return links


def scrape_url(url: str, max_chars: int = 5000) -> dict:
    """
    Fetch a URL and extract both its main text content and its outbound
    links. Returns {"text": str, "full_text": str, "links": [{"text", "url"}, ...]}
    — "text" is capped to max_chars (what actually feeds an LLM prompt
    downstream, for context-budget reasons); "full_text" is the complete
    untruncated extraction, kept around purely so tracing.py can record the
    real full page content when TRACE_ENABLED=1, independent of whatever cap
    the calling code needs for prompt-building. On error, "text"/"full_text"
    hold the error message and "links" is empty, so callers can treat a
    failure uniformly rather than needing a separate except path.
    """
    print(f"\n  \033[35m[SCRAPE] {url}\033[0m", flush=True)
    t0 = time.time()
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }

        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        print(f"  \033[35m[SCRAPE] HTTP {response.status_code}, {len(response.text)} bytes raw\033[0m", flush=True)

        soup  = BeautifulSoup(response.text, "html.parser")
        links = _extract_links(soup, url)

        for element in soup(["script", "style", "nav", "footer", "header"]):
            element.decompose()

        full_text = soup.get_text(separator="\n", strip=True)
        lines     = [line.strip() for line in full_text.splitlines() if line.strip()]
        full_text = "\n".join(lines)

        text = full_text
        if len(text) > max_chars:
            text = text[:max_chars] + "\n... (truncated)"

        print(f"  \033[35m[SCRAPE] Extracted {len(text)} chars, {len(links)} links ({time.time()-t0:.1f}s)\033[0m", flush=True)
        print(f"  \033[35m[SCRAPE] Preview: {text[:200].replace(chr(10), ' ')}\033[0m", flush=True)
        return {"text": text, "full_text": full_text, "links": links}

    except Exception as e:
        print(f"  \033[35m[SCRAPE] ERROR: {e}\033[0m", flush=True)
        error_msg = f"Scrape error for {url}: {str(e)}"
        return {"text": error_msg, "full_text": error_msg, "links": []}

# =============================================================================
# TEST
# =============================================================================

if __name__ == "__main__":
    print("Testing web search...")
    results = web_search("Xero New Zealand software")
    print(results[:1000])
    print("\n" + "="*50 + "\n")
