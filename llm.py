"""Free-tier-friendly LLM layer (temperature 0). Tries providers in order until one answers:
   GEMINI_API_KEY (Google AI Studio free) -> GROQ_API_KEY (free) -> OpenRouter ':free' models.
All are OpenAI-compatible chat endpoints. Hard deadlines so a slow/rate-limited model never blows the 30s budget."""
import json
import os
import re
import threading
import time
from pathlib import Path

import httpx


def _load_env():
    p = Path(__file__).parent / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

_client = httpx.Client(timeout=httpx.Timeout(25.0, connect=5.0))
_cool: dict[str, float] = {}      # "provider|model" -> unix time until which it is skipped (after a 429)
_cool_lock = threading.Lock()
LAST_MODEL = {"name": None}


def _csv(name, default):
    return [m.strip() for m in os.environ.get(name, default).split(",") if m.strip()]


def _chain() -> list[tuple[str, str, str, dict]]:
    """(label, url, key, extra_body) per attempt, best first."""
    out = []
    if os.environ.get("GEMINI_API_KEY"):
        for m in _csv("GEMINI_MODEL", "gemini-3.8-flash,gemini-3.7-flash,gemini-3.5-flash-lite"):
            out.append((f"gemini:{m}", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
                        os.environ["GEMINI_API_KEY"], {"model": m, "reasoning_effort": "low"}))
    if os.environ.get("GROQ_API_KEY"):
        for m in _csv("GROQ_MODEL", "openai/gpt-oss-120b,qwen/qwen3.8-27b"):
            extra = {"model": m}
            if m.startswith("openai/gpt-oss"):
                extra["reasoning_effort"] = "low"   # keep thinking tokens small so content is never empty
            out.append((f"groq:{m}", "https://api.groq.com/openai/v1/chat/completions",
                        os.environ["GROQ_API_KEY"], extra))
    if os.environ.get("OPENROUTER_API_KEY"):
        for m in _csv("OPENROUTER_MODEL", "google/gemma-4-31b-it:free,nvidia/nemotron-3-super-120b-a12b:free,"
                      "poolside/laguna-s-2.1:free,google/gemma-4-26b-a4b-it:free,nvidia/nemotron-3-ultra-550b-a55b:free"):
            out.append((f"openrouter:{m}", "https://openrouter.ai/api/v1/chat/completions",
                        os.environ["OPENROUTER_API_KEY"], {"model": m}))
    return out


MODELS = [c[0] for c in _chain()]


def available() -> bool:
    return bool(_chain())


def complete(system: str, user: str, max_tokens: int = 900, budget_s: float = 20.0) -> str:
    chain = _chain()
    if not chain:
        raise RuntimeError("no LLM key configured")
    deadline = time.time() + budget_s
    last = None
    for label, url, key, extra in chain:
        remaining = deadline - time.time()
        if remaining < 4:
            break
        with _cool_lock:
            if _cool.get(label, 0) > time.time():
                continue
        reasoning = "nemotron" in label or "inkling" in label
        try:
            r = _client.post(url, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                             json={**extra, "temperature": 0, "max_tokens": max(max_tokens, 3000) if reasoning else max_tokens,
                                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
                             timeout=min(remaining, 25.0))
            if r.status_code in (402, 403, 429, 503):
                with _cool_lock:
                    _cool[label] = time.time() + {429: 60, 503: 20}.get(r.status_code, 600)
                last = f"{label} HTTP {r.status_code}"
                continue
            r.raise_for_status()
            msg = (r.json().get("choices") or [{}])[0].get("message") or {}
            text = msg.get("content")
            if not text:
                last = f"{label} empty content"
                continue
            LAST_MODEL["name"] = label
            return text
        except Exception as e:  # noqa: BLE001
            last = f"{label} {type(e).__name__}"
    raise RuntimeError(f"LLM failed: {last}")


def parse_json(text: str) -> dict:
    text = re.sub(r"<think>[\s\S]*?</think>", "", text).strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise ValueError("no json")
    return json.loads(m.group())
