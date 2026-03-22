"""Document parsing for Canopy — extracts text from PDF, DOCX, and Markdown."""

import io
from pathlib import Path

MAX_CONTENT_CHARS = 100_000


def parse_document(file_bytes: bytes, filename: str) -> dict:
    """Parse a document and return extracted text.

    Returns:
        {filename, content, token_estimate}
    """
    ext = Path(filename).suffix.lower()

    if ext in (".md", ".txt", ".text", ".rst"):
        content = file_bytes.decode("utf-8", errors="replace")
    elif ext == ".pdf":
        content = _parse_pdf(file_bytes)
    elif ext == ".docx":
        content = _parse_docx(file_bytes)
    else:
        content = _parse_fallback(file_bytes, filename)

    # Truncate very large documents
    truncated = False
    if len(content) > MAX_CONTENT_CHARS:
        content = content[:MAX_CONTENT_CHARS]
        truncated = True

    token_estimate = len(content) // 4

    result = {
        "filename": filename,
        "content": content,
        "token_estimate": token_estimate,
    }
    if truncated:
        result["truncated"] = True
        result["content"] += f"\n\n[Document truncated at {MAX_CONTENT_CHARS:,} characters]"

    return result


def _parse_pdf(file_bytes: bytes) -> str:
    """Extract text from PDF using pdfplumber."""
    import pdfplumber

    pages = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                pages.append(text)
    return "\n\n".join(pages)


def _parse_docx(file_bytes: bytes) -> str:
    """Extract text from DOCX using docx2python."""
    from docx2python import docx2python

    result = docx2python(io.BytesIO(file_bytes))
    # docx2python returns nested lists: [[[paragraphs]]]
    # Flatten to text
    lines = []
    for table in result.body:
        for row in table:
            for cell in row:
                for paragraph in cell:
                    if paragraph.strip():
                        lines.append(paragraph.strip())
    return "\n\n".join(lines)


def _parse_fallback(file_bytes: bytes, filename: str) -> str:
    """Try markitdown as a fallback for other formats."""
    try:
        from markitdown import MarkItDown

        mid = MarkItDown()
        result = mid.convert_stream(io.BytesIO(file_bytes), file_extension=Path(filename).suffix)
        return result.text_content
    except Exception:
        # Last resort: try decoding as text
        return file_bytes.decode("utf-8", errors="replace")
