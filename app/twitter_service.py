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
        Scrapes tweet text using ScrapingDog.
        """
        if not self.api_key:
            logger.warning("No SCRAPINGDOG_API_KEY provided. Returning mock data.")
            return "Twitter API Key missing. Please set SCRAPINGDOG_API_KEY."

        tweet_id = self._extract_id(url)
        if not tweet_id:
            raise ValueError(f"Could not extract tweet ID from {url}")

        # Correct endpoint for single tweet scraping (X Post Scraper API)
        # Based on: https://api.scrapingdog.com/x/post
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
                    raise RuntimeError(f"Failed to fetch tweet {tweet_id}: {e}")
                time.sleep(1)
        
        data = r.json()
        text = self._parse_text(data)
        
        if not text:
            raise RuntimeError("Tweet text was empty.")
            
        return text

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
