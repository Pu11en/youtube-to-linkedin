import json
import logging
from datetime import datetime
from typing import List, Optional, Dict
from app.config import Config

try:
    from upstash_redis import Redis
except ImportError:
    Redis = None

logger = logging.getLogger(__name__)


class ClientManager:
    """Manages client configurations (name -> blotato_account_id mapping)."""
    KEY = "linkedin_clients"
    
    def __init__(self, config: Config):
        self.config = config
        if not config.kv_url or not config.kv_token:
            self.redis = None
            self._local_clients = {}
        else:
            self.redis = Redis(url=config.kv_url, token=config.kv_token)
    
    def get_all(self) -> Dict[str, dict]:
        """Get all clients as {name: {blotato_account_id: ...}}"""
        if not self.redis:
            return self._local_clients
        try:
            data = self.redis.get(self.KEY)
            if not data:
                return {}
            return json.loads(data)
        except Exception as e:
            logger.error(f"Redis get clients failed: {e}")
            return {}
    
    def add_client(self, name: str, blotato_account_id: str, settings: dict = None):
        """Add or update a client."""
        clients = self.get_all()
        client_data = {"blotato_account_id": blotato_account_id}
        if settings:
            client_data.update(settings)
        elif name in clients:
            # Preserve existing settings if only updating ID
            clients[name].update(client_data)
            client_data = clients[name]
        
        clients[name] = client_data
        if not self.redis:
            self._local_clients = clients
            return
        try:
            self.redis.set(self.KEY, json.dumps(clients))
        except Exception as e:
            logger.error(f"Redis set clients failed: {e}")
    
    def update_settings(self, name: str, settings: dict):
        """Update specific settings for a client."""
        clients = self.get_all()
        if name in clients:
            clients[name].update(settings)
            if self.redis:
                self.redis.set(self.KEY, json.dumps(clients))
            else:
                self._local_clients = clients
    
    def get_client(self, name: str) -> Optional[dict]:
        """Get a single client's config."""
        clients = self.get_all()
        return clients.get(name)
    
    def remove_client(self, name: str):
        """Remove a client."""
        clients = self.get_all()
        if name in clients:
            del clients[name]
            if self.redis:
                self.redis.set(self.KEY, json.dumps(clients))
            else:
                self._local_clients = clients


class SimpleQueue:
    """Manages a newline-separated list of URLs stored in Redis (Vercel KV)."""
    KEY = "youtube_queue_v2"
    DONE_KEY = "youtube_done_v2"

    def __init__(self, config: Config):
        if not config.kv_url or not config.kv_token:
            self.redis = None
            logger.warning("KV_URL/KV_TOKEN not set. Queue will be in-memory (and temporary).")
            self._local_queues = {}
        else:
            self.redis = Redis(url=config.kv_url, token=config.kv_token)

    def _queue_key(self, client_id: str = "default") -> str:
        return f"{self.KEY}:{client_id}"
    
    def _done_key(self, client_id: str = "default") -> str:
        return f"{self.DONE_KEY}:{client_id}"

    def get_urls(self, client_id: str = "default") -> List[str]:
        if not self.redis:
            return self._local_queues.get(client_id, [])
        try:
            data = self.redis.get(self._queue_key(client_id))
            if not data: return []
            return [u.strip() for u in data.split("\n") if u.strip()]
        except Exception as e:
            logger.error(f"Redis get failed: {e}")
            return []

    def set_urls(self, urls: List[str], client_id: str = "default"):
        text = "\n".join(u.strip() for u in urls if u.strip())
        if not self.redis:
            self._local_queues[client_id] = urls
            return
        try:
            self.redis.set(self._queue_key(client_id), text)
        except Exception as e:
            logger.error(f"Redis set failed: {e}")

    def add_url(self, url: str, client_id: str = "default"):
        urls = self.get_urls(client_id)
        if url not in urls:
            urls.append(url)
            self.set_urls(urls, client_id)

    def pop_next(self, client_id: str = "default") -> Optional[str]:
        urls = self.get_urls(client_id)
        if not urls: return None
        next_url = urls.pop(0)
        self.set_urls(urls, client_id)
        return next_url

    def mark_done(self, url: str, client_id: str = "default"):
        if not self.redis: return
        try:
            self.redis.lpush(self._done_key(client_id), json.dumps({
                "url": url,
                "done_at": datetime.utcnow().isoformat()
            }))
            # Keep only last 20 done
            self.redis.ltrim(self._done_key(client_id), 0, 19)
        except Exception as e:
            logger.error(f"Redis mark_done failed: {e}")

    def get_history(self, client_id: str = "default") -> List[dict]:
        if not self.redis: return []
        try:
            items = self.redis.lrange(self._done_key(client_id), 0, 19)
            return [json.loads(i) for i in items]
        except Exception as e:
            logger.error(f"Redis history failed: {e}")
            return []
