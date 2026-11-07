"""LLM providers — all optional and free to run.

  gemini : Google Gemini API (free tier) when GEMINI_API_KEY is set
  ollama : local model served by Ollama (e.g. qwen2.5:7b, good Arabic)
  none   : no generation; the assistant quotes the retrieved official passages verbatim

With LLM_PROVIDER=auto the first available provider is used.
"""
from __future__ import annotations

import logging
import time

import httpx

from ..config import settings

log = logging.getLogger("uniguide.llm")


class LLMError(Exception):
    pass


class GeminiLLM:
    name = "gemini"

    def __init__(self) -> None:
        from google import genai

        self.client = genai.Client(api_key=settings.gemini_api_key)
        self.model = settings.gemini_model

    def generate(self, system: str, prompt: str) -> str:
        from google.genai import types

        try:
            resp = self.client.models.generate_content(
                model=self.model, contents=prompt,
                config=types.GenerateContentConfig(system_instruction=system, temperature=0.1,
                                                   max_output_tokens=1200),
            )
            return (resp.text or "").strip()
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"Gemini error: {e}") from e

    @property
    def label(self) -> str:
        return f"Gemini ({self.model})"


class OllamaLLM:
    name = "ollama"

    def __init__(self) -> None:
        self.url = settings.ollama_url.rstrip("/")
        self.model = settings.ollama_model

    def available(self) -> bool:
        try:
            r = httpx.get(f"{self.url}/api/tags", timeout=2)
            names = {m["name"] for m in r.json().get("models", [])}
            return self.model in names or f"{self.model}:latest" in names
        except Exception:  # noqa: BLE001
            return False

    def generate(self, system: str, prompt: str) -> str:
        try:
            r = httpx.post(f"{self.url}/api/chat", timeout=180, json={
                "model": self.model, "stream": False,
                "options": {"temperature": 0.0, "num_ctx": 8192, "num_predict": 800},
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": prompt}],
            })
            r.raise_for_status()
            return r.json()["message"]["content"].strip()
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"Ollama error: {e}") from e

    @property
    def label(self) -> str:
        return f"Ollama ({self.model})"


_cached: tuple[float, object | None] | None = None


def get_llm():
    """Return the configured provider, or None for extractive (quote-only) mode."""
    global _cached
    if _cached and time.monotonic() - _cached[0] < 60:
        return _cached[1]
    provider = settings.llm_provider.lower()
    llm = None
    if provider in ("auto", "gemini") and settings.gemini_api_key:
        try:
            llm = GeminiLLM()
        except Exception as e:  # noqa: BLE001
            log.warning("Gemini unavailable: %s", e)
    if llm is None and provider in ("auto", "ollama"):
        o = OllamaLLM()
        if o.available():
            llm = o
    _cached = (time.monotonic(), llm)
    return llm
