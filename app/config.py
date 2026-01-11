import os
import random
import string
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

def _get_rotated_proxy_url() -> Optional[str]:
    """Get proxy URL with rotated session ID for fresh IP each time."""
    base_url = os.getenv("PROXY_URL", "")
    if not base_url:
        return None
    
    # If URL contains _session-, rotate it
    if "_session-" in base_url:
        import re
        # Generate random session ID
        new_session = ''.join(random.choices(string.ascii_letters + string.digits, k=12))
        # Replace existing session with new one
        rotated = re.sub(r'_session-[^:@]+', f'_session-{new_session}', base_url)
        return rotated
    
    return base_url

@dataclass
class Config:
    # API Keys
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    scrapingdog_api_key: str = os.getenv("SCRAPINGDOG_API_KEY", "")
    kie_api_key: str = os.getenv("KIE_API_KEY", "")
    
    # Storage (Cloudinary)
    cloudinary_cloud_name: str = os.getenv("CLOUDINARY_CLOUD_NAME", "")
    cloudinary_api_key: str = os.getenv("CLOUDINARY_API_KEY", "")
    cloudinary_api_secret: str = os.getenv("CLOUDINARY_API_SECRET", "")
    
    # Posting (Blotato)
    blotato_api_key: str = os.getenv("BLOTATO_API_KEY", "")
    blotato_account_id: str = os.getenv("BLOTATO_ACCOUNT_ID", "")
    
    # Queue (Upstash/Vercel KV)
    kv_url: str = os.getenv("KV_REST_API_URL", os.getenv("KV_URL", os.getenv("UPSTASH_REDIS_REST_URL", "")))
    kv_token: str = os.getenv("KV_REST_API_TOKEN", os.getenv("KV_TOKEN", os.getenv("UPSTASH_REDIS_REST_TOKEN", "")))

    # Security
    cron_secret: str = os.getenv("CRON_SECRET", "")
    
    # Models
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-1.5-pro")
    # Claude 4.5 Haiku - fastest and cheapest
    claude_model: str = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
    
    # Proxies - rotated session for fresh IP each request
    proxy_url: Optional[str] = field(default_factory=_get_rotated_proxy_url)
    
    # Telegram Bot
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_admin_chat_id: str = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")  # Your chat ID to restrict access
