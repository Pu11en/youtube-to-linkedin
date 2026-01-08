import logging
import traceback
import sys
import os
import re
import requests

# Ensure root directory is in path so we can import 'app'
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, render_template, request, jsonify

# Import from our new app structure
from app.config import Config
from app.queue_manager import SimpleQueue, ClientManager
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

# ============== TELEGRAM BOT ==============

def send_telegram(chat_id: str, text: str, cfg: Config):
    """Send a message via Telegram bot."""
    url = f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})

def extract_url(text: str) -> str:
    """Extract YouTube or Twitter URL from message text."""
    patterns = [
        r'(https?://(?:www\.)?youtube\.com/watch\?v=[\w-]+)',
        r'(https?://(?:www\.)?youtu\.be/[\w-]+)',
        r'(https?://(?:www\.)?twitter\.com/\w+/status/\d+)',
        r'(https?://(?:www\.)?x\.com/\w+/status/\d+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None

# Store active client per chat (in-memory, resets on deploy - fine for single admin)
active_client = {}

@app.route('/telegram-webhook', methods=['POST'])
def telegram_webhook():
    cfg = Config()
    data = request.json
    
    if not data or 'message' not in data:
        return jsonify({"ok": True})
    
    message = data['message']
    chat_id = str(message.get('chat', {}).get('id', ''))
    text = message.get('text', '').strip()
    
    # Optional: Restrict to your chat ID only
    if cfg.telegram_admin_chat_id and chat_id != cfg.telegram_admin_chat_id:
        send_telegram(chat_id, "⛔ Unauthorized. This bot is private.", cfg)
        return jsonify({"ok": True})
    
    clients = ClientManager(cfg)
    q = SimpleQueue(cfg)
    
    # Command: /start
    if text == '/start':
        send_telegram(chat_id, 
            "🚀 <b>LinkedIn Poster Bot</b>\n\n"
            "Send me a YouTube or Twitter/X link and I'll queue it for LinkedIn.\n\n"
            "<b>Commands:</b>\n"
            "/clients - List all clients\n"
            "/client &lt;name&gt; - Switch active client\n"
            "/status - Show queue counts\n"
            "/add &lt;name&gt; &lt;blotato_id&gt; - Add new client", cfg)
        return jsonify({"ok": True})
    
    # Command: /clients
    if text == '/clients':
        all_clients = clients.get_all()
        if not all_clients:
            send_telegram(chat_id, "No clients yet. Add one with:\n/add <name> <blotato_account_id>", cfg)
        else:
            current = active_client.get(chat_id, 'default')
            msg = "👥 <b>Clients:</b>\n"
            for name, info in all_clients.items():
                marker = " ✅" if name == current else ""
                msg += f"• {name}{marker}\n"
            send_telegram(chat_id, msg, cfg)
        return jsonify({"ok": True})
    
    # Command: /client <name>
    if text.startswith('/client '):
        name = text[8:].strip().lower()
        all_clients = clients.get_all()
        if name not in all_clients and name != 'default':
            send_telegram(chat_id, f"❌ Client '{name}' not found. Use /clients to see list.", cfg)
        else:
            active_client[chat_id] = name
            send_telegram(chat_id, f"✅ Switched to client: <b>{name}</b>", cfg)
        return jsonify({"ok": True})
    
    # Command: /status
    if text == '/status':
        current = active_client.get(chat_id, 'default')
        urls = q.get_urls(current)
        send_telegram(chat_id, f"📊 <b>Status for {current}:</b>\n• Queue: {len(urls)} URLs pending", cfg)
        return jsonify({"ok": True})
    
    # Command: /add <name> <blotato_id>
    if text.startswith('/add '):
        parts = text[5:].strip().split()
        if len(parts) < 2:
            send_telegram(chat_id, "Usage: /add <name> <blotato_account_id>", cfg)
        else:
            name = parts[0].lower()
            blotato_id = parts[1]
            clients.add_client(name, blotato_id)
            active_client[chat_id] = name
            send_telegram(chat_id, f"✅ Added client <b>{name}</b> and switched to it.", cfg)
        return jsonify({"ok": True})
    
    # Handle URL - add to queue
    url = extract_url(text)
    if url:
        current = active_client.get(chat_id, 'default')
        q.add_url(url, current)
        queue_size = len(q.get_urls(current))
        send_telegram(chat_id, f"✅ Added to <b>{current}</b> queue!\n\n📝 Queue size: {queue_size}", cfg)
        return jsonify({"ok": True})
    
    # Unknown command
    send_telegram(chat_id, "🤔 Send me a YouTube or Twitter/X link, or use /start for help.", cfg)
    return jsonify({"ok": True})

@app.route('/api/auto_process_all', methods=['POST'])
def auto_process_all():
    """Process one URL from each client's queue."""
    auth = request.headers.get('Authorization')
    cfg = Config()
    if cfg.cron_secret and auth != f"Bearer {cfg.cron_secret}":
        return jsonify({"error": "unauthorized"}), 401
    
    clients = ClientManager(cfg)
    q = SimpleQueue(cfg)
    results = []
    
    # Process default queue (your original account)
    all_client_names = ['default'] + list(clients.get_all().keys())
    
    for client_name in all_client_names:
        url = q.pop_next(client_name)
        if not url:
            continue
        
        try:
            # Get blotato_account_id for this client
            if client_name == 'default':
                blotato_account_id = cfg.blotato_account_id
            else:
                client_info = clients.get_client(client_name)
                blotato_account_id = client_info.get('blotato_account_id', cfg.blotato_account_id)
            
            pipeline = ContentPipeline(cfg, url, blotato_account_id=blotato_account_id)
            pipeline.run_all()
            q.mark_done(url, client_name)
            results.append({"client": client_name, "url": url, "status": "posted"})
        except Exception as e:
            logger.error(f"Auto process failed for {client_name}: {e}")
            results.append({"client": client_name, "url": url, "status": "failed", "error": str(e)})
    
    if not results:
        return jsonify({"status": "idle", "message": "No URLs in any queue"})
    
    return jsonify({"status": "processed", "results": results})

if __name__ == '__main__':
    app.run(debug=True, port=4000)
