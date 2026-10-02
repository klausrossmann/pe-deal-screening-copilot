import asyncio
import json
import tempfile
import unittest
from unittest.mock import patch

from app.workflows.analysis_cache import AnalysisCache
from app.workflows.screening import Criterion, screen_criterion

FAKE_CHUNK = {
    "chunk_id": "c1",
    "company_id": "demo",
    "page": 1,
    "document": "Test Doc",
    "file_name": "test.html",
    "content": "Recurring revenue from software subscriptions is stable.",
    "metadata": {},
    "distance": 0.2,
}
CRITERION = Criterion("recurring_revenue", "Recurring revenue?", "business_quality", "")


class DummyEmbeddingService:
    async def embed_query(self, text):
        return [1.0, 0.0]


class DummyChatService:
    def __init__(self):
        self.prompts = []

    async def generate(self, prompt):
        self.prompts.append(prompt)
        return '{"assessment": "strong_evidence", "rationale": "Recurring revenue is strong [1]."}'


class AnalysisCacheTestCase(unittest.TestCase):
    """Pure unit tests: only touches a temporary directory."""

    def setUp(self):
        self.cache = AnalysisCache(tempfile.mkdtemp())

    def test_save_then_load_returns_the_result(self):
        self.cache.save("atoss", "margin", "fp-1", {"assessment": "strong_evidence"})

        self.assertEqual(self.cache.load("atoss", "margin", "fp-1"), {"assessment": "strong_evidence"})
        saved = json.loads((self.cache.directory / "atoss" / "margin.json").read_text())
        self.assertEqual(saved["fingerprint"], "fp-1")
        self.assertIn("assessed_at", saved)

    def test_a_different_fingerprint_means_the_saved_result_is_stale(self):
        self.cache.save("atoss", "margin", "fp-1", {"assessment": "strong_evidence"})

        self.assertIsNone(self.cache.load("atoss", "margin", "fp-2"))

    def test_missing_result_returns_none(self):
        self.assertIsNone(self.cache.load("atoss", "margin", "fp-1"))

    def test_ids_that_could_escape_the_directory_are_rejected(self):
        for bad_id in ("../etc", "a/b", "", "atoss\n"):
            with self.assertRaises(ValueError):
                self.cache.load(bad_id, "margin", "fp-1")


class ScreenCriterionCacheTestCase(unittest.TestCase):
    """Pure unit test: retrieval and the corpus fingerprint are patched, so no Postgres is needed."""

    def setUp(self):
        self.cache = AnalysisCache(tempfile.mkdtemp())
        self.chat_service = DummyChatService()
        self.corpus = "corpus-v1"

        async def fake_retrieve_multi(*args, **kwargs):
            return [FAKE_CHUNK]

        patches = [
            patch("app.workflows.screening.retrieve_multi", fake_retrieve_multi),
            patch("app.workflows.screening.company_corpus_fingerprint", lambda company_id: self.corpus),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _screen(self, **kwargs):
        return asyncio.run(
            screen_criterion(CRITERION, "demo", DummyEmbeddingService(), self.chat_service, cache=self.cache, **kwargs)
        )

    def test_second_run_reuses_the_saved_result_without_calling_the_llm(self):
        first = self._screen()
        second = self._screen()

        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(second["assessment"], first["assessment"])
        self.assertEqual(second["sources"], first["sources"])
        self.assertEqual(len(self.chat_service.prompts), 1)

    def test_refresh_forces_a_new_assessment(self):
        self._screen()
        refreshed = self._screen(refresh=True)

        self.assertFalse(refreshed["cached"])
        self.assertEqual(len(self.chat_service.prompts), 2)

    def test_changed_documents_or_settings_invalidate_the_saved_result(self):
        self._screen()
        self.corpus = "corpus-v2"
        self._screen()
        self._screen(top_k=5)

        self.assertEqual(len(self.chat_service.prompts), 3)


if __name__ == "__main__":
    unittest.main()
