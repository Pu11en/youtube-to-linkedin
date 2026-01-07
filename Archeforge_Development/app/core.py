import os
import re
import time
import json
import hmac
import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Dict, Tuple, Optional
from urllib.parse import urlparse, parse_qs

import requests
from dotenv import load_dotenv

# Optional SDKs
try:
    import google.generativeai as genai
except Exception:
    genai = None

try:
    from anthropic import Anthropic
except Exception:
    Anthropic = None

from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

@dataclass
class Config:
    youtube_url: str
    gemini_api_key: str
    kie_api_key: str
    anthropic_api_key: str
    cloudinary_cloud_name: str
    cloudinary_api_key: str
    cloudinary_api_secret: str

    # Models
    gemini_model: str = "gemini-2.5-flash"
    claude_model: str = "claude-3-5-sonnet-latest"

    # Kie polling
    poll_interval_sec: int = 5
    poll_timeout_sec: int = 240

    kie_create_task_url: str = "https://api.kie.ai/api/v1/jobs/createTask"
    kie_record_info_url: str = "https://api.kie.ai/api/v1/jobs/recordInfo"

    @classmethod
    def from_env(cls, youtube_url: Optional[str] = None) -> 'Config':
        script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        load_dotenv(dotenv_path=os.path.join(script_dir, ".env"))

        def require_env(name: str) -> str:
            val = os.getenv(name, "").strip()
            if not val:
                # Don't raise here if we want to allow partial config for testing, 
                # but for production we usually want to fail fast.
                # For now, let's log a warning if missing, or raise if critical.
                pass 
            return val

        return cls(
            youtube_url=youtube_url or os.getenv("YOUTUBE_URL", ""),
            gemini_api_key=require_env("GEMINI_API_KEY"),
            kie_api_key=require_env("KIE_API_KEY"),
            anthropic_api_key=require_env("ANTHROPIC_API_KEY"),
            cloudinary_cloud_name=require_env("CLOUDINARY_CLOUD_NAME"),
            cloudinary_api_key=require_env("CLOUDINARY_API_KEY"),
            cloudinary_api_secret=require_env("CLOUDINARY_API_SECRET"),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
            claude_model=os.getenv("CLAUDE_MODEL", "claude-3-5-sonnet-latest"),
        )

class ContentPipeline:
    def __init__(self, config: Config):
        self.cfg = config
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
            if vid:
                return vid

        if "youtube.com" in parsed.netloc:
            if parsed.path == "/watch":
                qs = parse_qs(parsed.query)
                if "v" in qs and qs["v"]:
                    return qs["v"][0]

            m = re.match(r"^/(shorts|embed)/([^/?]+)", parsed.path)
            if m:
                return m.group(2)

        raise ValueError(f"Could not extract YouTube video id from: {url}")

    def slugify(self, s: str, max_len: int = 80) -> str:
        s = s.strip().lower()
        s = re.sub(r"[^a-z0-9]+", "-", s)
        s = re.sub(r"-{2,}", "-", s).strip("-")
        return s[:max_len] if len(s) > max_len else s

    def get_transcript(self) -> str:
        video_id = self.extract_youtube_video_id(self.cfg.youtube_url)
        logger.info(f"Fetching transcript for video ID: {video_id}")

        try:
            if hasattr(YouTubeTranscriptApi, "get_transcript"):
                chunks = YouTubeTranscriptApi.get_transcript(video_id)
            else:
                api = YouTubeTranscriptApi()
                if hasattr(api, "fetch"):
                    chunks = api.fetch(video_id)
                elif hasattr(api, "get_transcript"):
                    chunks = api.get_transcript(video_id)
                else:
                    options = api.list(video_id)
                    raise RuntimeError(f"TRANSCRIPT_ERROR: Unexpected API; api.list() returned: {options}")
        except TranscriptsDisabled:
            raise RuntimeError("TRANSCRIPT_ERROR: Captions/transcripts are disabled for this video.")
        except NoTranscriptFound:
            raise RuntimeError("TRANSCRIPT_ERROR: No transcript found for this video.")
        except Exception as e:
            raise RuntimeError(f"TRANSCRIPT_ERROR: Failed to fetch transcript: {e}")

        if isinstance(chunks, list):
            transcript = " ".join(str(item.get("text", "")).strip() for item in chunks if isinstance(item, dict))
        else:
            transcript = str(chunks)

        transcript = re.sub(r"\s+", " ", transcript).strip()
        if not transcript:
            raise RuntimeError("TRANSCRIPT_ERROR: Transcript was empty after cleaning.")
        return transcript

    def gemini_structured_summary(self, transcript: str) -> str:
        logger.info("Generating structured summary with Gemini...")
        if self.cfg.gemini_api_key and self.cfg.gemini_api_key.lower().startswith("fake"):
            return "Fake Summary for testing."

        if genai is None:
            raise RuntimeError("Gemini SDK not available")

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

        if not text:
            raise RuntimeError("Gemini returned an empty summary.")
        return text

    def gemini_infographic_brief(self, summary: str) -> str:
        logger.info("Generating infographic brief with Gemini...")
        if self.cfg.gemini_api_key and self.cfg.gemini_api_key.lower().startswith("fake"):
            return "Fake Infographic Brief"

        if genai is None:
            raise RuntimeError("Gemini SDK not available")

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

        if not text:
            raise RuntimeError("Gemini returned an empty infographic brief.")
        return text

    def kie_create_task(self, infographic_brief: str) -> str:
        logger.info("Creating Kie task...")
        if self.cfg.kie_api_key and str(self.cfg.kie_api_key).lower().startswith("fake"):
            return "fake-task-123"

        payload = {"model": "nano-banana-pro", "input": {"prompt": infographic_brief, "aspect_ratio": "16:9", "resolution": "2K", "output_format": "png"}}
        r = requests.post(self.cfg.kie_create_task_url, headers={"Authorization": f"Bearer {self.cfg.kie_api_key}", "Content-Type": "application/json"}, json=payload, timeout=60)
        self._http_raise(r)
        data = r.json()
        task_id = data.get("data", {}).get("taskId")
        if not task_id:
            raise RuntimeError(f"Kie createTask missing taskId. Response: {data}")
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
            if state == "success":
                return info
            if state == "fail":
                msg = info.get("data", {}).get("failMsg") or "Unknown failure"
                raise RuntimeError(f"Kie task failed: {msg}")
            time.sleep(self.cfg.poll_interval_sec)

    def kie_extract_image_url(self, task_info: Dict[str, Any]) -> str:
        result_json = task_info.get("data", {}).get("resultJson")
        if not result_json:
            raise RuntimeError(f"No resultJson in Kie recordInfo response: {task_info}")
        parsed = json.loads(result_json) if isinstance(result_json, str) else result_json
        urls = parsed.get("resultUrls") or parsed.get("result_urls") or []
        if not urls:
            raise RuntimeError(f"No resultUrls found in Kie resultJson: {parsed}")
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

    def claude_linkedin_post(self, transcript: str) -> str:
        logger.info("Generating LinkedIn post with Claude...")
        if self.cfg.anthropic_api_key and str(self.cfg.anthropic_api_key).lower().startswith("fake"):
            return "Fake LinkedIn Post"

        if Anthropic is None:
            raise RuntimeError("Anthropic SDK is not installed")

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

    def claude_newsletter(self, transcript: str) -> str:
        logger.info("Generating Newsletter with Claude...")
        if self.cfg.anthropic_api_key and str(self.cfg.anthropic_api_key).lower().startswith("fake"):
            return "Fake Newsletter"

        if Anthropic is None:
            raise RuntimeError("Anthropic SDK is not installed")

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

    def cloudinary_upload_image(self, image_bytes: bytes, public_id: str) -> Dict[str, Any]:
        logger.info("Uploading to Cloudinary...")
        timestamp = int(time.time())
        upload_url = f"https://api.cloudinary.com/v1_1/{self.cfg.cloudinary_cloud_name}/image/upload"

        def cloudinary_signature(api_secret: str, params: Dict[str, Any]) -> str:
            to_sign = "&".join(f"{k}={params[k]}" for k in sorted(params.keys()))
            return hashlib.sha1((to_sign + api_secret).encode("utf-8")).hexdigest()

        sign_params = {
            "public_id": public_id,
            "timestamp": timestamp,
        }
        signature = cloudinary_signature(self.cfg.cloudinary_api_secret, sign_params)

        files = {
            "file": ("infographic.png", image_bytes, "image/png"),
        }
        data = {
            "api_key": self.cfg.cloudinary_api_key,
            "timestamp": str(timestamp),
            "public_id": public_id,
            "signature": signature,
        }

        if self.cfg.cloudinary_api_key and str(self.cfg.cloudinary_api_key).lower().startswith("fake"):
            return {"secure_url": f"https://res.cloudinary.com/demo/{public_id}.png", "public_id": public_id}

        r = requests.post(upload_url, data=data, files=files, timeout=120)
        self._http_raise(r)
        return r.json()

    def run(self) -> Dict[str, Any]:
        transcript = self.get_transcript()
        summary = self.gemini_structured_summary(transcript)
        infographic_brief = self.gemini_infographic_brief(summary)

        task_id = self.kie_create_task(infographic_brief)
        task_info = self.kie_poll_until_success(task_id)
        kie_image_url = self.kie_extract_image_url(task_info)
        image_bytes, _ = self.download_bytes(kie_image_url)

        linkedin_post = self.claude_linkedin_post(transcript)
        newsletter = self.claude_newsletter(transcript)

        vid = self.extract_youtube_video_id(self.cfg.youtube_url)
        public_id = f"yt_to_linkedin/{self.slugify(vid)}_{int(time.time())}"
        cloudinary_resp = self.cloudinary_upload_image(image_bytes, public_id=public_id)

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
