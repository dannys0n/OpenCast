import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


def load_env_file(path):
    values = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            values[key] = value

    return values


def first_value(name, default, *sources):
    if name in os.environ:
        return os.environ[name]
    for source in sources:
        if name in source:
            return source[name]
    return default


@dataclass
class TextLLMConfig:
    model_api_base: str
    model_name: str
    system_prompt_base: str
    temperature: float
    max_tokens: int
    voice_name: str
    timeout_seconds: float

NO_THINK_SUFFIX = "/no_think"


def normalized_model_name(model_name):
    normalized = (model_name or "").strip()
    if not normalized:
        return "hf.co/unsloth/Qwen3-1.7B-GGUF:Q4_K_M"
    if normalized.endswith(NO_THINK_SUFFIX):
        return normalized[: -len(NO_THINK_SUFFIX)]
    return normalized


def append_no_think_prompt(prompt_text):
    text = (prompt_text or "").rstrip()
    if text.endswith(NO_THINK_SUFFIX):
        return text
    if not text:
        return NO_THINK_SUFFIX
    return f"{text}\n{NO_THINK_SUFFIX}"


def build_config(repo_root):
    repo_root = Path(repo_root).resolve()
    text_llm_env = load_env_file(repo_root / "deployment" / "text-llm" / ".env")

    return TextLLMConfig(
        model_api_base=first_value("MODEL_API_BASE", "http://127.0.0.1:12434", text_llm_env),
        model_name=normalized_model_name(
            first_value(
                "MODEL_NAME",
                "hf.co/unsloth/Qwen3-1.7B-GGUF:Q4_K_M",
                text_llm_env,
            )
        ),
        system_prompt_base=first_value(
            "SYSTEM_PROMPT",
            "You are an esports commentator.",
            text_llm_env,
        ),
        temperature=float(first_value("TEMPERATURE", "0.4", text_llm_env)),
        max_tokens=int(first_value("MAX_TOKENS", "160", text_llm_env)),
        voice_name=first_value("VOICE_NAME", "", text_llm_env),
        timeout_seconds=float(first_value("MODEL_TIMEOUT", "45", text_llm_env)),
    )


def build_system_prompt(base_prompt):
    return (
        f"{base_prompt} "
        "Return JSON only. "
        "Use exactly these keys: commentary, caster, emotion. "
        "commentary must be one short natural caster line. "
        "caster must be either play_by_play or color. "
        "emotion must be Calm, Excited, or Screaming. "
        "No markdown. No preamble. No code fences."
    )


def build_plain_text_system_prompt(base_prompt):
    return (
        f"{base_prompt} "
        "Return only one short natural commentary line as plain text. "
        "No JSON. No markdown. No preamble. No labels. No code fences."
    )


def extract_message_content(response_json):
    choices = response_json.get("choices") or []
    if not choices:
        raise RuntimeError("text model returned no choices")

    message = choices[0].get("message") or {}
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("text model returned empty content")
    content = content.strip()
    content = re.sub(r"<think>.*?</think>\s*", "", content, flags=re.DOTALL).strip()
    return content


def extract_json_object(raw_text):
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        pass

    start = raw_text.find("{")
    end = raw_text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise RuntimeError("text model response did not contain a JSON object")

    candidate = raw_text[start : end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"text model returned invalid JSON: {error}") from error


def request_chat_completion(config, system_prompt, user_prompt, *, temperature=None, max_tokens=None):
    request_body = {
        "model": normalized_model_name(config.model_name),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": append_no_think_prompt(user_prompt)},
        ],
        "temperature": config.temperature if temperature is None else temperature,
        "max_tokens": config.max_tokens if max_tokens is None else max_tokens,
        "stream": False,
    }

    request = urllib.request.Request(
        f"{config.model_api_base}/v1/chat/completions",
        data=json.dumps(request_body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
            response_json = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"text model HTTP {error.code}: {body}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"text model request failed: {error}") from error

    raw_text = extract_message_content(response_json)

    return {
        "request": request_body,
        "response": response_json,
        "raw_text": raw_text,
    }


def request_structured_commentary(config, prompt_text):
    result = request_chat_completion(
        config,
        build_system_prompt(config.system_prompt_base),
        prompt_text,
    )
    raw_text = result["raw_text"]
    parsed = extract_json_object(raw_text)

    return {
        "request": result["request"],
        "response": result["response"],
        "raw_text": raw_text,
        "parsed": parsed,
    }


def request_plain_commentary(config, prompt_text):
    result = request_chat_completion(
        config,
        build_plain_text_system_prompt(config.system_prompt_base),
        prompt_text,
    )
    raw_text = result["raw_text"]

    return {
        "request": result["request"],
        "response": result["response"],
        "raw_text": raw_text.strip(),
        "parsed": None,
    }
