import re
import logging
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger(__name__)

def detect_platform(url: str) -> str:
    if not url: return "youtube"
    url = url.lower()
    if "twitter.com" in url or "x.com" in url:
        return "twitter"
    return "youtube"

def extract_tweet_id(url: str) -> str:
    """Extracts numeric status ID from Twitter/X URLs"""
    if not url: return ""
    match = re.search(r"/status/(\d+)", url)
    if match:
        return match.group(1)
    return ""

def extract_youtube_id(url: str) -> str:
    """
    Robustly extracts the 11-character YouTube video ID from various URL formats.
    """
    if not url:
        return ""
    
    url = url.strip()
    
    # 1. Check if it's already an 11-char ID
    if len(url) == 11 and re.match(r'^[a-zA-Z0-9_-]{11}$', url):
        return url
    
    # 2. Check for explicit v= parameter (most reliable for standard links)
    match_v = re.search(r'[?&]v=([0-9A-Za-z_-]{11})', url)
    if match_v:
        return match_v.group(1)
        
    # 3. Check for path-based IDs (youtu.be, embed, shorts, /v/)
    # This regex looks for these prefixes and captures the ID that follows
    match_path = re.search(r'(?:youtu\.be\/|embed\/|shorts\/|\/v\/)([0-9A-Za-z_-]{11})', url)
    if match_path:
        return match_path.group(1)
        
    logger.warning(f"Could not extract video ID from URL: {url}")
    # Return original as fallback, though likely to fail downstream if it's not an ID
    return url
