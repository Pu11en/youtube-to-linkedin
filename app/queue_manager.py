import json
import logging
from datetime import datetime
from typing import List, Optional
from app.config import Config

try:
    from upstash_redis import Redis
except ImportError:
    Redis = None

logger = logging.getLogger(__name__)

class SimpleQueue:
    """Manages a newline-separated list of URLs stored in Redis (Vercel KV)."""
    KEY = "youtube_queue_v2"
    DONE_KEY = "youtube_done_v2"

    def __init__(self, config: Config):
        if not config.kv_url or not config.kv_token:
            self.redis = None
            logger.warning("KV_URL/KV_TOKEN not set. Queue will be in-memory (and temporary).")
            self._local_queue = []
        else:
            self.redis = Redis(url=config.kv_url, token=config.kv_token)

    def get_urls(self) -> List[str]:
        if not self.redis: return self._local_queue
        # Returns list of pending URLs
        try:
            data = self.redis.get(self.KEY)
            if not data: return []
            return [u.strip() for u in data.split("\n") if u.strip()]
        except Exception as e:
            logger.error(f"Redis get failed: {e}")
            return []

    def set_urls(self, urls: List[str]):
        text = "\n".join(u.strip() for u in urls if u.strip())
        if not self.redis:
            self._local_queue = urls
            return
        try:
            self.redis.set(self.KEY, text)
        except Exception as e:
            logger.error(f"Redis set failed: {e}")

    def add_url(self, url: str):
        urls = self.get_urls()
        if url not in urls:
            urls.append(url)
            self.set_urls(urls)

    def pop_next(self) -> Optional[str]:
        urls = self.get_urls()
        if not urls: return None
        next_url = urls.pop(0)
        self.set_urls(urls)
        return next_url

    def mark_done(self, url: str):
        if not self.redis: return
        try:
            self.redis.lpush(self.DONE_KEY, json.dumps({
                "url": url,
                "done_at": datetime.utcnow().isoformat()
            }))
            # Keep only last 20 done
            self.redis.ltrim(self.DONE_KEY, 0, 19)
        except Exception as e:
            logger.error(f"Redis mark_done failed: {e}")

    def get_history(self) -> List[dict]:
        if not self.redis: return []
        try:
            items = self.redis.lrange(self.DONE_KEY, 0, 19)
            return [json.loads(i) for i in items]
        except Exception as e:
            logger.error(f"Redis history failed: {e}")
            return []
