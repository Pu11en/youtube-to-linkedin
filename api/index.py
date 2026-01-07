import os
import re
import time
import json
import hashlib
import logging
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime
from urllib.parse import urlparse, parse_qs

import requests
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv

# Optional SDKs
try:
    from google import genai
except ImportError:
    genai = None

try:
    from anthropic import Anthropic
except ImportError:
    Anthropic = None

try:
    from upstash_redis import Redis
except ImportError:
    Redis = None

from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.proxies import GenericProxyConfig

# Configuration
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
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
    kv_url: str = os.getenv("KV_URL", os.getenv("UPSTASH_REDIS_REST_URL", ""))
    kv_token: str = os.getenv("KV_TOKEN", os.getenv("UPSTASH_REDIS_REST_TOKEN", ""))
    
    # Security
    cron_secret: str = os.getenv("CRON_SECRET", "")
    
    # Models
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-1.5-pro")
    claude_model: str = os.getenv("CLAUDE_MODEL", "claude-3-5-sonnet-20240620")
    
    # Proxies
    proxy_url: Optional[str] = os.getenv("PROXY_URL")

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
        data = self.redis.get(self.KEY)
        if not data: return []
        return [u.strip() for u in data.split("\n") if u.strip()]

    def set_urls(self, urls: List[str]):
        text = "\n".join(u.strip() for u in urls if u.strip())
        if not self.redis:
            self._local_queue = urls
            return
        self.redis.set(self.KEY, text)

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
        self.redis.lpush(self.DONE_KEY, json.dumps({
            "url": url,
            "done_at": datetime.utcnow().isoformat()
        }))
        # Keep only last 20 done
        self.redis.ltrim(self.DONE_KEY, 0, 19)

    def get_history(self) -> List[dict]:
        if not self.redis: return []
        items = self.redis.lrange(self.DONE_KEY, 0, 19)
        return [json.loads(i) for i in items]

class ContentPipeline:
    def __init__(self, config: Config, youtube_url: str):
        self.cfg = config
        self.youtube_url = youtube_url
        if config.gemini_api_key and genai:
            self.gemini_client = genai.Client(api_key=config.gemini_api_key)
        else:
            self.gemini_client = None

    def extract_id(self, url: str) -> str:
        parsed = urlparse(url)
        if "youtu.be" in parsed.netloc: return parsed.path.strip("/")
        if "youtube.com" in parsed.netloc:
            if "/watch" in parsed.path: return parse_qs(parsed.query).get("v", [""])[0]
            if "/shorts/" in parsed.path: return parsed.path.split("/")[-1]
        return url

    def get_transcript(self) -> str:
        vid = self.extract_id(self.youtube_url)
        proxies = {"http": self.cfg.proxy_url, "https": self.cfg.proxy_url} if self.cfg.proxy_url else None
        try:
            loader = YouTubeTranscriptApi.list_transcripts(vid, proxies=proxies)
            transcript = loader.find_transcript(['en']).fetch()
            return " ".join([t['text'] for t in transcript])
        except Exception as e:
            logger.error(f"Transcript failed: {e}")
            raise RuntimeError(f"Could not get transcript. Check proxy or if video has CC. {e}")

    def ai_gemini(self, transcript: str) -> str:
        if not self.gemini_client: raise RuntimeError("Gemini API key not configured")
        prompt = f"Summarize this YouTube transcript into a structured guide with Title, Key Points, and Workflow. Return plain text.\n\nTRANSCRIPT:\n{transcript}"
        response = self.gemini_client.models.generate_content(
            model=self.cfg.gemini_model,
            contents=prompt
        )
        return response.text

    def ai_infographic_brief(self, summary: str) -> str:
        if not self.gemini_client: raise RuntimeError("Gemini API key not configured")
        prompt = f"Create an infographic design brief for LinkedIn (16:9) from this summary. Focus on visual hierarchy. Plain text.\n\nSUMMARY:\n{summary}"
        response = self.gemini_client.models.generate_content(
            model=self.cfg.gemini_model,
            contents=prompt
        )
        return response.text

    def kie_generate_image(self, brief: str) -> str:
        headers = {"Authorization": f"Bearer {self.cfg.kie_api_key}", "Content-Type": "application/json"}
        payload = {"model": "nano-banana-pro", "input": {"prompt": brief, "aspect_ratio": "16:9"}}
        r = requests.post("https://api.kie.ai/api/v1/jobs/createTask", headers=headers, json=payload).json()
        task_id = r.get("data", {}).get("taskId")
        if not task_id: raise RuntimeError(f"Kie failed: {r}")
        
        # Poll
        for _ in range(30):
            time.sleep(5)
            res = requests.get("https://api.kie.ai/api/v1/jobs/recordInfo", headers=headers, params={"taskId": task_id}).json()
            state = res.get("data", {}).get("state")
            if state == "success":
                result_json = res.get("data", {}).get("resultJson")
                urls = json.loads(result_json).get("resultUrls", []) if isinstance(result_json, str) else result_json.get("resultUrls", [])
                return urls[0]
            if state == "fail": raise RuntimeError("Kie image generation failed")
        raise TimeoutError("Kie image generation timed out")

    def ai_claude_post(self, transcript: str) -> str:
        client = Anthropic(api_key=self.cfg.anthropic_api_key)
        prompt = f"Write a high-converting LinkedIn post from this transcript. Hook, bullets, CTA. Plain text.\n\nTRANSCRIPT:\n{transcript}"
        msg = client.messages.create(model=self.cfg.claude_model, max_tokens=1000, messages=[{"role": "user", "content": prompt}])
        return msg.content[0].text

    def cloudinary_upload(self, image_url: str) -> str:
        r = requests.get(image_url)
        timestamp = int(time.time())
        public_id = f"yt_{timestamp}"
        signature = hashlib.sha1(f"public_id={public_id}&timestamp={timestamp}{self.cfg.cloudinary_api_secret}".encode()).hexdigest()
        data = {"api_key": self.cfg.cloudinary_api_key, "timestamp": timestamp, "public_id": public_id, "signature": signature}
        files = {"file": r.content}
        res = requests.post(f"https://api.cloudinary.com/v1_1/{self.cfg.cloudinary_cloud_name}/image/upload", data=data, files=files).json()
        return res.get("secure_url")

    def blotato_post(self, text: str, image_url: str):
        if not self.cfg.blotato_api_key: return
        headers = {"blotato-api-key": self.cfg.blotato_api_key, "Content-Type": "application/json"}
        payload = {"post": {"accountId": self.cfg.blotato_account_id, "content": {"text": text, "mediaUrls": [image_url], "platform": "linkedin"}, "target": {"targetType": "linkedin"}}}
        requests.post("https://backend.blotato.com/v2/posts", headers=headers, json=payload)

    def run_full(self) -> dict:
        res = self.generate_content()
        self.post_content(res['post'], res['url'])
        return res

    def generate_content(self) -> dict:
        transcript = self.get_transcript()
        summary = self.ai_gemini(transcript)
        brief = self.ai_infographic_brief(summary)
        raw_img = self.kie_generate_image(brief)
        final_img = self.cloudinary_upload(raw_img)
        post_text = self.ai_claude_post(transcript)
        return {"url": final_img, "summary": summary, "post": post_text}

    def post_content(self, text: str, image_url: str):
        self.blotato_post(text, image_url)

app = Flask(__name__, template_folder='templates', static_folder='static')

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/api/queue', methods=['GET', 'POST'])
def handle_queue():
    cfg = Config()
    q = SimpleQueue(cfg)
    if request.method == 'POST':
        urls = request.json.get('urls', [])
        q.set_urls(urls)
        return jsonify({"status": "saved"})
    return jsonify({
        "urls": q.get_urls(), 
        "history": q.get_history(),
        "redis_active": q.redis is not None
    })

@app.route('/api/add', methods=['POST'])
def add_to_queue():
    url = request.json.get('url')
    if not url: return jsonify({"error": "no url"}), 400
    q = SimpleQueue(Config())
    q.add_url(url)
    return jsonify({"status": "added"})

@app.route('/api/generate', methods=['POST'])
def generate_only():
    url = request.json.get('url')
    if not url: return jsonify({"error": "no url"}), 400
    cfg = Config()
    try:
        pipeline = ContentPipeline(cfg, url)
        result = pipeline.generate_content()
        return jsonify({"status": "success", "result": result})
    except Exception as e:
        logger.error(f"Generation failed: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/post_custom', methods=['POST'])
def post_custom():
    data = request.json
    text = data.get('post')
    image_url = data.get('url')
    if not text or not image_url: return jsonify({"error": "missing data"}), 400
    
    cfg = Config()
    try:
        pipeline = ContentPipeline(cfg, "")
        pipeline.post_content(text, image_url)
        return jsonify({"status": "success"})
    except Exception as e:
        logger.error(f"Post failed: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/process_next', methods=['POST'])
def process_next():
    cfg = Config()
    q = SimpleQueue(cfg)
    url = q.pop_next()
    if not url: return jsonify({"status": "empty"})
    
    try:
        pipeline = ContentPipeline(cfg, url)
        result = pipeline.run_full()
        q.mark_done(url)
        return jsonify({"status": "success", "result": result})
    except Exception as e:
        # Put it back at the end of queue on failure? No, keep it out or log it.
        logger.error(f"Process failed: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/auto_process', methods=['POST'])
def auto_process():
    # Cron trigger
    auth = request.headers.get('Authorization')
    cfg = Config()
    if cfg.cron_secret and auth != f"Bearer {cfg.cron_secret}":
        return jsonify({"error": "unauthorized"}), 401
    
    q = SimpleQueue(cfg)
    url = q.pop_next()
    if not url: return jsonify({"status": "idle"})
    
    try:
        pipeline = ContentPipeline(cfg, url)
        pipeline.run_full()
        q.mark_done(url)
        return jsonify({"status": "posted", "url": url})
    except Exception as e:
        return jsonify({"status": "failed", "error": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True)
