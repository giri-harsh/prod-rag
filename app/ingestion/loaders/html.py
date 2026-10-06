import logfire
from bs4 import BeautifulSoup


def parse_html(file_path: str):
    """
    Parses HTML content using BeautifulSoup.
    Cleans scripts, styles, and extracts readable text for RAG.
    """
    with logfire.span("📄 HTML Parsing", filename=file_path):
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            
            soup = BeautifulSoup(content, "html.parser")
            
            # 1. Remove Junk (Scripts, Styles, Metadata)
            for script in soup(["script", "style", "meta", "noscript"]):
                script.decompose()
                
            # 1b. Preserve heading structure as markdown so the chunker can build heading paths
            for level in range(1, 7):
                for h in soup.find_all(f"h{level}"):
                    h.replace_with(f"\n\n{'#' * level} {h.get_text(' ', strip=True)}\n\n")

            # 2. Extract Text
            text = soup.get_text(separator="\n")
            
            # 3. Clean Whitespace (Collapse multiple newlines)
            out, blank = [], False
            for line in (ln.strip() for ln in text.splitlines()):  # keep single blank lines as paragraph breaks
                if line:
                    out.append(line)
                    blank = False
                elif not blank:
                    out.append("")
                    blank = True
            text_clean = "\n".join(out).strip()

            return text_clean
        except Exception as e:
            logfire.error(f"❌ HTML Parse Failed: {e}")
            raise e
