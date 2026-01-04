from Youtube_to_Linkedin import load_config, gemini_structured_summary, gemini_infographic_brief


def run():
    cfg = load_config()

    transcript = (
        "This is a sample transcript used to test the Gemini structured summary step. "
        "It explains the steps required to convert a video transcript into an infographic brief."
    )

    summary = gemini_structured_summary(cfg, transcript)
    brief = gemini_infographic_brief(cfg, summary)

    print("=== Structured Summary ===")
    print(summary)
    print("\n=== Infographic Brief ===")
    print(brief)


if __name__ == '__main__':
    run()
