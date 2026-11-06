import os
import re
import fitz  # PyMuPDF
from collections import Counter
from flask import Flask, request, render_template, jsonify

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 150 * 1024 * 1024  # 150 MB max PDF size


# ----------------------------------------------------------------------
# 1. TEXT SANITIZATION & XML ESCAPING (PRH SPEC 8.0 STRICT)
# ----------------------------------------------------------------------
def escape_prh_entities(text: str) -> str:
    """Encodes characters to standard PRH EPUB 3 entity format & applies typographic rules."""
    if not text:
        return ""

    # Basic XML Entity Escaping
    table = [
        ("&", "&amp;"),
        ("<", "&lt;"),
        (">", "&gt;"),
        ("—", "&#x2014;"),
        ("–", "&#x2013;"),
        ("…", "&#x2026;"),
        ("•", "&#x2022;"),
        ("™", "&#x2122;"),
        ("©", "&#x00A9;"),
        (" ", "&#x2009;"),
        ("\u2003", "&#x2003;"),
        ("\u00A0", "&#x00A0;"),
    ]
    for orig, rep in table:
        text = text.replace(orig, rep)

    # Convert straight quotes to smart curly quotes
    text = re.sub(r'(^|\s)"', r"\1“", text)
    text = re.sub(r'"', r"”", text)
    text = re.sub(r"(^|\s)'", r"\1‘", text)
    text = re.sub(r"'", r"’", text)

    # PRH Spec: Colliding quotes with thin space separation
    text = text.replace("’,”", "’&#x2009;”")
    text = text.replace("‘“", "“&#x2009;‘")
    text = text.replace("’“", "“&#x2009;’")
    text = text.replace("--", "&#x2014;")
    text = text.replace(". . .", "&#x2026;")

    return text.strip()


def build_xhtml_shell(section_title: str, book_title: str, isbn: str, body_inner: str) -> str:
    """Wraps body markup into PRH EPUB 3 / XHTML Strict skeleton."""
    safe_section = escape_prh_entities(section_title or "Chapter")
    safe_title = escape_prh_entities(book_title or "Book Title")
    safe_isbn = isbn.strip() if isbn else "9780000000000"

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html lang="en-US" xml:lang="en-US" xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head>
<title>{safe_section}, {safe_title}</title>
<link rel="stylesheet" type="text/css" href="../css/prh_standard.css"/>
<link rel="stylesheet" type="text/css" href="../css/{safe_isbn}_style.css"/>
<meta charset="UTF-8" />
</head>
<body lang="en-US" xml:lang="en-US">
<div class="page_top_padding">
{body_inner}
</div>
</body>
</html>"""


# ----------------------------------------------------------------------
# 2. SHARED UNIFIED PARAGRAPH & TYPOGRAPHY EXTRACTION ENGINE
# ----------------------------------------------------------------------
class UnifiedBookExtractor:
    @staticmethod
    def detect_page_label(page, page_num: int) -> str:
        """Finds Roman or Arabic page numbers located strictly in header/footer zones."""
        height = page.rect.height
        page_dict = page.get_text("dict")
        candidates = []

        for b in page_dict.get("blocks", []):
            if b.get("type") == 0:
                y0, y1 = b["bbox"][1], b["bbox"][3]
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

    @staticmethod
    def calc_dominant_font_size(doc, start_pno: int = 0, end_pno: int = None) -> float:
        """Calculates global body font size for heading hierarchy thresholds."""
        if end_pno is None or end_pno > len(doc):
            end_pno = len(doc)
        font_sizes = []
        for pno in range(start_pno, end_pno):
            for b in doc[pno].get_text("dict").get("blocks", []):
                if "lines" in b:
                    for line in b["lines"]:
                        for span in line.get("spans", []):
                            t = span.get("text", "").strip()
                            if len(t) > 3:
                                font_sizes.append(round(span.get("size", 10.0), 1))
        return Counter(font_sizes).most_common(1)[0][0] if font_sizes else 10.0

    @staticmethod
    def extract_structured_paragraphs(page, body_size: float, page_label: str) -> list:
        """
        Unified extraction engine (from chapter.py):
        1. Preserves inline bold, italics, superscripts on each span.
        2. Filters out running headers and repeating page labels.
        3. Groups multi-line text into complete paragraphs based on indentations & vertical gaps.
        4. Detects quadrant geometry (centered text, bottom-right signature blocks).
        """
        page_dict = page.get_text("dict")
        width = page.rect.width
        height = page.rect.height
        raw_blocks = [b for b in page_dict.get("blocks", []) if b.get("type") == 0]
        extracted_paragraphs = []

        for b in raw_blocks:
            lines = b.get("lines", [])
            if not lines:
                continue

            bx0, by0, bx1, by1 = b.get("bbox", (0, 0, 0, 0))
            block_mid_x = (bx0 + bx1) / 2.0
            is_centered_block = abs(block_mid_x - (width / 2.0)) < (width * 0.08)
            is_bottom_right = (bx0 > width * 0.45) and (by0 > height * 0.65)

            line_data = []
            for line in lines:
                spans = line.get("spans", [])
                if not spans:
                    continue

                full_line = "".join(s.get("text", "") for s in spans).strip()
                if not full_line or full_line.lower() == page_label.lower():
                    continue
                # Purge running headers
                if len(full_line) > 30 and full_line.isupper() and len(spans) == 1:
                    continue

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

                    clean = escape_prh_entities(stext)
                    if not clean:
                        continue

                    if is_super and clean.isdigit():
                        clean = f'<a id="note_backlink_{clean}" class="enref" href="notes.xhtml#note_{clean}">{clean}</a>'
                    else:
                        if is_bold:
                            clean = f"<b>{clean}</b>"
                        if is_italic:
                            clean = f"<i>{clean}</i>"
                    formatted_spans.append(clean)

                if formatted_spans:
                    line_data.append({
                        "bbox": line["bbox"],
                        "text": " ".join(formatted_spans),
                        "plain": full_line,
                        "avg_size": sum(span_sizes) / max(len(span_sizes), 1)
                    })

            if not line_data:
                continue

            # Reconstruct lines into paragraphs using indentation & line height gaps
            block_x0_min = min(l["bbox"][0] for l in line_data)
            current_para_lines = []
            current_para_sizes = []
            prev_y1 = None
            prev_line_h = None

            for l in line_data:
                x0, y0, x1, y1 = l["bbox"]
                line_h = y1 - y0
                is_indented = (x0 - block_x0_min) > 8.0
                has_gap = prev_y1 is not None and prev_line_h is not None and (y0 - prev_y1) > (prev_line_h * 0.4)

                if current_para_lines and (is_indented or has_gap):
                    joined = " ".join(current_para_lines)
                    avg_sz = sum(current_para_sizes) / max(len(current_para_sizes), 1)
                    extracted_paragraphs.append({
                        "text": joined,
                        "plain": re.sub(r"<[^>]+>", "", joined).strip(),
                        "avg_size": avg_sz,
                        "is_centered": is_centered_block,
                        "is_bottom_right": is_bottom_right,
                        "raw_lines": current_para_lines
                    })
                    current_para_lines = [l["text"]]
                    current_para_sizes = [l["avg_size"]]
                else:
                    current_para_lines.append(l["text"])
                    current_para_sizes.append(l["avg_size"])

                prev_y1 = y1
                prev_line_h = line_h

            if current_para_lines:
                joined = " ".join(current_para_lines)
                avg_sz = sum(current_para_sizes) / max(len(current_para_sizes), 1)
                extracted_paragraphs.append({
                    "text": joined,
                    "plain": re.sub(r"<[^>]+>", "", joined).strip(),
                    "avg_size": avg_sz,
                    "is_centered": is_centered_block,
                    "is_bottom_right": is_bottom_right,
                    "raw_lines": current_para_lines
                })

        return extracted_paragraphs


# ----------------------------------------------------------------------
# 3. CONVERSION ROUTINES (ALL POWERED BY UNIFIED ENGINE)
# ----------------------------------------------------------------------
def convert_chapter_pages(doc, book_title: str, isbn: str, start_page: int = 1, end_page: int = None) -> tuple:
    """Converts Body Matter / Chapters (continuous multi-page flow)."""
    if end_page is None or end_page > len(doc):
        end_page = len(doc)

    body_size = UnifiedBookExtractor.calc_dominant_font_size(doc, start_page - 1, end_page)
    h1_thresh = body_size * 1.45
    h2_thresh = body_size * 1.20

    body_lines = []
    detected_section_title = "Chapter"
    first_title_found = False
    is_first_body_para = True

    for pno in range(start_page - 1, end_page):
        page = doc[pno]
        label = UnifiedBookExtractor.detect_page_label(page, pno + 1)
        pagebreak_marker = f'<span id="page_{label.lower()}" epub:type="pagebreak" role="doc-pagebreak" title="{label}"/>'
        page_has_content = False

        paragraphs = UnifiedBookExtractor.extract_structured_paragraphs(page, body_size, label)

        for p in paragraphs:
            joined = p["text"]
            plain_t = p["plain"]
            avg_sz = p["avg_size"]

            prefix = pagebreak_marker if not page_has_content else ""
            page_has_content = True

            if avg_sz >= h1_thresh and len(plain_t) < 80:
                clean_head = plain_t.title()
                body_lines.append(f"<h1>{prefix}{clean_head}</h1>")
                if not first_title_found:
                    detected_section_title = clean_head
                    first_title_found = True
            elif avg_sz >= h2_thresh and len(plain_t) < 80:
                body_lines.append(f'<h2 class="chaptitle">{prefix}{joined}</h2>')
                if not first_title_found:
                    detected_section_title = plain_t
                    first_title_found = True
            elif re.match(r"^[A-Z\s\.]+:\s", plain_t):
                parts = joined.split(":", 1)
                body_lines.append(f'<p class="indent">{prefix}<b>{parts[0]}:</b>{parts[1]}</p>')
            elif plain_t.startswith("•") or plain_t.startswith("o "):
                list_txt = plain_t.lstrip("•o ").strip()
                body_lines.append(f'<ul class="list-none"><li><p>{prefix}{list_txt}</p></li></ul>')
            elif is_first_body_para:
                body_lines.append(f'<p class="dropcaps2line">{prefix}{joined}</p>')
                is_first_body_para = False
            else:
                body_lines.append(f'<p class="indent">{prefix}{joined}</p>')

        if not page_has_content:
            body_lines.append(f"<p>{pagebreak_marker}</p>")

    return detected_section_title, "\n".join(body_lines)


def convert_front_matter_page(doc, page_num: int, matter_type: str, book_title: str, isbn: str) -> tuple:
    """Converts Front Matter pages using the unified extraction engine."""
    page_idx = max(0, min(page_num - 1, len(doc) - 1))
    page = doc[page_idx]
    body_size = UnifiedBookExtractor.calc_dominant_font_size(doc, page_idx, page_idx + 1)
    label = UnifiedBookExtractor.detect_page_label(page, page_idx + 1)
    pagebreak = f'<span id="page_{label.lower()}" epub:type="pagebreak" role="doc-pagebreak" title="{label}"/>'
    
    paragraphs = UnifiedBookExtractor.extract_structured_paragraphs(page, body_size, label)
    section_title = matter_type

    if matter_type in ["Cover / Full Page Art", "Title Page"]:
        img_suffix = "cover" if "Cover" in matter_type else "title_page"
        body = (
            f'<div class="height_100">\n'
            f'  {pagebreak}\n'
            f'  <img class="fill_height" src="../images/{isbn}_{img_suffix}.jpg" alt="{escape_prh_entities(book_title)}"/>\n'
            f'</div>'
        )
        return section_title, body

    elif matter_type == "Copyright":
        section_title = f"Copyright, {book_title}"
        tags = [pagebreak]
        for p in paragraphs:
            t = p["text"]
            t = re.sub(r"(ISBN[:\s]+)([\d\-]+)", r"Trade ISBN&#x2003;\2", t, flags=re.IGNORECASE)
            cls = "crt spaceabove" if any(w in t.lower() for w in ["published", "copyright", "isbn", "all rights"]) else "crt"
            tags.append(f'<p class="{cls}">{t}</p>')
        tags.append('<p id="release_identifier_line" class="spaceabove">amn_prh_8.0_c0_r0</p>')
        return section_title, "\n".join(tags)

    elif matter_type == "Contents / TOC":
        section_title = f"Contents, {book_title}"
        toc_lines = []
        for p in paragraphs:
            t = p["text"]
            plain = p["plain"]
            if not t or ("contents" in plain.lower() and len(plain) < 15):
                continue
            m = re.match(r"^(.*?)(?:\.{2,}|\s{2,}|\s+)(\d+|[ivxlcdm]+)$", plain.strip(), re.IGNORECASE)
            if m:
                ch_title, pnum = m.groups()
                toc_lines.append(f'<p class="toc_chap"><a href="ch{pnum}.xhtml"><b>{ch_title.strip()}</b></a></p>')
            else:
                toc_lines.append(f'<p class="toc_chap">{t}</p>')

        body = (
            f'<div role="doc-toc">\n'
            f'<h1 class="Chapter-Titled2">{pagebreak}<span class="bb">Contents</span></h1>\n'
            f'<div class="toc_chap">\n' + "\n".join(toc_lines) + '\n</div>\n</div>'
        )
        return section_title, body

    elif matter_type == "Dedication / Epigraph":
        section_title = f"Dedication, {book_title}"
        paras = [pagebreak]
        for p in paragraphs:
            t = p["text"]
            if p["is_centered"]:
                paras.append(f'<p class="center spaceabove">{t}</p>')
            else:
                paras.append(f'<p class="spaceabove">{t}</p>')
        return section_title, "\n".join(paras)

    else:  # Half Title / Introduction / Foreword
        section_title = matter_type
        rendered = [pagebreak]
        is_first_para = True
        for p in paragraphs:
            t = p["text"]
            plain = p["plain"]
            sz = p["avg_size"]
            if sz > body_size * 1.35 and len(plain) < 60:
                clean_h = plain.title()
                rendered.append(f'<h1>{clean_h}</h1>')
            elif is_first_para:
                rendered.append(f'<p class="dropcaps2line">{t}</p>')
                is_first_para = False
            else:
                rendered.append(f'<p class="indent">{t}</p>')
        return section_title, "\n".join(rendered)


def convert_back_matter_page(doc, page_num: int, matter_type: str, book_title: str, isbn: str) -> tuple:
    """Converts Back Matter pages using the unified extraction engine."""
    page_idx = max(0, min(page_num - 1, len(doc) - 1))
    page = doc[page_idx]
    body_size = UnifiedBookExtractor.calc_dominant_font_size(doc, page_idx, page_idx + 1)
    label = UnifiedBookExtractor.detect_page_label(page, page_idx + 1)
    pagebreak = f'<span id="page_{label.lower()}" epub:type="pagebreak" role="doc-pagebreak" title="{label}"/>'
    
    paragraphs = UnifiedBookExtractor.extract_structured_paragraphs(page, body_size, label)
    section_title = matter_type

    if matter_type == "Photo Gallery / Cover":
        body = (
            f'{pagebreak}\n'
            f'<div class="height_100">\n'
            f'  <img class="fill_height" src="../images/{isbn}_p{label}.jpg" alt="{escape_prh_entities(book_title)}"/>\n'
            f'</div>'
        )
        return "Illustrations", body

    elif matter_type in ["Bibliography / Further Reading", "Numbered List (<ol>)"]:
        all_text = "\n".join(p["plain"] for p in paragraphs)
        numbered_items = re.findall(r"(?:^|\n)\s*(\d+)[\.\)]\s+(.*?)(?=(?:\n\s*\d+[\.\)]|\Z))", all_text, re.S)
        header_text = matter_type
        if paragraphs and paragraphs[0]["avg_size"] > body_size * 1.3:
            header_text = paragraphs[0]["plain"]

        ol_items = []
        if numbered_items:
            for num, content in numbered_items:
                clean_content = " ".join(content.split())
                ol_items.append(f"<li><p>{clean_content}</p></li>")
        else:
            for p in paragraphs:
                t = p["text"]
                if t and p["plain"] != header_text:
                    clean_t = re.sub(r"^\d+[\.\)]\s*", "", t).strip()
                    ol_items.append(f"<li><p>{clean_t}</p></li>")

        body = (
            f'<h1 class="Chapter-Titled2">{pagebreak}{header_text}</h1>\n'
            f'<ol>\n' + "\n".join(ol_items) + '\n</ol>'
        )
        return header_text, body

    elif matter_type == "Notes / Endnotes":
        section_title = f"Notes, {book_title}"
        note_tags = []
        for p in paragraphs:
            t = p["text"]
            plain = p["plain"]
            m = re.match(r"^(\d+)\.\s+(.*)$", plain)
            if m:
                nnum = m.group(1)
                nbody = re.sub(r"^\d+\.\s*", "", t)
                note_tags.append(
                    f'<p class="en"><span id="note_{nnum}" class="ennum">'
                    f'<a href="chapter.xhtml#note_backlink_{nnum}">{nnum}</a></span> {nbody}</p>'
                )
            elif note_tags:
                note_tags[-1] = note_tags[-1][:-4] + " " + t + "</p>"

        body = (
            f'<div role="doc-endnotes">\n'
            f'<h1 class="Chapter-Titled2">{pagebreak}<span class="bb">Notes</span></h1>\n'
            + "\n".join(note_tags) + '\n</div>'
        )
        return section_title, body

    else:  # Acknowledgments / About the Author / Back Matter General
        rendered = [pagebreak]
        signature_elements = []
        is_first_body_para = True

        for p in paragraphs:
            txt = p["text"]
            plain = p["plain"]
            avg_size = p["avg_size"]
            is_centered = p["is_centered"]
            is_bottom_right = p["is_bottom_right"]

            if avg_size > body_size * 1.35 and len(plain) < 80:
                clean_h = plain.title()
                rendered.append(f'<h1>{clean_h}</h1>')
                continue

            if body_size * 1.15 < avg_size <= body_size * 1.35 and len(plain) < 80:
                rendered.append(f'<h2>{txt}</h2>')
                continue

            # Bottom-right quadrant signature block (e.g. author name, date)
            if is_bottom_right and len(plain) < 150:
                for line in p["raw_lines"]:
                    signature_elements.append(f"<p>{line}</p>")
                continue

            if is_centered:
                rendered.append(f'<p class="center spaceabove">{txt}</p>')
                continue

            if is_first_body_para:
                rendered.append(f'<p class="dropcaps2line">{txt}</p>')
                is_first_body_para = False
            else:
                rendered.append(f'<p class="indent">{txt}</p>')

        if signature_elements:
            rendered.append(
                '<div class="right spaceabove">\n'
                '<div class="dynamic_box">\n' +
                "\n".join(signature_elements) +
                '\n</div>\n</div>'
            )

        return section_title, "\n".join(rendered)


# ----------------------------------------------------------------------
# 4. FLASK ROUTES
# ----------------------------------------------------------------------
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/convert', methods=['POST'])
def convert():
    try:
        if 'pdf' not in request.files:
            return jsonify({'success': False, 'error': 'No PDF file uploaded'}), 400

        pdf_file = request.files['pdf']
        if pdf_file.filename == '':
            return jsonify({'success': False, 'error': 'Empty filename'}), 400

        category = request.form.get('matter_category', 'chapter')
        matter_type = request.form.get('matter_type', 'Body Matter: Full Chapter')
        book_title = request.form.get('book_title', 'Untitled')
        isbn = request.form.get('isbn', '9780000000000')

        try:
            start_page = int(request.form.get('start_page', 1))
        except ValueError:
            start_page = 1

        end_page_val = request.form.get('end_page', '').strip()
        end_page = int(end_page_val) if end_page_val and end_page_val.isdigit() else None

        # Read PDF bytes
        pdf_bytes = pdf_file.read()
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")

        if len(doc) == 0:
            return jsonify({'success': False, 'error': 'PDF document contains no pages'}), 400

        # Execute conversion based on category
        if category == 'chapter':
            section_title, inner_body = convert_chapter_pages(doc, book_title, isbn, start_page, end_page)
        elif category == 'fm':
            section_title, inner_body = convert_front_matter_page(doc, start_page, matter_type, book_title, isbn)
        else:
            # Back matter
            section_title, inner_body = convert_back_matter_page(doc, start_page, matter_type, book_title, isbn)

        full_xhtml = build_xhtml_shell(section_title, book_title, isbn, inner_body)

        return jsonify({
            'success': True,
            'section_title': section_title,
            'xhtml': full_xhtml
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


if __name__ == '__main__':
    print("Starting PRH EPUB 3 Automation Studio on http://127.0.0.1:5000")
    app.run(host='127.0.0.1', port=5000, debug=True)
