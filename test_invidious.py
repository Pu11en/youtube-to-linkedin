import requests
import json

video_id = "Cb49pGTSigI"
instances = [
    "https://inv.tux.pizza",
    "https://invidious.projectsegfau.lt",
    "https://vid.puffyan.us",
    "https://invidious.fdn.fr"
]

for inst in instances:
    print(f"Trying {inst}...")
    try:
        url = f"{inst}/api/v1/captions/{video_id}"
        resp = requests.get(url, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            print(f"Success on {inst}")
            print(json.dumps(data, indent=2))
            
            # Try to get the first english track
            for track in data.get("captions", []):
                if track.get("languageCode") == "en":
                    track_url = f"{inst}{track['url']}"
                    print(f"Fetching track from: {track_url}")
                    t_resp = requests.get(track_url)
                    print(f"Track content start: {t_resp.text[:200]}")
                    break
            break
    except Exception as e:
        print(f"Failed {inst}: {e}")
