import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.ingestion.manifest import add_company, get_company, load_companies, load_sources
from app.ingestion.uploads import UploadedFile, register_uploads, safe_file_name

COMPANIES_YAML = """# Comment that must survive
companies:
  - id: acme
    name: Acme
    screening_tags:
      - vertical_software"""  # deliberately no trailing newline

SOURCES_YAML = """sources:
  # Acme
  - company_id: acme
    file_name: existing.html
    document_type: company_profile
    year: 2025
    title: Existing
    source_url: https://example.com/acme
"""

METADATA = {
    "company_id": "acme",
    "document_type": "annual_report",
    "year": 2025,
    "title": "Acme Annual Report 2025",
    "source_url": "https://example.com/acme/ar-2025",
}


class ManifestWriteTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.companies_path = str(self.tmp / "companies.yaml")
        Path(self.companies_path).write_text(COMPANIES_YAML, encoding="utf-8")

    def test_add_company_appends_and_keeps_comments(self):
        add_company({"id": "beta", "name": "Beta GmbH", "ticker": None, "screening_tags": ["b2b"]}, self.companies_path)

        self.assertEqual([c["id"] for c in load_companies(self.companies_path)], ["acme", "beta"])
        self.assertEqual(get_company("beta", self.companies_path)["name"], "Beta GmbH")
        self.assertIn("# Comment that must survive", Path(self.companies_path).read_text(encoding="utf-8"))

    def test_add_company_rejects_duplicate_id(self):
        with self.assertRaises(ValueError):
            add_company({"id": "acme", "name": "Other"}, self.companies_path)


class RegisterUploadsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.raw_dir = self.tmp / "raw"
        self.sources_path = self.tmp / "sources.yaml"
        self.companies_path = str(self.tmp / "companies.yaml")
        self.sources_path.write_text(SOURCES_YAML, encoding="utf-8")
        Path(self.companies_path).write_text(COMPANIES_YAML, encoding="utf-8")

    def _register(self, files, metadata):
        return register_uploads(
            files, metadata, raw_dir=self.raw_dir, sources_path=self.sources_path, companies_path=self.companies_path
        )

    def test_registers_multiple_files(self):
        files = [UploadedFile("report 2025.pdf", b"%PDF-1.7 ..."), UploadedFile("about.html", b"<html></html>")]
        sources = self._register(files, [METADATA, {**METADATA, "title": "About"}])

        self.assertEqual([s.file_name for s in sources], ["report_2025.pdf", "about.html"])
        self.assertTrue((self.raw_dir / "acme" / "report_2025.pdf").exists())
        manifest = load_sources(self.sources_path)
        self.assertEqual(len(manifest), 3)
        self.assertEqual(manifest[1].source_url, METADATA["source_url"])
        self.assertIn("# Acme", self.sources_path.read_text(encoding="utf-8"))

    def test_invalid_batch_writes_nothing(self):
        files = [UploadedFile("ok.html", b"<html></html>"), UploadedFile("bad.pdf", b"not a pdf")]
        with self.assertRaises(ValueError):
            self._register(files, [METADATA, METADATA])

        self.assertFalse(self.raw_dir.exists())
        self.assertEqual(len(load_sources(self.sources_path)), 1)

    def test_rejects_unknown_company_duplicates_and_unsupported_types(self):
        cases = [
            ([UploadedFile("a.html", b"x")], [{**METADATA, "company_id": "nope"}]),
            ([UploadedFile("existing.html", b"x")], [METADATA]),
            ([UploadedFile("a.html", b"x"), UploadedFile("a.html", b"y")], [METADATA, METADATA]),
            ([UploadedFile("a.exe", b"x")], [METADATA]),
            ([UploadedFile("a.html", b"x")], []),
        ]
        for files, metadata in cases:
            with self.subTest(files=[f.file_name for f in files]), self.assertRaises(ValueError):
                self._register(files, metadata)

    def test_safe_file_name_strips_directories(self):
        self.assertEqual(safe_file_name("../../etc/evil.html"), "evil.html")
        self.assertEqual(safe_file_name("..\\..\\évil.pdf"), "_vil.pdf")
        self.assertEqual(safe_file_name(".hidden.html"), "hidden.html")


class UploadEndpointValidationTestCase(unittest.TestCase):
    """Request validation only; rejected before anything touches disk or Postgres."""

    def setUp(self):
        from app.main import app

        self.client = TestClient(app)

    def test_upload_requires_all_metadata(self):
        incomplete = {key: value for key, value in METADATA.items() if key != "source_url"}
        response = self.client.post(
            "/ingestion/upload",
            files=[("files", ("a.html", b"<html></html>", "text/html"))],
            data={"metadata": json.dumps([incomplete])},
        )
        self.assertEqual(response.status_code, 422)

    def test_create_company_validates_id(self):
        response = self.client.post(
            "/companies",
            json={
                "id": "../evil",
                "name": "Evil",
                "website": "https://evil.example",
                "country": "Germany",
                "category": "software",
                "ownership": "private",
                "screening_tags": ["x"],
            },
        )
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
