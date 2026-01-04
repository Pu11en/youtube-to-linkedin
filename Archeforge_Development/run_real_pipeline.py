import json
import traceback
from Youtube_to_Linkedin import load_config, run_pipeline


def main():
    cfg = load_config()
    try:
        result = run_pipeline(cfg)
        print(json.dumps(result, indent=2))
    except Exception as e:
        print("PIPELINE_ERROR:")
        traceback.print_exc()


if __name__ == '__main__':
    main()
