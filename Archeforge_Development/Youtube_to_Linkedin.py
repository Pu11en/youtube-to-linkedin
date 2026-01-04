import os
import sys
import re
import time
import json
import hmac
import hashlib

from dataclasses import dataclass
from typing import Any, Dict, Tuple
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


def require_env(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


def load_config() -> Config:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(dotenv_path=os.path.join(script_dir, ".env"))

    return Config(
        youtube_url=require_env("YOUTUBE_URL"),
        gemini_api_key=require_env("GEMINI_API_KEY"),
        kie_api_key=require_env("KIE_API_KEY"),
        anthropic_api_key=require_env("ANTHROPIC_API_KEY"),
        cloudinary_cloud_name=require_env("CLOUDINARY_CLOUD_NAME"),
        cloudinary_api_key=require_env("CLOUDINARY_API_KEY"),
        cloudinary_api_secret=require_env("CLOUDINARY_API_SECRET"),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        claude_model=os.getenv("CLAUDE_MODEL", "claude-3-5-sonnet-latest"),
    )


def http_raise(resp: requests.Response) -> None:
    if resp.ok:
        return
    try:
        detail = resp.json()
    except Exception:
        detail = resp.text[:800]
    raise RuntimeError(f"HTTP {resp.status_code}: {detail}")


def extract_youtube_video_id(url: str) -> str:
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


def slugify(s: str, max_len: int = 80) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s[:max_len] if len(s) > max_len else s


# Transcript
def get_transcript_youtube_api(youtube_url: str) -> str:
    video_id = extract_youtube_video_id(youtube_url)

    try:
        # Try module-level API first
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
# Gemini
def gemini_structured_summary(cfg: Config, transcript: str) -> str:
    if cfg.gemini_api_key and cfg.gemini_api_key.lower().startswith("fake"):
        return (
            "Suggested Title: Sample Video on Building Pipelines\n\n"
            "Key Takeaways:\n"
            "- Break problems into clear phases.\n"
            "- Use transcripts as ground-truth for content generation.\n"
            "- Generate visuals from concise design briefs.\n\n"
            "Step-by-step Workflow:\n"
            "1. Get transcript. 2. Summarize. 3. Build infographic brief. 4. Generate image. 5. Upload."
        )

    if genai is None:
        raise RuntimeError("Gemini SDK not available; install google.generativeai or adjust to the new SDK API")

    try:
        genai.configure(api_key=cfg.gemini_api_key)
    except Exception:
        pass

    try:
        model = genai.GenerativeModel(cfg.gemini_model)
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
        extra = ""
        try:
            if hasattr(genai, "list_models"):
                models = list(genai.list_models())
                extra = f" Available models (sample): {[str(m) for m in models[:10]]}"
        except Exception:
            extra = " (failed to list models)"
        raise RuntimeError(f"Gemini call failed: {e}.{extra}")

    if not text:
        raise RuntimeError("Gemini returned an empty summary.")
    return text


def gemini_infographic_brief(cfg: Config, summary: str) -> str:
    if cfg.gemini_api_key and cfg.gemini_api_key.lower().startswith("fake"):
        return (
            "Headline: How to build a simple content pipeline\n\n"
            "Layout: 16:9 landscape with headline top-left, 3 step blocks across center, tools callouts bottom.\n\n"
            "Sections:\n"
            "- Problem: Too manual to repurpose long-form video.\n"
            "- Solution: Automate transcript → summary → infographic → publish.\n\n"
            "Visuals: icons for Transcript, AI Summary, Design Brief, Image Generator, Cloud Upload.\n\n"
            "Short copy blocks: Keep each step to one concise sentence for readability on LinkedIn."
        )

    if genai is None:
        raise RuntimeError("Gemini SDK not available; install google.generativeai or adjust to the new SDK API")

    try:
        model = genai.GenerativeModel(cfg.gemini_model)
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
        extra = ""
        try:
            if hasattr(genai, "list_models"):
                models = list(genai.list_models())
                extra = f" Available models (sample): {[str(m) for m in models[:10]]}"
        except Exception:
            extra = " (failed to list models)"
        raise RuntimeError(f"Gemini call failed: {e}.{extra}")

    if not text:
        raise RuntimeError("Gemini returned an empty infographic brief.")
    return text


# Kie
def kie_create_task(cfg: Config, infographic_brief: str) -> str:
    if cfg.kie_api_key and str(cfg.kie_api_key).lower().startswith("fake"):
        return "fake-task-123"

    payload = {"model": "nano-banana-pro", "input": {"prompt": infographic_brief, "aspect_ratio": "16:9", "resolution": "2K", "output_format": "png"}}
    r = requests.post(cfg.kie_create_task_url, headers={"Authorization": f"Bearer {cfg.kie_api_key}", "Content-Type": "application/json"}, json=payload, timeout=60)
    http_raise(r)
    data = r.json()
    task_id = data.get("data", {}).get("taskId")
    if not task_id:
        raise RuntimeError(f"Kie createTask missing taskId. Response: {data}")
    return task_id


def kie_poll_until_success(cfg: Config, task_id: str) -> Dict[str, Any]:
    start = time.time()
    while True:
        if time.time() - start > cfg.poll_timeout_sec:
            raise TimeoutError(f"Kie task timed out after {cfg.poll_timeout_sec}s. taskId={task_id}")

        if cfg.kie_api_key and str(cfg.kie_api_key).lower().startswith("fake"):
            fake_result = {"data": {"state": "success", "resultJson": json.dumps({"resultUrls": [
                "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8Xw8AAn8B9pWb1wAAAABJRU5ErkJggg=="
            ]})}}
            return fake_result

        r = requests.get(cfg.kie_record_info_url, headers={"Authorization": f"Bearer {cfg.kie_api_key}"}, params={"taskId": task_id}, timeout=60)
        http_raise(r)
        info = r.json()
        state = info.get("data", {}).get("state")
        if state == "success":
            return info
        if state == "fail":
            msg = info.get("data", {}).get("failMsg") or "Unknown failure"
            raise RuntimeError(f"Kie task failed: {msg}")
        time.sleep(cfg.poll_interval_sec)


def kie_extract_image_url(task_info: Dict[str, Any]) -> str:
    result_json = task_info.get("data", {}).get("resultJson")
    if not result_json:
        raise RuntimeError(f"No resultJson in Kie recordInfo response: {task_info}")
    parsed = json.loads(result_json) if isinstance(result_json, str) else result_json
    urls = parsed.get("resultUrls") or parsed.get("result_urls") or []
    if not urls:
        raise RuntimeError(f"No resultUrls found in Kie resultJson: {parsed}")
    return urls[0]


def download_bytes(url: str) -> Tuple[bytes, str]:
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
    http_raise(r)
    return r.content, r.headers.get("Content-Type", "application/octet-stream")


# Claude (Anthropic)
def claude_linkedin_post(cfg: Config, transcript: str) -> str:
    if cfg.anthropic_api_key and str(cfg.anthropic_api_key).lower().startswith("fake"):
        return (
            "Hook: Here's a short hook to grab attention.\n\n"
            "This post summarizes the key ideas from the video and invites discussion.\n\n"
            "- Key point one\n- Key point two\n- Key point three\n\n"
            "What do you think about these ideas?"
        )

    if Anthropic is None:
        raise RuntimeError("Anthropic SDK is not installed or failed to import. Install 'anthropic'.")

    client = Anthropic(api_key=cfg.anthropic_api_key)
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
        msg = client.messages.create(model=cfg.claude_model, max_tokens=1200, temperature=0.7, messages=[{"role": "user", "content": prompt}])
    except Exception as e:
        alt_model = None
        model_names = []
        try:
                models = []
                if hasattr(client, "models") and hasattr(client.models, "list"):
                    models = list(client.models.list())
                elif hasattr(client, "list_models"):
                    models = list(client.list_models())

                model_names = []
                for m in models:
                    cand = getattr(m, "id", None) or getattr(m, "name", None) or getattr(m, "display_name", None) or str(m)
                    model_names.append(cand)
                    if "claude" in str(cand).lower():
                        alt_model = cand
                        break
        except Exception:
            model_names = model_names or []

        if alt_model:
            try:
                msg = client.messages.create(model=alt_model, max_tokens=1200, temperature=0.7, messages=[{"role": "user", "content": prompt}])
            except Exception as e2:
                raise RuntimeError(f"Anthropic requests failed for both '{cfg.claude_model}' and '{alt_model}'. Last error: {e2}")
        else:
            raise RuntimeError(f"Anthropic/Claude request failed: {e}. Available models: {model_names}")

    try:
        return getattr(msg, "content")[0].text.strip()
    except Exception:
        try:
            return str(msg)
        except Exception:
            raise RuntimeError("Unable to decode Anthropic response into text.")


def claude_newsletter(cfg: Config, transcript: str) -> str:
    if cfg.anthropic_api_key and str(cfg.anthropic_api_key).lower().startswith("fake"):
        return (
            "Title: Transforming Video into Visual Briefs\n\n"
            "This newsletter explains a conceptual workflow to repurpose video transcripts into infographic briefs.\n\n"
            "Step-by-step workflow:\n1. Extract transcript.\n2. Summarize with AI.\n3. Create infographic brief.\n\n"
            "Tools mentioned: Gemini, image generator, Cloudinary.\n\n"
            "Takeaways: Start small, iterate, and test visuals on LinkedIn."
        )

    if Anthropic is None:
        raise RuntimeError("Anthropic SDK is not installed or failed to import. Install 'anthropic'.")

    client = Anthropic(api_key=cfg.anthropic_api_key)
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
        msg = client.messages.create(model=cfg.claude_model, max_tokens=2200, temperature=0.7, messages=[{"role": "user", "content": prompt}])
    except Exception as e:
        alt_model = None
        model_names = []
        try:
                models = []
                if hasattr(client, "models") and hasattr(client.models, "list"):
                    models = list(client.models.list())
                elif hasattr(client, "list_models"):
                    models = list(client.list_models())

                model_names = []
                for m in models:
                    cand = getattr(m, "id", None) or getattr(m, "name", None) or getattr(m, "display_name", None) or str(m)
                    model_names.append(cand)
                    if "claude" in str(cand).lower():
                        alt_model = cand
                        break
        except Exception:
            model_names = model_names or []

        if alt_model:
            try:
                msg = client.messages.create(model=alt_model, max_tokens=2200, temperature=0.7, messages=[{"role": "user", "content": prompt}])
            except Exception as e2:
                raise RuntimeError(f"Anthropic requests failed for both '{cfg.claude_model}' and '{alt_model}'. Last error: {e2}")
        else:
            raise RuntimeError(f"Anthropic/Claude request failed: {e}. Available models: {model_names}")

    try:
        return getattr(msg, "content")[0].text.strip()
    except Exception:
        try:
            return str(msg)
        except Exception:
            raise RuntimeError("Unable to decode Anthropic response into text.")


# STEP 6: Cloudinary Upload (signed)
# -----------------------------

def cloudinary_signature(api_secret: str, params: Dict[str, Any]) -> str:
    to_sign = "&".join(f"{k}={params[k]}" for k in sorted(params.keys()))
    # Cloudinary expects SHA1 of the string_to_sign + api_secret (not HMAC)
    return hashlib.sha1((to_sign + api_secret).encode("utf-8")).hexdigest()


def cloudinary_upload_image(cfg: Config, image_bytes: bytes, public_id: str) -> Dict[str, Any]:
    timestamp = int(time.time())
    upload_url = f"https://api.cloudinary.com/v1_1/{cfg.cloudinary_cloud_name}/image/upload"

    sign_params = {
        "public_id": public_id,
        "timestamp": timestamp,
    }
    signature = cloudinary_signature(cfg.cloudinary_api_secret, sign_params)

    files = {
        "file": ("infographic.png", image_bytes, "image/png"),
    }
    data = {
        "api_key": cfg.cloudinary_api_key,
        "timestamp": str(timestamp),
        "public_id": public_id,
        "signature": signature,
    }

    # Fake-mode: return a stable-looking URL
    if cfg.cloudinary_api_key and str(cfg.cloudinary_api_key).lower().startswith("fake"):
        return {"secure_url": f"https://res.cloudinary.com/demo/{public_id}.png", "public_id": public_id}

    r = requests.post(upload_url, data=data, files=files, timeout=120)
    http_raise(r)
    return r.json()


# -----------------------------
# PIPELINE
# -----------------------------

def run_pipeline(cfg: Config = None) -> Dict[str, Any]:
    if cfg is None:
        cfg = load_config()

    transcript = get_transcript_youtube_api(cfg.youtube_url)
    summary = gemini_structured_summary(cfg, transcript)
    infographic_brief = gemini_infographic_brief(cfg, summary)

    task_id = kie_create_task(cfg, infographic_brief)
    task_info = kie_poll_until_success(cfg, task_id)
    kie_image_url = kie_extract_image_url(task_info)
    image_bytes, _ = download_bytes(kie_image_url)

    linkedin_post = claude_linkedin_post(cfg, transcript)
    newsletter = claude_newsletter(cfg, transcript)

    vid = extract_youtube_video_id(cfg.youtube_url)
    public_id = f"yt_to_linkedin/{slugify(vid)}_{int(time.time())}"
    cloudinary_resp = cloudinary_upload_image(cfg, image_bytes, public_id=public_id)

    # -----------------------------
    # OUTPUTS packaging
    # -----------------------------
    final_outputs = {
        "input": {"youtube_url": cfg.youtube_url},
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
        },
    }

    # Save to files (Phase 6 optional)
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
    os.makedirs(out_dir, exist_ok=True)

    with open(os.path.join(out_dir, "summary.txt"), "w", encoding="utf-8") as f:
        f.write(summary)
    with open(os.path.join(out_dir, "infographic_brief.txt"), "w", encoding="utf-8") as f:
        f.write(infographic_brief)
    with open(os.path.join(out_dir, "linkedin_post.txt"), "w", encoding="utf-8") as f:
        f.write(linkedin_post)
    with open(os.path.join(out_dir, "newsletter.txt"), "w", encoding="utf-8") as f:
        f.write(newsletter)
    with open(os.path.join(out_dir, "result.json"), "w", encoding="utf-8") as f:
        json.dump(final_outputs, f, indent=2)
    with open(os.path.join(out_dir, "infographic.png"), "wb") as f:
        f.write(image_bytes)

    return final_outputs


if __name__ == "__main__":
    try:
        result = run_pipeline()
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(f"\nCRITICAL ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

