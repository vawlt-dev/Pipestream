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
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, region="nz-en", max_results=max_results))

        
        if not results:
            return f"No results found for: {query}"

        output = []
        for r in results:
            title = r.get("title", "No title")
            url = r.get("href", r.get("link", ""))
            snippet = r.get("body", r.get("snippet", ""))

            output.append(f"**{title}**\n{url}\n{snippet}\n")
        
        return "\n".join(output)
        
    except Exception as e:
        return f"Search error: {str(e)}"

# =============================================================================
# URL SCRAPING
# =============================================================================

def scrape_url(url: str, max_chars: int = 5000) -> str:
    """
    Fetch and extract text content from a URL.
    
    Returns the main text content, limited to max_chars.
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, "html.parser")
        
        # Remove script and style elements
        for element in soup(["script", "style", "nav", "footer", "header"]):
            element.decompose()
        
        # Get text
        text = soup.get_text(separator="\n", strip=True)
        
        # Clean up whitespace
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        text = "\n".join(lines)
        
        # Truncate if needed
        if len(text) > max_chars:
            text = text[:max_chars] + "\n... (truncated)"
        
        return text
        
    except Exception as e:
        return f"Scrape error for {url}: {str(e)}"

# =============================================================================
# TEST
# =============================================================================

if __name__ == "__main__":
    print("Testing web search...")
    results = web_search("Xero New Zealand software")
    print(results[:1000])
    print("\n" + "="*50 + "\n")
