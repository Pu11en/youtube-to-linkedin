
try:
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api.proxies import GenericProxyConfig
    from youtube_transcript_api.formatters import TextFormatter
    
    print("Imports successful.")
    
    ytt = YouTubeTranscriptApi()
    print("Instance created (default config).")
    
    proxy_config = GenericProxyConfig(http_url="http://test", https_url="https://test")
    ytt_proxy = YouTubeTranscriptApi(proxy_config=proxy_config)
    print("Instance created (proxy config).")
    
    print("Symbol check OK.")
    
except ImportError as e:
    print(f"ImportError: {e}")
except Exception as e:
    print(f"Error: {e}")
