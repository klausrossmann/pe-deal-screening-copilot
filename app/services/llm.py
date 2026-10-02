from __future__ import annotations

import asyncio
import os
from functools import lru_cache

DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "google": "models/gemini-flash-latest",
    "groq": "openai/gpt-oss-120b",
}
# 1 is safe on free tiers (Groq: 8K tokens/minute); raise LLM_MAX_CONCURRENCY on paid tiers.
DEFAULT_MAX_CONCURRENCY = 1
# Free tiers have low tokens-per-minute limits; the SDKs wait for the server's retry-after before each retry.
MAX_RETRIES = 8


class ChatService:
    """Generates text completions using the provider set in LLM_PROVIDER."""

    def __init__(self, provider: str | None = None, model_name: str | None = None, max_concurrency: int | None = None):
        self.provider = (provider or os.getenv("LLM_PROVIDER", "openai")).lower()
        if self.provider not in DEFAULT_MODELS:
            raise ValueError(f"Unsupported LLM_PROVIDER '{self.provider}'. Use one of {list(DEFAULT_MODELS)}.")
        self.model_name = model_name or os.getenv("LLM_MODEL", DEFAULT_MODELS[self.provider])
        self.max_concurrency = max(1, max_concurrency or int(os.getenv("LLM_MAX_CONCURRENCY", DEFAULT_MAX_CONCURRENCY)))
        # Workflows fire many calls at once; this caps how many are in flight, to stay under provider rate limits.
        self._slots = asyncio.Semaphore(self.max_concurrency)

        # Provider libraries are imported lazily so only the chosen one has to be installed.
        if self.provider == "openai":
            from openai import OpenAI

            self.client = OpenAI(api_key=self._require_env("OPENAI_API_KEY"), max_retries=MAX_RETRIES)
        elif self.provider == "groq":
            from groq import Groq

            self.client = Groq(api_key=self._require_env("GROQ_API_KEY"), max_retries=MAX_RETRIES)
        else:
            from google import genai
            from google.genai import types

            # Google's free tier returns transient 503/429s under load; retry a few times with backoff
            # instead of failing the whole ask() call on what the API itself calls "usually temporary".
            retry_options = types.HttpRetryOptions(attempts=5, initial_delay=1.0, max_delay=20.0, exp_base=2.0)
            self.client = genai.Client(
                api_key=self._require_env("GOOGLE_API_KEY"),
                http_options=types.HttpOptions(retry_options=retry_options),
            )

    @staticmethod
    def _require_env(name: str) -> str:
        value = os.getenv(name)
        if not value:
            raise RuntimeError(f"{name} is required for the selected LLM_PROVIDER")
        return value

    async def generate(self, prompt: str) -> str:
        async with self._slots:
            # The provider SDKs are synchronous; a worker thread keeps the event loop free while waiting.
            return await asyncio.to_thread(self._generate_blocking, prompt)

    def _generate_blocking(self, prompt: str) -> str:
        if self.provider in ("openai", "groq"):
            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.choices[0].message.content

        response = self.client.models.generate_content(model=self.model_name, contents=prompt)
        return response.text


@lru_cache
def get_chat_service() -> ChatService:
    """One shared instance per process, so the concurrency limit applies across all API requests."""
    return ChatService()
