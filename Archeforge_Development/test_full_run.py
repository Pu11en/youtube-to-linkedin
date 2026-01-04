from Youtube_to_Linkedin import load_config, run_pipeline


def run_fake_pipeline():
    cfg = load_config()

    # Force fake keys for external services so we don't call real APIs during testing
    cfg.gemini_api_key = "FAKE_GEMINI_KEY"
    cfg.kie_api_key = "FAKE_KIE_KEY"
    cfg.anthropic_api_key = "FAKE_ANTHROPIC_KEY"
    cfg.cloudinary_api_key = "FAKE_CLOUDINARY_KEY"
    cfg.cloudinary_api_secret = "FAKE"

    result = run_pipeline(cfg)
    import json

    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    run_fake_pipeline()
