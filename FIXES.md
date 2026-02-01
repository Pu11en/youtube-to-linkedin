# FIXES.md - YouTube to LinkedIn Bot Updates

**Date:** February 1, 2025
**Updated by:** Defy (Clawd AI Assistant)

---

## Summary

Added automatic YouTube video discovery capabilities to the existing YouTube-to-LinkedIn content automation system. The system can now automatically watch YouTube channels and add new videos to the queue for processing.

---

## Existing System (Already Working)

The system already had these features before this update:
- ✅ Multi-client support via Telegram bot
- ✅ Upstash Redis queue management
- ✅ Experiment/A/B testing with variation tracking
- ✅ Daily batch processing (5 posts/day at 1am, 6am, 11am, 4pm, 10pm CT)
- ✅ Preview/approval workflow via Telegram
- ✅ Blotato integration for LinkedIn posting
- ✅ YouTube transcript extraction with Invidious fallback
- ✅ Claude + Gemini content generation
- ✅ Kie.ai infographic generation
- ✅ Cloudinary image hosting

---

## New Features Added

### 1. YouTube Discovery Module
New file: `app/youtube_discovery.py`

Functions:
- `discover_channel_videos(channel_id, max_results)` - Get latest videos from a channel
- `discover_playlist_videos(playlist_id, max_results)` - Get videos from a playlist
- `search_videos(query, max_results)` - Search YouTube
- `get_watched_channels()` - Get configured channel IDs from environment
- `get_watched_playlists()` - Get configured playlist IDs from environment

### 2. New API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/discover/channel` | POST | Discover videos from a YouTube channel |
| `/api/discover/playlist` | POST | Discover videos from a playlist |
| `/api/discover/search` | POST | Search YouTube for videos |
| `/api/auto_discover` | POST/GET | Auto-discover from watched channels and add to queue |
| `/api/health` | GET | Health check endpoint |

### 3. Cron Jobs Updated

| Path | Schedule | Description |
|------|----------|-------------|
| `/api/auto_discover` | 0 6 * * 1-5 | Discover new videos at 6 AM UTC (midnight CT) weekdays |
| `/api/auto_process_all` | 0 7 * * 1-5 | Process and schedule posts at 7 AM UTC (1 AM CT) weekdays |

---

## New Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `YOUTUBE_API_KEY` | YouTube Data API v3 key | For auto-discovery |
| `WATCHED_CHANNELS` | Comma-separated channel IDs to monitor | For auto-discovery |
| `WATCHED_PLAYLISTS` | Comma-separated playlist IDs to monitor | Optional |

---

## How Auto-Discovery Works

1. **Cron runs at 6 AM UTC (midnight CT)** - `/api/auto_discover` is triggered
2. **Checks watched channels** - Fetches latest 3 videos from each channel in `WATCHED_CHANNELS`
3. **Filters duplicates** - Compares against existing queue URLs and history
4. **Adds to queue** - New videos are added to the 'drew' client queue
5. **Sends notification** - Telegram message if any videos were added
6. **Processing happens at 7 AM UTC** - `/api/auto_process_all` picks up the queue

---

## API Usage Examples

### Discover Channel Videos
```bash
curl -X POST https://your-app.vercel.app/api/discover/channel \
  -H "Content-Type: application/json" \
  -d '{"channel_id": "UCxxxxxx", "max_results": 5}'
```

### Search YouTube
```bash
curl -X POST https://your-app.vercel.app/api/discover/search \
  -H "Content-Type: application/json" \
  -d '{"query": "AI automation tutorials", "max_results": 5}'
```

### Manually Trigger Auto-Discovery
```bash
curl -X POST https://your-app.vercel.app/api/auto_discover \
  -H "Authorization: Bearer YOUR_CRON_SECRET"
```

---

## Getting a YouTube API Key

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or select existing)
3. Enable "YouTube Data API v3"
4. Create credentials → API Key
5. Restrict the key to YouTube Data API v3 only
6. Add to Vercel: `vercel env add YOUTUBE_API_KEY`

---

## Finding Channel IDs

YouTube channel IDs start with "UC" and are 24 characters long.

**Method 1: From channel URL**
- Go to channel → View Page Source → Search for "channelId"

**Method 2: Using API**
```bash
curl "https://www.googleapis.com/youtube/v3/channels?part=id&forUsername=USERNAME&key=YOUR_API_KEY"
```

**Method 3: From video**
- Open any video from the channel
- Click channel name → URL contains `/channel/UCxxxxxxxx`

---

## Example Channels to Watch

Add these to `WATCHED_CHANNELS` (comma-separated):
```
WATCHED_CHANNELS=UCxxxxxx,UCyyyyyy,UCzzzzzz
```

Some AI/Tech channels:
- Matt Wolfe (AI): UC-actual-channel-id
- Sam Altman: UC-actual-channel-id
- Fireship: UC-actual-channel-id

---

## Files Changed

- `app/youtube_discovery.py` - NEW: YouTube discovery module
- `api/index.py` - Added discovery endpoints
- `vercel.json` - Added auto_discover cron
- `.env.example` - Added YOUTUBE_API_KEY and WATCHED_CHANNELS
- `FIXES.md` - This documentation

---

## Deployment

```bash
# Add environment variables
vercel env add YOUTUBE_API_KEY
vercel env add WATCHED_CHANNELS

# Commit and push
git add .
git commit -m "feat: Add YouTube auto-discovery"
git push origin main

# Deploy (or auto-deploys on push)
vercel --prod
```

---

## Troubleshooting

### "YOUTUBE_API_KEY not set"
- Add the key to Vercel environment variables
- Redeploy after adding

### "Channel not found"
- Verify channel ID starts with "UC"
- Try using the channel's actual ID, not username

### "Quota exceeded"
- YouTube API has a daily quota of 10,000 units
- Each channel check uses ~2-3 units
- Reduce WATCHED_CHANNELS or check frequency

### "No new videos found"
- Videos may already be in queue/history
- Channel may not have posted recently
- Check logs for actual API responses
