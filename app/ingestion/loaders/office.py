import logfire


def parse_office(file_path: str) -> str:
    """Parse .docx / .pptx locally with python-docx / python-pptx (no heavy `unstructured` dependency)."""
    with logfire.span("Office parsing", filename=file_path):
        ext = file_path.lower().rsplit(".", 1)[-1]
        if ext == "docx":
            from docx import Document

            d = Document(file_path)
            parts = [p.text for p in d.paragraphs if p.text.strip()]
            for t in d.tables:
                for row in t.rows:
                    parts.append(" | ".join(c.text.strip() for c in row.cells))
            return "\n\n".join(parts)
        if ext == "pptx":
            from pptx import Presentation

            parts = []
            for i, slide in enumerate(Presentation(file_path).slides, 1):
                texts = [sh.text_frame.text for sh in slide.shapes if sh.has_text_frame and sh.text_frame.text.strip()]
                if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
                    texts.append(slide.notes_slide.notes_text_frame.text)
                if texts:
                    parts.append(f"[Slide {i}]\n" + "\n".join(texts))
            return "\n\n".join(parts)
        raise ValueError(f"unsupported office type: {ext}")
