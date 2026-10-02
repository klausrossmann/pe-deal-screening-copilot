import asyncio
import unittest
import uuid
from unittest.mock import patch

from sqlalchemy import delete

from app.db.bootstrap import init_db
from app.db.models import ChunkRecord, Company, Document
from app.db.session import get_session
from app.workflows.screening import (
    Criterion,
    _parse_assessment,
    load_screening_config,
    screen_company,
    screen_criterion,
)


class DummyEmbeddingService:
    async def embed_query(self, text):
        return [1.0, 0.0]


class DummyChatService:
    def __init__(self, reply: str = '{"assessment": "strong_evidence", "rationale": "Recurring revenue is strong [1]."}'):
        self.reply = reply
        self.prompts = []

    async def generate(self, prompt):
        self.prompts.append(prompt)
        return self.reply


class LoadScreeningConfigTestCase(unittest.TestCase):
    """Pure unit test: just reads config/screening_config.yaml from disk."""

    def test_loads_criteria_grouped_by_dimension(self):
        criteria = load_screening_config("config/screening_config.yaml")

        self.assertGreater(len(criteria), 0)
        ids = {c.id for c in criteria}
        self.assertIn("recurring_revenue", ids)
        recurring_revenue = next(c for c in criteria if c.id == "recurring_revenue")
        self.assertEqual(recurring_revenue.dimension, "business_quality")
        self.assertTrue(recurring_revenue.question)

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            load_screening_config("config/does_not_exist.yaml")


class ParseAssessmentTestCase(unittest.TestCase):
    """Pure unit test: no Postgres, no LLM call."""

    def test_parses_clean_json(self):
        result = _parse_assessment('{"assessment": "moderate_evidence", "rationale": "Some evidence [1]."}')
        self.assertEqual(result["assessment"], "moderate_evidence")
        self.assertEqual(result["rationale"], "Some evidence [1].")

    def test_parses_json_wrapped_in_code_fence(self):
        raw = '```json\n{"assessment": "weak_evidence", "rationale": "Thin evidence."}\n```'
        result = _parse_assessment(raw)
        self.assertEqual(result["assessment"], "weak_evidence")

    def test_falls_back_to_insufficient_evidence_on_invalid_json(self):
        result = _parse_assessment("not json at all")
        self.assertEqual(result["assessment"], "insufficient_evidence")

    def test_falls_back_to_insufficient_evidence_on_unknown_assessment_value(self):
        result = _parse_assessment('{"assessment": "very_strong", "rationale": "..."}')
        self.assertEqual(result["assessment"], "insufficient_evidence")


class ScreenCriterionNoEvidenceTestCase(unittest.TestCase):
    """Pure unit test: retrieve is patched, so no Postgres is needed."""

    def test_screen_criterion_skips_the_llm_without_evidence(self):
        chat_service = DummyChatService()
        criterion = Criterion(
            id="recurring_revenue",
            question="What evidence is there of recurring revenue?",
            dimension="business_quality",
            dimension_description="Quality and defensibility of the business",
        )

        async def fake_retrieve(*args, **kwargs):
            return []

        with patch("app.workflows.screening.retrieve", fake_retrieve):
            result = asyncio.run(
                screen_criterion(criterion, "demo", DummyEmbeddingService(), chat_service, top_k=3)
            )

        self.assertEqual(result["assessment"], "insufficient_evidence")
        self.assertEqual(result["sources"], [])
        self.assertEqual(chat_service.prompts, [])


class ScreenCompanyWorkflowTestCase(unittest.TestCase):
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

    def test_screen_criterion_grounds_the_assessment_in_retrieved_sources(self):
        chat_service = DummyChatService()
        criterion = Criterion(
            id="recurring_revenue",
            question="What evidence is there of recurring revenue?",
            dimension="business_quality",
            dimension_description="Quality and defensibility of the business",
        )

        result = asyncio.run(
            screen_criterion(criterion, "demo", DummyEmbeddingService(), chat_service, top_k=3)
        )

        self.assertEqual(result["assessment"], "strong_evidence")
        self.assertEqual(len(result["sources"]), 1)
        self.assertEqual(result["sources"][0]["document"], "Test Doc")
        self.assertIn("Recurring revenue from software subscriptions", chat_service.prompts[0])

    def test_screen_company_groups_results_by_dimension(self):
        chat_service = DummyChatService()

        result = asyncio.run(
            screen_company("demo", DummyEmbeddingService(), chat_service, top_k=3)
        )

        self.assertEqual(result["company_id"], "demo")
        self.assertGreater(len(result["dimensions"]), 0)
        business_quality = next(d for d in result["dimensions"] if d["dimension"] == "business_quality")
        self.assertGreater(len(business_quality["criteria"]), 0)
        self.assertTrue(all(c["assessment"] == "strong_evidence" for c in business_quality["criteria"]))


if __name__ == "__main__":
    unittest.main()
