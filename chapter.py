import os
import re
import fitz  # PyMuPDF: pip install PyMuPDF
from collections import Counter


def escape_prh_entities(text: str) -> str:
    """Applies PRH punctuation, smart quotes, dashes, and entity escaping."""
    if not text:
        return ""

    # XML Escaping
    text = (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )

    # Convert standard double/single quotes to smart curly quotes
    text = re.sub(r'(^|\s)"', r"\1“", text)
    text = re.sub(r'"', r"”", text)
    text = re.sub(r"(^|\s)'", r"\1‘", text)
    text = re.sub(r"'", r"’", text)

    # Handle Colliding Quotes (insert thin space &#x2009;)
    text = text.replace("’,”", "’&#x2009;”")
    text = text.replace("‘“", "“&#x2009;‘")
    text = text.replace("’“", "“&#x2009;’")

    # Fix Dashes & Ellipses
    text = text.replace("--", "&#x2014;")
    text = text.replace("—", "&#x2014;")
    text = text.replace("–", "&#x2013;")
    text = text.replace(". . .", "&#x2026;")
    text = text.replace("…", "&#x2026;")

    return text.strip()


def parse_metadata_header(text_block: str) -> dict:
    """Parses the mandatory PRH Intake Gateway header: [MATTER:... | GENRE:... | ISBN:... | TITLE:... | PARTNER:...]"""
    meta = {}
    match = re.search(
        r"\[MATTER:\s*(.*?)\s*\|\s*GENRE:\s*(.*?)\s*\|\s*ISBN:\s*(.*?)\s*\|\s*TITLE:\s*(.*?)\s*\|\s*PARTNER:\s*(.*?)\]",
        text_block,
        re.IGNORECASE,
    )
    if match:
        meta["matter"] = match.group(1).strip()
        meta["genre"] = match.group(2).strip()
        meta["isbn"] = match.group(3).strip()
        meta["title"] = match.group(4).strip()
        meta["partner"] = match.group(5).strip().lower()
    return meta


def detect_page_label(page, page_num: int) -> str:
    """Dynamically finds Roman or Arabic page numbers located in header/footer zones."""
    height = page.rect.height
    page_dict = page.get_text("dict")
    candidates = []

    for b in page_dict.get("blocks", []):
        if b.get("type") == 0:
            y0, y1 = b["bbox"][1], b["bbox"][3]
            # Header zone (< 12%) or footer zone (> 88%)
            if y1 < height * 0.12 or y0 > height * 0.88:
                for line in b.get("lines", []):
                    for span in line.get("spans", []):
                        txt = span.get("text", "").strip(" .[]()-")
                        if txt:
                            candidates.append(txt)

    roman_regex = r"^(?=[mdclxvi]+$)m*(c[md]|d?c*)(x[cl]|l?x*)(i[xv]|v?i*)$"
    for cand in candidates:
        if re.match(roman_regex, cand.lower()):
            return cand.lower()
        if cand.isdigit() and len(cand) <= 4:
            return cand

    return str(page_num)


def extract_paragraphs_from_page(page, body_size: float, page_label: str):
    """
    Groups line fragments into proper book paragraphs based on indentations,
    line spacing (vertical gap), and block boundaries, while preserving inline
    bold, italic, and superscripts.
    """
    page_dict = page.get_text("dict")
    raw_blocks = [b for b in page_dict.get("blocks", []) if b.get("type") == 0]
    paragraphs = []

    for b in raw_blocks:
        lines = b.get("lines", [])
        if not lines:
            continue

        # Determine paragraph start based on indentation (x0) and line spacing (y gaps)
        # Normal lines in this block:
        line_data = []
        for line in lines:
            spans = line.get("spans", [])
            if not spans:
                continue

            full_line_text = "".join(s.get("text", "") for s in spans).strip()
            if not full_line_text:
                continue

            # Skip header/footer line matching page label
            if full_line_text.lower() == page_label.lower():
                continue
            # Running header filter
            if len(full_line_text) > 30 and full_line_text.isupper() and len(spans) == 1:
                continue

            # Build line text with inline styling preserved
            formatted_spans = []
            span_sizes = []
            for s in spans:
                stext = s.get("text", "")
                if not stext:
                    continue

                font = s.get("font", "").lower()
                flags = s.get("flags", 0)
                sz = s.get("size", body_size)
                span_sizes.append(sz)

                is_italic = bool(flags & 2) or "italic" in font or "oblique" in font
                is_bold = bool(flags & 16) or "bold" in font or "black" in font
                is_super = bool(flags & 1)

                clean_span = escape_prh_entities(stext)
                if not clean_span:
                    continue

                if is_super and clean_span.isdigit():
                    clean_span = f'<a id="note_backlink_{clean_span}" class="enref" href="notes.xhtml#note_{clean_span}">{clean_span}</a>'
                else:
                    if is_bold:
                        clean_span = f"<b>{clean_span}</b>"
                    if is_italic:
                        clean_span = f"<i>{clean_span}</i>"

                formatted_spans.append(clean_span)

            if formatted_spans:
                line_data.append({
                    "bbox": line["bbox"],
                    "text": " ".join(formatted_spans),
                    "plain": full_line_text,
                    "avg_size": sum(span_sizes) / max(len(span_sizes), 1)
                })

        if not line_data:
            continue

        # Group lines within this block into paragraphs
        # In printed books, a new paragraph has an indent (x0 > baseline) OR a larger vertical gap
        block_x0_min = min(l["bbox"][0] for l in line_data)
        
        current_para_lines = []
        current_para_sizes = []
        prev_y1 = None
        prev_line_h = None

        for idx, l in enumerate(line_data):
            x0, y0, x1, y1 = l["bbox"]
            line_h = y1 - y0
            is_indented = (x0 - block_x0_min) > 8.0  # Indented start of paragraph (like in the image)
            has_gap = prev_y1 is not None and prev_line_h is not None and (y0 - prev_y1) > (prev_line_h * 0.4)

            # If there's an indent or large gap (and we already have lines), commit current paragraph
            if current_para_lines and (is_indented or has_gap):
                joined_text = " ".join(current_para_lines)
                avg_sz = sum(current_para_sizes) / max(len(current_para_sizes), 1)
                paragraphs.append({
                    "text": joined_text,
                    "avg_size": avg_sz,
                    "indented": True
                })
                current_para_lines = [l["text"]]
                current_para_sizes = [l["avg_size"]]
            else:
                current_para_lines.append(l["text"])
                current_para_sizes.append(l["avg_size"])

            prev_y1 = y1
            prev_line_h = line_h

        if current_para_lines:
            joined_text = " ".join(current_para_lines)
            avg_sz = sum(current_para_sizes) / max(len(current_para_sizes), 1)
            paragraphs.append({
                "text": joined_text,
                "avg_size": avg_sz,
                "indented": False
            })

    return paragraphs


def chapter_pdf_to_prh_xhtml(pdf_path: str, metadata_string: str, output_xhtml_path: str):
    """Converts continuous chapter PDF to valid PRH EPUB 3 / XHTML Strict."""
    meta = parse_metadata_header(metadata_string)
    if not meta or "isbn" not in meta:
        print(
            "> **PRH CONVERSION GATEWAY — METADATA REQUIRED:**\n"
            "Please specify the matter type and book metadata before conversion:\n"
            "[MATTER: Front Matter | Body Matter | Back Matter | GENRE: Fiction | Nonfiction | ISBN: 978XXXXXXXXXX | TITLE: Book Title | PARTNER: xx]"
        )
        return

    isbn = meta["isbn"]
    book_title = meta.get("title", "Chapter")
    doc = fitz.open(pdf_path)

    # 1. Typography Analysis: Calculate Dominant Body Font Size
    font_sizes = []
    for page in doc:
        for b in page.get_text("dict").get("blocks", []):
            if "lines" in b:
                for line in b["lines"]:
                    for span in line.get("spans", []):
                        t = span.get("text", "").strip()
                        if len(t) > 3:
                            font_sizes.append(round(span.get("size", 10.0), 1))

    body_size = Counter(font_sizes).most_common(1)[0][0] if font_sizes else 10.0
    h1_thresh = body_size * 1.45
    h2_thresh = body_size * 1.20

    # 2. XHTML Skeleton conforming to PRH Spec 8.0
    xhtml_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE html>',
        '<html lang="en-US" xml:lang="en-US" xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">',
        "<head>",
        f"<title>Chapter, {escape_prh_entities(book_title)}</title>",
        '<link rel="stylesheet" type="text/css" href="../css/prh_standard.css"/>',
        f'<link rel="stylesheet" type="text/css" href="../css/{isbn}_style.css"/>',
        '<meta charset="UTF-8" />',
        "</head>",
        '<body lang="en-US" xml:lang="en-US">',
        '<div class="page_top_padding">',  # STRICT: Top-level <body> child MUST be a <div>
    ]

    is_first_body_para = True

    for page_num, page in enumerate(doc):
        current_page = page_num + 1
        page_label = detect_page_label(page, current_page)
        pagebreak_marker = f'<span id="page_{page_label.lower()}" epub:type="pagebreak" role="doc-pagebreak" title="{page_label}"/>'

        paragraphs = extract_paragraphs_from_page(page, body_size, page_label)
        page_has_content = False

        for p in paragraphs:
            ptxt = p["text"]
            avg_size = p["avg_size"]

            if not ptxt:
                continue

            prefix = pagebreak_marker if not page_has_content else ""
            page_has_content = True

            # Headings
            plain_test = re.sub(r"<[^>]+>", "", ptxt).strip()
            if avg_size >= h1_thresh and len(plain_test) < 80:
                clean_h = plain_test.title()
                xhtml_lines.append(f"<h1>{prefix}{clean_h}</h1>")
            elif avg_size >= h2_thresh and len(plain_test) < 80:
                xhtml_lines.append(f'<h2 class="chaptitle">{prefix}{ptxt}</h2>')
            # Dialogue / Script Tagging (e.g., "SPEAKER: dialogue")
            elif re.match(r"^[A-Z\s\.]+:\s", plain_test):
                parts = ptxt.split(":", 1)
                xhtml_lines.append(f'<p class="indent">{prefix}<b>{parts[0]}:</b>{parts[1]}</p>')
            # Bullet list items
            elif plain_test.startswith("•") or plain_test.startswith("o "):
                list_txt = plain_test.lstrip("•o ").strip()
                xhtml_lines.append(f'<ul class="list-none"><li><p>{prefix}{list_txt}</p></li></ul>')
            else:
                # Standard Body Paragraphs
                # PRH Spec: First paragraph after chapter title gets dropcaps / noindent
                if is_first_body_para:
                    xhtml_lines.append(f'<p class="dropcaps2line">{prefix}{ptxt}</p>')
                    is_first_body_para = False
                else:
                    xhtml_lines.append(f'<p class="indent">{prefix}{ptxt}</p>')

        if not page_has_content:
            xhtml_lines.append(f"<p>{pagebreak_marker}</p>")

    xhtml_lines.extend(["</div>", "</body>", "</html>"])

    final_output = "\n".join(xhtml_lines)

    with open(output_xhtml_path, "w", encoding="utf-8") as f:
        f.write(final_output)

    print(f"[SUCCESS] XHTML compiled and saved to: {output_xhtml_path}")


# --- EXAMPLE TRIGGER ---
if __name__ == "__main__":
    header = "[MATTER: Body Matter | GENRE: Fiction | ISBN: 9780307885647 | TITLE: Frankenstein | PARTNER: xx]"
    # chapter_pdf_to_prh_xhtml("sample.pdf", header, "output.xhtml")
