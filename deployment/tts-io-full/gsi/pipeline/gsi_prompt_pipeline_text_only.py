import os

os.environ.setdefault("TEXT_LLM_EXPECT_JSON", "0")

from gsi_prompt_pipeline import main


if __name__ == "__main__":
    main()
