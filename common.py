"""Shared helpers: paths, local-LLM client, JSON utilities."""
import json
import re
from pathlib import Path

import requests

# --- Configuration ----------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
RESUME_PDF = BASE_DIR / "resume.pdf"
PROFILE_JSON = BASE_DIR / "user_profile.json"
QA_CACHE_JSON = BASE_DIR / "qa_cache.json"
JOBS_JSON = BASE_DIR / "jobs.json"

LLM_BASE_URL = "http://localhost:11434/v1"
LLM_MODEL = "qwen3.6-hermes"
LLM_NUM_CTX = 16384  # the form filler needs far less than Hermes (64k) - a small context loads reliably
LLM_NUM_GPU = 6      # GPU layers; kept low so it still loads when other apps (e.g. Lightroom) hold GPU memory

LLM_NATIVE_URL = "http://localhost:11434/api/chat"


# --- LLM --------------------------------------------------------------------
def extract_json(text: str):
    """Pull the first JSON object/array out of an LLM reply (strips <think> and code fences)."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"(\{.*\}|\[.*\])", text, flags=re.DOTALL)
        if not match:
            raise ValueError(f"No JSON found in LLM reply: {text[:200]!r}")
        return json.loads(match.group(1))


def ask_llm_json(system: str, user: str, retries: int = 1):
    """Query the local LLM (Ollama native API, big context, no thinking) and return parsed JSON."""
    last_err = None
    for _ in range(retries + 1):
        try:
            r = requests.post(
                LLM_NATIVE_URL,
                json={
                    "model": LLM_MODEL,
                    "stream": False,
                    "think": False,
                    "format": "json",
                    "options": {"temperature": 0, "num_ctx": LLM_NUM_CTX, "num_gpu": LLM_NUM_GPU},
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                },
                timeout=1800,
            )
            r.raise_for_status()
            return extract_json(r.json()["message"]["content"])
        except Exception as exc:  # connection errors, bad JSON, etc.
            last_err = exc
    raise RuntimeError(f"LLM call failed ({LLM_NATIVE_URL}, model {LLM_MODEL}). Is Ollama running? Details: {last_err}")

# --- JSON file helpers ------------------------------------------------------
def load_json(path: Path, default):
    """Load JSON from path; return default if missing or corrupt."""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(f"[warn] {path.name} is not valid JSON; ignoring it.")
        return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


