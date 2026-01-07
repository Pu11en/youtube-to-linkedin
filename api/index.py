import os
import re
import time
import json
import hashlib
import logging
import functools
import random
from dataclasses import dataclass
from typing import Any, Dict, Tuple, Optional
from urllib.parse import urlparse, parse_qs
from datetime import datetime

import requests

# Optional Google Sheets SDK
try:
    import gspread
    from google.oauth2.service_account import Credentials as ServiceAccountCredentials
except ImportError:
    gspread = None
    ServiceAccountCredentials = None
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify

# Optional SDKs
try:
    import google.generativeai as genai
except ImportError:
    genai = None

try:
    from anthropic import Anthropic
except ImportError:
    Anthropic = None

from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.proxies import GenericProxyConfig
try:
    from youtube_transcript_api._errors import RequestBlocked, TranscriptsDisabled, NoTranscriptFound
except ImportError:
    # Fallback for older versions
    RequestBlocked = Exception
    TranscriptsDisabled = Exception
    NoTranscriptFound = Exception

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

def retry_api_call(retries=3, delay=2):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_e = None
            for i in range(retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_e = e
                    logger.warning(f"Attempt {i+1}/{retries} failed for {func.__name__}: {e}. Retrying in {delay}s...")
                    time.sleep(delay * (2 ** i))
            logger.error(f"All {retries} attempts failed for {func.__name__}")
            raise last_e
        return wrapper
    return decorator

@dataclass
class Config:
    youtube_url: str
    gemini_api_key: str
    kie_api_key: str
    anthropic_api_key: str
    cloudinary_cloud_name: str
    cloudinary_api_key: str
    cloudinary_api_secret: str
    gemini_model: str = "gemini-3-pro-preview"
    claude_model: str = "claude-sonnet-4-20250514"
    poll_interval_sec: int = 5
    poll_timeout_sec: int = 240
    kie_create_task_url: str = "https://api.kie.ai/api/v1/jobs/createTask"
    kie_record_info_url: str = "https://api.kie.ai/api/v1/jobs/recordInfo"
    proxy_url: Optional[str] = None
    youtube_cookies_content: Optional[str] = None
    # Blotato API for LinkedIn posting
    blotato_api_key: Optional[str] = None
    blotato_account_id: Optional[str] = None
    # Google Sheets for queue management
    google_sheets_credentials: Optional[str] = None
    google_sheet_id: Optional[str] = None
    # Cron auth secret
    cron_secret: Optional[str] = None

    @classmethod
    def from_env(cls, youtube_url: Optional[str] = None) -> 'Config':
        # Load .env from current directory or parent
        load_dotenv()

        def require_env(name: str) -> str:
            return os.getenv(name, "").strip()

        return cls(
            youtube_url=youtube_url or os.getenv("YOUTUBE_URL", ""),
            gemini_api_key=require_env("GEMINI_API_KEY"),
            kie_api_key=require_env("KIE_API_KEY"),
            anthropic_api_key=require_env("ANTHROPIC_API_KEY"),
            cloudinary_cloud_name=require_env("CLOUDINARY_CLOUD_NAME"),
            cloudinary_api_key=require_env("CLOUDINARY_API_KEY"),
            cloudinary_api_secret=require_env("CLOUDINARY_API_SECRET"),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-3-pro-preview"),
            claude_model=os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514"),
            proxy_url=os.getenv("PROXY_URL", "").strip() or None,
            youtube_cookies_content=os.getenv("YOUTUBE_COOKIES", "").strip() or None,
            blotato_api_key=os.getenv("BLOTATO_API_KEY", "").strip() or None,
            blotato_account_id=os.getenv("BLOTATO_ACCOUNT_ID", "").strip() or None,
            google_sheets_credentials=os.getenv("GOOGLE_SHEETS_CREDENTIALS", "").strip() or None,
            google_sheet_id=os.getenv("GOOGLE_SHEET_ID", "").strip() or None,
            cron_secret=os.getenv("CRON_SECRET", "").strip() or None,
        )

class ContentPipeline:
    def __init__(self, config: Config):
        self.cfg = config
        logger.info(f"ContentPipeline initialized with Gemini Model: {self.cfg.gemini_model}")
        self._setup_apis()

    def _setup_apis(self):
        if self.cfg.gemini_api_key and genai:
            try:
                genai.configure(api_key=self.cfg.gemini_api_key)
            except Exception as e:
                logger.warning(f"Failed to configure Gemini: {e}")

    def _http_raise(self, resp: requests.Response) -> None:
        if resp.ok:
            return
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text[:800]
        raise RuntimeError(f"HTTP {resp.status_code}: {detail}")

    def extract_youtube_video_id(self, url: str) -> str:
        parsed = urlparse(url)
        if parsed.netloc in ("youtu.be", "www.youtu.be"):
            vid = parsed.path.strip("/")
            if vid: return vid
        if "youtube.com" in parsed.netloc:
            if parsed.path == "/watch":
                qs = parse_qs(parsed.query)
                if "v" in qs and qs["v"]: return qs["v"][0]
            m = re.match(r"^/(shorts|embed)/([^/?]+)", parsed.path)
            if m: return m.group(2)
        raise ValueError(f"Could not extract YouTube video id from: {url}")

    def slugify(self, s: str, max_len: int = 80) -> str:
        s = s.strip().lower()
        s = re.sub(r"[^a-z0-9]+", "-", s)
        s = re.sub(r"-{2,}", "-", s).strip("-")
        return s[:max_len] if len(s) > max_len else s

    @retry_api_call(retries=3, delay=2)
    def get_transcript(self) -> str:
        # Demo/Test Mode Bypass
        if "demo=true" in self.cfg.youtube_url.lower():
            logger.info("Demo mode detected, returning fake transcript.")
            return "This is a demo transcript. In this video, we discuss how to use AI to automate content creation. Step 1 is to get the transcript. Step 2 is to summarize it. Step 3 is to create assets."

        video_id = self.extract_youtube_video_id(self.cfg.youtube_url)
        logger.info(f"Fetching transcript for video ID: {video_id}")
        
        # Build the YouTubeTranscriptApi with proxy config if available
        proxy_config = None
        if self.cfg.proxy_url:
            logger.info(f"Using proxy: {self.cfg.proxy_url[:30]}...")
            proxy_config = GenericProxyConfig(
                http_url=self.cfg.proxy_url,
                https_url=self.cfg.proxy_url,
            )
        
        try:
            # Create the API instance with or without proxy
            ytt_api = YouTubeTranscriptApi(proxy_config=proxy_config)
            
            # Fetch the transcript
            logger.info("Fetching transcript using new API...")
            fetched_transcript = ytt_api.fetch(video_id, languages=['en'])
            
            # Convert to raw data (list of dicts)
            chunks = fetched_transcript.to_raw_data()
            logger.info(f"Successfully fetched {len(chunks)} transcript segments.")
            
        except Exception as e:
            error_str = str(e).lower()
            logger.error(f"Transcript fetch failed: {e}")
            
            if "blocked" in error_str or "ipblocked" in error_str or "requestblocked" in error_str:
                raise RuntimeError("YOUTUBE_BLOCK: Vercel IP is blocked by YouTube. Please set 'PROXY_URL' in Vercel Environment Variables.")
            if "disabled" in error_str:
                raise RuntimeError("TRANSCRIPT_ERROR: Captions/transcripts are disabled for this video.")
            if "no transcript" in error_str:
                raise RuntimeError("TRANSCRIPT_ERROR: No transcript found for this video.")
            
            raise RuntimeError(f"TRANSCRIPT_ERROR: Failed to fetch transcript: {e}")

        # Process chunks into text
        if isinstance(chunks, list):
            transcript = " ".join(str(item.get("text", "")).strip() for item in chunks if isinstance(item, dict))
        else:
            transcript = str(chunks)
        transcript = re.sub(r"\s+", " ", transcript).strip()
        if not transcript:
            raise RuntimeError("TRANSCRIPT_ERROR: Transcript was empty after cleaning.")
        return transcript

    @retry_api_call()
    def gemini_structured_summary(self, transcript: str) -> str:
        logger.info("Generating structured summary with Gemini...")
        if self.cfg.gemini_api_key and self.cfg.gemini_api_key.lower().startswith("fake"):
            return "Fake Summary for testing."
        if genai is None: raise RuntimeError("Gemini SDK not available")
        try:
            model = genai.GenerativeModel(self.cfg.gemini_model)
            prompt = (
                "Analyze the YouTube transcript below and produce a STRUCTURED SUMMARY with:\n"
                "1) Suggested Title\n"
                "2) 5-10 Key Takeaways (bullets)\n"
                "3) Step-by-step Workflow (numbered)\n"
                "4) Tools/Services Mentioned (bullets)\n\n"
                "Return plain text. No markdown.\n\n"
                f"TRANSCRIPT:\n{transcript}"
            )
            resp = model.generate_content(prompt)
            text = (getattr(resp, "text", "") or "").strip()
        except Exception as e:
            raise RuntimeError(f"Gemini call failed: {e}")
        if not text: raise RuntimeError("Gemini returned an empty summary.")
        return text

    @retry_api_call()
    def gemini_infographic_brief(self, summary: str) -> str:
        logger.info("Generating infographic brief with Gemini...")
        if self.cfg.gemini_api_key and self.cfg.gemini_api_key.lower().startswith("fake"):
            return "Fake Infographic Brief"
        if genai is None: raise RuntimeError("Gemini SDK not available")
        try:
            model = genai.GenerativeModel(self.cfg.gemini_model)
            prompt = (
                "Create a detailed INFOGRAPHIC CREATIVE BRIEF from the summary below.\n"
                "Requirements:\n"
                "- LinkedIn infographic\n"
                "- 16:9 layout\n"
                "- Clear hierarchy: headline, sections, numbered steps, tool callouts\n"
                "- Include layout suggestions and visual element ideas (icons/diagrams)\n"
                "- Use short readable text blocks suitable for an infographic\n"
                "- Return plain text ONLY (no markdown)\n\n"
                f"SUMMARY:\n{summary}"
            )
            resp = model.generate_content(prompt)
            text = (getattr(resp, "text", "") or "").strip()
        except Exception as e:
            raise RuntimeError(f"Gemini call failed: {e}")
        if not text: raise RuntimeError("Gemini returned an empty infographic brief.")
        return text

    @retry_api_call()
    def kie_create_task(self, infographic_brief: str) -> str:
        logger.info("Creating Kie task...")
        if self.cfg.kie_api_key and str(self.cfg.kie_api_key).lower().startswith("fake"):
            return "fake-task-123"
        payload = {"model": "nano-banana-pro", "input": {"prompt": infographic_brief, "aspect_ratio": "16:9", "resolution": "2K", "output_format": "png"}}
        r = requests.post(self.cfg.kie_create_task_url, headers={"Authorization": f"Bearer {self.cfg.kie_api_key}", "Content-Type": "application/json"}, json=payload, timeout=60)
        self._http_raise(r)
        # Safely parse JSON response
        try:
            data = r.json()
        except Exception as parse_err:
            raise RuntimeError(f"Kie API returned invalid JSON. Status: {r.status_code}, Body: {r.text[:500]}")
        if data is None:
            raise RuntimeError(f"Kie API returned empty response. Status: {r.status_code}")
        # Check for API-level errors (e.g., 401, 402 with code in JSON)
        if data.get("code") and data.get("code") != 200:
            raise RuntimeError(f"Kie API error {data.get('code')}: {data.get('msg', 'Unknown error')}")
        task_id = data.get("data", {}).get("taskId")
        if not task_id: raise RuntimeError(f"Kie createTask missing taskId. Response: {data}")
        return task_id

    def kie_poll_until_success(self, task_id: str) -> Dict[str, Any]:
        logger.info(f"Polling Kie task {task_id}...")
        start = time.time()
        while True:
            if time.time() - start > self.cfg.poll_timeout_sec:
                raise TimeoutError(f"Kie task timed out after {self.cfg.poll_timeout_sec}s. taskId={task_id}")
            if self.cfg.kie_api_key and str(self.cfg.kie_api_key).lower().startswith("fake"):
                return {"data": {"state": "success", "resultJson": json.dumps({"resultUrls": ["https://via.placeholder.com/800x450.png?text=Fake+Infographic"]})}}
            r = requests.get(self.cfg.kie_record_info_url, headers={"Authorization": f"Bearer {self.cfg.kie_api_key}"}, params={"taskId": task_id}, timeout=60)
            self._http_raise(r)
            info = r.json()
            state = info.get("data", {}).get("state")
            if state == "success": return info
            if state == "fail":
                msg = info.get("data", {}).get("failMsg") or "Unknown failure"
                raise RuntimeError(f"Kie task failed: {msg}")
            time.sleep(self.cfg.poll_interval_sec)

    def kie_extract_image_url(self, task_info: Dict[str, Any]) -> str:
        result_json = task_info.get("data", {}).get("resultJson")
        if not result_json: raise RuntimeError(f"No resultJson in Kie recordInfo response: {task_info}")
        parsed = json.loads(result_json) if isinstance(result_json, str) else result_json
        urls = parsed.get("resultUrls") or parsed.get("result_urls") or []
        if not urls: raise RuntimeError(f"No resultUrls found in Kie resultJson: {parsed}")
        return urls[0]

    def download_bytes(self, url: str) -> Tuple[bytes, str]:
        if url.startswith("data:"):
            try:
                header, b64 = url.split(",", 1)
                mime = header.split(";")[0][5:] if ";" in header else header[5:]
                import base64
                data = base64.b64decode(b64)
                return data, mime or "application/octet-stream"
            except Exception as e:
                raise RuntimeError(f"Failed to decode data URL: {e}")
        r = requests.get(url, timeout=120)
        self._http_raise(r)
        return r.content, r.headers.get("Content-Type", "application/octet-stream")

    @retry_api_call()
    def claude_linkedin_post(self, transcript: str) -> str:
        logger.info("Generating LinkedIn post with Claude...")
        if self.cfg.anthropic_api_key and str(self.cfg.anthropic_api_key).lower().startswith("fake"):
            return "Fake LinkedIn Post"
        if Anthropic is None: raise RuntimeError("Anthropic SDK is not installed")
        client = Anthropic(api_key=self.cfg.anthropic_api_key)
        prompt = (
            "Write a professional LinkedIn post based on the transcript below.\n"
            "Constraints:\n"
            "- Strong hook in first 2 lines\n"
            "- Short paragraphs and spacing for retention\n"
            "- Include 3-7 bullets max if helpful\n"
            "- End with a clear CTA question\n"
            "- Do NOT use markdown syntax\n\n"
            f"TRANSCRIPT:\n{transcript}"
        )
        try:
            msg = client.messages.create(model=self.cfg.claude_model, max_tokens=1200, temperature=0.7, messages=[{"role": "user", "content": prompt}])
            return getattr(msg, "content")[0].text.strip()
        except Exception as e:
            raise RuntimeError(f"Anthropic/Claude request failed: {e}")

    @retry_api_call()
    def claude_newsletter(self, transcript: str) -> str:
        logger.info("Generating Newsletter with Claude...")
        if self.cfg.anthropic_api_key and str(self.cfg.anthropic_api_key).lower().startswith("fake"):
            return "Fake Newsletter"
        if Anthropic is None: raise RuntimeError("Anthropic SDK is not installed")
        client = Anthropic(api_key=self.cfg.anthropic_api_key)
        prompt = (
            "Write a LinkedIn newsletter article based on the transcript below.\n"
            "Constraints:\n"
            "- Include a title\n"
            "- Clear sections with blank lines (but NO markdown headings)\n"
            "- Include a numbered step-by-step workflow section\n"
            "- Include a Tools Mentioned section\n"
            "- End with practical takeaways and CTA\n"
            "- Do NOT use markdown syntax\n\n"
            f"TRANSCRIPT:\n{transcript}"
        )
        try:
            msg = client.messages.create(model=self.cfg.claude_model, max_tokens=2200, temperature=0.7, messages=[{"role": "user", "content": prompt}])
            return getattr(msg, "content")[0].text.strip()
        except Exception as e:
            raise RuntimeError(f"Anthropic/Claude request failed: {e}")

    @retry_api_call()
    def cloudinary_upload_image(self, image_bytes: bytes, public_id: str) -> Dict[str, Any]:
        logger.info("Uploading to Cloudinary...")
        timestamp = int(time.time())
        upload_url = f"https://api.cloudinary.com/v1_1/{self.cfg.cloudinary_cloud_name}/image/upload"
        def cloudinary_signature(api_secret: str, params: Dict[str, Any]) -> str:
            to_sign = "&".join(f"{k}={params[k]}" for k in sorted(params.keys()))
            return hashlib.sha1((to_sign + api_secret).encode("utf-8")).hexdigest()
        sign_params = {"public_id": public_id, "timestamp": timestamp}
        signature = cloudinary_signature(self.cfg.cloudinary_api_secret, sign_params)
        files = {"file": ("infographic.png", image_bytes, "image/png")}
        data = {"api_key": self.cfg.cloudinary_api_key, "timestamp": str(timestamp), "public_id": public_id, "signature": signature}
        if self.cfg.cloudinary_api_key and str(self.cfg.cloudinary_api_key).lower().startswith("fake"):
            return {"secure_url": f"https://res.cloudinary.com/demo/{public_id}.png", "public_id": public_id}
        r = requests.post(upload_url, data=data, files=files, timeout=120)
        self._http_raise(r)
        return r.json()

    @retry_api_call()
    def blotato_post_to_linkedin(
        self,
        text: str,
        image_url: Optional[str] = None,
        scheduled_time: Optional[str] = None
    ) -> Dict[str, Any]:
        """Post to LinkedIn via Blotato API.

        Args:
            text: The LinkedIn post content
            image_url: Optional Cloudinary image URL to attach
            scheduled_time: Optional ISO 8601 timestamp for scheduled posting

        Returns:
            Dict with postSubmissionId from Blotato
        """
        logger.info("Posting to LinkedIn via Blotato...")

        if not self.cfg.blotato_api_key or not self.cfg.blotato_account_id:
            raise RuntimeError("BLOTATO_API_KEY and BLOTATO_ACCOUNT_ID must be configured")

        # Build fake response for testing
        if self.cfg.blotato_api_key.lower().startswith("fake"):
            return {"postSubmissionId": "fake-submission-123"}

        payload = {
            "post": {
                "accountId": self.cfg.blotato_account_id,
                "content": {
                    "text": text,
                    "mediaUrls": [image_url] if image_url else [],
                    "platform": "linkedin"
                },
                "target": {
                    "targetType": "linkedin"
                }
            }
        }

        if scheduled_time:
            payload["scheduledTime"] = scheduled_time

        r = requests.post(
            "https://backend.blotato.com/v2/posts",
            headers={
                "Content-Type": "application/json",
                "blotato-api-key": self.cfg.blotato_api_key
            },
            json=payload,
            timeout=30
        )
        self._http_raise(r)
        return r.json()

    def run(self) -> Dict[str, Any]:
        logger.info("Starting pipeline run...")
        transcript = self.get_transcript()
        logger.info("Step 1: Transcript fetched")
        summary = self.gemini_structured_summary(transcript)
        logger.info("Step 2: Summary generated")
        infographic_brief = self.gemini_infographic_brief(summary)
        logger.info("Step 3: Infographic brief generated")
        task_id = self.kie_create_task(infographic_brief)
        logger.info(f"Step 4: Kie task created (ID: {task_id})")
        task_info = self.kie_poll_until_success(task_id)
        logger.info("Step 5: Kie task finished")
        kie_image_url = self.kie_extract_image_url(task_info)
        image_bytes, _ = self.download_bytes(kie_image_url)
        logger.info("Step 6: Image downloaded")
        linkedin_post = self.claude_linkedin_post(transcript)
        logger.info("Step 7: LinkedIn post generated")
        newsletter = self.claude_newsletter(transcript)
        logger.info("Step 8: Newsletter generated")
        vid = self.extract_youtube_video_id(self.cfg.youtube_url)
        public_id = f"yt_to_linkedin/{self.slugify(vid)}_{int(time.time())}"
        cloudinary_resp = self.cloudinary_upload_image(image_bytes, public_id=public_id)
        logger.info("Step 9: Cloudinary upload complete")
        return {
            "input": {"youtube_url": self.cfg.youtube_url},
            "outputs": {
                "cloudinary_infographic_url": cloudinary_resp.get("secure_url") or cloudinary_resp.get("url"),
                "video_summary": summary,
                "linkedin_post": linkedin_post,
                "newsletter_article": newsletter,
            },
            "debug": {
                "kie_task_id": task_id,
                "kie_result_image_url": kie_image_url,
                "cloudinary_public_id": public_id,
            }
        }

# Initialize Flask App
app = Flask(__name__, template_folder='templates', static_folder='static')

@app.route('/', methods=['GET'])
def index():
    return render_template('index.html')

@app.route('/process', methods=['POST'])
def process():
    print("Request received at /process")
    logger.info("Request received at /process")
    youtube_url = request.form.get('youtube_url')
    if not youtube_url:
        return jsonify({'error': 'YouTube URL is required'}), 400
    try:
        logger.info(f"Processing URL: {youtube_url}")
        config = Config.from_env(youtube_url=youtube_url)
        pipeline = ContentPipeline(config)
        result = pipeline.run()
        logger.info("Pipeline completed successfully")
        return render_template('result.html', result=result)
    except Exception as e:
        logger.error(f"Pipeline failed: {e}", exc_info=True)
        return render_template('error.html', error=str(e))

@app.route('/api/step1_transcript', methods=['POST'])
def step1_transcript():
    try:
        data = request.json
        url = data.get('youtube_url')
        config = Config.from_env(youtube_url=url)
        pipeline = ContentPipeline(config)
        transcript = pipeline.get_transcript()
        return jsonify({'status': 'success', 'transcript': transcript})
    except Exception as e:
        logger.error(f"Step 1 failed: {e}")
        return jsonify({'status': 'failed', 'error': str(e)}), 500

@app.route('/api/step2_summary', methods=['POST'])
def step2_summary():
    try:
        data = request.json
        transcript = data.get('transcript')
        config = Config.from_env(youtube_url="placeholder")
        pipeline = ContentPipeline(config)
        summary = pipeline.gemini_structured_summary(transcript)
        return jsonify({'status': 'success', 'summary': summary})
    except Exception as e:
        logger.error(f"Step 2 failed: {e}")
        return jsonify({'status': 'failed', 'error': str(e)}), 500

@app.route('/api/step3_brief', methods=['POST'])
def step3_brief():
    try:
        data = request.json
        summary = data.get('summary')
        config = Config.from_env(youtube_url="placeholder")
        pipeline = ContentPipeline(config)
        brief = pipeline.gemini_infographic_brief(summary)
        return jsonify({'status': 'success', 'brief': brief})
    except Exception as e:
        logger.error(f"Step 3 failed: {e}")
        return jsonify({'status': 'failed', 'error': str(e)}), 500

@app.route('/api/step4_image_init', methods=['POST'])
def step4_image_init():
    try:
        data = request.json
        brief = data.get('brief')
        config = Config.from_env(youtube_url="placeholder")
        pipeline = ContentPipeline(config)
        task_id = pipeline.kie_create_task(brief)
        return jsonify({'status': 'success', 'task_id': task_id})
    except Exception as e:
        logger.error(f"Step 4 failed: {e}")
        return jsonify({'status': 'failed', 'error': str(e)}), 500

@app.route('/api/step5_image_check', methods=['POST'])
def step5_image_check():
    try:
        import requests
        data = request.json
        task_id = data.get('task_id')
        config = Config.from_env(youtube_url="placeholder")
        headers = {"Authorization": f"Bearer {config.kie_api_key}"}
        r = requests.get(config.kie_record_info_url, headers=headers, params={"taskId": task_id}, timeout=10)
        info = r.json()
        state = info.get("data", {}).get("state")
        if state == "success":
            pipeline = ContentPipeline(config)
            image_url = pipeline.kie_extract_image_url(info)
            return jsonify({'status': 'success', 'image_url': image_url})
        elif state == "fail":
            # Extract error message string, not the whole object
            fail_msg = info.get("data", {}).get("failMsg") or info.get("msg") or json.dumps(info)
            logger.error(f"Kie task failed: {fail_msg}")
            return jsonify({'status': 'failed', 'error': fail_msg})
        else:
            return jsonify({'status': 'pending'})
    except Exception as e:
        logger.error(f"Step 5 failed: {e}")
        return jsonify({'status': 'failed', 'error': str(e)}), 500

@app.route('/api/step6_linkedin', methods=['POST'])
def step6_linkedin():
    try:
        data = request.json
        transcript = data.get('transcript')
        config = Config.from_env(youtube_url="placeholder")
        pipeline = ContentPipeline(config)
        post = pipeline.claude_linkedin_post(transcript)
        return jsonify({'status': 'success', 'post': post})
    except Exception as e:
        logger.error(f"Step 6 failed: {e}")
        return jsonify({'status': 'failed', 'error': str(e)}), 500

@app.route('/api/step7_newsletter', methods=['POST'])
def step7_newsletter():
    try:
        data = request.json
        transcript = data.get('transcript')
        config = Config.from_env(youtube_url="placeholder")
        pipeline = ContentPipeline(config)
        newsletter = pipeline.claude_newsletter(transcript)
        return jsonify({'status': 'success', 'newsletter': newsletter})
    except Exception as e:
        logger.error(f"Step 7 failed: {e}")
        return jsonify({'status': 'failed', 'error': str(e)}), 500

@app.route('/api/step8_upload', methods=['POST'])
def step8_upload():
    try:
        import time
        data = request.json
        image_url = data.get('image_url')
        youtube_url = data.get('youtube_url')
        config = Config.from_env(youtube_url=youtube_url)
        pipeline = ContentPipeline(config)
        image_bytes, _ = pipeline.download_bytes(image_url)
        vid = pipeline.extract_youtube_video_id(youtube_url)
        public_id = f"yt_to_linkedin/{pipeline.slugify(vid)}_{int(time.time())}"
        resp = pipeline.cloudinary_upload_image(image_bytes, public_id)
        final_url = resp.get("secure_url") or resp.get("url")
        return jsonify({'status': 'success', 'cloudinary_url': final_url})
    except Exception as e:
        logger.error(f"Step 8 failed: {e}")
        return jsonify({'status': 'failed', 'error': str(e)}), 500

@app.route('/api/step9_post', methods=['POST'])
def step9_post():
    """Post to LinkedIn via Blotato API.

    Request JSON:
        - post: The LinkedIn post text (required)
        - image_url: Cloudinary image URL (optional)
        - scheduled_time: ISO 8601 timestamp for scheduling (optional)
    """
    try:
        data = request.json
        post_text = data.get('post')
        image_url = data.get('image_url')
        scheduled_time = data.get('scheduled_time')

        if not post_text:
            return jsonify({'status': 'failed', 'error': 'post text is required'}), 400

        config = Config.from_env(youtube_url="placeholder")

        if not config.blotato_api_key or not config.blotato_account_id:
            return jsonify({
                'status': 'failed',
                'error': 'BLOTATO_API_KEY and BLOTATO_ACCOUNT_ID must be set in environment'
            }), 400

        pipeline = ContentPipeline(config)
        result = pipeline.blotato_post_to_linkedin(
            text=post_text,
            image_url=image_url,
            scheduled_time=scheduled_time
        )

        return jsonify({
            'status': 'success',
            'postSubmissionId': result.get('postSubmissionId'),
            'scheduled': scheduled_time is not None
        })
    except Exception as e:
        logger.error(f"Step 9 (Blotato post) failed: {e}")
        return jsonify({'status': 'failed', 'error': str(e)}), 500


# ============================================
# Google Sheets Queue Helper Functions
# ============================================

def get_sheets_client():
    """Initialize and return Google Sheets client."""
    if gspread is None or ServiceAccountCredentials is None:
        raise RuntimeError("gspread and google-auth packages are required for Google Sheets integration")

    creds_json_str = os.getenv("GOOGLE_SHEETS_CREDENTIALS", "")
    if not creds_json_str:
        raise RuntimeError("GOOGLE_SHEETS_CREDENTIALS environment variable not set")

    creds_json = json.loads(creds_json_str)
    creds = ServiceAccountCredentials.from_service_account_info(
        creds_json,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return gspread.authorize(creds)


def get_next_youtube_url_from_sheets() -> Optional[Dict[str, Any]]:
    """Fetch the next pending YouTube URL from Google Sheets.

    Expected sheet format:
        Column A: URL
        Column B: Status (pending/done)
        Column C: Processed At (timestamp)

    Returns:
        Dict with 'url' and 'row_number', or None if no pending URLs
    """
    sheet_id = os.getenv("GOOGLE_SHEET_ID", "")
    if not sheet_id:
        raise RuntimeError("GOOGLE_SHEET_ID environment variable not set")

    client = get_sheets_client()
    sheet = client.open_by_key(sheet_id).sheet1

    records = sheet.get_all_records()
    for i, row in enumerate(records):
        status = str(row.get("Status", "")).lower().strip()
        if status == "pending":
            return {
                "url": row.get("URL", ""),
                "row_number": i + 2  # +2 for header row and 0-index
            }
    return None


def mark_url_as_done_in_sheets(row_number: int) -> None:
    """Mark a URL as done in Google Sheets."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID", "")
    if not sheet_id:
        raise RuntimeError("GOOGLE_SHEET_ID environment variable not set")

    client = get_sheets_client()
    sheet = client.open_by_key(sheet_id).sheet1

    # Update Status column (B) and Processed At column (C)
    sheet.update_cell(row_number, 2, "done")
    sheet.update_cell(row_number, 3, datetime.utcnow().isoformat() + "Z")


@app.route('/queue', methods=['GET'])
def queue_page():
    """Queue management page."""
    return render_template('queue.html')


@app.route('/api/queue', methods=['GET'])
def get_queue():
    """View current queue from Google Sheets."""
    try:
        sheet_id = os.getenv("GOOGLE_SHEET_ID", "")
        if not sheet_id:
            return jsonify({'status': 'failed', 'error': 'GOOGLE_SHEET_ID not configured'}), 400

        client = get_sheets_client()
        sheet = client.open_by_key(sheet_id).sheet1
        records = sheet.get_all_records()

        pending = [r for r in records if str(r.get("Status", "")).lower().strip() == "pending"]
        done = [r for r in records if str(r.get("Status", "")).lower().strip() == "done"]

        return jsonify({
            'status': 'success',
            'pending_count': len(pending),
            'done_count': len(done),
            'pending_urls': [r.get("URL") for r in pending],
            'done_urls': [{'url': r.get("URL"), 'processed_at': r.get("Processed At", "")} for r in done],
            'total': len(records)
        })
    except Exception as e:
        logger.error(f"Failed to get queue: {e}")
        return jsonify({'status': 'failed', 'error': str(e)}), 500


@app.route('/api/queue/add', methods=['POST'])
def add_to_queue():
    """Add a YouTube URL to the queue."""
    try:
        data = request.json
        url = data.get('url', '').strip()
        if not url:
            return jsonify({'status': 'failed', 'error': 'URL is required'}), 400

        sheet_id = os.getenv("GOOGLE_SHEET_ID", "")
        if not sheet_id:
            return jsonify({'status': 'failed', 'error': 'GOOGLE_SHEET_ID not configured'}), 400

        client = get_sheets_client()
        sheet = client.open_by_key(sheet_id).sheet1
        sheet.append_row([url, 'pending', ''])

        return jsonify({'status': 'success', 'message': 'URL added to queue'})
    except Exception as e:
        logger.error(f"Failed to add to queue: {e}")
        return jsonify({'status': 'failed', 'error': str(e)}), 500


@app.route('/api/queue/process_next', methods=['POST'])
def process_next_in_queue():
    """Manually trigger processing of the next pending URL."""
    try:
        next_item = get_next_youtube_url_from_sheets()
        if not next_item:
            return jsonify({'status': 'empty', 'message': 'No pending URLs in queue'})

        youtube_url = next_item['url']
        row_number = next_item['row_number']
        logger.info(f"Processing URL: {youtube_url} (row {row_number})")

        config = Config.from_env(youtube_url=youtube_url)
        pipeline = ContentPipeline(config)
        result = pipeline.run()

        mark_url_as_done_in_sheets(row_number)

        return jsonify({
            'status': 'success',
            'youtube_url': youtube_url,
            'result': {
                'cloudinary_url': result['outputs']['cloudinary_infographic_url'],
                'linkedin_post': result['outputs']['linkedin_post'][:200] + '...',
            }
        })
    except Exception as e:
        logger.error(f"Process next failed: {e}", exc_info=True)
        return jsonify({'status': 'failed', 'error': str(e)}), 500


@app.route('/api/auto_process', methods=['POST'])
def auto_process():
    """Automatically process next pending YouTube URL.

    Called by GitHub Actions cron job every 3 hours.
    Requires Authorization header with CRON_SECRET.
    """
    try:
        # Verify authorization
        auth_header = request.headers.get('Authorization', '')
        config = Config.from_env(youtube_url="placeholder")

        if config.cron_secret:
            expected = f"Bearer {config.cron_secret}"
            if auth_header != expected:
                logger.warning("Unauthorized auto_process attempt")
                return jsonify({'error': 'Unauthorized'}), 401

        # Get next pending URL from Google Sheets
        next_item = get_next_youtube_url_from_sheets()
        if not next_item:
            logger.info("No pending URLs in queue")
            return jsonify({
                'status': 'empty',
                'message': 'No pending URLs in queue'
            })

        youtube_url = next_item['url']
        row_number = next_item['row_number']
        logger.info(f"Auto-processing URL: {youtube_url} (row {row_number})")

        # Run full pipeline
        config = Config.from_env(youtube_url=youtube_url)
        pipeline = ContentPipeline(config)
        result = pipeline.run()

        # Post to LinkedIn via Blotato
        post_result = pipeline.blotato_post_to_linkedin(
            text=result['outputs']['linkedin_post'],
            image_url=result['outputs']['cloudinary_infographic_url']
        )

        # Mark as done in Google Sheets
        mark_url_as_done_in_sheets(row_number)

        logger.info(f"Successfully processed and posted: {youtube_url}")
        return jsonify({
            'status': 'success',
            'youtube_url': youtube_url,
            'blotato_submission_id': post_result.get('postSubmissionId'),
            'cloudinary_url': result['outputs']['cloudinary_infographic_url']
        })

    except Exception as e:
        logger.error(f"Auto-process failed: {e}", exc_info=True)
        return jsonify({'status': 'failed', 'error': str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
