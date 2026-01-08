import os
import re
import time
import requests
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

def http_raise(resp: requests.Response) -> None:
    if resp.ok:
        return
    try:
        detail = resp.json()
    except Exception:
        detail = resp.text[:200]
    raise RuntimeError(f"HTTP {resp.status_code}: {detail}")

class TwitterService:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def get_tweet_text(self, url: str) -> str:
        """
        Scrapes tweet text using ScrapingDog with Nitter fallback.
        """
        tweet_id = self._extract_id(url)
        if not tweet_id:
            raise ValueError(f"Could not extract tweet ID from {url}")

        if self.api_key:
            try:
                # Primary: Scrapingdog
                return self._scrape_scrapingdog(tweet_id)
            except Exception as e:
                logger.warning(f"Scrapingdog failed: {e}. Trying Nitter fallback...")
        
        # Fallback: Nitter
        return self._scrape_nitter(tweet_id)

    def _scrape_scrapingdog(self, tweet_id: str) -> str:
        # Correct endpoint for single tweet scraping (X Post Scraper API)
        api_url = "http://api.scrapingdog.com/x/post"
        params = {"api_key": self.api_key, "tweetId": tweet_id}
        
        logger.info(f"Fetching tweet {tweet_id} via ScrapingDog...")

        for attempt in range(3):
            try:
                r = requests.get(api_url, params=params, timeout=30)
                if r.status_code == 500 and attempt < 2:
                    time.sleep(1)
                    continue
                http_raise(r)
                break
            except Exception as e:
                if attempt == 2:
                    raise RuntimeError(f"Scrapingdog fetch failed: {e}")
                time.sleep(1)
        
        data = r.json()
        text = self._parse_text(data)
        if not text:
            raise RuntimeError("Tweet text was empty from Scrapingdog.")
        return text

    def _scrape_nitter(self, tweet_id: str) -> str:
        """Uses a public Nitter instance as fallback."""
        # Use a reliable Nitter instance
        nitter_instances = [
            "https://nitter.net",
            "https://nitter.cz",
            "https://nitter.it"
        ]
        
        for instance in nitter_instances:
            try:
                logger.info(f"Trying Nitter ({instance}) for {tweet_id}...")
                # Nitter format usually: instance/i/status/id
                # But we can try the RSS feed or just the page
                r = requests.get(f"{instance}/i/status/{tweet_id}", timeout=15)
                if r.ok:
                    # Very basic scrape from HTML
                    # Look for <div class="tweet-content media-body">
                    from html import unescape
                    content_match = re.search(r'<div class="tweet-content media-body">(.*?)</div>', r.text, re.DOTALL)
                    if content_match:
                        raw_html = content_match.group(1)
                        # Strip HTML tags
                        text = re.sub(r'<[^>]+>', '', raw_html)
                        text = unescape(text).strip()
                        if text:
                            return f"Tweet (via Nitter): {text}"
            except Exception as e:
                logger.warning(f"Nitter instance {instance} failed: {e}")
                continue
                
        raise RuntimeError(f"All scraping methods failed for tweet {tweet_id}")

    def _extract_id(self, url: str) -> str:
        match = re.search(r"/status/(\d+)", url)
        if match:
            return match.group(1)
        return ""

    def _parse_text(self, data: Dict[str, Any]) -> str:
        # Normalize defensively (Scrapingdog /x/post response format)
        text = data.get("full_tweet") or data.get("tweet") or data.get("text") or data.get("full_text") or ""
        
        # We can also append author context
        user = data.get("user") or {}
        handle = user.get("profile_handle") or data.get("author_handle") or ""
        
        cleaned = re.sub(r"\s+", " ", str(text)).strip()
        
        if handle:
             return f"Tweet by @{handle}: {cleaned}"
        return cleaned
