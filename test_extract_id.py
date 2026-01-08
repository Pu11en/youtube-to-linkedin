from urllib.parse import urlparse, parse_qs

def extract_id(url: str) -> str:
    parsed = urlparse(url)
    print(f"URL: {url}")
    print(f"  Netloc: {parsed.netloc}")
    print(f"  Path: {parsed.path}")
    print(f"  Query: {parsed.query}")
    
    if "youtu.be" in parsed.netloc: return parsed.path.strip("/")
    if "youtube.com" in parsed.netloc:
        if "/watch" in parsed.path: return parse_qs(parsed.query).get("v", [""])[0]
        if "/shorts/" in parsed.path: return parsed.path.split("/")[-1]
    return url

urls = [
    "https://www.youtube.com/watch?v=snF5eGKoiJI",
    "http://www.youtube.com/watch?v=snF5eGKoiJI",
    "www.youtube.com/watch?v=snF5eGKoiJI",
    "youtube.com/watch?v=snF5eGKoiJI",
    "https://youtu.be/snF5eGKoiJI",
    "snF5eGKoiJI" 
]

for u in urls:
    print(f"Result: {extract_id(u)}")
    print("-" * 20)
