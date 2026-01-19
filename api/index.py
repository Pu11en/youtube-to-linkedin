import logging
import traceback
import sys
import os
import re
import json
import hashlib
import requests
from datetime import datetime, timedelta

# Ensure root directory is in path so we can import 'app'
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, render_template, request, jsonify

# Import from our new app structure
from app.config import Config
from app.queue_manager import SimpleQueue, ClientManager, ExperimentTracker, DailyPostTracker
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
        result = pipeline.run_all()
        q.mark_done(url)
        
        # Log experiment
        tracker = ExperimentTracker(cfg)
        tracker.log_experiment(
            post_id=result.get("post_id", ""),
            variation=result.get("variation", "unknown"),
            url=url,
            post_text=result.get("post_text", "")
        )
        
        return jsonify({"status": "posted", "url": url, "post_id": result.get("post_id")})
    except Exception as e:
        logger.error(f"Auto process failed: {e}")
        return jsonify({"status": "failed", "error": str(e)}), 500

# ============== EXPERIMENT TRACKING ==============

@app.route('/api/experiments', methods=['GET'])
def get_experiments():
    """Get experiment statistics and winners."""
    cfg = Config()
    tracker = ExperimentTracker(cfg)
    return jsonify({
        "stats": tracker.get_stats(),
        "winners": tracker.get_winners()
    })

@app.route('/api/experiments/winner', methods=['POST'])
def mark_experiment_winner():
    """Mark a post as a winner (performed well)."""
    post_id = request.json.get('post_id')
    if not post_id:
        return jsonify({"error": "post_id required"}), 400
    
    cfg = Config()
    tracker = ExperimentTracker(cfg)
    success = tracker.mark_winner(post_id)
    
    if success:
        return jsonify({"status": "marked_winner", "post_id": post_id})
    else:
        return jsonify({"error": "post not found"}), 404

# ============== TELEGRAM BOT ==============

def send_telegram(chat_id: str, text: str, cfg: Config, reply_markup: dict = None, photo_url: str = None):
    """Send a message (optionally with photo and keyboard) via Telegram bot."""
    # If photo_url is provided and valid, try sending with photo
    if photo_url and photo_url.strip() and photo_url.startswith('http'):
        url = f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendPhoto"
        payload = {
            "chat_id": chat_id,
            "photo": photo_url,
            "caption": text[:1024],  # Telegram caption limit
            "parse_mode": "HTML"
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
            
        try:
            r = requests.post(url, json=payload, timeout=10)
            r.raise_for_status()
            return  # Success
        except Exception as e:
            logger.warning(f"Telegram sendPhoto failed, falling back to text: {e}")
            # Fall through to send as text message
    
    # Send as text message (fallback or no photo)
    url = f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id, 
        "text": text, 
        "parse_mode": "HTML"
    }
    
    if reply_markup:
        payload["reply_markup"] = reply_markup
        
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")

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

def handle_callback_query(callback, cfg: Config):
    """Handle approval/cancel, style buttons, and winner marking."""
    chat_id = str(callback.get('message', {}).get('chat', {}).get('id', ''))
    data = callback.get('data', '')
    query_id = callback.get('id')
    clients_mgr = ClientManager(cfg)
    
    # Answer callback to remove loading state in Telegram
    requests.post(f"https://api.telegram.org/bot{cfg.telegram_bot_token}/answerCallbackQuery", 
                  json={"callback_query_id": query_id})
    
    # Handle winner marking (format: "winner:post_id")
    if data.startswith("winner:"):
        post_id = data.split(":", 1)[1]
        tracker = ExperimentTracker(cfg)
        success = tracker.mark_winner(post_id)
        if success:
            stats = tracker.get_stats()
            send_telegram(chat_id, f"🏆 <b>Marked as winner!</b>\n\nPost ID: <code>{post_id}</code>\n\n📊 Stats:\n• Total experiments: {stats.get('total_experiments', 0)}\n• Total winners: {stats.get('total_winners', 0)}\n• Weights: {json.dumps(stats.get('weights', {}), indent=2)}", cfg)
        else:
            send_telegram(chat_id, f"❌ Could not find post {post_id}", cfg)
        return jsonify({"ok": True})
    
    # Callback format: "action:client_name:payload"
    parts = data.split(':')
    if len(parts) < 3:
        return jsonify({"ok": True})
    
    action, client_name, payload = parts[0], parts[1], parts[2]
    q = SimpleQueue(cfg)

    if action == "setstyle":
        clients_mgr.update_settings(client_name, {"style": payload})
        send_telegram(chat_id, f"✅ Style for <b>{client_name}</b> set to: <b>{payload}</b>", cfg)
        return jsonify({"ok": True})

    preview_key = f"preview:{client_name}:{payload}"
    if action == "cancel":
        if q.redis: q.redis.delete(preview_key)
        send_telegram(chat_id, f"❌ Post cancelled for <b>{client_name}</b>.", cfg)
        return jsonify({"ok": True})

    if action == "post":
        if not q.redis:
            send_telegram(chat_id, "❌ Error: Redis not available for preview.", cfg)
            return jsonify({"ok": True})
            
        preview_data_raw = q.redis.get(preview_key)
        if not preview_data_raw:
            send_telegram(chat_id, "❌ Error: Preview data expired or not found.", cfg)
            return jsonify({"ok": True})
        
        preview_data = json.loads(preview_data_raw)
        url = preview_data.get('url')
        post_text = preview_data.get('post_text')
        image_url = preview_data.get('image_url')
        blotato_id = preview_data.get('blotato_account_id')
        
        send_telegram(chat_id, f"🚀 Posting to LinkedIn for <b>{client_name}</b>...", cfg)
        
        try:
            pipeline = ContentPipeline(cfg, url, blotato_account_id=blotato_id)
            pipeline.post_blotato(post_text, image_url)
            q.mark_done(url, client_name)
            q.redis.delete(preview_key)
            send_telegram(chat_id, f"✅ <b>Successfully posted to LinkedIn!</b>\nClient: {client_name}", cfg)
        except Exception as e:
            send_telegram(chat_id, f"❌ Failed to post: {str(e)[:200]}", cfg)
            
    return jsonify({"ok": True})

@app.route('/api/clear_locks', methods=['POST', 'GET'])
def clear_locks():
    """Emergency endpoint to clear all stuck locks."""
    cfg = Config()
    q = SimpleQueue(cfg)
    clients = ClientManager(cfg)
    
    if not q.redis:
        return jsonify({"error": "no redis"}), 500
    
    cleared = 0
    all_clients = ['drew'] + list(clients.get_all().keys())
    for name in all_clients:
        if q.redis.delete(f"processing_lock:{name}"):
            cleared += 1
        if q.redis.delete(f"test_lock:{name}"):
            cleared += 1
    
    # Clear previews too
    try:
        cursor = 0
        while True:
            cursor, keys = q.redis.scan(cursor, match="preview:*", count=100)
            for key in keys:
                q.redis.delete(key)
                cleared += 1
            if cursor == 0:
                break
    except: pass
    
    # Clear message dedup keys
    try:
        cursor = 0
        while True:
            cursor, keys = q.redis.scan(cursor, match="msg:*", count=100)
            for key in keys:
                q.redis.delete(key)
                cleared += 1
            if cursor == 0:
                break
    except: pass
    
    return jsonify({"status": "cleared", "count": cleared})

@app.route('/telegram-webhook', methods=['POST'])
def telegram_webhook():
    cfg = Config()
    data = request.json
    
    if not data:
        return jsonify({"ok": True})

    # Handle Callback Queries (Buttons)
    if 'callback_query' in data:
        return handle_callback_query(data['callback_query'], cfg)
    
    if 'message' not in data:
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
    tracker = ExperimentTracker(cfg)
    
    # Command: /start
    if text == '/start':
        send_telegram(chat_id, 
            "🚀 <b>LinkedIn Poster Bot Pro</b>\n\n"
            "Send me a YouTube or Twitter/X link and I'll queue it for LinkedIn.\n\n"
            "<b>Commands:</b>\n"
            "/dashboard - Full overview of all queues\n"
            "/queue - Show current client's queue\n"
            "/style - Choose post style (Story, Curiosity, etc.)\n"
            "/clients - List all clients\n"
            "/add &lt;name&gt; &lt;blotato_id&gt; - Add new client\n"
            "/client &lt;name&gt; - Switch active client\n"
            "/delete_client &lt;name&gt; - Remove a client\n"
            "/go - Process next URL now\n"
            "/stop - Force quit all processing\n"
            "/history - Recent posts\n"
            "/stats - View experiment statistics\n"
            "/remove &lt;number&gt; - Remove URL from queue\n"
            "/clear - Clear current queue", cfg)
        return jsonify({"ok": True})
    
    # Command: /stats - Show experiment statistics
    if text == '/stats':
        stats = tracker.get_stats()
        weights = stats.get('weights', {})
        variation_counts = stats.get('variation_counts', {})
        winner_counts = stats.get('winner_counts', {})
        
        weight_lines = []
        for var, w in sorted(weights.items(), key=lambda x: -x[1]):
            wins = winner_counts.get(var.split(':')[-1] if ':' in var else var, 0)
            total = variation_counts.get(var.split(':')[-1] if ':' in var else var, 0)
            weight_lines.append(f"• {var}: {w:.1f}x ({wins}/{total})")
        
        msg = f"📊 <b>Experiment Statistics</b>\n\n"
        msg += f"🧪 Total experiments: {stats.get('total_experiments', 0)}\n"
        msg += f"🏆 Total winners: {stats.get('total_winners', 0)}\n\n"
        
        if weight_lines:
            msg += "<b>Variation Weights:</b>\n"
            msg += "\n".join(weight_lines[:15])  # Top 15
        else:
            msg += "<i>No experiments yet. Weights will appear after you mark winners!</i>"
        
        send_telegram(chat_id, msg, cfg)
        return jsonify({"ok": True})
    
    # Command: /stop - Force quit all processing
    if text == '/stop':
        if q.redis:
            all_clients = ['drew'] + list(clients.get_all().keys())
            cleared_locks = 0
            cleared_previews = 0
            
            for client_name in all_clients:
                # Clear processing locks
                if q.redis.delete(f"processing_lock:{client_name}"):
                    cleared_locks += 1
                # Clear test locks
                if q.redis.delete(f"test_lock:{client_name}"):
                    cleared_locks += 1
            
            # Clear ALL preview keys using scan
            try:
                cursor = 0
                while True:
                    cursor, keys = q.redis.scan(cursor, match="preview:*", count=100)
                    for key in keys:
                        q.redis.delete(key)
                        cleared_previews += 1
                    if cursor == 0:
                        break
            except:
                pass
            
            send_telegram(chat_id, 
                f"🛑 <b>FULL STOP!</b>\n\n"
                f"✅ Cleared {cleared_locks} lock(s)\n"
                f"✅ Cleared {cleared_previews} preview(s)\n\n"
                f"Ready for new commands.", cfg)
        else:
            send_telegram(chat_id, "❌ Redis not available.", cfg)
        return jsonify({"ok": True})
    
    # Command: /style - Toggle style
    if text.startswith('/style'):
        current = active_client.get(chat_id, 'drew')
        parts = text.split()
        styles = ["default", "soulprint", "thought_leader", "how_to", "curiosity", "story"]
        
        if len(parts) < 2:
            kb = {
                "inline_keyboard": [[
                    {"text": "🔮 SoulPrint", "callback_data": f"setstyle:{current}:soulprint"},
                    {"text": "Default", "callback_data": f"setstyle:{current}:default"}
                ], [
                    {"text": "Thought Leader", "callback_data": f"setstyle:{current}:thought_leader"},
                    {"text": "How-To", "callback_data": f"setstyle:{current}:how_to"}
                ], [
                    {"text": "Curiosity", "callback_data": f"setstyle:{current}:curiosity"},
                    {"text": "Story", "callback_data": f"setstyle:{current}:story"}
                ]]
            }
            send_telegram(chat_id, f"🎨 <b>Choose post style for {current}:</b>", cfg, reply_markup=kb)
        else:
            style = parts[1].lower()
            if style in styles:
                clients.update_settings(current, {"style": style})
                send_telegram(chat_id, f"✅ Style for <b>{current}</b> set to: <b>{style}</b>", cfg)
            else:
                send_telegram(chat_id, f"❌ Invalid style. Choose from: {', '.join(styles)}", cfg)
        return jsonify({"ok": True})
    
    # Command: /dashboard - Full overview
    if text == '/dashboard':
        all_clients = clients.get_all()
        client_names = ['drew'] + list(all_clients.keys())
        current = active_client.get(chat_id, 'drew')
        
        msg = "📊 <b>DASHBOARD</b>\n"
        msg += f"━━━━━━━━━━━━━━━\n"
        msg += f"Active: <b>{current}</b>\n\n"
        
        total_queued = 0
        for name in client_names:
            urls = q.get_urls(name)
            history = q.get_history(name)
            
            # Get settings
            info = clients.get_client(name) or {}
            preview = "👁" if info.get('preview_mode') else "🚀"
            style = info.get('style', 'soulprint')
            
            marker = " 👈" if name == current else ""
            msg += f"<b>{name}</b> {preview} {marker}\n"
            msg += f"  📝 Queue: {len(urls)}\n"
            msg += f"  🎨 Style: {style}\n"
            msg += f"  ✅ Total: {len(history)}\n\n"
            total_queued += len(urls)
        
        msg += f"━━━━━━━━━━━━━━━\n"
        msg += f"Total pending: {total_queued}\n"
        msg += f"🚀 = Auto-Post | 👁 = Preview"
        send_telegram(chat_id, msg, cfg)
        return jsonify({"ok": True})
    
    # Command: /queue - Show current queue with numbers and estimated times
    if text == '/queue':
        current = active_client.get(chat_id, 'drew')
        urls = q.get_urls(current)
        daily_tracker = DailyPostTracker(cfg)
        posts_today = daily_tracker.get_daily_count()
        remaining_today = daily_tracker.get_remaining_today()
        is_weekday = daily_tracker.is_weekday()
        
        if not urls:
            status_msg = f"📊 Today: {posts_today}/5 posts" if is_weekday else "📊 Weekend - no posting"
            send_telegram(chat_id, f"📭 Queue for <b>{current}</b> is empty!\n\n{status_msg}", cfg)
        else:
            # Post times in Chicago: 1am, 5:45am, 10:30am, 3:15pm, 10pm
            # Using zoneinfo for accurate Chicago time
            try:
                from zoneinfo import ZoneInfo
            except ImportError:
                from backports.zoneinfo import ZoneInfo
            
            chicago_tz = ZoneInfo("America/Chicago")
            post_hours = [(1, 0), (5, 45), (10, 30), (15, 15), (22, 0)]  # (hour, minute)
            now_chicago = datetime.now(chicago_tz)
            
            def get_next_post_times(count):
                """Get next post times, skipping weekends."""
                times = []
                check_date = now_chicago.date()
                slots_used_today = posts_today  # Already posted today
                
                while len(times) < count:
                    # Check if this day is a weekday (0=Mon, 4=Fri)
                    if check_date.weekday() < 5:  # Weekday
                        for hour, minute in post_hours:
                            if len(times) >= count:
                                break
                            candidate = datetime(check_date.year, check_date.month, check_date.day, 
                                                hour, minute, tzinfo=chicago_tz)
                            # If today, skip already-used slots and past times
                            if check_date == now_chicago.date():
                                if candidate <= now_chicago:
                                    continue
                                # Account for posts already made today
                                slot_index = post_hours.index((hour, minute))
                                if slot_index < slots_used_today:
                                    continue
                            times.append(candidate)
                    check_date += timedelta(days=1)
                return times
            
            next_times = get_next_post_times(len(urls))
            
            status_msg = f"📊 Today: {posts_today}/5 posts ({remaining_today} left)" if is_weekday else "📊 Weekend - resumes Monday"
            msg = f"📝 <b>Queue for {current}:</b>\n{status_msg}\n\n"
            for i, url in enumerate(urls):
                short_url = url[:40] + "..." if len(url) > 40 else url
                if i < len(next_times):
                    est_time = next_times[i].strftime("%b %d, %I:%M%p CT")
                else:
                    est_time = "TBD"
                msg += f"{i+1}. {short_url}\n   🕐 {est_time}\n\n"
            msg += f"💡 /remove &lt;number&gt; to remove\n💡 /go to post now"
            send_telegram(chat_id, msg, cfg)
        return jsonify({"ok": True})
    
    # Command: /history - Recent posts
    if text == '/history':
        current = active_client.get(chat_id, 'drew')
        history = q.get_history(current)
        if not history:
            send_telegram(chat_id, f"📭 No posts yet for <b>{current}</b>", cfg)
        else:
            msg = f"✅ <b>Recent posts for {current}:</b>\n\n"
            for item in history[:10]:
                url = item.get('url', 'Unknown')
                done_at = item.get('done_at', '')[:10]  # Just date
                short_url = url[:40] + "..." if len(url) > 40 else url
                msg += f"• {short_url}\n  {done_at}\n"
            send_telegram(chat_id, msg, cfg)
        return jsonify({"ok": True})
    
    # Command: /remove <number>
    if text.startswith('/remove '):
        try:
            num = int(text[8:].strip())
            current = active_client.get(chat_id, 'drew')
            urls = q.get_urls(current)
            if num < 1 or num > len(urls):
                send_telegram(chat_id, f"❌ Invalid number. Queue has {len(urls)} items.", cfg)
            else:
                removed = urls.pop(num - 1)
                q.set_urls(urls, current)
                send_telegram(chat_id, f"🗑 Removed from queue:\n{removed}", cfg)
        except ValueError:
            send_telegram(chat_id, "Usage: /remove <number>", cfg)
        return jsonify({"ok": True})
    
    # Command: /clear - Clear queue
    if text == '/clear':
        current = active_client.get(chat_id, 'drew')
        q.set_urls([], current)
        send_telegram(chat_id, f"🗑 Cleared queue for <b>{current}</b>", cfg)
        return jsonify({"ok": True})
    
    # Command: /test - DISABLED for now
    if text == '/test':
        send_telegram(chat_id, "🚫 /test is disabled. Use /go instead.", cfg)
        return jsonify({"ok": True})
    
    # OLD TEST CODE - DISABLED
    if False and text == '/test_disabled':
        current = active_client.get(chat_id, 'drew')
        
        # Prevent duplicate test runs with a lock
        lock_key = f"test_lock:{current}"
        try:
            if q.redis:
                if q.redis.get(lock_key):
                    send_telegram(chat_id, f"⏳ Test already running for <b>{current}</b>. Wait or /stop", cfg)
                    return jsonify({"ok": True})
                q.redis.setex(lock_key, 300, "1")  # 5 min lock
        except: pass
        
        urls = q.get_urls(current)
        if not urls:
            try:
                if q.redis: q.redis.delete(lock_key)
            except: pass
            send_telegram(chat_id, f"📭 Queue for <b>{current}</b> is empty!", cfg)
            return jsonify({"ok": True})
        
        url = urls[0]  # Peek, don't pop
        client_info = clients.get_client(current) or {}
        blotato_account_id = client_info.get('blotato_account_id', cfg.blotato_account_id)
        style = client_info.get('style', 'soulprint')
        
        send_telegram(chat_id, f"🧪 <b>TEST MODE</b> for {current}...\n\n🔗 {url}\n\n⏳ Processing... (60-120s)", cfg)
        
        try:
            pipeline = ContentPipeline(cfg, url, blotato_account_id=blotato_account_id, style=style)
            result = pipeline.run_all(skip_post=True)
            
            image_url = result.get('image_url', '')
            post_text = result.get('post_text', '')
            
            # Send image first (if available)
            if image_url:
                send_telegram(chat_id, f"🖼 <b>IMAGE PREVIEW</b>", cfg, photo_url=image_url)
            else:
                send_telegram(chat_id, "⚠️ No image generated", cfg)
            
            # Send caption/post text as separate message
            caption_preview = f"📝 <b>CAPTION PREVIEW for {current}</b>\n<b>Style:</b> {style}\n\n{post_text[:3500]}"
            send_telegram(chat_id, caption_preview, cfg)
            
            send_telegram(chat_id, f"✅ Test complete. URL still in queue.\n\nUse /go to generate for real.", cfg)
        except Exception as e:
            send_telegram(chat_id, f"❌ Test failed: {str(e)[:400]}", cfg)
        finally:
            try:
                if q.redis: q.redis.delete(lock_key)
            except: pass
        return jsonify({"ok": True})
    
    # Command: /process or /go - Process next URL (always preview first)
    if text == '/process' or text == '/go':
        current = active_client.get(chat_id, 'drew')
        
        # Prevent duplicate processing with a lock
        lock_key = f"processing_lock:{current}"
        try:
            if q.redis:
                existing_lock = q.redis.get(lock_key)
                if existing_lock:
                    send_telegram(chat_id, f"⏳ Already processing for <b>{current}</b>. Please wait or /stop", cfg)
                    return jsonify({"ok": True})
                q.redis.setex(lock_key, 300, "1")  # 5 min lock
        except: pass
        
        url = q.pop_next(current)
        if not url:
            try:
                if q.redis: q.redis.delete(lock_key)
            except: pass
            send_telegram(chat_id, f"📭 Queue for <b>{current}</b> is empty!", cfg)
            return jsonify({"ok": True})
        
        client_info = clients.get_client(current) or {}
        blotato_account_id = client_info.get('blotato_account_id', cfg.blotato_account_id)
        style = client_info.get('style', 'soulprint')
        
        remaining = len(q.get_urls(current))
        send_telegram(chat_id, f"👀 Generating for <b>{current}</b>...\n\n🔗 {url}\n📝 {remaining} left\n\n⏳ Processing... (30-60s)", cfg)
        
        try:
            # Get experiment weights to influence variation selection
            tracker = ExperimentTracker(cfg)
            weights = tracker.get_weights()
            
            pipeline = ContentPipeline(cfg, url, blotato_account_id=blotato_account_id, style=style)
            result = pipeline.run_all(skip_post=True)
            
            # Log experiment
            post_id = result.get("post_id", "")
            tracker.log_experiment(
                post_id=post_id,
                variation=result.get("variation", "unknown"),
                url=url,
                post_text=result.get("post_text", "")
            )
            
            url_hash = hashlib.md5(url.encode()).hexdigest()[:10]
            preview_key = f"preview:{current}:{url_hash}"
            if q.redis:
                q.redis.setex(preview_key, 3600 * 24, json.dumps(result))
            
                # Add winner button with post_id
                kb = {
                    "inline_keyboard": [
                        [
                            {"text": "✅ Approve & Post", "callback_data": f"post:{current}:{url_hash}"},
                            {"text": "❌ Cancel", "callback_data": f"cancel:{current}:{url_hash}"}
                        ],
                        [
                            {"text": "🏆 Mark as Winner", "callback_data": f"winner:{post_id}"}
                        ]
                    ]
                }
                variation_info = result.get('variation', 'unknown')
                preview_text = f"📝 <b>PREVIEW for {current}</b>\n\n🧪 <i>Variation: {variation_info}</i>\n\n{result['post_text'][:3400]}"
                send_telegram(chat_id, preview_text, cfg, reply_markup=kb, photo_url=result.get('image_url'))
            else:
                send_telegram(chat_id, "❌ Redis unavailable, could not store preview.", cfg)
        except Exception as e:
            send_telegram(chat_id, f"❌ Failed: {str(e)[:400]}", cfg)
        finally:
            try:
                if q.redis: q.redis.delete(lock_key)
            except: pass
        return jsonify({"ok": True})
    
    # Command: /clients
    if text == '/clients':
        all_clients = clients.get_all()
        if not all_clients:
            send_telegram(chat_id, "No clients yet. Add one with:\n/add <name> <blotato_account_id>", cfg)
        else:
            current = active_client.get(chat_id, 'drew')
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
        if name not in all_clients and name != 'drew':
            send_telegram(chat_id, f"❌ Client '{name}' not found. Use /clients to see list.", cfg)
        else:
            active_client[chat_id] = name
            send_telegram(chat_id, f"✅ Switched to client: <b>{name}</b>", cfg)
        return jsonify({"ok": True})
    
    # Command: /status
    if text == '/status':
        current = active_client.get(chat_id, 'drew')
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
    
    # Command: /delete_client <name>
    if text.startswith('/delete_client '):
        name = text[15:].strip().lower()
        if name == 'drew':
            send_telegram(chat_id, "❌ Cannot delete drew client.", cfg)
        else:
            clients.remove_client(name)
            if active_client.get(chat_id) == name:
                active_client[chat_id] = 'drew'
            send_telegram(chat_id, f"🗑 Deleted client <b>{name}</b>. Switched to 'drew'.", cfg)
        return jsonify({"ok": True})
    
    # Handle URL - add to queue
    url = extract_url(text)
    if url:
        current = active_client.get(chat_id, 'drew')
        q.add_url(url, current)
        queue_size = len(q.get_urls(current))
        send_telegram(chat_id, f"✅ Added to <b>{current}</b> queue!\n\n📝 Queue size: {queue_size}", cfg)
        return jsonify({"ok": True})
    
    # Unknown command
    send_telegram(chat_id, "🤔 Send me a YouTube or Twitter/X link, or use /start for help.", cfg)
    return jsonify({"ok": True})

@app.route('/api/auto_process_all', methods=['POST', 'GET'])
def auto_process_all():
    """Process ONE URL from queue. Enforces 5 posts/day limit on weekdays only (Chicago time)."""
    cfg = Config()
    
    # Check daily limit and weekday
    tracker = DailyPostTracker(cfg)
    
    if not tracker.is_weekday():
        return jsonify({"status": "skipped", "reason": "weekend", "message": "No posting on weekends"})
    
    if not tracker.can_post_today():
        return jsonify({
            "status": "skipped", 
            "reason": "daily_limit", 
            "message": f"Daily limit reached ({tracker.MAX_POSTS_PER_DAY} posts)",
            "count_today": tracker.get_daily_count()
        })
    
    clients = ClientManager(cfg)
    q = SimpleQueue(cfg)
    
    # Get all client names and find FIRST queue with items (round-robin approach)
    all_client_names = ['drew'] + list(clients.get_all().keys())
    
    # Find first client with a URL in queue
    selected_client = None
    url = None
    for client_name in all_client_names:
        urls = q.get_urls(client_name)
        if urls:
            url = q.pop_next(client_name)
            selected_client = client_name
            break
    
    if not url:
        return jsonify({"status": "idle", "message": "No URLs in any queue"})
    
    try:
        # Get settings for this client
        client_info = clients.get_client(selected_client) or {}
        preview_enabled = client_info.get('preview_mode', False)
        blotato_account_id = client_info.get('blotato_account_id', cfg.blotato_account_id)
        style = client_info.get('style', 'soulprint')
        
        pipeline = ContentPipeline(cfg, url, blotato_account_id=blotato_account_id, style=style)
        result = pipeline.run_all(skip_post=preview_enabled)
        
        if preview_enabled:
            # Store preview and notify admin
            url_hash = hashlib.md5(url.encode()).hexdigest()[:10]
            preview_key = f"preview:{selected_client}:{url_hash}"
            if q.redis:
                q.redis.setex(preview_key, 3600 * 24, json.dumps(result))
                
                if cfg.telegram_bot_token and cfg.telegram_admin_chat_id:
                    kb = {
                        "inline_keyboard": [[
                            {"text": "✅ Post Now", "callback_data": f"post:{selected_client}:{url_hash}"},
                            {"text": "❌ Cancel", "callback_data": f"cancel:{selected_client}:{url_hash}"}
                        ]]
                    }
                    preview_text = f"📝 <b>AUTO-PREVIEW for {selected_client}</b>\n\n{result.get('post_text', '')[:3500]}"
                    send_telegram(cfg.telegram_admin_chat_id, preview_text, cfg, reply_markup=kb, photo_url=result.get('image_url'))
            
            # Increment daily count even for previews
            tracker.increment_daily_count()
            return jsonify({"client": selected_client, "url": url, "status": "previewed", "posts_today": tracker.get_daily_count()})
        else:
            q.mark_done(url, selected_client)
            tracker.increment_daily_count()
            
            # Send Telegram notification
            if cfg.telegram_bot_token and cfg.telegram_admin_chat_id:
                remaining = len(q.get_urls(selected_client))
                posts_today = tracker.get_daily_count()
                remaining_today = tracker.get_remaining_today()
                send_telegram(cfg.telegram_admin_chat_id, 
                    f"🚀 <b>Auto-posted to LinkedIn!</b>\n\n"
                    f"Client: {selected_client}\n"
                    f"🔗 {url[:50]}...\n"
                    f"📝 {remaining} left in queue\n"
                    f"📊 {posts_today}/5 posts today ({remaining_today} remaining)", cfg)
            
            return jsonify({"client": selected_client, "url": url, "status": "posted", "posts_today": tracker.get_daily_count()})
    except Exception as e:
        logger.error(f"Auto process failed for {selected_client}: {e}")
        
        # Notify on failure
        if cfg.telegram_bot_token and cfg.telegram_admin_chat_id:
            send_telegram(cfg.telegram_admin_chat_id,
                f"❌ <b>Auto-post failed</b>\n\n"
                f"Client: {selected_client}\n"
                f"🔗 {url[:50]}...\n"
                f"Error: {str(e)[:100]}", cfg)
        
        return jsonify({"client": selected_client, "url": url, "status": "failed", "error": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, port=4000)
