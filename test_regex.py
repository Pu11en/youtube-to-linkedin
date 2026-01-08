import re

def extract_id(url: str) -> str:
    # 1. Check if it's just an ID (11 chars)
    if len(url) == 11 and re.match(r'^[a-zA-Z0-9_-]{11}$', url):
        return url
        
    # 2. Regex for standard URLs
    # Priority: standard v= param, then short urls, then others
    regex_patterns = [
        r"(?:v=|\/)([0-9A-Za-z_-]{11}).*",
        r"(?:youtu\.be\/)([0-9A-Za-z_-]{11})",
        r"(?:shorts\/)([0-9A-Za-z_-]{11})",
    ]
    
    # Combined regex is often better
    # Supported:
    # youtube.com/watch?v=ID
    # youtube.com/embed/ID
    # youtube.com/v/ID
    # youtu.be/ID
    # youtube.com/shorts/ID
    
    match = re.search(r'(?:v=|\/|youtu\.be\/|embed\/|shorts\/)([0-9A-Za-z_-]{11})', url)
    if match:
        return match.group(1)

    return url

urls = [
    "https://www.youtube.com/watch?v=snF5eGKoiJI",
    "http://www.youtube.com/watch?v=snF5eGKoiJI",
    "www.youtube.com/watch?v=snF5eGKoiJI",
    "youtube.com/watch?v=snF5eGKoiJI",
    "https://youtu.be/snF5eGKoiJI",
    "snF5eGKoiJI",
    "https://www.youtube.com/shorts/snF5eGKoiJI",
    "youtube.com/shorts/snF5eGKoiJI",
    "https://www.youtube.com/watch?v=snF5eGKoiJI&t=123"
]

for u in urls:
    print(f"Input: {u} -> Extracted: {extract_id(u)}")
