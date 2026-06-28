"""
LLM Reasoner — calls DashScope/Qwen-compatible API.
Provides general-purpose LLM utilities for the financial document intelligence platform.
"""

import json
import os
import re
import time
import requests


# ── Load .env file ──
def _load_dotenv(path: str = ".env"):
    """Load key=value pairs from a .env file into os.environ (simple, no dependencies)."""
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if key and val and key not in os.environ:
                os.environ[key] = val

_load_dotenv()


# ── Token estimation ──
def estimate_tokens(text: str) -> int:
    """Heuristic token counter: Chinese chars ~1.3/token, non-Chinese ~3.5/token."""
    if not text:
        return 0
    chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff' or '\u3400' <= c <= '\u4dbf')
    other_chars = len(text) - chinese_chars
    return int(chinese_chars / 1.3 + other_chars / 3.5)


# ── Default config ──
DEFAULT_CONFIG = {
    "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "api_key": "",
    "model": "qwen-plus-latest",
    "temperature": 0.0,
    "max_tokens": 4096,
}


def get_llm_config():
    """Get LLM configuration from environment (loaded from .env or system env)."""
    return {
        "api_base": os.environ.get("LLM_API_BASE", DEFAULT_CONFIG["api_base"]),
        "api_key": os.environ.get("LLM_API_KEY", os.environ.get("OPENAI_API_KEY", DEFAULT_CONFIG["api_key"])),
        "model": os.environ.get("LLM_MODEL", DEFAULT_CONFIG["model"]),
        "temperature": float(os.environ.get("LLM_TEMPERATURE", str(DEFAULT_CONFIG["temperature"]))),
        "max_tokens": int(os.environ.get("LLM_MAX_TOKENS", str(DEFAULT_CONFIG["max_tokens"]))),
    }


def call_llm(messages: list[dict], config: dict | None = None, max_retries: int = 2,
             log_qid: str = "", round_num: int = 0) -> dict:
    """
    Call an OpenAI-compatible LLM API with retry on transient failures.
    Returns {"content": str, "usage": dict} or {"error": str}
    """
    if config is None:
        config = get_llm_config()

    api_key = config.get("api_key", "")
    if not api_key:
        return {"error": "No API key configured"}

    url = f"{config['api_base'].rstrip('/')}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    payload = {
        "model": config["model"],
        "messages": messages,
        "temperature": config["temperature"],
        "max_tokens": config["max_tokens"],
    }

    # Add thinking=disabled if model supports it (Qwen-specific)
    if "qwen" in config["model"].lower():
        payload["thinking"] = {"type": "disabled"}

    last_error = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=(10, 60))
            resp.raise_for_status()
            result = resp.json()
            content = result["choices"][0]["message"]["content"]

            # Debug logging
            import datetime
            if log_qid:
                os.makedirs("results/raw_responses", exist_ok=True)
                with open(f"results/raw_responses/{log_qid}.txt", "a", encoding="utf-8") as df:
                    df.write(f"\n=== Round {round_num} {datetime.datetime.now()} ===\n")
                    df.write(f"MESSAGES:\n{json.dumps(messages, ensure_ascii=False, indent=2)[:8000]}\n")
                    df.write(f"RESPONSE:\n{content}\n")
                    df.write(f"Usage: {result.get('usage', {})}\n")
            else:
                os.makedirs("results", exist_ok=True)
                with open("results/llm_debug.log", "a", encoding="utf-8") as df:
                    df.write(f"\n=== {datetime.datetime.now()} ===\n")
                    df.write(f"Response:\n{content[:2000]}\n")
                    df.write(f"Usage: {result.get('usage', {})}\n")

            return {
                "content": content,
                "usage": result.get("usage", {}),
                "error": None,
            }
        except requests.exceptions.Timeout:
            last_error = f"API timeout (attempt {attempt + 1}/{max_retries + 1})"
            if attempt < max_retries:
                time.sleep(2 ** attempt)
        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code if e.response is not None else 0
            if status_code in (429, 503) and attempt < max_retries:
                last_error = f"HTTP {status_code} (attempt {attempt + 1}/{max_retries + 1})"
                time.sleep(2 ** attempt * 2)
            else:
                return {"error": f"HTTP {status_code}: {e}", "content": "", "usage": {}}
        except requests.exceptions.RequestException as e:
            last_error = f"Request failed: {e}"
            if attempt < max_retries:
                time.sleep(2 ** attempt)
        except (KeyError, IndexError) as e:
            return {"error": f"Unexpected response: {e}", "content": "", "usage": {}}

    return {"error": last_error or "API request failed after retries", "content": "", "usage": {}}


def parse_json_from_response(content: str) -> dict:
    """Extract JSON object from LLM response (may be wrapped in markdown or have trailing text)."""
    # Try direct parse
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    # Try extracting from markdown code block
    m = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', content)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Try finding any JSON object
    m = re.search(r'\{[\s\S]*\}', content)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    return {"error": "No JSON found", "raw": content[:500]}


def reason(prompt: str, system: str = "", config: dict | None = None, json_mode: bool = False) -> dict:
    """
    Simple single-turn LLM call with optional JSON mode.
    
    Args:
        prompt: User message content
        system: Optional system message
        config: Optional LLM config (uses default if None)
        json_mode: If True, parse response as JSON
    
    Returns:
        {"content": str, "parsed": dict|None, "usage": dict, "error": str|None}
    """
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    result = call_llm(messages, config)

    response = {
        "content": result.get("content", ""),
        "parsed": None,
        "usage": result.get("usage", {}),
        "error": result.get("error"),
    }

    if json_mode and not result.get("error"):
        response["parsed"] = parse_json_from_response(result["content"])

    return response