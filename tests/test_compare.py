import asyncio
import unittest
import uuid
from unittest.mock import patch

from sqlalchemy import delete

from app.db.bootstrap import init_db
from app.db.models import ChunkRecord, Company, Document
from app.db.session import get_session
from app.workflows.compare import compare_companies


class DummyEmbeddingService:
    async def embed_query(self, text):
        return [1.0, 0.0]


class DummyChatService:
    def __init__(self, reply: str = '{"assessment": "strong_evidence", "rationale": "Recurring revenue is strong [1]."}'):
        self.reply = reply

    async def generate(self, prompt):
        return self.reply


class CompareCompaniesValidationTestCase(unittest.TestCase):
    """Pure unit test: no Postgres, no LLM call needed."""

    def test_unknown_dimension_raises(self):
        with self.assertRaises(KeyError):
            asyncio.run(
                compare_companies(
                    "nemetschek", "atoss", DummyEmbeddingService(), DummyChatService(), dimension="not_a_real_dimension"
                )
            )


class CompareCompaniesMergeTestCase(unittest.TestCase):
    """Pure unit test: screen_company is patched, only checks the per-criterion merge logic."""

    def test_merges_both_companies_assessments_per_criterion(self):
        async def fake_screen_company(company_id, *args, dimension=None, **kwargs):
            assessment = "strong_evidence" if company_id == "nemetschek" else "weak_evidence"
            return {
                "company_id": company_id,
                "dimensions": [
                    {
                        "dimension": "growth",
                        "description": "Potential for continued revenue growth",
                        "criteria": [
                            {
                                "criterion": "organic_growth",
                                "question": "What evidence is there of organic growth?",
                                "assessment": assessment,
                                "rationale": f"{company_id} rationale",
                                "sources": [],
                            }
                        ],
                    }
                ],
            }

        with patch("app.workflows.compare.screen_company", fake_screen_company):
            result = asyncio.run(
                compare_companies(
                    "nemetschek", "atoss", DummyEmbeddingService(), DummyChatService(), dimension="growth"
                )
            )

        self.assertEqual(result["company_a"], "nemetschek")
        self.assertEqual(result["company_b"], "atoss")
        self.assertEqual(len(result["dimensions"]), 1)
        criterion = result["dimensions"][0]["criteria"][0]
        self.assertEqual(criterion["criterion"], "organic_growth")
        self.assertEqual(criterion["company_a"]["assessment"], "strong_evidence")
        self.assertEqual(criterion["company_b"]["assessment"], "weak_evidence")


class CompareCompaniesWorkflowTestCase(unittest.TestCase):
    """Needs the Postgres container. Only touches rows of two fake companies."""

    def setUp(self):
        init_db()
        self._clean_rows("demo")
        self._clean_rows("demo2")
        self._insert_chunk("demo", "Recurring revenue from software subscriptions is stable.", [1.0, 0.0])
        self._insert_chunk("demo2", "Recurring revenue from software subscriptions is stable.", [1.0, 0.0])

    def tearDown(self):
        self._clean_rows("demo")
        self._clean_rows("demo2")

    @staticmethod
    def _clean_rows(company_id: str):
        with get_session() as session:
            session.execute(delete(ChunkRecord).where(ChunkRecord.company_id == company_id))
            session.execute(delete(Document).where(Document.company_id == company_id))
            session.execute(delete(Company).where(Company.id == company_id))
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

    def test_compare_companies_runs_real_retrieval_for_both_companies(self):
        result = asyncio.run(
            compare_companies(
                "demo", "demo2", DummyEmbeddingService(), DummyChatService(), top_k=3, dimension="business_quality"
            )
        )

        self.assertEqual(result["dimension"], "business_quality")
        self.assertEqual(len(result["dimensions"]), 1)
        criterion = next(
            c for c in result["dimensions"][0]["criteria"] if c["criterion"] == "recurring_revenue"
        )
        self.assertEqual(criterion["company_a"]["assessment"], "strong_evidence")
        self.assertEqual(criterion["company_b"]["assessment"], "strong_evidence")


if __name__ == "__main__":
    unittest.main()
