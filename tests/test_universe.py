import asyncio
import unittest
import uuid
from unittest.mock import patch

from sqlalchemy import delete

from app.db.bootstrap import init_db
from app.db.models import ChunkRecord, Company, Document
from app.db.session import get_session
from app.workflows.universe import screen_universe


class DummyEmbeddingService:
    async def embed_query(self, text):
        return [1.0, 0.0]


class DummyChatService:
    def __init__(self, reply: str = '{"assessment": "strong_evidence", "rationale": "Recurring revenue is strong [1]."}'):
        self.reply = reply

    async def generate(self, prompt):
        return self.reply


class ScreenUniverseValidationTestCase(unittest.TestCase):
    """Pure unit tests: no Postgres, no LLM call needed."""

    def test_unknown_criterion_raises(self):
        with self.assertRaises(KeyError):
            asyncio.run(
                screen_universe("not_a_real_criterion", DummyEmbeddingService(), DummyChatService())
            )

    def test_invalid_min_assessment_raises(self):
        with self.assertRaises(ValueError):
            asyncio.run(
                screen_universe(
                    "recurring_revenue", DummyEmbeddingService(), DummyChatService(), min_assessment="super_strong"
                )
            )


class ScreenUniverseFilteringTestCase(unittest.TestCase):
    """Pure unit test: list_companies_with_documents and screen_criterion are both patched."""

    def test_matches_only_include_companies_at_or_above_the_threshold(self):
        assessments = {"nemetschek": "strong_evidence", "atoss": "weak_evidence", "suse": "insufficient_evidence"}

        async def fake_screen_criterion(criterion, company_id, *args, **kwargs):
            return {
                "criterion": criterion.id,
                "dimension": criterion.dimension,
                "question": criterion.question,
                "assessment": assessments[company_id],
                "rationale": "...",
                "sources": [],
            }

        with (
            patch("app.workflows.universe.list_companies_with_documents", return_value=list(assessments)),
            patch("app.workflows.universe.screen_criterion", fake_screen_criterion),
        ):
            result = asyncio.run(
                screen_universe(
                    "recurring_revenue",
                    DummyEmbeddingService(),
                    DummyChatService(),
                    min_assessment="moderate_evidence",
                )
            )

        self.assertEqual(len(result["results"]), 3)
        self.assertEqual([r["company_id"] for r in result["matches"]], ["nemetschek"])


class ScreenUniverseWorkflowTestCase(unittest.TestCase):
    """Needs the Postgres container. Only touches rows of the fake 'demo' company."""

    def setUp(self):
        init_db()
        self._clean_demo_rows()
        self._insert_chunk("demo", "Recurring revenue from software subscriptions is stable.", [1.0, 0.0])

    def tearDown(self):
        self._clean_demo_rows()

    @staticmethod
    def _clean_demo_rows():
        with get_session() as session:
            session.execute(delete(ChunkRecord).where(ChunkRecord.company_id == "demo"))
            session.execute(delete(Document).where(Document.company_id == "demo"))
            session.execute(delete(Company).where(Company.id == "demo"))
            session.commit()

    @staticmethod
    def _insert_chunk(company_id: str, content: str, embedding: list[float]):
        with get_session() as session:
            if session.get(Company, company_id) is None:
                session.add(Company(id=company_id, name=company_id))
                session.flush()
            document_id = str(uuid.uuid4())
            session.add(
                Document(
                    id=document_id,
                    company_id=company_id,
                    document_type="annual_report",
                    title="Test Doc",
                    year=2025,
                    file_name="test.html",
                    content_hash=str(uuid.uuid4()),
                )
            )
            session.flush()
            session.add(
                ChunkRecord(
                    id=str(uuid.uuid4()),
                    document_id=document_id,
                    company_id=company_id,
                    chunk_index=0,
                    page_number=1,
                    content=content,
                    embedding=embedding,
                    metadata_={},
                )
            )
            session.commit()

    def test_screen_universe_runs_real_criterion_lookup_and_retrieval(self):
        with patch("app.workflows.universe.list_companies_with_documents", return_value=["demo"]):
            result = asyncio.run(
                screen_universe("recurring_revenue", DummyEmbeddingService(), DummyChatService(), top_k=3)
            )

        self.assertEqual(result["criterion"], "recurring_revenue")
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["company_id"], "demo")
        self.assertEqual(result["results"][0]["assessment"], "strong_evidence")
        self.assertEqual(result["matches"], result["results"])


if __name__ == "__main__":
    unittest.main()
