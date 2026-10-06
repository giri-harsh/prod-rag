import logfire


def parse_office(file_path: str) -> str:
    """Parse .docx / .pptx locally with python-docx / python-pptx. Headings become markdown headings."""
    with logfire.span("Office parsing", filename=file_path):
        ext = file_path.lower().rsplit(".", 1)[-1]
        if ext == "docx":
            from docx import Document

            d = Document(file_path)
            parts = []
            for p in d.paragraphs:
                if not p.text.strip():
                    continue
                st = (p.style.name or "") if p.style is not None else ""
                last = st.split()[-1] if st else ""
                if st.lower().startswith("heading") and last.isdigit():
                    parts.append(f"{'#' * min(6, int(last))} {p.text.strip()}")
                elif st.lower() == "title":
                    parts.append(f"# {p.text.strip()}")
                else:
                    parts.append(p.text)
            for t in d.tables:
                for row in t.rows:
                    parts.append(" | ".join(c.text.strip() for c in row.cells))
            return "\n\n".join(parts)
        if ext == "pptx":
            from pptx import Presentation

            parts = []
            for i, slide in enumerate(Presentation(file_path).slides, 1):
                title = slide.shapes.title.text_frame.text.strip() if slide.shapes.title is not None else ""
                texts = [
                    sh.text_frame.text for sh in slide.shapes
                    if sh.has_text_frame and sh.text_frame.text.strip() and sh.text_frame.text.strip() != title
                ]
                if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
                    texts.append(slide.notes_slide.notes_text_frame.text)
                if texts or title:
                    head = f"# {title}" if title else f"# Slide {i}"
                    parts.append(head + "\n\n" + "\n".join(texts))
            return "\n\n".join(parts)
        raise ValueError(f"unsupported office type: {ext}")
