from __future__ import annotations

import os

BATCH_SIZE = 64

DEFAULT_MODELS = {
    "sentence_transformers": "sentence-transformers/all-MiniLM-L6-v2",
    "openai": "text-embedding-3-small",
    "google": "models/text-embedding-004",
}


class EmbeddingService:
    """Turns text into vectors using the provider set in EMBEDDING_PROVIDER."""

    def __init__(self, provider: str | None = None, model_name: str | None = None):
        self.provider = (provider or os.getenv("EMBEDDING_PROVIDER", "sentence_transformers")).lower()
        if self.provider not in DEFAULT_MODELS:
            raise ValueError(f"Unsupported EMBEDDING_PROVIDER '{self.provider}'. Use one of {list(DEFAULT_MODELS)}.")
        self.model_name = model_name or os.getenv("EMBEDDING_MODEL", DEFAULT_MODELS[self.provider])

        # Provider libraries are imported lazily so only the chosen one has to be installed.
        if self.provider == "sentence_transformers":
            from sentence_transformers import SentenceTransformer

            self.client = SentenceTransformer(self.model_name)
        elif self.provider == "openai":
            from openai import OpenAI

            self.client = OpenAI(api_key=self._require_env("OPENAI_API_KEY"))
        else:
            import google.generativeai as genai

            genai.configure(api_key=self._require_env("GOOGLE_API_KEY"))
            self.client = genai

    @staticmethod
    def _require_env(name: str) -> str:
        value = os.getenv(name)
        if not value:
            raise RuntimeError(f"{name} is required for the selected EMBEDDING_PROVIDER")
        return value

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), BATCH_SIZE):
            vectors.extend(self._embed(texts[start : start + BATCH_SIZE], "retrieval_document"))
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        return self._embed([text], "retrieval_query")[0]

    def _embed(self, texts: list[str], task_type: str) -> list[list[float]]:
        if self.provider == "sentence_transformers":
            return self.client.encode(texts, normalize_embeddings=True).astype(float).tolist()

        if self.provider == "openai":
            response = self.client.embeddings.create(model=self.model_name, input=texts)
            return [item.embedding for item in response.data]

        result = self.client.embed_content(model=self.model_name, content=texts, task_type=task_type)
        return result["embedding"]
