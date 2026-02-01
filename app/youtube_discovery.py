"""YouTube Discovery Module - Auto-discover videos from channels and playlists."""

import os
import logging
import requests
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)


def get_youtube_api_key() -> str:
    """Get YouTube Data API v3 key from environment."""
    return os.getenv("YOUTUBE_API_KEY", "").strip()


def discover_channel_videos(channel_id: str, max_results: int = 5) -> List[Dict]:
    """Fetch latest videos from a YouTube channel.
    
    Args:
        channel_id: YouTube channel ID (starts with UC...)
        max_results: Maximum number of videos to return
        
    Returns:
        List of dicts with video_id, title, published_at, url
    """
    api_key = get_youtube_api_key()
    if not api_key:
        logger.warning("YOUTUBE_API_KEY not set")
        return []
    
    try:
        # Get channel's uploads playlist
        channel_url = f"https://www.googleapis.com/youtube/v3/channels?part=contentDetails&id={channel_id}&key={api_key}"
        resp = requests.get(channel_url, timeout=10)
        
        if not resp.ok:
            logger.error(f"Channel API failed: {resp.status_code}")
            return []
        
        channel_data = resp.json()
        if not channel_data.get("items"):
            logger.error(f"Channel {channel_id} not found")
            return []
        
        uploads_playlist_id = channel_data["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
        
        # Get videos from uploads playlist
        playlist_url = f"https://www.googleapis.com/youtube/v3/playlistItems?part=snippet&playlistId={uploads_playlist_id}&maxResults={max_results}&key={api_key}"
        resp = requests.get(playlist_url, timeout=10)
        
        if not resp.ok:
            logger.error(f"Playlist API failed: {resp.status_code}")
            return []
        
        playlist_data = resp.json()
        videos = []
        
        for item in playlist_data.get("items", []):
            snippet = item.get("snippet", {})
            video_id = snippet.get("resourceId", {}).get("videoId")
            
            if video_id:
                videos.append({
                    "video_id": video_id,
                    "title": snippet.get("title", ""),
                    "published_at": snippet.get("publishedAt", ""),
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "thumbnail": snippet.get("thumbnails", {}).get("medium", {}).get("url", "")
                })
        
        return videos
        
    except Exception as e:
        logger.error(f"Error discovering channel videos: {e}")
        return []


def discover_playlist_videos(playlist_id: str, max_results: int = 10) -> List[Dict]:
    """Fetch videos from a YouTube playlist.
    
    Args:
        playlist_id: YouTube playlist ID
        max_results: Maximum number of videos to return
        
    Returns:
        List of dicts with video_id, title, url
    """
    api_key = get_youtube_api_key()
    if not api_key:
        logger.warning("YOUTUBE_API_KEY not set")
        return []
    
    try:
        playlist_url = f"https://www.googleapis.com/youtube/v3/playlistItems?part=snippet&playlistId={playlist_id}&maxResults={max_results}&key={api_key}"
        resp = requests.get(playlist_url, timeout=10)
        
        if not resp.ok:
            logger.error(f"Playlist API failed: {resp.status_code}")
            return []
        
        playlist_data = resp.json()
        videos = []
        
        for item in playlist_data.get("items", []):
            snippet = item.get("snippet", {})
            video_id = snippet.get("resourceId", {}).get("videoId")
            
            if video_id:
                videos.append({
                    "video_id": video_id,
                    "title": snippet.get("title", ""),
                    "url": f"https://www.youtube.com/watch?v={video_id}"
                })
        
        return videos
        
    except Exception as e:
        logger.error(f"Error fetching playlist: {e}")
        return []


def search_videos(query: str, max_results: int = 5) -> List[Dict]:
    """Search YouTube for videos matching a query.
    
    Args:
        query: Search query
        max_results: Maximum results to return
        
    Returns:
        List of video dicts
    """
    api_key = get_youtube_api_key()
    if not api_key:
        logger.warning("YOUTUBE_API_KEY not set")
        return []
    
    try:
        search_url = f"https://www.googleapis.com/youtube/v3/search?part=snippet&type=video&q={query}&maxResults={max_results}&key={api_key}"
        resp = requests.get(search_url, timeout=10)
        
        if not resp.ok:
            logger.error(f"Search API failed: {resp.status_code}")
            return []
        
        search_data = resp.json()
        videos = []
        
        for item in search_data.get("items", []):
            video_id = item.get("id", {}).get("videoId")
            snippet = item.get("snippet", {})
            
            if video_id:
                videos.append({
                    "video_id": video_id,
                    "title": snippet.get("title", ""),
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "channel_title": snippet.get("channelTitle", "")
                })
        
        return videos
        
    except Exception as e:
        logger.error(f"Error searching videos: {e}")
        return []


def get_watched_channels() -> List[str]:
    """Get list of channel IDs to watch from environment.
    
    Environment variable WATCHED_CHANNELS should be comma-separated channel IDs.
    """
    channels = os.getenv("WATCHED_CHANNELS", "").strip()
    if not channels:
        return []
    return [c.strip() for c in channels.split(",") if c.strip()]


def get_watched_playlists() -> List[str]:
    """Get list of playlist IDs to watch from environment.
    
    Environment variable WATCHED_PLAYLISTS should be comma-separated playlist IDs.
    """
    playlists = os.getenv("WATCHED_PLAYLISTS", "").strip()
    if not playlists:
        return []
    return [p.strip() for p in playlists.split(",") if p.strip()]
