from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

from pypdf import PdfReader

from app.schemas.documents import Page

# Content of these tags is code or hidden, not visible text.
_SKIPPED_TAGS = {"script", "style", "noscript", "template"}


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text and not self._skip_depth:
            self._parts.append(text)

    def get_text(self) -> str:
        # Postgres text columns cannot hold NUL characters, which some sources contain.
        return "\n".join(self._parts).replace("\x00", "")


def _extract_html_text(file_path: str | Path) -> str:
    html = Path(file_path).read_text(encoding="utf-8", errors="ignore")
    parser = _HTMLTextExtractor()
    parser.feed(html)
    return parser.get_text()


def extract_pages(file_path: str | Path) -> list[Page]:
    file_path = Path(file_path)
    suffix = file_path.suffix.lower()

    if suffix in {".html", ".htm"}:
        text = _extract_html_text(file_path)
        return [Page(page_number=1, text=text)]

    if suffix != ".pdf":
        raise ValueError(f"Unsupported source format: {suffix or file_path.name}")

    reader = PdfReader(str(file_path))
    pages: list[Page] = []
    for page_number, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        pages.append(Page(page_number=page_number, text=text.replace("\x00", "")))
    return pages
