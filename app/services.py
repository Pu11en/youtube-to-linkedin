import time
import json
import hashlib
import logging
import requests
from typing import Optional, Dict, Any

# Third-party SDKs
try:
    from google import genai
except ImportError:
    genai = None

try:
    from anthropic import Anthropic
except ImportError:
    Anthropic = None

from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.proxies import GenericProxyConfig
from youtube_transcript_api.formatters import TextFormatter

from app.config import Config
from app.utils import extract_youtube_id, detect_platform
from app.twitter_service import TwitterService

logger = logging.getLogger(__name__)

class ContentPipeline:
    def __init__(self, config: Config, url: str = "", blotato_account_id: str = None):
        self.cfg = config
        self.url = url
        self.platform = detect_platform(url)
        # Allow override for multi-client support
        self.blotato_account_id = blotato_account_id or config.blotato_account_id
        
        # Initialize Gemini Client
        if config.gemini_api_key and genai:
            try:
                self.gemini_client = genai.Client(api_key=config.gemini_api_key)
            except Exception as e:
                logger.error(f"Failed to init Gemini client: {e}")
                self.gemini_client = None
        else:
            self.gemini_client = None
            
        # Initialize Anthropic Client
        if config.anthropic_api_key and Anthropic:
            try:
                self.anthropic_client = Anthropic(api_key=config.anthropic_api_key)
            except Exception as e:
                logger.error(f"Failed to init Anthropic client: {e}")
                self.anthropic_client = None
        else:
            self.anthropic_client = None

    def get_content(self) -> str:
        """Fetches content based on platform."""
        if self.platform == "twitter":
            ts = TwitterService(self.cfg.scrapingdog_api_key)
            return ts.get_tweet_text(self.url)
        else:
            return self.get_transcript()

    def get_transcript(self) -> str:
        """Fetches and formats transcript from YouTube."""
        video_id = extract_youtube_id(self.url)
        if not video_id:
            raise ValueError("No valid video ID extracted from URL")

        # Configure proxy if available
        proxy_config = None
        if self.cfg.proxy_url:
            proxy_config = GenericProxyConfig(
                http_url=self.cfg.proxy_url,
                https_url=self.cfg.proxy_url,
            )
        
        try:
            # 1. New Instance-based API
            ytt_api = YouTubeTranscriptApi(proxy_config=proxy_config)
            
            # 2. Fetch transcript (try English, fallback to auto-generated English)
            # You can add more languages to the list if needed.
            transcript_list = ytt_api.list_transcripts(self.video_id)
            
            # Try to find a manual english transcript, or an auto-generated one
            # The .find_transcript(['en']) is smart enough to find 'en', 'en-US', etc.
            try:
                # Prefer manual
                transcript = transcript_list.find_manually_created_transcript(['en'])
            except:
                try:
                    # Fallback to generated
                    transcript = transcript_list.find_generated_transcript(['en'])
                except:
                    # Fallback to ANY english
                    transcript = transcript_list.find_transcript(['en'])
            
            # 3. Download the actual data
            fetched_transcript = transcript.fetch()
            
            # 4. Format to text
            formatter = TextFormatter()
            text = formatter.format_transcript(fetched_transcript)
            return text
            
        except Exception as e:
             # Fallback: Try direct fetch if list_transcripts fails (sometimes robust for simple cases)
             try:
                logger.info("list_transcripts failed, trying direct fetch fallback...")
                ytt_api = YouTubeTranscriptApi(proxy_config=proxy_config)
                fetched_transcript = ytt_api.fetch(self.video_id, languages=['en'])
                formatter = TextFormatter()
                return formatter.format_transcript(fetched_transcript)
             except Exception as e2:
                logger.error(f"Transcript failed: {e}")
                raise RuntimeError(f"Could not get transcript. Check proxy or if video has CC. Error: {e}")

    def generate_summary(self, content: str) -> str:
        """Uses Gemini to summarize the content."""
        if not self.gemini_client:
            raise RuntimeError("Gemini API key not configured or SDK missing")
            
        source_label = "YouTube transcript" if self.platform == "youtube" else "Tweet text"
        prompt = f"Summarize this {source_label} into a structured guide with Title, Key Points, and Workflow. Return plain text.\n\nCONTENT:\n{content}"
        try:
            response = self.gemini_client.models.generate_content(
                model=self.cfg.gemini_model,
                contents=prompt
            )
            return response.text
        except Exception as e:
            logger.error(f"Gemini summary failed: {e}")
            raise RuntimeError(f"Gemini generation failed: {e}")

    def generate_brief(self, summary: str) -> str:
        """Uses Gemini to create an infographic brief."""
        if not self.gemini_client:
            raise RuntimeError("Gemini API key not configured")
            
        prompt = f"Create an infographic design brief for LinkedIn (16:9) from this summary. Focus on visual hierarchy. Plain text.\n\nSUMMARY:\n{summary}"
        try:
            response = self.gemini_client.models.generate_content(
                model=self.cfg.gemini_model,
                contents=prompt
            )
            return response.text
        except Exception as e:
            logger.error(f"Gemini brief failed: {e}")
            raise RuntimeError(f"Gemini brief generation failed: {e}")

    def generate_image_kie(self, brief: str) -> str:
        """Generates an image using Kie.ai."""
        if not self.cfg.kie_api_key:
            raise RuntimeError("Kie API key not configured")
            
        headers = {
            "Authorization": f"Bearer {self.cfg.kie_api_key}", 
            "Content-Type": "application/json"
        }
        payload = {
            "model": "nano-banana-pro", 
            "input": {"prompt": brief, "aspect_ratio": "16:9"}
        }
        
        try:
            r = requests.post("https://api.kie.ai/api/v1/jobs/createTask", headers=headers, json=payload, timeout=10)
            r.raise_for_status()
            data = r.json()
            task_id = data.get("data", {}).get("taskId")
            if not task_id:
                raise RuntimeError(f"No taskId returned from Kie: {data}")
        except Exception as e:
            raise RuntimeError(f"Kie task creation failed: {e}")

        # Poll for completion
        start_time = time.time()
        while time.time() - start_time < 120: # 2 minute timeout
            time.sleep(5)
            try:
                res = requests.get(
                    "https://api.kie.ai/api/v1/jobs/recordInfo", 
                    headers=headers, 
                    params={"taskId": task_id},
                    timeout=10
                )
                res_data = res.json()
                state = res_data.get("data", {}).get("state")
                
                if state == "success":
                    result_json = res_data.get("data", {}).get("resultJson")
                    # Handle stringified JSON if necessary
                    if isinstance(result_json, str):
                        result_json = json.loads(result_json)
                    
                    urls = result_json.get("resultUrls", [])
                    if urls:
                        return urls[0]
                    else:
                        raise RuntimeError("Kie success but no URLs found")
                        
                if state == "fail":
                    raise RuntimeError(f"Kie generation failed: {res_data}")
            except Exception as e:
                logger.warning(f"Kie polling error: {e}")
                continue
                
        raise TimeoutError("Kie image generation timed out")

    def generate_post_claude(self, content: str) -> str:
        """Generates a LinkedIn post using Claude."""
        if not self.anthropic_client:
            raise RuntimeError("Anthropic API key not configured or SDK missing")
        
        if self.platform == "twitter":
            prompt = f"Write a high-converting LinkedIn post expanding on this tweet. Hook, bullets, CTA. Plain text.\n\nTWEET:\n{content}"
        else:
            prompt = f"Write a high-converting LinkedIn post from this transcript. Hook, bullets, CTA. Plain text.\n\nTRANSCRIPT:\n{content}"
        
        try:
            msg = self.anthropic_client.messages.create(
                model=self.cfg.claude_model,
                max_tokens=1000,
                messages=[{"role": "user", "content": prompt}]
            )
            # Handle text block response
            if hasattr(msg.content[0], 'text'):
                return msg.content[0].text
            return str(msg.content)
        except Exception as e:
            logger.error(f"Claude post generation failed: {e}")
            raise RuntimeError(f"Claude generation failed: {e}")

    def upload_cloudinary(self, image_url: str) -> str:
        """Uploads an image URL to Cloudinary and returns the secure URL."""
        if not self.cfg.cloudinary_cloud_name:
            return image_url # Fallback if not configured
            
        try:
            # Download image first
            r = requests.get(image_url, timeout=30)
            r.raise_for_status()
            
            timestamp = int(time.time())
            public_id = f"yt_{timestamp}"
            
            # Generate signature
            to_sign = f"public_id={public_id}&timestamp={timestamp}{self.cfg.cloudinary_api_secret}"
            signature = hashlib.sha1(to_sign.encode()).hexdigest()
            
            data = {
                "api_key": self.cfg.cloudinary_api_key,
                "timestamp": timestamp,
                "public_id": public_id,
                "signature": signature
            }
            files = {"file": r.content}
            
            upload_url = f"https://api.cloudinary.com/v1_1/{self.cfg.cloudinary_cloud_name}/image/upload"
            res = requests.post(upload_url, data=data, files=files, timeout=30)
            res.raise_for_status()
            return res.json().get("secure_url")
        except Exception as e:
            logger.error(f"Cloudinary upload failed: {e}")
            return image_url # Return original on failure

    def post_blotato(self, text: str, image_url: str):
        """Posts to LinkedIn via Blotato API."""
        # Validate required credentials
        if not self.cfg.blotato_api_key or not self.cfg.blotato_api_key.strip():
            raise RuntimeError("BLOTATO_API_KEY is not configured")
        
        if not self.blotato_account_id or not self.blotato_account_id.strip():
            raise RuntimeError("BLOTATO_ACCOUNT_ID is not configured")

        logger.info(f"Posting to LinkedIn via Blotato (account: {self.blotato_account_id[:8]}...)")

        headers = {
            "Content-Type": "application/json",
            "blotato-api-key": self.cfg.blotato_api_key.strip()
        }
        
        payload = {
            "post": {
                "accountId": self.blotato_account_id.strip(),
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
        
        try:
            r = requests.post(
                "https://backend.blotato.com/v2/posts",
                headers=headers,
                json=payload,
                timeout=30
            )
            r.raise_for_status()
            logger.info("Successfully posted to LinkedIn via Blotato")
            return r.json()
        except requests.exceptions.HTTPError as e:
            # Log the response body for debugging 422 errors
            error_detail = ""
            try:
                error_detail = f" - Response: {r.text}"
            except:
                pass
            logger.error(f"Blotato post failed: {e}{error_detail}")
            raise RuntimeError(f"Blotato post failed: {e}{error_detail}")
        except Exception as e:
            logger.error(f"Blotato post failed: {e}")
            raise RuntimeError(f"Blotato post failed: {e}")

    def run_all(self) -> Dict[str, Any]:
        """Runs the full pipeline."""
        content = self.get_content()
        summary = self.generate_summary(content)
        brief = self.generate_brief(summary)
        
        try:
            raw_img = self.generate_image_kie(brief)
            final_img = self.upload_cloudinary(raw_img)
        except Exception as e:
            logger.error(f"Image generation pipeline failed: {e}")
            final_img = "" # Continue without image if it fails
            
        post_text = self.generate_post_claude(content)
        
        if final_img:
            self.post_blotato(post_text, final_img)
            
        return {
            "url": final_img, 
            "summary": summary, 
            "post": post_text,
            "brief": brief
        }
