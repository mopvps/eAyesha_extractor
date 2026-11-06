import os
import re
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
import fitz  # PyMuPDF


def escape_prh_entities(text: str) -> str:
    """Escapes special characters to PRH-compliant XML entities."""
    replacements = [
        ("&", "&amp;"),
        ("<", "&lt;"),
        (">", "&gt;"),
        ("—", "&#x2014;"),
        ("–", "&#x2013;"),
        ("…", "&#x2026;"),
        ("•", "&#x2022;"),
        ("™", "&#x2122;"),
        ("©", "&#x00A9;"),
        ("\u2003", "&#x2003;"),
        ("\u00A0", "&#x00A0;"),
    ]
    for orig, rep in replacements:
        text = text.replace(orig, rep)
    return text


def build_xhtml_shell(section_title: str, book_title: str, isbn: str, body_inner: str) -> str:
    """Standard EPUB 3 / PRH XHTML Strict shell."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html lang="en-US" xml:lang="en-US" xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head>
<title>{escape_prh_entities(section_title)}, {escape_prh_entities(book_title)}</title>
<link rel="stylesheet" type="text/css" href="../css/prh_standard.css"/>
<link rel="stylesheet" type="text/css" href="../css/{isbn}_style.css"/>
<meta charset="UTF-8" />
</head>
<body lang="en-US" xml:lang="en-US">
{body_inner}
</body>
</html>"""


class DynamicPDFParser:
    """Extracts rich styling (bold, italics, superscripts, headings) from PDF dict."""

    @staticmethod
    def extract_rich_blocks(page):
        """Inspects PDF layout dictionary and preserves fonts, weights, and sizes."""
        page_dict = page.get_text("dict")
        blocks_data = []

        for block in page_dict.get("blocks", []):
            if block.get("type") == 0:  # Text block
                block_lines = []
                avg_size = 0.0
                total_spans = 0

                for line in block.get("lines", []):
                    line_spans = []
                    for span in line.get("spans", []):
                        text = span.get("text", "")
                        if not text.strip():
                            continue

                        font = span.get("font", "").lower()
                        flags = span.get("flags", 0)
                        size = span.get("size", 10.0)
                        avg_size += size
                        total_spans += 1

                        # Detect formatting from PDF font properties
                        is_italic = bool(flags & 2) or "italic" in font or "oblique" in font
                        is_bold = bool(flags & 16) or "bold" in font or "black" in font

                        # Check if superscript (e.g. note reference)
                        is_superscript = flags & 1 == 1

                        span_html = escape_prh_entities(text)
                        if is_superscript and span_html.isdigit():
                            span_html = f'<a id="note_backlink_{span_html}" class="enref" href="notes.xhtml#note_{span_html}">{span_html}</a>'
                        else:
                            if is_bold:
                                span_html = f"<b>{span_html}</b>"
                            if is_italic:
                                span_html = f"<i>{span_html}</i>"

                        line_spans.append(span_html)

                    if line_spans:
                        block_lines.append(" ".join(line_spans))

                if block_lines:
                    blocks_data.append({
                        "text": " ".join(block_lines),
                        "avg_size": avg_size / max(total_spans, 1),
                        "raw": block
                    })
            elif block.get("type") == 1:  # Image block
                blocks_data.append({"type": "image", "raw": block})

        return blocks_data

    @staticmethod
    def auto_detect_page_type(page, blocks) -> str:
        """Dynamically identifies the page type by examining typography and contents."""
        text = page.get_text().strip().lower()
        images = page.get_images()

        if len(text) < 30 and len(images) > 0:
            return "Cover / Full Page Art"
        if re.search(r"\b(contents|table of contents)\b", text) and re.search(r"(\.{3,}|\d+\s*$)", text, re.M):
            return "Contents / TOC"
        if re.search(r"isbn|copyright|all rights reserved|first published|printed in", text):
            return "Copyright"
        if re.search(r"notes to (?:introduction|chapter)", text):
            return "Notes / Endnotes"
        if re.search(r"suggestions for further reading|bibliography|references", text):
            return "Bibliography / Further Reading"
        if "half title" in text or (len(blocks) == 1 and blocks[0].get("avg_size", 0) > 14):
            return "Half Title"

        return "Introduction / Chapter"

    @staticmethod
    def detect_page_number_label(page) -> str:
        """Dynamically detects Roman or Arabic page labels in headers/footers."""
        text = page.get_text().strip()
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if not lines:
            return "1"

        candidates = [lines[0], lines[-1]]  # Corners/margins
        roman_re = r"^(?=[mdclxvi]+$)m*(c[md]|d?c*)(x[cl]|l?x*)(i[xv]|v?i*)$"

        for cand in candidates:
            # Check Roman
            if re.match(roman_re, cand.lower()):
                return cand.lower()
            # Check standalone digits
            if cand.isdigit() and len(cand) <= 4:
                return cand

        return "1"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Dynamic PRH PDF to XHTML Converter")
        self.geometry("1150x780")
        self.minsize(950, 650)

        self.pdf_doc = None
        self.setup_ui()

    def setup_ui(self):
        # 1. File Selection Frame
        f_frame = ttk.LabelFrame(self, text=" 1. PDF File ", padding=10)
        f_frame.pack(fill=tk.X, padx=15, pady=5)

        self.pdf_path_entry = ttk.Entry(f_frame)
        self.pdf_path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))

        ttk.Button(f_frame, text="Browse PDF...", command=self.browse_pdf).pack(side=tk.RIGHT)

        # 2. Controls & Metadata Frame
        c_frame = ttk.LabelFrame(self, text=" 2. Page & Book Options ", padding=10)
        c_frame.pack(fill=tk.X, padx=15, pady=5)

        ttk.Label(c_frame, text="Title:").grid(row=0, column=0, sticky=tk.W, padx=5, pady=3)
        self.title_entry = ttk.Entry(c_frame, width=32)
        self.title_entry.insert(0, "The Portable Enlightenment Reader")
        self.title_entry.grid(row=0, column=1, sticky=tk.W, padx=5, pady=3)

        ttk.Label(c_frame, text="ISBN:").grid(row=0, column=2, sticky=tk.W, padx=5, pady=3)
        self.isbn_entry = ttk.Entry(c_frame, width=18)
        self.isbn_entry.insert(0, "9781101127971")
        self.isbn_entry.grid(row=0, column=3, sticky=tk.W, padx=5, pady=3)

        # Page index spinner
        ttk.Label(c_frame, text="PDF Page:").grid(row=1, column=0, sticky=tk.W, padx=5, pady=3)
        self.page_spin = ttk.Spinbox(c_frame, from_=1, to=1, width=8, command=self.on_page_change)
        self.page_spin.grid(row=1, column=1, sticky=tk.W, padx=5, pady=3)

        # Dropdown
        ttk.Label(c_frame, text="Page Type:").grid(row=1, column=2, sticky=tk.W, padx=5, pady=3)
        self.page_types = [
            "Auto-Detect",
            "Cover / Full Page Art",
            "Title Page",
            "Half Title",
            "Copyright",
            "Dedication / Epigraph",
            "Contents / TOC",
            "Introduction / Chapter",
            "Notes / Endnotes",
            "Bibliography / Further Reading",
        ]
        self.page_type_var = tk.StringVar(value="Auto-Detect")
        self.type_combo = ttk.Combobox(c_frame, textvariable=self.page_type_var, values=self.page_types, state="readonly", width=25)
        self.type_combo.grid(row=1, column=3, sticky=tk.W, padx=5, pady=3)

        ttk.Label(c_frame, text="Page Label (epub:type):").grid(row=1, column=4, sticky=tk.W, padx=5, pady=3)
        self.page_label_entry = ttk.Entry(c_frame, width=10)
        self.page_label_entry.grid(row=1, column=5, sticky=tk.W, padx=5, pady=3)

        # 3. Actions
        btn_frame = ttk.Frame(self, padding=5)
        btn_frame.pack(fill=tk.X, padx=15)

        ttk.Button(btn_frame, text="⚡ Extract & Apply PRH XHTML Tags", command=self.process_current_page).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="📋 Copy XHTML", command=self.copy_xhtml).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="💾 Save .xhtml", command=self.save_xhtml).pack(side=tk.RIGHT, padx=5)

        # 4. Panes
        paned = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=15, pady=10)

        left_f = ttk.LabelFrame(paned, text=" Extracted PDF Elements & Raw View ", padding=5)
        paned.add(left_f, weight=1)
        self.raw_view = scrolledtext.ScrolledText(left_f, wrap=tk.WORD, font=("Consolas", 10))
        self.raw_view.pack(fill=tk.BOTH, expand=True)

        right_f = ttk.LabelFrame(paned, text=" Generated Dynamic PRH XHTML ", padding=5)
        paned.add(right_f, weight=2)
        self.xhtml_view = scrolledtext.ScrolledText(right_f, wrap=tk.NONE, font=("Consolas", 10))
        self.xhtml_view.pack(fill=tk.BOTH, expand=True)

    def browse_pdf(self):
        f = filedialog.askopenfilename(filetypes=[("PDF Documents", "*.pdf")])
        if f:
            self.pdf_path_entry.delete(0, tk.END)
            self.pdf_path_entry.insert(0, f)
            self.pdf_doc = fitz.open(f)
            self.page_spin.config(to=len(self.pdf_doc))
            self.page_spin.set(1)
            self.on_page_change()

    def on_page_change(self):
        if not self.pdf_doc:
            return
        idx = int(self.page_spin.get()) - 1
        page = self.pdf_doc[idx]

        # Dynamically auto-detect page number / label
        detected_label = DynamicPDFParser.detect_page_number_label(page)
        self.page_label_entry.delete(0, tk.END)
        self.page_label_entry.insert(0, detected_label)

        # Update raw view
        self.raw_view.delete("1.0", tk.END)
        self.raw_view.insert(tk.END, page.get_text())

        # Run process
        self.process_current_page()

    def process_current_page(self):
        if not self.pdf_doc:
            return

        idx = int(self.page_spin.get()) - 1
        page = self.pdf_doc[idx]
        blocks = DynamicPDFParser.extract_rich_blocks(page)

        page_type = self.page_type_var.get()
        if page_type == "Auto-Detect":
            page_type = DynamicPDFParser.auto_detect_page_type(page, blocks)

        book_title = self.title_entry.get().strip()
        isbn = self.isbn_entry.get().strip()
        label = self.page_label_entry.get().strip() or str(idx + 1)
        pagebreak = f'<span id="page_{label.lower()}" epub:type="pagebreak" role="doc-pagebreak" title="{label}"/>'

        body = ""
        section_title = page_type

        # Generate XHTML dynamically based on structural block analysis
        if page_type in ["Cover / Full Page Art", "Title Page"]:
            img_suffix = "cover" if page_type == "Cover / Full Page Art" else "title_page"
            body = (
                f'<div class="height_100">\n'
                f'  {pagebreak}\n'
                f'  <img class="fill_height" src="../images/{isbn}_{img_suffix}.jpg" alt="{escape_prh_entities(book_title)}"/>\n'
                f'</div>'
            )

        elif page_type == "Copyright":
            section_title = f"Copyright, {book_title}"
            tags = [pagebreak]
            for b in blocks:
                t = b.get("text", "")
                if not t:
                    continue
                # Insert trade vs ebook ISBN formatting
                t = re.sub(r"(ISBN[:\s]+)([\d\-]+)", r"Trade ISBN&#x2003;\2", t)
                cls = "crt spaceabove" if any(w in t.lower() for w in ["published", "copyright", "isbn"]) else "crt"
                tags.append(f'<p class="{cls}">{t}</p>')
            tags.append('<p id="release_identifier_line" class="spaceabove">amn_prh_8.0_c0_r0</p>')
            body = '<div class="page_top_padding">\n' + "\n".join(tags) + "\n</div>"

        elif page_type == "Contents / TOC":
            section_title = f"Contents, {book_title}"
            toc_lines = []
            for b in blocks:
                t = b.get("text", "")
                if "contents" in t.lower() and len(t) < 15:
                    continue
                # Parse dotted lines or tab gaps with page numbers dynamically
                m = re.match(r"^(.*?)(?:\.{2,}|\s{4,})(\w+)$", t)
                if m:
                    ch_title, pnum = m.groups()
                    toc_lines.append(f'<p class="toc_chap"><a href="ch{pnum}.xhtml"><b>{ch_title.strip()}</b></a></p>')
                else:
                    toc_lines.append(f'<p class="toc_chap">{t}</p>')

            body = (
                f'<div role="doc-toc">\n'
                f'<div class="page_top_padding">\n'
                f'<h1 class="Chapter-Titled2">{pagebreak}<span class="bb">Contents</span></h1>\n'
                f'<div class="toc_chap">\n' + "\n".join(toc_lines) + f'\n</div>\n</div>\n</div>'
            )

        elif page_type == "Notes / Endnotes":
            section_title = f"Notes to Introduction, {book_title}"
            note_tags = []
            for b in blocks:
                t = b.get("text", "")
                m = re.match(r"^(\d+)\.\s+(.*)$", t)
                if m:
                    nnum, nbody = m.groups()
                    note_tags.append(
                        f'<p class="en"><span id="note_{nnum}" class="ennum">'
                        f'<a href="intro.xhtml#note_backlink_{nnum}">{nnum}</a></span> {nbody}</p>'
                    )
                elif note_tags:
                    note_tags[-1] = note_tags[-1][:-4] + " " + t + "</p>"

            body = (
                f'<div class="page_top_padding" role="doc-endnotes">\n'
                f'<h1 class="Chapter-Titled2">{pagebreak}<span class="bb">Notes to Introduction</span></h1>\n'
                + "\n".join(note_tags) + "\n</div>"
            )

        else:  # Standard Chapters, Introduction, Epigraphs
            section_title = f"Chapter, {book_title}"
            rendered = [pagebreak]
            is_first_para = True

            # Calculate baseline font size to distinguish headings dynamically
            sizes = [b.get("avg_size", 10.0) for b in blocks if "avg_size" in b]
            median_size = sorted(sizes)[len(sizes) // 2] if sizes else 10.0

            for b in blocks:
                t = b.get("text", "")
                sz = b.get("avg_size", 10.0)

                # Skip header/footer line repeating page number
                if t.strip().lower() == label.lower():
                    continue

                # Heading: noticeably larger than median or single-line uppercase
                if sz > median_size * 1.25 or (is_first_para and len(t) < 40 and t.isupper()):
                    clean_head = re.sub(r"<[^>]+>", "", t).title()
                    rendered.append(f'<h1>{clean_head}</h1>')
                    continue

                # Author sign-offs (e.g., "A.M.")
                if len(re.sub(r"<[^>]+>", "", t).strip()) <= 5:
                    rendered.append(
                        f'<div class="right">\n<div class="dynamic_box">\n  <p class="spaceabove">{t}</p>\n</div>\n</div>'
                    )
                    continue

                # Paragraph dropcaps & indentation
                if is_first_para:
                    rendered.append(f'<p class="dropcaps2line">{t}</p>')
                    is_first_para = False
                else:
                    rendered.append(f'<p class="indent">{t}</p>')

            body = '<div class="page_top_padding">\n' + "\n".join(rendered) + "\n</div>"

        full_xhtml = build_xhtml_shell(section_title, book_title, isbn, body)
        self.xhtml_view.delete("1.0", tk.END)
        self.xhtml_view.insert(tk.END, full_xhtml)

    def copy_xhtml(self):
        self.clipboard_clear()
        self.clipboard_append(self.xhtml_view.get("1.0", tk.END))
        messagebox.showinfo("Copied", "XHTML copied to clipboard!")

    def save_xhtml(self):
        content = self.xhtml_view.get("1.0", tk.END).strip()
        if not content:
            return
        f = filedialog.asksaveasfilename(defaultextension=".xhtml", filetypes=[("XHTML", "*.xhtml")])
        if f:
            with open(f, "w", encoding="utf-8") as fp:
                fp.write(content)
            messagebox.showinfo("Saved", f"Exported to:\n{f}")


if __name__ == "__main__":
    app = App()
    app.mainloop()