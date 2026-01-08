import os
from dataclasses import dataclass
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

@dataclass
class Config:
    # API Keys
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
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
    
    # Proxies
    proxy_url: Optional[str] = os.getenv("PROXY_URL")
