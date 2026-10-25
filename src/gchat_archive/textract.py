#!/usr/bin/env python3
"""Extract plain text from downloaded attachments.

    python -m gchat_archive.textract attachments.jsonl attachments_text.jsonl

Parsers come from the optional ``[text]`` extra (pypdf, python-docx, openpyxl,
beautifulsoup4). Without them the archive still syncs and searches message text;
only file contents are skipped. macOS ``textutil`` covers .doc/.rtf for free.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

MISSING: set[str] = set()

MAX_CHARS = 400_000


def textutil(path: Path) -> str:
    """macOS only; other platforms simply return no text for .doc/.rtf."""
    if sys.platform != "darwin":
        return ""
    try:
        out = subprocess.run(
            ["/usr/bin/textutil", "-convert", "txt", "-stdout", str(path)],
            capture_output=True, timeout=90,
        )
        return out.stdout.decode("utf-8", errors="replace")
    except (subprocess.SubprocessError, OSError):
        return ""


def read_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        reader = PdfReader(str(path))
        chunks = []
        for page in reader.pages:
            chunks.append(page.extract_text() or "")
        return "\n".join(chunks)
    except Exception:
        return ""


def read_docx(path: Path) -> str:
    try:
        import docx
    except ImportError:
        return textutil(path)
    try:
        document = docx.Document(str(path))
        parts = [p.text for p in document.paragraphs]
        for table in document.tables:
            for row in table.rows:
                parts.append(" | ".join(cell.text.strip() for cell in row.cells))
        return "\n".join(parts)
    except Exception:
        return ""


def read_xlsx(path: Path) -> str:
    try:
        from openpyxl import load_workbook
    except ImportError:
        return ""
    try:
        wb = load_workbook(str(path), read_only=True, data_only=True)
        parts = []
        for sheet in wb.worksheets:
            parts.append("# %s" % sheet.title)
            for row in sheet.iter_rows(values_only=True):
                cells = [str(c) for c in row if c is not None]
                if cells:
                    parts.append(" | ".join(cells))
        wb.close()
        return "\n".join(parts)
    except Exception:
        return ""


def read_html(path: Path) -> str:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return path.read_text(errors="replace")
    try:
        soup = BeautifulSoup(path.read_text(errors="replace"), "html.parser")
        return soup.get_text(" ", strip=True)
    except Exception:
        return ""


def read_text(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


HANDLERS = {
    "pdf": read_pdf,
    "docx": read_docx,
    "xlsx": read_xlsx,
    "xlsm": read_xlsx,
    "html": read_html,
    "htm": read_html,
    "doc": textutil,
    "rtf": textutil,
    "csv": read_text,
    "txt": read_text,
    "md": read_text,
    "json": read_text,
}


def run(manifest: Path, out_path: Path) -> None:

    # Incremental: keep what earlier runs produced and only extract new files,
    # so the daily sync does not re-parse hundreds of PDFs.
    seen: set[str] = set()
    if out_path.exists():
        with out_path.open() as prev:
            for line in prev:
                if not line.strip():
                    continue
                try:
                    key = json.loads(line).get("key")
                except json.JSONDecodeError:
                    continue
                if key:
                    seen.add(key)

    done = 0
    written = 0
    with out_path.open("a") as out:
        for line in manifest.read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            key = rec.get("key")
            if not key or key in seen or rec.get("error") or not rec.get("path"):
                continue
            seen.add(key)
            path = Path(rec["path"])
            if not path.exists():
                continue
            handler = HANDLERS.get(path.suffix.lstrip(".").lower())
            text = handler(path) if handler else ""
            text = " ".join(text.split())[:MAX_CHARS]
            done += 1
            if not text:
                continue
            written += 1
            out.write(
                json.dumps(
                    {
                        "key": key,
                        "message_id": rec.get("message_id"),
                        "space_id": rec.get("space_id"),
                        "name": rec.get("name"),
                        "content_type": rec.get("content_type"),
                        "chars": len(text),
                        "text": text,
                    }
                )
                + "\n"
            )
    print("extracted %d files, %d with text -> %s" % (done, written, out_path))


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    run(Path(sys.argv[1]), Path(sys.argv[2]))


if __name__ == "__main__":
    main()
