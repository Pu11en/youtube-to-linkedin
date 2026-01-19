import time
import json
import hashlib
import logging
import re
import random
import requests
from typing import Optional, Dict, Any, Tuple

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


# =============================================================================
# STYLE VARIATIONS - Experiment with different approaches
# =============================================================================

HOOK_VARIATIONS = {
    "bold_claim": """Start with a bold, slightly controversial claim that challenges conventional wisdom.
Example: "Most developers are doing this completely wrong." or "This tool is criminally underrated." """,
    
    "personal_test": """Start with a personal experiment with specific timeframe and results.
Example: "Just tested X for 8 weeks straight. The results?" or "I spent 3 months building with this." """,
    
    "numbers_first": """Lead with a striking statistic or number.
Example: "15+ hours saved per week." or "From 0 to 10,000 users in 6 weeks." """,
    
    "question_hook": """Open with a thought-provoking question that creates curiosity.
Example: "What if you could 10x your output without working harder?" """,
    
    "pattern_interrupt": """Start with something unexpected that stops the scroll.
Example: "Delete your to-do list." or "Stop learning new frameworks." """
}

STRUCTURE_VARIATIONS = {
    "bullets_bold": """Use bullet points with **bold headers** followed by description.
Format: • **Header Name** - Specific description with metrics (13 minutes, 38 sources)""",
    
    "numbered_steps": """Use numbered list for sequential steps or ranked items.
Format: 1. **Step Name** - What to do and why""",
    
    "short_paragraphs": """Use very short paragraphs (1-2 sentences each) without bullets.
Create rhythm through line breaks.""",
    
    "problem_solution": """Structure as Problem → Insight → Solution for each point.
Show the pain, then the relief."""
}

CLOSER_VARIATIONS = {
    "impressive_part": """Use "The most impressive part?" as transition to key insight.""",
    
    "mindset_shift": """Use "The biggest mindset shift:" to share the transformation.""",
    
    "bottom_line": """Use "Bottom line:" for a direct, no-nonsense summary.""",
    
    "real_talk": """Use "Here's the truth:" for authentic, direct closing."""
}

CTA_VARIATIONS = {
    "which_first": """End with "Which of these are you most excited to try first?" """,
    
    "what_would": """End with "What [specific task] would you want [tool] to handle?" """,
    
    "drop_comment": """End with "Drop a 🔥 if you're trying this" or similar engagement ask.""",
    
    "save_this": """End with "Save this for later - you'll need it." """,
    
    "hot_take": """End with "Hot take? Let me know if you disagree." """
}


class ContentPipeline:
    def __init__(self, config: Config, url: str = "", blotato_account_id: str = None, style: str = "default"):
        self.cfg = config
        self.url = url
        self.platform = detect_platform(url)
        # Allow override for multi-client support
        self.blotato_account_id = blotato_account_id or config.blotato_account_id
        self.style = style
        self.experiment_variation = None  # Track which variation was used
        
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

    def _parse_caption_text(self, vtt_content: str) -> str:
        """Extract plain text from VTT/SRT caption format."""
        lines = vtt_content.split('\n')
        text_lines = []
        for line in lines:
            line = line.strip()
            # Skip timing lines, headers, and empty lines
            if not line or '-->' in line or line.startswith('WEBVTT') or line.isdigit():
                continue
            # Remove VTT styling tags
            line = re.sub(r'<[^>]+>', '', line)
            if line:
                text_lines.append(line)
        return ' '.join(text_lines)

    def _fetch_transcript_via_piped(self, video_id: str) -> str:
        """Fallback: fetch transcript via Piped instances."""
        piped_instances = [
            "https://pipedapi.kavin.rocks",
            "https://pipedapi.adminforge.de", 
            "https://api.piped.yt",
            "https://pipedapi.in.projectsegfau.lt",
        ]
        
        for instance in piped_instances:
            try:
                # Piped streams endpoint includes captions
                streams_url = f"{instance}/streams/{video_id}"
                resp = requests.get(streams_url, timeout=15, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                })
                if resp.status_code != 200:
                    logger.warning(f"Piped {instance} returned {resp.status_code}")
                    continue
                
                data = resp.json()
                subtitles = data.get("subtitles", [])
                
                # Find English subtitles
                for sub in subtitles:
                    lang = sub.get("code", "").lower()
                    if lang.startswith("en"):
                        sub_url = sub.get("url", "")
                        if sub_url:
                            sub_resp = requests.get(sub_url, timeout=15)
                            if sub_resp.status_code == 200:
                                transcript = self._parse_caption_text(sub_resp.text)
                                if transcript:
                                    logger.info(f"Successfully fetched transcript via Piped {instance}")
                                    return transcript
            except Exception as e:
                logger.warning(f"Piped {instance} failed: {e}")
                continue
        
        return None  # Return None to try next fallback

    def _fetch_transcript_via_invidious(self, video_id: str) -> str:
        """Fallback: fetch transcript via Invidious instances when YouTube blocks."""
        instances = [
            "https://inv.nadeko.net",
            "https://yewtu.be",
            "https://invidious.nerdvpn.de",
            "https://inv.tux.pizza",
            "https://invidious.projectsegfau.lt",
            "https://vid.puffyan.us",
            "https://invidious.fdn.fr",
        ]
        
        for instance in instances:
            try:
                # Get available captions
                captions_url = f"{instance}/api/v1/captions/{video_id}"
                resp = requests.get(captions_url, timeout=10, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                })
                if resp.status_code != 200:
                    logger.warning(f"Invidious {instance} returned {resp.status_code}")
                    continue
                
                data = resp.json()
                captions = data.get("captions", [])
                
                # Find English track
                for track in captions:
                    lang = track.get("languageCode", "").lower()
                    if lang.startswith("en"):
                        track_url = track.get('url', '')
                        if not track_url.startswith('http'):
                            track_url = f"{instance}{track_url}"
                        track_resp = requests.get(track_url, timeout=15, headers={
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                        })
                        if track_resp.status_code == 200:
                            transcript = self._parse_caption_text(track_resp.text)
                            if transcript:
                                logger.info(f"Successfully fetched transcript via {instance}")
                                return transcript
            except Exception as e:
                logger.warning(f"Invidious {instance} failed: {e}")
                continue
        
        return None  # Return None to try next fallback

    def _fetch_transcript_via_youtubei(self, video_id: str) -> str:
        """Fallback: fetch transcript via YouTube's internal API (no auth needed for captions)."""
        try:
            # Get video page to extract caption tracks
            watch_url = f"https://www.youtube.com/watch?v={video_id}"
            resp = requests.get(watch_url, timeout=15, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
            })
            
            if resp.status_code != 200:
                return None
                
            # Look for timedtext URL in the page
            import re
            # Find captionTracks in the page
            caption_match = re.search(r'"captionTracks":\s*(\[.*?\])', resp.text)
            if caption_match:
                import json
                tracks = json.loads(caption_match.group(1))
                for track in tracks:
                    lang = track.get("languageCode", "").lower()
                    if lang.startswith("en"):
                        base_url = track.get("baseUrl", "")
                        if base_url:
                            # Fetch the transcript
                            caption_resp = requests.get(base_url, timeout=15, headers={
                                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                            })
                            if caption_resp.status_code == 200:
                                # Parse XML transcript
                                text_parts = re.findall(r'<text[^>]*>([^<]+)</text>', caption_resp.text)
                                if text_parts:
                                    # Decode HTML entities
                                    import html
                                    transcript = ' '.join(html.unescape(t) for t in text_parts)
                                    logger.info("Successfully fetched transcript via YouTubei")
                                    return transcript
        except Exception as e:
            logger.warning(f"YouTubei fallback failed: {e}")
        
        return None

    def _get_fresh_proxy_url(self) -> Optional[str]:
        """Get proxy URL with fresh session ID for this request."""
        import random
        import string
        
        base_url = self.cfg.proxy_url
        if not base_url:
            return None
        
        # If URL contains _session-, rotate it with fresh session
        if "_session-" in base_url:
            new_session = ''.join(random.choices(string.ascii_letters + string.digits, k=12))
            rotated = re.sub(r'_session-[^:@]+', f'_session-{new_session}', base_url)
            return rotated
        
        return base_url

    def get_transcript(self) -> str:
        """Fetches and formats transcript from YouTube with multiple fallbacks."""
        video_id = extract_youtube_id(self.url)
        if not video_id:
            raise ValueError("No valid video ID extracted from URL")

        errors = []
        
        # Method 1: Try YouTubeTranscriptApi with proxy (fresh session each time)
        proxy_config = None
        fresh_proxy = self._get_fresh_proxy_url()
        if fresh_proxy:
            logger.info(f"Using proxy with fresh session: {fresh_proxy[:50]}...")
            proxy_config = GenericProxyConfig(
                http_url=fresh_proxy,
                https_url=fresh_proxy,
            )
        else:
            logger.warning("No PROXY_URL configured - YouTube will likely block requests")
        
        try:
            ytt_api = YouTubeTranscriptApi(proxy_config=proxy_config)
            fetched_transcript = ytt_api.fetch(video_id, languages=['en', 'en-US', 'en-GB'])
            formatter = TextFormatter()
            logger.info("Successfully fetched transcript via YouTubeTranscriptApi")
            return formatter.format_transcript(fetched_transcript)
        except Exception as e:
            logger.warning(f"YouTubeTranscriptApi failed: {e}")
            errors.append(f"YouTubeTranscriptApi: {e}")
        
        # Method 2: Try Piped API
        logger.info("Trying Piped fallback...")
        try:
            result = self._fetch_transcript_via_piped(video_id)
            if result:
                return result
            errors.append("Piped: No transcript found")
        except Exception as e:
            logger.warning(f"Piped fallback failed: {e}")
            errors.append(f"Piped: {e}")
        
        # Method 3: Try Invidious
        logger.info("Trying Invidious fallback...")
        try:
            result = self._fetch_transcript_via_invidious(video_id)
            if result:
                return result
            errors.append("Invidious: No transcript found")
        except Exception as e:
            logger.warning(f"Invidious fallback failed: {e}")
            errors.append(f"Invidious: {e}")
        
        # Method 4: Try direct YouTube page scraping
        logger.info("Trying YouTubei fallback...")
        try:
            result = self._fetch_transcript_via_youtubei(video_id)
            if result:
                return result
            errors.append("YouTubei: No transcript found")
        except Exception as e:
            logger.warning(f"YouTubei fallback failed: {e}")
            errors.append(f"YouTubei: {e}")
        
        # All methods failed
        raise RuntimeError(f"TRANSCRIPT_FAILED: All methods failed to get transcript. Errors: {'; '.join(errors)}")

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
        
        # For SoulPrint style, replace AI tool mentions and add branding
        if self.style == "soulprint":
            summary = self._replace_ai_mentions(summary)
            prompt = f"""Create an infographic design brief for LinkedIn (16:9) from this summary.

MANDATORY BRANDING - SOULPRINT:
- COLOR SCHEME: BLACK background (#000000) with BURNT ORANGE (#CC5500) accents ONLY
- Replace ANY AI tool names (ChatGPT, GPT, Claude, Grok, Gemini, Copilot, Perplexity, etc.) with "SoulPrint"
- LOGO: Include the SoulPrint orange ring logo next to the text "SoulPrint"
- Logo reference URL: https://res.cloudinary.com/djg0pqts6/image/upload/v1767860409/Vector_1_opozvz.png
- Logo is an orange ring/circle - must appear next to "SoulPrint" text
- Position: Logo + "SoulPrint" in top-left header OR bottom-right corner

DESIGN STYLE:
- Dark premium aesthetic - BLACK background
- BURNT ORANGE (#CC5500) for highlights, borders, icons, accents
- White or light gray (#EEEEEE) for main body text
- Modern, minimalist, sleek, professional
- No other colors - only black, burnt orange, white/gray

Focus on visual hierarchy. Plain text brief only.

SUMMARY:
{summary}"""
        else:
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

    def _replace_ai_mentions(self, text: str) -> str:
        """Replace mentions of competitor AI tools with SoulPrint."""
        import re
        ai_tools = [
            r'\bChatGPT\b', r'\bGPT-4\b', r'\bGPT-3\b', r'\bGPT\b',
            r'\bClaude\b', r'\bGrok\b', r'\bGemini\b', r'\bCopilot\b',
            r'\bPerplexity\b', r'\bBard\b', r'\bLLM\b', r'\bLlama\b',
            r'\bMistral\b', r'\bAI assistant\b', r'\bAI chatbot\b'
        ]
        for pattern in ai_tools:
            text = re.sub(pattern, 'SoulPrint', text, flags=re.IGNORECASE)
        return text

    def generate_image_kie(self, brief: str) -> str:
        """Generates an image using Kie.ai."""
        if not self.cfg.kie_api_key:
            raise RuntimeError("Kie API key not configured")
        
        # For SoulPrint style, prepend strict color instructions
        if self.style == "soulprint":
            color_prefix = """STRICT COLOR PALETTE - DO NOT DEVIATE:
- Background: Pure BLACK (#000000)
- Accent color: BURNT ORANGE (#CC5500) 
- Text: WHITE or light gray
- NO other colors allowed. Only black, burnt orange, and white/gray.

IMPORTANT: Do NOT include any logo or "SoulPrint" branding text in the image. 
The logo will be added separately. Focus only on the infographic content.

"""
            brief = color_prefix + brief
            
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

    def _select_variation(self, weights: Dict[str, float] = None) -> Tuple[str, str, str, str, str]:
        """Select variations for this post, weighted by past performance."""
        weights = weights or {}
        
        def weighted_choice(options: dict, category: str) -> Tuple[str, str]:
            """Choose from options, weighted by performance."""
            items = list(options.items())
            option_weights = []
            for name, _ in items:
                key = f"{category}:{name}"
                option_weights.append(weights.get(key, 1.0))
            
            # Normalize and select
            total = sum(option_weights)
            r = random.random() * total
            cumulative = 0
            for (name, value), w in zip(items, option_weights):
                cumulative += w
                if r <= cumulative:
                    return name, value
            return items[-1]  # Fallback
        
        hook_name, hook_prompt = weighted_choice(HOOK_VARIATIONS, "hook")
        struct_name, struct_prompt = weighted_choice(STRUCTURE_VARIATIONS, "structure")
        closer_name, closer_prompt = weighted_choice(CLOSER_VARIATIONS, "closer")
        cta_name, cta_prompt = weighted_choice(CTA_VARIATIONS, "cta")
        
        variation_id = f"{hook_name}|{struct_name}|{closer_name}|{cta_name}"
        
        return variation_id, hook_prompt, struct_prompt, closer_prompt, cta_prompt

    def generate_post_claude(self, content: str, weights: Dict[str, float] = None) -> str:
        """Generates a LinkedIn post using Claude with experimental variations."""
        if not self.anthropic_client:
            raise RuntimeError("Anthropic API key not configured or SDK missing")
        
        # Select variations for this experiment
        variation_id, hook_prompt, struct_prompt, closer_prompt, cta_prompt = self._select_variation(weights)
        self.experiment_variation = variation_id
        logger.info(f"Using variation: {variation_id}")
        
        # Build dynamic prompt with selected variations
        experimental_prompt = f"""
Write a LinkedIn post in this style and structure:

**HOOK (First 1-2 lines):**
{hook_prompt}
- Include specific numbers when possible (hours, weeks, percentages)

**TRANSITION (Line 3-4):**
- Use "Here's what blew my mind:" or "Here's the game-changer:" or "The results?"
- One sentence explaining WHY this matters

**MAIN VALUE SECTION:**
{struct_prompt}
- Include 5-8 specific use cases, tips, or insights
- Add specific metrics where possible (times, quantities, percentages)
- Make each point actionable and concrete

**CLOSER:**
{closer_prompt}
- 1-2 sentences on the key insight or transformation

**CTA (Final line):**
{cta_prompt}

**CRITICAL RULES:**
- NO hashtags anywhere
- NO emojis  
- Use **bold** for headers only
- Keep paragraphs short (1-2 sentences max)
- Be specific with numbers - never vague
- First person perspective ("I tested", "changed for me")
- Confident, direct tone - not salesy or hype-y
- Focus on practical, actionable value
"""
        
        # Add SoulPrint rules if needed
        if self.style == "soulprint":
            experimental_prompt += """

ADDITIONAL FOR SOULPRINT:
Replace ANY mention of AI tools (ChatGPT, GPT, Claude, Grok, Gemini, Copilot, Perplexity, OpenAI, AI assistant, LLM, etc.) with "SoulPrint" instead."""
        
        source_label = "tweet" if self.platform == "twitter" else "transcript"
        prompt = f"{experimental_prompt}\n\nCONTENT ({source_label}):\n{content}\n\nWrite the post now. Return ONLY the post text, nothing else."
        
        try:
            msg = self.anthropic_client.messages.create(
                model=self.cfg.claude_model,
                max_tokens=1500,
                messages=[{"role": "user", "content": prompt}]
            )
            # Handle text block response
            if hasattr(msg.content[0], 'text'):
                post_text = msg.content[0].text
            else:
                post_text = str(msg.content)
            
            # Safety: remove any hashtags that slip through
            post_text = re.sub(r'\n#\w+.*$', '', post_text, flags=re.MULTILINE)
            post_text = re.sub(r'#\w+', '', post_text)
            
            return post_text.strip()
        except Exception as e:
            logger.error(f"Claude post generation failed: {e}")
            raise RuntimeError(f"Claude generation failed: {e}")

    def upload_cloudinary(self, image_url: str) -> str:
        """Uploads an image URL to Cloudinary and returns the secure URL.
        If SoulPrint style, adds logo overlay."""
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
            base_url = res.json().get("secure_url")
            
            # If SoulPrint style, add logo overlay using Cloudinary transformations
            if self.style == "soulprint" and base_url:
                # The SoulPrint logo is already on Cloudinary: Vector_1_opozvz
                # Add it as overlay in bottom-right corner
                # Transform URL to add overlay
                # Format: /image/upload/l_Vector_1_opozvz,w_120,g_south_east,x_30,y_30/public_id
                base_url = base_url.replace(
                    f"/image/upload/{public_id}",
                    f"/image/upload/l_Vector_1_opozvz,w_100,g_south_east,x_20,y_20/{public_id}"
                )
            
            return base_url
        except Exception as e:
            logger.error(f"Cloudinary upload failed: {e}")
            return image_url # Return original on failure

    def post_blotato(self, text: str, image_url: str, scheduled_time: str = None):
        """Posts to LinkedIn via Blotato API.
        
        Args:
            text: Post text content
            image_url: URL of the image to attach
            scheduled_time: Optional ISO 8601 timestamp for scheduled posting (e.g., '2026-01-20T16:00:00Z')
        """
        # Validate required credentials
        if not self.cfg.blotato_api_key or not self.cfg.blotato_api_key.strip():
            raise RuntimeError("BLOTATO_API_KEY is not configured")
        
        if not self.blotato_account_id or not self.blotato_account_id.strip():
            raise RuntimeError("BLOTATO_ACCOUNT_ID is not configured")

        post_type = f"scheduled for {scheduled_time}" if scheduled_time else "immediate"
        logger.info(f"Posting to LinkedIn via Blotato ({post_type}, account: {self.blotato_account_id[:8]}...)")

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
        
        # Add scheduled time if provided
        if scheduled_time:
            payload["scheduledTime"] = scheduled_time
        
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

    def run_all(self, skip_post: bool = False) -> Dict[str, Any]:
        """
        Runs the full pipeline. 
        If skip_post is True, it returns the generated data without posting to LinkedIn.
        """
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
        
        # Generate unique post ID for experiment tracking
        post_id = hashlib.md5(f"{self.url}:{time.time()}".encode()).hexdigest()[:12]
        
        result = {
            "platform": self.platform,
            "url": self.url,
            "image_url": final_img, 
            "summary": summary, 
            "post_text": post_text,
            "brief": brief,
            "blotato_account_id": self.blotato_account_id,
            "post_id": post_id,
            "variation": self.experiment_variation
        }

        if not skip_post and final_img:
            self.post_blotato(post_text, final_img)
            result["posted"] = True
        else:
            result["posted"] = False
            
        return result
