"""DocumentParser: uploaded file -> SourceMaterial.

Supported: TXT / Markdown, PDF (text layer), PPTX (slide text), DOCX (paragraphs).
Legacy binary `.ppt` cannot be read and raises DocumentParsingError with a
hint to re-save it as PPTX.

Every format is first converted into a flat list of `_Block`s
(heading / paragraph / list_item / note / table / code / formula / image) and
then assembled into sections by one shared routine, so the analyzer sees the
same structure regardless of the original format.

Known limits (documented, not hidden):
  * PDF: only the embedded text layer is read (no OCR). Headings are guessed
    from numbering patterns because pypdf does not expose font information.
  * PDF/PPTX/DOCX tables and images are recorded as metadata only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import DocumentParsingError, EmptyDocument, UnsupportedFileType
from ..models.source import (
    CodeBlock,
    ContentBlock,
    Formula,
    Heading,
    ImageMeta,
    SourceLocation,
    SourceMaterial,
    SourceSection,
    TableData,
)


@dataclass
class _Block:
    kind: str  # heading|paragraph|list_item|note|table|code|formula|image
    text: str = ""
    level: int = 0
    line: int | None = None
    page: int | None = None
    rows: list[list[str]] = field(default_factory=list)
    language: str | None = None
    alt: str | None = None


_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+•▪◦]|\d+[.)])\s+(.*\S)\s*$")
_PLAIN_HEADING = re.compile(
    r"^(?:제\s*\d+\s*[장절편]|Chapter\s+\d+|\d+(?:\.\d+)*[.)]?)\s+\S", re.IGNORECASE
)
_SENTENCE_END = ("다.", ".", "?", "!", ":", "다", "요", "음", "함", "。", "”", "\"", ")")
_HEADING_REJECT_END = (".", "?", "!", ":", "다", "。")
_INLINE_FORMULA = re.compile(r"\$\$(.+?)\$\$|(?<![\\$])\$(?!\s)([^$\n]{1,200}?)(?<!\s)\$(?!\$)")
_MONO_FONTS = {"consolas", "courier new", "courier", "menlo", "monaco", "d2coding", "lucida console"}


def _read_text(path: Path, warnings: list[str]) -> str:
    data = path.read_bytes()
    for enc in ("utf-8-sig", "cp949"):
        try:
            text = data.decode(enc)
            if enc != "utf-8-sig":
                warnings.append(f"UTF-8이 아닌 인코딩({enc})으로 읽었습니다.")
            return text
        except UnicodeDecodeError:
            continue
    warnings.append("인코딩을 확정하지 못해 일부 문자가 깨졌을 수 있습니다.")
    return data.decode("utf-8", errors="replace")


def _is_plain_heading(line: str, prev_blank: bool) -> bool:
    s = line.strip()
    if not prev_blank or len(s) > 60 or not _PLAIN_HEADING.match(s):
        return False
    # Nouns like "개요" / "요약" end in 요/약..., so only real sentence enders reject.
    return not s.endswith(_HEADING_REJECT_END)


def _plain_heading_level(line: str) -> int:
    m = re.match(r"^(\d+(?:\.\d+)+)", line.strip())
    return min(m.group(1).count(".") + 1, 4) if m else 1


def _inline_formulas(text: str, line: int | None, page: int | None) -> list[_Block]:
    out = []
    for m in _INLINE_FORMULA.finditer(text):
        expr = (m.group(1) or m.group(2) or "").strip()
        if expr:
            out.append(_Block("formula", expr, line=line, page=page))
    return out


# ---------------------------------------------------------------------------
# Format -> blocks
# ---------------------------------------------------------------------------
def _text_to_blocks(
    text: str,
    *,
    page: int | None = None,
    allow_plain_headings: bool = True,
    heading_needs_blank: bool = True,
) -> list[_Block]:
    lines = text.splitlines()
    has_md_heading = any(_MD_HEADING.match(l) for l in lines)
    use_plain = allow_plain_headings and not has_md_heading

    blocks: list[_Block] = []
    para: list[tuple[int, str]] = []
    code: list[str] = []
    code_lang: str | None = None
    code_start = 0
    in_code = False
    in_math = False
    math: list[str] = []
    math_start = 0
    table: list[list[str]] = []
    table_start = 0

    def flush_para():
        if para:
            first_line = para[0][0]
            body = "\n".join(t for _, t in para)
            blocks.append(_Block("paragraph", body, line=first_line, page=page))
            blocks.extend(_inline_formulas(body, first_line, page))
            para.clear()

    def flush_table():
        if table:
            blocks.append(_Block("table", rows=list(table), line=table_start, page=page))
            table.clear()

    prev_blank = True
    for i, raw in enumerate(lines, 1):
        line = raw.rstrip()
        stripped = line.strip()

        if in_code:
            if stripped.startswith("```"):
                blocks.append(
                    _Block("code", "\n".join(code), line=code_start, page=page, language=code_lang)
                )
                code, in_code = [], False
            else:
                code.append(raw.rstrip("\n"))
            continue
        if in_math:
            if stripped.endswith("$$"):
                math.append(stripped[:-2])
                blocks.append(_Block("formula", "\n".join(math).strip(), line=math_start, page=page))
                math, in_math = [], False
            else:
                math.append(stripped)
            continue

        if stripped.startswith("```"):
            flush_para(); flush_table()
            in_code, code_start = True, i
            code_lang = stripped[3:].strip() or None
            continue
        if stripped == "$$" or (stripped.startswith("$$") and not stripped.endswith("$$", 2)):
            flush_para(); flush_table()
            in_math, math_start = True, i
            math = [stripped[2:]] if len(stripped) > 2 else []
            continue

        # markdown table rows
        if stripped.startswith("|") and stripped.count("|") >= 2:
            flush_para()
            if not table:
                table_start = i
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                table.append(cells)
            prev_blank = False
            continue
        flush_table()

        if not stripped:
            flush_para()
            prev_blank = True
            continue

        if m := _MD_HEADING.match(line):
            flush_para()
            blocks.append(_Block("heading", m.group(2).strip(), level=len(m.group(1)), line=i, page=page))
        elif use_plain and _is_plain_heading(line, prev_blank or not heading_needs_blank):
            flush_para()
            blocks.append(
                _Block("heading", stripped, level=_plain_heading_level(line), line=i, page=page)
            )
        elif m := _LIST_ITEM.match(line):
            flush_para()
            item = m.group(1).strip()
            blocks.append(_Block("list_item", item, line=i, page=page))
            blocks.extend(_inline_formulas(item, i, page))
        elif stripped.startswith("$$") and stripped.endswith("$$") and len(stripped) > 4:
            flush_para()
            blocks.append(_Block("formula", stripped[2:-2].strip(), line=i, page=page))
        else:
            para.append((i, stripped))
        prev_blank = False

    flush_para(); flush_table()
    if in_code and code:
        blocks.append(_Block("code", "\n".join(code), line=code_start, page=page, language=code_lang))
    return blocks


def _reflow_pdf_text(text: str) -> str:
    """PDF text comes as visually wrapped lines; rejoin lines of one paragraph."""
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            out.append("")
            continue
        prev = out[-1] if out else ""
        prev_is_heading = (
            len(prev) <= 60
            and _PLAIN_HEADING.match(prev) is not None
            and not prev.endswith(_HEADING_REJECT_END)
        )
        starts_new = (
            not prev
            or prev_is_heading
            or prev.endswith(_SENTENCE_END)
            or _LIST_ITEM.match(line) is not None
            or _PLAIN_HEADING.match(line) is not None
        )
        if starts_new:
            out.append(line)
        else:
            out[-1] = prev + " " + line
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------
def _assemble(
    blocks: list[_Block],
    *,
    source_id: str,
    filename: str,
    file_type: str,
    fallback_title: str,
    images: list[_Block] | None = None,
    page_count: int | None = None,
    warnings: list[str] | None = None,
) -> SourceMaterial:
    sections: list[SourceSection] = []
    headings: list[Heading] = []
    tables: list[TableData] = []
    codes: list[CodeBlock] = []
    formulas: list[Formula] = []
    image_meta: list[ImageMeta] = []
    stack: list[SourceSection] = []
    current: SourceSection | None = None

    def loc(sec: SourceSection | None, b: _Block) -> SourceLocation:
        return SourceLocation(
            section_id=sec.id if sec else None,
            section_title=sec.title if sec else None,
            line=b.line,
            page=b.page,
        )

    def add_section(title: str, level: int, b: _Block, parent: str | None) -> SourceSection:
        sec = SourceSection(
            id=f"s{len(sections) + 1:02d}",
            order=len(sections) + 1,
            title=title,
            level=level,
            parent_id=parent,
            line=b.line,
            page=b.page,
        )
        sections.append(sec)
        return sec

    for b in blocks:
        if b.kind == "heading":
            while stack and stack[-1].level >= b.level:
                stack.pop()
            current = add_section(b.text, b.level, b, stack[-1].id if stack else None)
            stack.append(current)
            headings.append(
                Heading(level=b.level, text=b.text, section_id=current.id, line=b.line, page=b.page)
            )
            continue

        if current is None:  # content before the first heading
            current = add_section("도입", 1, b, None)

        if b.kind in ("paragraph", "list_item", "note"):
            current.blocks.append(
                ContentBlock(kind=b.kind, text=b.text, line=b.line, page=b.page)
            )
        elif b.kind == "table":
            tables.append(TableData(section_id=current.id, rows=b.rows, location=loc(current, b)))
        elif b.kind == "code":
            codes.append(
                CodeBlock(
                    section_id=current.id, language=b.language, code=b.text, location=loc(current, b)
                )
            )
        elif b.kind == "formula":
            formulas.append(
                Formula(section_id=current.id, expression=b.text, location=loc(current, b))
            )
        elif b.kind == "image":
            image_meta.append(
                ImageMeta(section_id=current.id, name=b.text, page=b.page, alt_text=b.alt)
            )

    for sec in sections:
        sec.text = "\n".join(cb.text for cb in sec.blocks)
        sec.char_count = len(sec.text)

    title = next((h.text for h in headings if h.level == 1), None) or fallback_title
    raw_parts = []
    for sec in sections:
        raw_parts.append(sec.title)
        if sec.text:
            raw_parts.append(sec.text)
    for t in tables:
        raw_parts.extend(" | ".join(r) for r in t.rows)
    raw_text = "\n".join(raw_parts).strip()

    return SourceMaterial(
        id=source_id,
        filename=filename,
        title=title,
        file_type=file_type,
        raw_text=raw_text,
        headings=headings,
        sections=sections,
        tables=tables,
        code_blocks=codes,
        formulas=formulas,
        images_metadata=image_meta,
        page_count=page_count,
        warnings=warnings or [],
    )


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
class DocumentParser:
    def parse(self, path: Path, filename: str, source_id: str) -> SourceMaterial:
        ext = Path(filename).suffix.lower()
        try:
            if ext in (".txt", ".md", ".markdown"):
                material = self._parse_text(path, filename, source_id, ext)
            elif ext == ".pdf":
                material = self._parse_pdf(path, filename, source_id)
            elif ext == ".pptx":
                material = self._parse_pptx(path, filename, source_id)
            elif ext == ".docx":
                material = self._parse_docx(path, filename, source_id)
            elif ext == ".ppt":
                raise DocumentParsingError(
                    "구형 PPT(.ppt) 파일은 직접 분석할 수 없습니다. "
                    "PowerPoint에서 PPTX로 저장한 뒤 다시 업로드해 주세요."
                )
            else:
                raise UnsupportedFileType()
        except (DocumentParsingError, EmptyDocument, UnsupportedFileType):
            raise
        except Exception as exc:  # corrupted file, library failure, ...
            raise DocumentParsingError(
                f"'{filename}' 파일을 읽지 못했습니다. 파일이 손상되었거나 지원되지 않는 구조일 수 있습니다."
            ) from exc

        if not material.raw_text.strip():
            raise EmptyDocument(
                "문서에서 텍스트를 찾지 못했습니다. 이미지로만 된 문서(스캔본)는 "
                "OCR을 지원하지 않습니다."
            )
        return material

    # -- txt / md ----------------------------------------------------------
    def _parse_text(self, path: Path, filename: str, source_id: str, ext: str) -> SourceMaterial:
        warnings: list[str] = []
        text = _read_text(path, warnings)
        blocks = _text_to_blocks(text)
        return _assemble(
            blocks,
            source_id=source_id,
            filename=filename,
            file_type="markdown" if ext != ".txt" else "txt",
            fallback_title=Path(filename).stem,
            warnings=warnings,
        )

    # -- pdf ---------------------------------------------------------------
    def _parse_pdf(self, path: Path, filename: str, source_id: str) -> SourceMaterial:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                ok = reader.decrypt("")
            except Exception:
                ok = 0
            if not ok:
                raise DocumentParsingError("암호가 걸린 PDF는 분석할 수 없습니다. 암호를 해제한 뒤 업로드해 주세요.")

        warnings: list[str] = []
        blocks: list[_Block] = []
        images: list[_Block] = []
        page_blocks: list[list[_Block]] = []
        for pno, page in enumerate(reader.pages, 1):
            text = _reflow_pdf_text(page.extract_text() or "")
            # PDF text has no reliable blank lines around headings.
            pb = _text_to_blocks(text, page=pno, heading_needs_blank=False)
            page_blocks.append(pb)
            try:
                for img in page.images:
                    images.append(_Block("image", img.name, page=pno))
            except Exception:
                pass

        has_heading = any(b.kind == "heading" for pb in page_blocks for b in pb)
        for pno, pb in enumerate(page_blocks, 1):
            if not has_heading and pb:
                blocks.append(_Block("heading", f"페이지 {pno}", level=1, page=pno))
            blocks.extend(pb)
            blocks.extend(i for i in images if i.page == pno)
        if not has_heading:
            warnings.append("PDF에서 제목 구조를 찾지 못해 페이지 단위로 나누었습니다.")

        title = None
        try:
            title = (reader.metadata.title or "").strip() if reader.metadata else None
        except Exception:
            pass
        return _assemble(
            blocks,
            source_id=source_id,
            filename=filename,
            file_type="pdf",
            fallback_title=title or Path(filename).stem,
            page_count=len(reader.pages),
            warnings=warnings,
        )

    # -- pptx --------------------------------------------------------------
    def _parse_pptx(self, path: Path, filename: str, source_id: str) -> SourceMaterial:
        from pptx import Presentation
        from pptx.enum.shapes import MSO_SHAPE_TYPE, PP_PLACEHOLDER

        prs = Presentation(str(path))
        blocks: list[_Block] = []
        first_title: str | None = None

        def walk(shapes):
            for shp in shapes:
                if shp.shape_type == MSO_SHAPE_TYPE.GROUP:
                    yield from walk(shp.shapes)
                else:
                    yield shp

        for idx, slide in enumerate(prs.slides, 1):
            title_shape = slide.shapes.title
            title = (title_shape.text_frame.text.strip() if title_shape is not None else "") or ""
            title_id = title_shape.shape_id if title_shape is not None else None
            body: list[_Block] = []
            for shp in walk(slide.shapes):
                if shp.shape_type == MSO_SHAPE_TYPE.PICTURE:
                    alt = None
                    try:
                        descr = shp._element.xpath(".//p:cNvPr/@descr")
                        alt = descr[0] if descr else None
                    except Exception:
                        pass
                    body.append(_Block("image", shp.name, page=idx, alt=alt))
                    continue
                if getattr(shp, "has_table", False) and shp.has_table:
                    rows = [[c.text.strip() for c in r.cells] for r in shp.table.rows]
                    body.append(_Block("table", rows=rows, page=idx))
                    continue
                if not getattr(shp, "has_text_frame", False) or not shp.has_text_frame:
                    continue
                if title_id is not None and shp.shape_id == title_id:
                    continue
                is_body = False
                try:
                    is_body = shp.is_placeholder and shp.placeholder_format.type in (
                        PP_PLACEHOLDER.BODY,
                        PP_PLACEHOLDER.OBJECT,
                    )
                except Exception:
                    pass
                for para in shp.text_frame.paragraphs:
                    text = "".join(r.text for r in para.runs).strip()
                    if not text:
                        continue
                    kind = "list_item" if (is_body or para.level > 0) else "paragraph"
                    body.append(_Block(kind, text, page=idx))
            if not title:
                first_text = next((b for b in body if b.kind in ("paragraph", "list_item")), None)
                title = first_text.text[:60] if first_text else f"슬라이드 {idx}"
            first_title = first_title or title
            blocks.append(_Block("heading", title, level=2, page=idx))
            blocks.extend(body)
            if slide.has_notes_slide:
                notes = slide.notes_slide.notes_text_frame.text if slide.notes_slide.notes_text_frame else ""
                for ln in notes.splitlines():
                    if ln.strip():
                        blocks.append(_Block("note", ln.strip(), page=idx))

        deck_title = None
        try:
            deck_title = (prs.core_properties.title or "").strip() or None
        except Exception:
            pass
        return _assemble(
            blocks,
            source_id=source_id,
            filename=filename,
            file_type="pptx",
            fallback_title=deck_title or first_title or Path(filename).stem,
            page_count=len(prs.slides),
        )

    # -- docx --------------------------------------------------------------
    def _parse_docx(self, path: Path, filename: str, source_id: str) -> SourceMaterial:
        from docx import Document
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        doc = Document(str(path))
        blocks: list[_Block] = []
        pending_code: list[str] = []
        img_counter = 0

        def flush_code():
            if pending_code:
                blocks.append(_Block("code", "\n".join(pending_code)))
                pending_code.clear()

        for child in doc.element.body.iterchildren():
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "tbl":
                flush_code()
                rows = [[c.text.strip() for c in r.cells] for r in Table(child, doc).rows]
                blocks.append(_Block("table", rows=rows))
                continue
            if tag != "p":
                continue
            para = Paragraph(child, doc)
            text = para.text.strip()
            style = (para.style.name if para.style is not None else "") or ""

            try:
                for expr in child.xpath(".//m:oMath"):
                    t = "".join(expr.xpath(".//m:t/text()")).strip()
                    if t:
                        blocks.append(_Block("formula", t))
                for name in child.xpath(".//pic:cNvPr/@name"):
                    img_counter += 1
                    blocks.append(_Block("image", name or f"image-{img_counter}"))
            except Exception:
                pass

            if not text:
                flush_code()
                continue

            runs = [r for r in para.runs if r.text.strip()]
            is_code = "code" in style.lower() or (
                bool(runs) and all((r.font.name or "").lower() in _MONO_FONTS for r in runs)
            )
            if is_code:
                pending_code.append(para.text)
                continue
            flush_code()

            hm = re.match(r"^(?:Heading|제목)\s*(\d)$", style, re.IGNORECASE)
            if hm:
                blocks.append(_Block("heading", text, level=int(hm.group(1))))
            elif style.lower() == "title":
                blocks.append(_Block("heading", text, level=1))
            elif style.lower().startswith("list") or (
                child.pPr is not None and child.pPr.numPr is not None
            ):
                blocks.append(_Block("list_item", text))
            else:
                blocks.append(_Block("paragraph", text))
                blocks.extend(_inline_formulas(text, None, None))
        flush_code()

        core_title = None
        try:
            core_title = (doc.core_properties.title or "").strip() or None
        except Exception:
            pass
        return _assemble(
            blocks,
            source_id=source_id,
            filename=filename,
            file_type="docx",
            fallback_title=core_title or Path(filename).stem,
        )
