import logging
import traceback
import sys
import os

# Ensure root directory is in path so we can import 'app'
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, render_template, request, jsonify

# Import from our new app structure
from app.config import Config
from app.queue_manager import SimpleQueue
from app.services import ContentPipeline

# Configuration
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder='templates', static_folder='static')

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/api/queue', methods=['GET', 'POST'])
def handle_queue():
    cfg = Config()
    q = SimpleQueue(cfg)
    try:
        if request.method == 'POST':
            urls = request.json.get('urls', [])
            q.set_urls(urls)
            return jsonify({"status": "saved"})
        return jsonify({
            "urls": q.get_urls(), 
            "history": q.get_history(),
            "redis_active": q.redis is not None
        })
    except Exception as e:
        logger.error(f"Queue error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/add', methods=['POST'])
def add_to_queue():
    url = request.json.get('url')
    if not url: return jsonify({"error": "no url"}), 400
    try:
        q = SimpleQueue(Config())
        q.add_url(url)
        return jsonify({"status": "added"})
    except Exception as e:
        logger.error(f"Add queue error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/generate', methods=['POST'])
def generate_only():
    url = request.json.get('url')
    if not url: return jsonify({"error": "no url"}), 400
    cfg = Config()
    try:
        pipeline = ContentPipeline(cfg, url)
        # We only want to generate content, not post it
        # Step 1: Content (Transcript or Tweet Text)
        content = pipeline.get_content()
        # Step 2: Summary
        summary = pipeline.generate_summary(content)
        # Step 3: Brief -> Image
        brief = pipeline.generate_brief(summary)
        raw_img = pipeline.generate_image_kie(brief)
        final_img = pipeline.upload_cloudinary(raw_img)
        # Step 4: Post Text
        post_text = pipeline.generate_post_claude(content)
        
        result = {
            "url": final_img, 
            "summary": summary, 
            "post": post_text,
            "brief": brief
        }
        return jsonify({"status": "success", "result": result})
    except Exception as e:
        logger.error(f"Generation failed: {e}")
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/post_custom', methods=['POST'])
def post_custom():
    data = request.json
    text = data.get('post')
    image_url = data.get('url')
    if not text or not image_url: return jsonify({"error": "missing data"}), 400
    
    cfg = Config()
    try:
        pipeline = ContentPipeline(cfg) # No URL needed for posting
        pipeline.post_blotato(text, image_url)
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
        result = pipeline.run_all()
        q.mark_done(url)
        return jsonify({"status": "success", "result": result})
    except Exception as e:
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
        pipeline.run_all()
        q.mark_done(url)
        return jsonify({"status": "posted", "url": url})
    except Exception as e:
        logger.error(f"Auto process failed: {e}")
        return jsonify({"status": "failed", "error": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, port=4000)
