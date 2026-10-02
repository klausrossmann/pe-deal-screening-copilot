import asyncio
import hashlib
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import delete

from app.db.bootstrap import init_db
from app.db.models import ChunkRecord, Company, Document
from app.db.session import get_session
from app.ingestion.chunker import chunk_pages, chunk_text
from app.ingestion.loader import compute_file_hash
from app.ingestion.manifest import SourceDocument, load_sources
from app.ingestion.parser import extract_pages
from app.ingestion.pipeline import ingest_document
from app.retrieval.retriever import retrieve, retrieve_multi
from app.schemas.documents import Page


class IngestionPipelineTestCase(unittest.TestCase):
    def test_compute_file_hash(self):
        with tempfile.NamedTemporaryFile("wb", delete=False) as tmp:
            tmp.write(b"hello world")
            path = tmp.name

        try:
            digest = compute_file_hash(path)
            expected = hashlib.sha256(b"hello world").hexdigest()
            self.assertEqual(digest, expected)
        finally:
            os.unlink(path)

    def test_chunk_text_keeps_overlap(self):
        text = "alpha beta gamma delta epsilon zeta eta theta"
        chunks = chunk_text(text, chunk_size=20, chunk_overlap=5)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 20 for chunk in chunks))
        self.assertTrue(chunks[0].startswith("alpha"))
        self.assertTrue(chunks[1].startswith("gamma") or chunks[1].startswith("beta"))

    def test_load_sources_reads_manifest(self):
        sources = load_sources("config/sources.yaml")
        self.assertTrue(len(sources) >= 1)
        self.assertEqual(sources[0].company_id, "nemetschek")
        self.assertTrue(hasattr(sources[0], "file_name"))

    def test_chunk_text_never_drops_words(self):
        text = " ".join(f"word{i}" for i in range(300))
        chunks = chunk_text(text, chunk_size=60, chunk_overlap=20)
        self.assertTrue(all(len(chunk) <= 60 for chunk in chunks))
        self.assertEqual(set(" ".join(chunks).split()), set(text.split()))

    def test_chunk_pages_keeps_page_numbers(self):
        pages = [Page(1, "first page " * 5), Page(2, ""), Page(3, "third page")]
        chunks = chunk_pages(pages, chunk_size=20, chunk_overlap=5)
        self.assertEqual({chunk.page_number for chunk in chunks}, {1, 3})
        self.assertEqual([chunk.chunk_index for chunk in chunks], list(range(len(chunks))))

    def test_extract_pages_handles_html(self):
        html_path = Path(tempfile.mkdtemp()) / "sample.html"
        html_path.write_text(
            "<html><head><style>p {color: red}</style><script>var x = 1;</script></head>"
            "<body><h1>Annual Report</h1><p>Recurring revenue is stable.</p></body></html>",
            encoding="utf-8",
        )

        pages = extract_pages(html_path)
        self.assertEqual(len(pages), 1)
        self.assertIn("Annual Report", pages[0].text)
        self.assertIn("Recurring revenue", pages[0].text)
        self.assertNotIn("var x", pages[0].text)
        self.assertNotIn("color: red", pages[0].text)


class IngestDocumentTestCase(unittest.TestCase):
    """Needs the Postgres container. Only touches rows of the 'demo' company."""

    def setUp(self):
        init_db()
        self._clean_demo_rows()
        self.file_path = Path("data/raw/demo/persisted_demo.html")
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self.file_path.write_text(
            "<html><body><h1>Persisted Demo</h1><p>Acme builds enterprise software.</p></body></html>",
            encoding="utf-8",
        )

    def tearDown(self):
        self.file_path.unlink(missing_ok=True)
        self._clean_demo_rows()

    @staticmethod
    def _clean_demo_rows():
        with get_session() as session:
            session.execute(delete(ChunkRecord).where(ChunkRecord.company_id == "demo"))
            session.execute(delete(Document).where(Document.company_id == "demo"))
            session.execute(delete(Company).where(Company.id == "demo"))
            session.commit()

    def test_ingest_document_persists_and_skips_duplicates(self):
        class DummyEmbeddingService:
            async def embed_documents(self, texts):
                return [[0.0] * 1536 for _ in texts]

        source = SourceDocument(
            company_id="demo",
            file_name="persisted_demo.html",
            document_type="annual_report",
            year=2025,
            title="Persisted Demo",
            source_url="https://example.com/demo",
        )
        demo_company = {"id": "demo", "name": "Demo Corp"}

        with patch("app.ingestion.pipeline.get_company", return_value=demo_company):
            first = asyncio.run(ingest_document(source, DummyEmbeddingService()))
            second = asyncio.run(ingest_document(source, DummyEmbeddingService()))

        self.assertTrue(first["ingested"])
        self.assertFalse(second["ingested"])

        with get_session() as session:
            self.assertEqual(session.query(Document).filter_by(company_id="demo").count(), 1)
            chunks = session.query(ChunkRecord).filter_by(company_id="demo").all()
            self.assertEqual(len(chunks), first["chunks"])
            self.assertEqual(chunks[0].metadata_["company_name"], "Demo Corp")


class RetrieveTestCase(unittest.TestCase):
    """Needs the Postgres container. Only touches rows of fake 'demo'/'demo2' companies."""

    def setUp(self):
        init_db()
        self._clean_demo_rows()

    def tearDown(self):
        self._clean_demo_rows()

    @staticmethod
    def _clean_demo_rows():
        with get_session() as session:
            session.execute(delete(ChunkRecord).where(ChunkRecord.company_id.in_(["demo", "demo2"])))
            session.execute(delete(Document).where(Document.company_id.in_(["demo", "demo2"])))
            session.execute(delete(Company).where(Company.id.in_(["demo", "demo2"])))
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

    def test_retrieve_ranks_by_distance_and_filters_company(self):
        self._insert_chunk("demo", "Recurring revenue from software subscriptions is stable.", [1.0, 0.0])
        self._insert_chunk("demo", "The product is sold via one-off hardware deals.", [0.0, 1.0])
        self._insert_chunk("demo2", "Nemetschek M&A activity across the group.", [1.0, 0.0])

        class DummyEmbeddingService:
            async def embed_query(self, text):
                return [1.0, 0.0]

        results = asyncio.run(retrieve("recurring software revenue", DummyEmbeddingService(), company_id="demo", top_k=2))

        self.assertEqual(len(results), 2)
        self.assertTrue(all(r["company_id"] == "demo" for r in results))
        self.assertIn("Recurring revenue", results[0]["content"])
        self.assertLess(results[0]["distance"], results[1]["distance"])
        self.assertTrue(all(r["chunk_id"] for r in results))

    def test_max_distance_drops_chunks_that_are_too_far_away(self):
        self._insert_chunk("demo", "Recurring revenue from software subscriptions is stable.", [1.0, 0.0])
        self._insert_chunk("demo", "The product is sold via one-off hardware deals.", [0.0, 1.0])

        class DummyEmbeddingService:
            async def embed_query(self, text):
                return [1.0, 0.0]

        results = asyncio.run(
            retrieve("recurring revenue", DummyEmbeddingService(), company_id="demo", top_k=5, max_distance=0.5)
        )

        self.assertEqual(len(results), 1)
        self.assertIn("Recurring revenue", results[0]["content"])

    def test_retrieve_multi_merges_queries_and_keeps_each_chunk_once(self):
        self._insert_chunk("demo", "Recurring revenue from software subscriptions is stable.", [1.0, 0.0])
        self._insert_chunk("demo", "EBITDA margin rose to 30%.", [0.0, 1.0])

        class DummyEmbeddingService:
            async def embed_query(self, text):
                return [1.0, 0.0] if "recurring" in text else [0.0, 1.0]

        results = asyncio.run(
            retrieve_multi(
                ["recurring revenue", "EBITDA margin"], DummyEmbeddingService(), company_id="demo", top_k=5
            )
        )

        self.assertEqual(len(results), 2)
        self.assertEqual(len({r["chunk_id"] for r in results}), 2)
        self.assertTrue(all(r["distance"] < 1e-6 for r in results))


if __name__ == "__main__":
    unittest.main()
