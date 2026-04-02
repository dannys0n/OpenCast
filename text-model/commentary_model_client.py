#!/usr/bin/env python3

"""OpenAI-compatible Docker Model Runner client for local esports commentary."""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from opencast_bootstrap import maybe_reexec_with_repo_venv

maybe_reexec_with_repo_venv(__file__, REPO_ROOT)

import httpx

TEXT_MODEL_DIR = Path(__file__).resolve().parent
REPO_ROOT = TEXT_MODEL_DIR.parent
DEFAULT_DMR_BASE_URL = os.environ.get(
    "OPENCAST_COMMENTARY_DMR_BASE_URL",
    "http://localhost:12434/engines/v1",
)
DEFAULT_COMMENTARY_MODEL = os.environ.get(
    "OPENCAST_COMMENTARY_MODEL",
    "huggingface.co/qwen/qwen2.5-0.5b-instruct-gguf:Q4_K_M",
)
DEFAULT_PROMPT_FILE = os.environ.get(
    "OPENCAST_COMMENTARY_PROMPT_FILE",
    str(TEXT_MODEL_DIR / "prompts" / "esports_casting.md"),
)
DEFAULT_TEMPERATURE = float(os.environ.get("OPENCAST_COMMENTARY_TEMPERATURE", "0.4"))
DEFAULT_TOP_P = float(os.environ.get("OPENCAST_COMMENTARY_TOP_P", "0.9"))
DEFAULT_MAX_TOKENS = int(os.environ.get("OPENCAST_COMMENTARY_MAX_TOKENS", "160"))
DEFAULT_LINES_PER_UPDATE = int(os.environ.get("OPENCAST_COMMENTARY_LINES_PER_UPDATE", "1"))
DEFAULT_TIMEOUT = 60.0

LINE_PREFIX_RE = re.compile(r"^\s*(?:[-*]|\d+[.)])\s*")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def resolve_repo_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return REPO_ROOT / raw_path.removeprefix("./")


@dataclass(frozen=True)
class CommentaryGenerationConfig:
    base_url: str = DEFAULT_DMR_BASE_URL
    model: str = DEFAULT_COMMENTARY_MODEL
    prompt_file: str = DEFAULT_PROMPT_FILE
    temperature: float = DEFAULT_TEMPERATURE
    top_p: float = DEFAULT_TOP_P
    max_tokens: int = DEFAULT_MAX_TOKENS
    lines_per_update: int = DEFAULT_LINES_PER_UPDATE
    timeout: float = DEFAULT_TIMEOUT


def load_prompt(prompt_file: str) -> str:
    path = resolve_repo_path(prompt_file)
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {prompt_file}")
    return path.read_text(encoding="utf-8").strip()


def build_user_prompt(match_state: str, lines_per_update: int, context: str | None = None) -> str:
    sections = [
        "Write live esports commentary for the match-state update below.",
        f"Return {lines_per_update} line(s).",
        "Each line must be exactly one sentence and ready for TTS playback.",
        "Do not add consequences, guesses, or strategy claims that are not stated in the notes.",
        "Do not add lead, advantage, or outcome language unless the notes explicitly mention it.",
    ]
    if context:
        sections.extend(["", "Extra context:", context.strip()])
    sections.extend(["", "Match-state update:", match_state.strip()])
    return "\n".join(sections)


def normalize_commentary_lines(text: str, lines_per_update: int) -> list[str]:
    raw_lines = [part.strip() for part in text.splitlines() if part.strip()]
    if not raw_lines:
        raw_lines = [text.strip()]

    normalized: list[str] = []
    for raw_line in raw_lines:
        line = LINE_PREFIX_RE.sub("", raw_line).strip()
        if not line:
            continue
        sentences = [part.strip() for part in SENTENCE_SPLIT_RE.split(line) if part.strip()]
        if not sentences:
            sentences = [line]
        for sentence in sentences:
            cleaned = sentence.strip().strip('"').strip("'")
            if not cleaned:
                continue
            if cleaned[-1] not in ".!?":
                cleaned = f"{cleaned}."
            normalized.append(cleaned)
            if len(normalized) >= lines_per_update:
                return normalized
    return normalized


class DockerModelRunnerClient:
    def __init__(self, config: CommentaryGenerationConfig):
        self.config = config
        self.system_prompt = load_prompt(config.prompt_file)

    def list_models(self) -> list[str]:
        with httpx.Client(timeout=self.config.timeout) as client:
            response = client.get(f"{self.config.base_url}/models")
            response.raise_for_status()
        data = response.json().get("data") or []
        return [entry.get("id", "") for entry in data if entry.get("id")]

    def ensure_model_available(self) -> None:
        models = self.list_models()
        if self.config.model not in models:
            available = ", ".join(models) if models else "none"
            raise RuntimeError(
                f"Model {self.config.model!r} is not available in Docker Model Runner. "
                f"Available models: {available}"
            )

    def generate_commentary(self, match_state: str, context: str | None = None) -> list[str]:
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {
                    "role": "user",
                    "content": build_user_prompt(
                        match_state=match_state,
                        lines_per_update=self.config.lines_per_update,
                        context=context,
                    ),
                },
            ],
            "temperature": self.config.temperature,
            "top_p": self.config.top_p,
            "max_tokens": self.config.max_tokens,
        }

        with httpx.Client(timeout=self.config.timeout) as client:
            response = client.post(f"{self.config.base_url}/chat/completions", json=payload)
            response.raise_for_status()

        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("Docker Model Runner returned no choices.")

        message = choices[0].get("message") or {}
        content = (message.get("content") or "").strip()
        if not content:
            raise RuntimeError("Docker Model Runner returned empty commentary.")

        lines = normalize_commentary_lines(content, self.config.lines_per_update)
        if not lines:
            raise RuntimeError("Could not normalize commentary into sentence-sized lines.")
        return lines


def load_match_state(args: argparse.Namespace) -> str:
    if args.match_state:
        return args.match_state.strip()
    if args.input_file:
        return Path(args.input_file).read_text(encoding="utf-8").strip()
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    raise SystemExit("Provide --match-state, --input-file, or stdin.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate short esports commentary via Docker Model Runner.")
    parser.add_argument("--base-url", default=DEFAULT_DMR_BASE_URL, help=f"DMR API base URL (default: {DEFAULT_DMR_BASE_URL})")
    parser.add_argument("--model", default=DEFAULT_COMMENTARY_MODEL, help="Docker Model Runner model ID")
    parser.add_argument("--prompt-file", default=DEFAULT_PROMPT_FILE, help="System prompt file")
    parser.add_argument("--match-state", default=None, help="Inline match-state update")
    parser.add_argument("--input-file", default=None, help="Read one match-state update from a file")
    parser.add_argument("--context", default=None, help="Optional persistent casting context")
    parser.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE, help="Sampling temperature")
    parser.add_argument("--top-p", type=float, default=DEFAULT_TOP_P, help="Top-p sampling")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS, help="Maximum tokens to generate")
    parser.add_argument(
        "--lines-per-update",
        type=int,
        default=DEFAULT_LINES_PER_UPDATE,
        help="Target number of short commentary lines",
    )
    parser.add_argument("--list-models", action="store_true", help="List models visible from the DMR API")
    parser.add_argument("--check-model", action="store_true", help="Fail if the configured model is not available")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = CommentaryGenerationConfig(
        base_url=args.base_url,
        model=args.model,
        prompt_file=args.prompt_file,
        temperature=args.temperature,
        top_p=args.top_p,
        max_tokens=args.max_tokens,
        lines_per_update=args.lines_per_update,
    )
    client = DockerModelRunnerClient(config)

    if args.list_models:
        for model_id in client.list_models():
            print(model_id)
        return

    if args.check_model:
        client.ensure_model_available()

    match_state = load_match_state(args)
    for line in client.generate_commentary(match_state=match_state, context=args.context):
        print(line)


if __name__ == "__main__":
    main()
