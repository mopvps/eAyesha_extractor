import os
import re
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from collections import Counter
import fitz  # PyMuPDF


# ----------------------------------------------------------------------
# 1. XML ENTITY ENCODING
# ----------------------------------------------------------------------
def escape_prh_entities(text: str) -> str:
    """Encodes characters to standard PRH EPUB 3 entity format."""
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
    return text


def build_xhtml_shell(section_title: str, book_title: str, isbn: str, body_inner: str) -> str:
    """Wraps body markup into PRH EPUB 3 / XHTML Strict skeleton."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html lang="en-US" xml:lang="en-US" xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">\n'
        '<head>\n'
        f'<title>{escape_prh_entities(section_title)}, {escape_prh_entities(book_title)}</title>\n'
        '<link rel="stylesheet" type="text/css" href="../css/prh_standard.css"/>\n'
        f'<link rel="stylesheet" type="text/css" href="../css/{isbn}_style.css"/>\n'
        '<meta charset="UTF-8" />\n'
        '</head>\n'
        '<body lang="en-US" xml:lang="en-US">\n'
        f'{body_inner}\n'
        '</body>\n'
        '</html>'
    )


# ----------------------------------------------------------------------
# 2. GEOMETRIC & TYPOGRAPHIC ANALYZER
# ----------------------------------------------------------------------
class DynamicPageAnalyzer:
    def __init__(self, page, isbn: str, img_output_dir: str = "images"):
        self.page = page
        self.isbn = isbn
        self.img_output_dir = img_output_dir
        self.rect = page.rect
        self.width = self.rect.width
        self.height = self.rect.height
        self.dict_data = page.get_text("dict")
        
        # Analyze font sizes across the page to establish hierarchy
        self.body_font_size = self._calc_dominant_font_size()

    def _calc_dominant_font_size(self) -> float:
        sizes = []
        for b in self.dict_data.get("blocks", []):
            if b.get("type") == 0:
                for line in b.get("lines", []):
                    for span in line.get("spans", []):
                        txt = span.get("text", "").strip()
                        if len(txt) > 3:  # Only count real words
                            sizes.append(round(span.get("size", 10.0), 1))
        if sizes:
            return Counter(sizes).most_common(1)[0][0]
        return 10.0

    def detect_page_label(self) -> str:
        """Finds Roman or Arabic page numbers located strictly in header/footer zones."""
        candidates = []
        for b in self.dict_data.get("blocks", []):
            if b.get("type") == 0:
                y0, y1 = b["bbox"][1], b["bbox"][3]
                # Header zone (< 8% height) or Footer zone (> 92% height)
                if y1 < self.height * 0.10 or y0 > self.height * 0.90:
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

        return str(self.page.number + 1)

    def extract_and_save_images(self, doc) -> list:
        """Dynamically extracts all real images embedded on this page."""
        os.makedirs(self.img_output_dir, exist_ok=True)
        img_tags = []
        page_imgs = self.page.get_images(full=True)

        for i, img_info in enumerate(page_imgs):
            xref = img_info[0]
            base_image = doc.extract_image(xref)
            image_bytes = base_image["image"]
            ext = base_image["ext"]

            img_filename = f"{self.isbn}_p{self.page.number + 1}_{i + 1}.{ext}"
            img_path = os.path.join(self.img_output_dir, img_filename)

            with open(img_path, "wb") as f:
                f.write(image_bytes)

            img_tags.append({
                "path": f"../images/{img_filename}",
                "width": base_image.get("width", 0),
                "height": base_image.get("height", 0)
            })
        return img_tags

    def analyze_blocks(self):
        """Processes blocks with precise geometric alignments and font weights."""
        processed = []
        page_label = self.detect_page_label()

        for b in self.dict_data.get("blocks", []):
            if b.get("type") == 0:  # Text block
                bbox = b["bbox"]
                bx0, by0, bx1, by1 = bbox
                block_mid_x = (bx0 + bx1) / 2.0

                # Skip header/footer line if it only contains the page label
                block_plain = "".join(s["text"] for l in b["lines"] for s in l["spans"]).strip()
                if block_plain.lower() == page_label.lower():
                    continue

                # Position classifications
                is_centered = abs(block_mid_x - (self.width / 2.0)) < (self.width * 0.08)
                is_bottom_right = (bx0 > self.width * 0.45) and (by0 > self.height * 0.65)

                line_strings = []
                block_sizes = []

                for line in b.get("lines", []):
                    span_strings = []
                    for span in line.get("spans", []):
                        txt = span.get("text", "")
                        if not txt.strip():
                            continue

                        font = span.get("font", "").lower()
                        flags = span.get("flags", 0)
                        size = span.get("size", 10.0)
                        block_sizes.append(size)

                        is_italic = bool(flags & 2) or "italic" in font or "oblique" in font
                        is_bold = bool(flags & 16) or "bold" in font or "black" in font
                        is_super = bool(flags & 1)

                        t_esc = escape_prh_entities(txt)
                        if is_super and t_esc.isdigit():
                            t_esc = f'<a id="note_backlink_{t_esc}" class="enref" href="notes.xhtml#note_{t_esc}">{t_esc}</a>'
                        else:
                            if is_bold:
                                t_esc = f"<b>{t_esc}</b>"
                            if is_italic:
                                t_esc = f"<i>{t_esc}</i>"

                        span_strings.append(t_esc)

                    if span_strings:
                        line_strings.append(" ".join(span_strings))

                if line_strings:
                    avg_size = sum(block_sizes) / max(len(block_sizes), 1)
                    processed.append({
                        "type": "text",
                        "text": " ".join(line_strings),
                        "lines": line_strings,
                        "avg_size": avg_size,
                        "is_centered": is_centered,
                        "is_bottom_right": is_bottom_right,
                        "bbox": bbox
                    })
            elif b.get("type") == 1:
                processed.append({
                    "type": "image_block",
                    "bbox": b["bbox"]
                })

        return processed, page_label


# ----------------------------------------------------------------------
# 3. PURE DYNAMIC PRH TAG BUILDER (ZERO HARDCODED NAMES)
# ----------------------------------------------------------------------
class DynamicTagBuilder:
    @staticmethod
    def build(blocks, images, page_label: str, page_type: str, book_title: str) -> tuple:
        pagebreak = f'<span id="page_{page_label.lower()}" epub:type="pagebreak" role="doc-pagebreak" title="{page_label}"/>'
        
        # 1. Full Page Image Gallery / Cover
        if (len(blocks) == 0 and len(images) > 0) or page_type == "Photo Gallery / Cover":
            img_divs = []
            for img in images:
                img_divs.append(
                    f'<div class="height_100">\n'
                    f'  <img class="fill_height" src="{img["path"]}" alt="{escape_prh_entities(book_title)}"/>\n'
                    f'</div>'
                )
            body = (
                f'<div class="page_top_padding">\n{pagebreak}\n' +
                "\n".join(img_divs) +
                "\n</div>"
            )
            return "Illustrations", body

        # 2. Numbered Ordered List (Bibliography / References)
        all_text = "\n".join(b.get("text", "") for b in blocks if b["type"] == "text")
        numbered_items = re.findall(r"(?:^|\n)\s*(\d+)[\.\)]\s+(.*?)(?=(?:\n\s*\d+[\.\)]|\Z))", all_text, re.S)

        if len(numbered_items) >= 3 or page_type == "Numbered List (<ol>)":
            header_text = "Bibliography and Further Reading"
            if blocks and blocks[0]["avg_size"] > 12.0:
                header_text = re.sub(r"<[^>]+>", "", blocks[0]["text"]).strip()

            ol_items = []
            for num, content in numbered_items:
                clean_content = " ".join(content.split())
                ol_items.append(f"<li><p>{clean_content}</p></li>")

            body = (
                '<div class="page_top_padding">\n'
                f'<h1 class="Chapter-Titled2">{pagebreak}{header_text}</h1>\n'
                f'<ol>\n' + "\n".join(ol_items) + '\n</ol>\n'
                '</div>'
            )
            return header_text, body

        # 3. Dynamic Structural Document Parser
        rendered_elements = [pagebreak]
        signature_elements = []
        is_first_body_para = True

        for b in blocks:
            if b["type"] == "image_block":
                rendered_elements.append(
                    '<div class="margin_top_bottom">\n'
                    '  <div class="width_25">\n'
                    '    <img class="fill" src="../images/art.jpg" alt="" role="presentation"/>\n'
                    '  </div>\n'
                    '</div>'
                )
                continue

            txt = b["text"]
            avg_size = b["avg_size"]
            is_centered = b["is_centered"]
            is_bottom_right = b["is_bottom_right"]

            # Heading 1 (Large font size ratio > 1.35x)
            if avg_size > 14.0 and len(txt) < 80:
                clean_h = re.sub(r"<[^>]+>", "", txt).title()
                rendered_elements.append(f'<h1>{clean_h}</h1>')
                continue

            # Heading 2 (Medium font size ratio > 1.15x)
            if 12.0 < avg_size <= 14.0 and len(txt) < 80:
                rendered_elements.append(f'<h2>{txt}</h2>')
                continue

            # Signature block (Bottom-right quadrant)
            if is_bottom_right and len(txt) < 150:
                for line in b["lines"]:
                    signature_elements.append(f"<p>{line}</p>")
                continue

            # Centered elements (Praise, quotes, promos)
            if is_centered:
                if txt.startswith("“") or txt.startswith('"'):
                    rendered_elements.append(f'<p class="center spaceabove">{txt}</p>')
                elif txt.startswith("—") or txt.startswith("-"):
                    txt_attr = re.sub(r"^[—\-]\s*(.*)", r'—<span class="fv-smallcaps">\1</span>', txt)
                    rendered_elements.append(f'<p class="center">{txt_attr}</p>')
                else:
                    rendered_elements.append(f'<p class="center">{txt}</p>')
                continue

            # Standard Paragraphs
            if is_first_body_para:
                rendered_elements.append(f'<p class="dropcaps2line">{txt}</p>')
                is_first_body_para = False
            else:
                rendered_elements.append(f'<p class="indent">{txt}</p>')

        if signature_elements:
            rendered_elements.append(
                '<div class="right spaceabove">\n'
                '<div class="dynamic_box">\n' +
                "\n".join(signature_elements) +
                '\n</div>\n</div>'
            )

        section_title = "Chapter"
        for el in rendered_elements:
            m = re.search(r"<h1>(.*?)</h1>", el)
            if m:
                section_title = m.group(1)
                break

        body = '<div class="page_top_padding">\n' + "\n\n".join(rendered_elements) + '\n</div>'
        return section_title, body


# ----------------------------------------------------------------------
# 4. GUI APPLICATION INTERFACE
# ----------------------------------------------------------------------
class PRHDynamicStudio(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PRH Fully Dynamic Book-to-XHTML Studio")
        self.geometry("1180x820")
        self.minsize(980, 700)

        self.pdf_doc = None
        self.setup_ui()

    def setup_ui(self):
        # File Selector
        top_f = ttk.LabelFrame(self, text=" 1. PDF File Input ", padding=10)
        top_f.pack(fill=tk.X, padx=15, pady=5)

        self.file_entry = ttk.Entry(top_f)
        self.file_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))
        ttk.Button(top_f, text="Browse PDF...", command=self.browse_pdf).pack(side=tk.RIGHT)

        # Meta & Settings
        meta_f = ttk.LabelFrame(self, text=" 2. Book Parameters & Dynamic Parser ", padding=10)
        meta_f.pack(fill=tk.X, padx=15, pady=5)

        ttk.Label(meta_f, text="Book Title:").grid(row=0, column=0, sticky=tk.W, padx=5, pady=3)
        self.title_entry = ttk.Entry(meta_f, width=32)
        self.title_entry.insert(0, "The Storyteller: The Novelization")
        self.title_entry.grid(row=0, column=1, sticky=tk.W, padx=5, pady=3)

        ttk.Label(meta_f, text="Ebook ISBN:").grid(row=0, column=2, sticky=tk.W, padx=5, pady=3)
        self.isbn_entry = ttk.Entry(meta_f, width=20)
        self.isbn_entry.insert(0, "9781613982976")
        self.isbn_entry.grid(row=0, column=3, sticky=tk.W, padx=5, pady=3)

        ttk.Label(meta_f, text="Page Override:").grid(row=1, column=0, sticky=tk.W, padx=5, pady=3)
        self.mode_var = tk.StringVar(value="Fully Dynamic (Auto-Detect)")
        self.mode_combo = ttk.Combobox(
            meta_f,
            textvariable=self.mode_var,
            state="readonly",
            values=[
                "Fully Dynamic (Auto-Detect)",
                "Numbered List (<ol>)",
                "Photo Gallery / Cover",
            ],
            width=28,
        )
        self.mode_combo.grid(row=1, column=1, sticky=tk.W, padx=5, pady=3)

        ttk.Label(meta_f, text="PDF Page #:").grid(row=1, column=2, sticky=tk.W, padx=5, pady=3)
        self.page_spin = ttk.Spinbox(meta_f, from_=1, to=1, width=8, command=self.on_page_change)
        self.page_spin.grid(row=1, column=3, sticky=tk.W, padx=5, pady=3)

        ttk.Label(meta_f, text="Detected Label:").grid(row=1, column=4, sticky=tk.W, padx=5, pady=3)
        self.label_entry = ttk.Entry(meta_f, width=10)
        self.label_entry.grid(row=1, column=5, sticky=tk.W, padx=5, pady=3)

        # Actions
        btn_bar = ttk.Frame(self, padding=5)
        btn_bar.pack(fill=tk.X, padx=15)

        ttk.Button(btn_bar, text="⚡ Run Dynamic Extraction", command=self.process_page).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_bar, text="📋 Copy XHTML", command=self.copy_xhtml).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_bar, text="💾 Save .xhtml", command=self.save_xhtml).pack(side=tk.RIGHT, padx=5)

        # Panes
        paned = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=15, pady=10)

        left_f = ttk.LabelFrame(paned, text=" PDF Extracted Elements ", padding=5)
        paned.add(left_f, weight=1)
        self.raw_view = scrolledtext.ScrolledText(left_f, wrap=tk.WORD, font=("Consolas", 10))
        self.raw_view.pack(fill=tk.BOTH, expand=True)

        right_f = ttk.LabelFrame(paned, text=" Generated PRH EPUB 3 XHTML ", padding=5)
        paned.add(right_f, weight=2)
        self.xhtml_view = scrolledtext.ScrolledText(right_f, wrap=tk.NONE, font=("Consolas", 10))
        self.xhtml_view.pack(fill=tk.BOTH, expand=True)

    def browse_pdf(self):
        f = filedialog.askopenfilename(filetypes=[("PDF Documents", "*.pdf")])
        if f:
            self.file_entry.delete(0, tk.END)
            self.file_entry.insert(0, f)
            self.pdf_doc = fitz.open(f)
            self.page_spin.config(to=len(self.pdf_doc))
            self.page_spin.set(1)
            self.on_page_change()

    def on_page_change(self):
        if not self.pdf_doc:
            return
        idx = int(self.page_spin.get()) - 1
        page = self.pdf_doc[idx]

        analyzer = DynamicPageAnalyzer(page, self.isbn_entry.get().strip())
        detected_label = analyzer.detect_page_label()

        self.label_entry.delete(0, tk.END)
        self.label_entry.insert(0, detected_label)

        self.raw_view.delete("1.0", tk.END)
        self.raw_view.insert(tk.END, page.get_text())

        self.process_page()

    def process_page(self):
        if not self.pdf_doc:
            return

        idx = int(self.page_spin.get()) - 1
        page = self.pdf_doc[idx]
        isbn = self.isbn_entry.get().strip()
        book_title = self.title_entry.get().strip()

        analyzer = DynamicPageAnalyzer(page, isbn)
        blocks, detected_label = analyzer.analyze_blocks()
        images = analyzer.extract_and_save_images(self.pdf_doc)

        label = self.label_entry.get().strip() or detected_label
        page_type = self.mode_var.get()

        section_title, body = DynamicTagBuilder.build(blocks, images, label, page_type, book_title)
        full_xhtml = build_xhtml_shell(section_title, book_title, isbn, body)

        self.xhtml_view.delete("1.0", tk.END)
        self.xhtml_view.insert(tk.END, full_xhtml)

    def copy_xhtml(self):
        self.clipboard_clear()
        self.clipboard_append(self.xhtml_view.get("1.0", tk.END))
        messagebox.showinfo("Copied", "Dynamic XHTML copied to clipboard!")

    def save_xhtml(self):
        content = self.xhtml_view.get("1.0", tk.END).strip()
        if not content:
            return
        out = filedialog.asksaveasfilename(defaultextension=".xhtml", filetypes=[("XHTML", "*.xhtml")])
        if out:
            with open(out, "w", encoding="utf-8") as f:
                f.write(content)
            messagebox.showinfo("Saved", f"Exported to:\n{out}")


if __name__ == "__main__":
    app = PRHDynamicStudio()
    app.mainloop()