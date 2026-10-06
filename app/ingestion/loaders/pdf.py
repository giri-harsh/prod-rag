import logfire
from pypdf import PdfReader


def parse_pdf(file_path: str) -> str:
    """
    Extract text from a PDF locally using pypdf.
    Falls back to pdfplumber for pages that yield no text (e.g. image-heavy pages).
    """
    with logfire.span("PDF Parsing (local)", filename=file_path):
        try:
            reader = PdfReader(file_path)
            total_pages = len(reader.pages)
            logfire.info(f"PDF has {total_pages} pages.")

            text_parts: list[str] = []
            blank_pages: list[int] = []

            for i, page in enumerate(reader.pages):
                text = page.extract_text() or ""
                if text.strip():
                    text_parts.append(text)
                else:
                    blank_pages.append(i + 1)

            # Fallback: use pdfplumber for any pages pypdf returned blank
            if blank_pages:
                logfire.info(f"pypdf returned blank on pages {blank_pages} — retrying with pdfplumber.")
                try:
                    import pdfplumber
                    with pdfplumber.open(file_path) as pdf:
                        for page_num in blank_pages:
                            page = pdf.pages[page_num - 1]
                            fallback_text = page.extract_text() or ""
                            if fallback_text.strip():
                                text_parts.append(fallback_text)
                except Exception as plumber_err:
                    logfire.warning(f"pdfplumber fallback failed: {plumber_err}")

            full_text = "\n".join(text_parts)

            if not full_text.strip():
                logfire.warning(f"No text extracted from {file_path}. File may be fully image-based.")
            else:
                logfire.info(f"Extracted {len(full_text)} characters from {file_path}.")

            return full_text

        except Exception as e:
            logfire.error(f"PDF Parse Failed for {file_path}: {e}")
            raise


def parse_pdf_pages(file_path: str) -> list[tuple[int, str]]:
    """Per-page text [(page_number, text)] using pypdf, falling back to pdfplumber for blank pages."""
    with logfire.span("PDF page parsing", filename=file_path):
        reader = PdfReader(file_path)
        pages = [(i + 1, (pg.extract_text() or "")) for i, pg in enumerate(reader.pages)]
        blank = [n for n, t in pages if not t.strip()]
        if blank:
            try:
                import pdfplumber

                with pdfplumber.open(file_path) as pdf:
                    for n in blank:
                        pages[n - 1] = (n, pdf.pages[n - 1].extract_text() or "")
            except Exception as e:
                logfire.warn("pdfplumber fallback failed", error=str(e))
        return [(n, t) for n, t in pages if t.strip()]
