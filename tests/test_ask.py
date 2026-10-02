import asyncio
import unittest
import uuid
from unittest.mock import patch

from sqlalchemy import delete

from app.db.bootstrap import init_db
from app.db.models import ChunkRecord, Company, Document
from app.db.session import get_session
from app.workflows.ask import NO_EVIDENCE_ANSWER, ask


class DummyEmbeddingService:
    async def embed_query(self, text):
        return [1.0, 0.0]


class DummyChatService:
    def __init__(self):
        self.prompts = []

    async def generate(self, prompt):
        self.prompts.append(prompt)
        return "Recurring revenue is strong [1]."


class AskNoEvidenceTestCase(unittest.TestCase):
    """Pure unit test: retrieve is patched, so no Postgres is needed."""

    def test_ask_returns_fallback_without_calling_the_llm(self):
        chat_service = DummyChatService()

        async def fake_retrieve(*args, **kwargs):
            return []

        with patch("app.workflows.ask.retrieve", fake_retrieve):
            result = asyncio.run(
                ask("What evidence of recurring revenue?", DummyEmbeddingService(), chat_service, company_id="demo")
            )

        self.assertEqual(result["answer"], NO_EVIDENCE_ANSWER)
        self.assertEqual(result["sources"], [])
        self.assertEqual(chat_service.prompts, [])


class AskWorkflowTestCase(unittest.TestCase):
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

    def test_ask_grounds_the_answer_in_retrieved_sources(self):
        chat_service = DummyChatService()

        result = asyncio.run(
            ask(
                "What evidence is there of recurring revenue?",
                DummyEmbeddingService(),
                chat_service,
                company_id="demo",
                top_k=3,
            )
        )

        self.assertEqual(result["answer"], "Recurring revenue is strong [1].")
        self.assertEqual(len(result["sources"]), 1)
        self.assertEqual(result["sources"][0]["document"], "Test Doc")
        self.assertEqual(len(chat_service.prompts), 1)
        self.assertIn("Recurring revenue from software subscriptions", chat_service.prompts[0])


if __name__ == "__main__":
    unittest.main()
