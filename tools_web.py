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

import time

# =============================================================================
# WEB SEARCH
# =============================================================================

def web_search(query: str, max_results: int = 5) -> str:
    """
    Search the web using DuckDuckGo.
    Returns a formatted string of results with titles, URLs, and snippets.
    """
    import time as _time
    print(f"\n  \033[36m[WEB SEARCH] query={query!r}  max_results={max_results}\033[0m", flush=True)
    t0 = _time.time()
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, region="nz-en", max_results=max_results))

        if not results:
            print(f"  \033[36m[WEB SEARCH] No results ({_time.time()-t0:.1f}s)\033[0m", flush=True)
            return f"No results found for: {query}"

        output = []
        for i, r in enumerate(results, 1):
            title   = r.get("title", "No title")
            url     = r.get("href", r.get("link", ""))
            snippet = r.get("body", r.get("snippet", ""))
            print(f"  \033[36m[WEB SEARCH]  {i}. {title}\033[0m", flush=True)
            print(f"  \033[36m             {url}\033[0m", flush=True)
            output.append(f"**{title}**\n{url}\n{snippet}\n")

        result_str = "\n".join(output)
        print(f"  \033[36m[WEB SEARCH] Done — {len(results)} results, {len(result_str)} chars ({_time.time()-t0:.1f}s)\033[0m", flush=True)
        return result_str

    except Exception as e:
        print(f"  \033[36m[WEB SEARCH] ERROR: {e}\033[0m", flush=True)
        return f"Search error: {str(e)}"

# =============================================================================
# URL SCRAPING
# =============================================================================

def scrape_url(url: str, max_chars: int = 5000) -> str:
    """
    Fetch and extract text content from a URL.
    Returns the main text content, limited to max_chars.
    """
    import time as _time
    print(f"\n  \033[35m[SCRAPE] {url}\033[0m", flush=True)
    t0 = _time.time()
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }

        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        print(f"  \033[35m[SCRAPE] HTTP {response.status_code}, {len(response.text)} bytes raw\033[0m", flush=True)

        soup = BeautifulSoup(response.text, "html.parser")
        for element in soup(["script", "style", "nav", "footer", "header"]):
            element.decompose()

        text  = soup.get_text(separator="\n", strip=True)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        text  = "\n".join(lines)

        if len(text) > max_chars:
            text = text[:max_chars] + "\n... (truncated)"

        print(f"  \033[35m[SCRAPE] Extracted {len(text)} chars ({_time.time()-t0:.1f}s)\033[0m", flush=True)
        print(f"  \033[35m[SCRAPE] Preview: {text[:200].replace(chr(10), ' ')}\033[0m", flush=True)
        return text

    except Exception as e:
        print(f"  \033[35m[SCRAPE] ERROR: {e}\033[0m", flush=True)
        return f"Scrape error for {url}: {str(e)}"

# =============================================================================
# TEST
# =============================================================================

if __name__ == "__main__":
    print("Testing web search...")
    results = web_search("Xero New Zealand software")
    print(results[:1000])
    print("\n" + "="*50 + "\n")
