# Youtube -> LinkedIn Infographic Pipeline

This repo runs a pipeline that: fetches a YouTube transcript, summarizes with Gemini, creates an infographic brief, generates an image (Kie), drafts LinkedIn post and newsletter (Anthropic), and uploads the image to Cloudinary.

Prerequisites
- Python 3.10+ (3.12 recommended)
- Git (optional)

Quick setup (PowerShell)

```powershell
cd C:\Users\david\OneDrive\Desktop\Archeforge_Development
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

.env
- Put your API keys and settings in a `.env` file next to `Youtube_to_Linkedin.py`.
- Required variables:
  - `YOUTUBE_URL` (e.g. https://www.youtube.com/watch?v=...)
  - `GEMINI_API_KEY`
  - `KIE_API_KEY`
  - `ANTHROPIC_API_KEY`
  - `CLOUDINARY_CLOUD_NAME`
  - `CLOUDINARY_API_KEY`
  - `CLOUDINARY_API_SECRET`
- Optional:
  - `GEMINI_MODEL` (default: `gemini-2.5-flash`)
  - `CLAUDE_MODEL` (optional; script will try to auto-detect a usable Claude model)

Run pipeline (real keys)

```powershell
# Activate venv first
.\.venv\Scripts\Activate.ps1
python run_real_pipeline.py
```

Run pipeline in fake-mode (for local testing)
- You can set any API key to start with `fake` to exercise local fakes for that service.

```powershell
# e.g. in .env set GEMINI_API_KEY=fake-123 and KIE_API_KEY=fake-123
python test_full_run.py
```

Outputs
- The pipeline writes outputs to the `output/` directory: `summary.txt`, `infographic_brief.txt`, `linkedin_post.txt`, `newsletter.txt`, `infographic.png`, and `result.json`.

Notes
- The script supports both legacy `google.generativeai` and the newer `google-genai` packages; installing both is harmless and can help compatibility.
- If Anthropic model calls fail due to unavailable model names, the script attempts to discover a usable `claude` model automatically.

If you want, I can: commit these changes, pin package versions in `requirements.txt`, or switch the code to `google.genai` strictly. Which would you prefer next?

