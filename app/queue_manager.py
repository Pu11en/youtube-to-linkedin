import json
import logging
from datetime import datetime, timedelta
from typing import List, Optional, Dict
from app.config import Config

try:
    from upstash_redis import Redis
except ImportError:
    Redis = None

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# Chicago timezone for daily tracking
CHICAGO_TZ = ZoneInfo("America/Chicago")


class DailyPostTracker:
    """Tracks daily post count to enforce 5 posts per weekday limit."""
    DAILY_KEY_PREFIX = "daily_posts"
    MAX_POSTS_PER_DAY = 5
    
    def __init__(self, config: Config):
        if not config.kv_url or not config.kv_token:
            self.redis = None
            self._local_counts = {}
        else:
            self.redis = Redis(url=config.kv_url, token=config.kv_token)
    
    def _get_chicago_date(self) -> str:
        """Get current date in Chicago timezone as YYYY-MM-DD."""
        return datetime.now(CHICAGO_TZ).strftime("%Y-%m-%d")
    
    def _get_chicago_weekday(self) -> int:
        """Get current weekday in Chicago timezone (0=Monday, 6=Sunday)."""
        return datetime.now(CHICAGO_TZ).weekday()
    
    def is_weekday(self) -> bool:
        """Check if today is a weekday (Mon-Fri) in Chicago timezone."""
        return self._get_chicago_weekday() < 5
    
    def _daily_key(self) -> str:
        """Redis key for today's post count."""
        return f"{self.DAILY_KEY_PREFIX}:{self._get_chicago_date()}"
    
    def get_daily_count(self) -> int:
        """Get number of posts made today."""
        if not self.redis:
            return self._local_counts.get(self._get_chicago_date(), 0)
        try:
            count = self.redis.get(self._daily_key())
            return int(count) if count else 0
        except Exception as e:
            logger.error(f"Redis get daily count failed: {e}")
            return 0
    
    def increment_daily_count(self) -> int:
        """Increment today's post count. Returns new count."""
        if not self.redis:
            date_key = self._get_chicago_date()
            self._local_counts[date_key] = self._local_counts.get(date_key, 0) + 1
            return self._local_counts[date_key]
        try:
            new_count = self.redis.incr(self._daily_key())
            # Set expiry to 48 hours (auto-cleanup)
            self.redis.expire(self._daily_key(), 48 * 60 * 60)
            return new_count
        except Exception as e:
            logger.error(f"Redis increment daily count failed: {e}")
            return 0
    
    def can_post_today(self) -> bool:
        """Check if we can still post today (under limit and is weekday)."""
        if not self.is_weekday():
            return False
        return self.get_daily_count() < self.MAX_POSTS_PER_DAY
    
    def get_remaining_today(self) -> int:
        """Get remaining posts allowed today."""
        if not self.is_weekday():
            return 0
        return max(0, self.MAX_POSTS_PER_DAY - self.get_daily_count())


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
        if name not in clients:
            # Create the client entry if it doesn't exist (e.g., 'default')
            clients[name] = {}
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


class ExperimentTracker:
    """Tracks post experiments and learns from winners."""
    EXPERIMENTS_KEY = "post_experiments"
    WINNERS_KEY = "post_winners"
    WEIGHTS_KEY = "variation_weights"
    
    def __init__(self, config: Config):
        if not config.kv_url or not config.kv_token:
            self.redis = None
        else:
            self.redis = Redis(url=config.kv_url, token=config.kv_token)
    
    def log_experiment(self, post_id: str, variation: str, url: str, post_text: str):
        """Log which variation was used for a post."""
        if not self.redis:
            return
        try:
            data = {
                "post_id": post_id,
                "variation": variation,
                "url": url,
                "post_preview": post_text[:500],
                "created_at": datetime.utcnow().isoformat(),
                "is_winner": False
            }
            self.redis.hset(self.EXPERIMENTS_KEY, post_id, json.dumps(data))
            # Keep last 100 experiments
            all_keys = self.redis.hkeys(self.EXPERIMENTS_KEY)
            if len(all_keys) > 100:
                oldest = sorted(all_keys)[:len(all_keys) - 100]
                for k in oldest:
                    self.redis.hdel(self.EXPERIMENTS_KEY, k)
        except Exception as e:
            logger.error(f"Log experiment failed: {e}")
    
    def mark_winner(self, post_id: str):
        """Mark a post as a winner - performed well."""
        if not self.redis:
            return False
        try:
            data = self.redis.hget(self.EXPERIMENTS_KEY, post_id)
            if not data:
                return False
            experiment = json.loads(data)
            experiment["is_winner"] = True
            experiment["won_at"] = datetime.utcnow().isoformat()
            
            # Update experiment record
            self.redis.hset(self.EXPERIMENTS_KEY, post_id, json.dumps(experiment))
            
            # Add to winners list
            self.redis.lpush(self.WINNERS_KEY, json.dumps(experiment))
            self.redis.ltrim(self.WINNERS_KEY, 0, 49)  # Keep last 50 winners
            
            # Update variation weights
            self._update_weights(experiment["variation"])
            return True
        except Exception as e:
            logger.error(f"Mark winner failed: {e}")
            return False
    
    def _update_weights(self, winning_variation: str):
        """Increase weight for winning variation."""
        try:
            weights = self.get_weights()
            current = weights.get(winning_variation, 1.0)
            weights[winning_variation] = min(current + 0.5, 5.0)  # Cap at 5x
            self.redis.set(self.WEIGHTS_KEY, json.dumps(weights))
        except Exception as e:
            logger.error(f"Update weights failed: {e}")
    
    def get_weights(self) -> Dict[str, float]:
        """Get current variation weights."""
        if not self.redis:
            return {}
        try:
            data = self.redis.get(self.WEIGHTS_KEY)
            if not data:
                return {}
            return json.loads(data)
        except:
            return {}
    
    def get_winners(self) -> List[dict]:
        """Get list of winning posts."""
        if not self.redis:
            return []
        try:
            items = self.redis.lrange(self.WINNERS_KEY, 0, 49)
            return [json.loads(i) for i in items]
        except:
            return []
    
    def get_stats(self) -> Dict:
        """Get experiment statistics."""
        if not self.redis:
            return {"total": 0, "winners": 0, "weights": {}}
        try:
            total = self.redis.hlen(self.EXPERIMENTS_KEY) or 0
            winners = self.redis.llen(self.WINNERS_KEY) or 0
            weights = self.get_weights()
            
            # Count by variation
            variation_counts = {}
            winner_counts = {}
            all_experiments = self.redis.hgetall(self.EXPERIMENTS_KEY) or {}
            for exp_data in all_experiments.values():
                exp = json.loads(exp_data)
                var = exp.get("variation", "unknown")
                variation_counts[var] = variation_counts.get(var, 0) + 1
                if exp.get("is_winner"):
                    winner_counts[var] = winner_counts.get(var, 0) + 1
            
            return {
                "total_experiments": total,
                "total_winners": winners,
                "weights": weights,
                "variation_counts": variation_counts,
                "winner_counts": winner_counts
            }
        except Exception as e:
            logger.error(f"Get stats failed: {e}")
            return {"error": str(e)}
