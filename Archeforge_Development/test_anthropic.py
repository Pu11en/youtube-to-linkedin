from Youtube_to_Linkedin import load_config, claude_linkedin_post, claude_newsletter
import sys

def test():
    print("Loading config...")
    cfg = load_config()
    print("Config loaded.")
    transcript = "This is a test transcript for verifying Anthropic integration."
    
    print("Testing LinkedIn Post...")
    try:
        post = claude_linkedin_post(cfg, transcript)
        print("LinkedIn Post Success!")
        print(post[:100] + "...")
    except Exception as e:
        print(f"LinkedIn Post FAILED: {e}")
        import traceback
        traceback.print_exc()

    print("Testing Newsletter...")
    try:
        news = claude_newsletter(cfg, transcript)
        print("Newsletter Success!")
        print(news[:100] + "...")
    except Exception as e:
        print(f"Newsletter FAILED: {e}")

if __name__ == "__main__":
    test()
