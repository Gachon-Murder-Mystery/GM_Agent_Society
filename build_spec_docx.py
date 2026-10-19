"""Export the game spec Markdown to DOCX with working internal links (stdlib only).

python -m gm_coordinator.build_spec_docx GAME_SPEC_v1.0.md "<out>.docx"
Supports: #/##/### headings, <a id> anchors (as bookmarks), [text](#id) links,
**bold**, `code`, bullet/numbered lines and pipe tables.
"""
import re
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile
from xml.etree import ElementTree as E

from .build_collaboration_documents import NS as W, node, styles, xml

XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
REL_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
PAGE_WIDTH = 14638  # landscape A4 minus margins, in dxa
INLINE = re.compile(r"\[([^\]]+)\]\(#([^)]+)\)|\*\*([^*]+)\*\*|`([^`]+)`")


def bookmark(name):
    return re.sub(r"[^0-9A-Za-z_]", "_", name)  # Word bookmarks reject hyphens


def text_run(parent, text, bold=False, link=False):
    run = node("r", parent)
    if bold or link:
        props = node("rPr", run)
        if bold:
            node("b", props)
        if link:
            node("color", props, val="1F4E6B")
            node("u", props, val="single")
    t = node("t", run)
    t.text = text
    t.set(XML_SPACE, "preserve")


def inline(parent, text):
    position = 0
    for match in INLINE.finditer(text):
        if match.start() > position:
            text_run(parent, text[position:match.start()])
        label, target, bold, code = match.groups()
        if label is not None:
            link = node("hyperlink", parent, anchor=bookmark(target), history=1)
            text_run(link, label, link=True)
        else:
            text_run(parent, bold or code, bold=bold is not None)
        position = match.end()
    if position < len(text):
        text_run(parent, text[position:])


def paragraph(parent, text, style="Normal", anchors=(), counter=None):
    p = node("p", parent)
    node("pStyle", node("pPr", p), val=style)
    for name in anchors:
        counter[0] += 1
        start = node("bookmarkStart", p, id=counter[0])
        start.set(f"{{{W}}}name", bookmark(name))  # node() reserves the name keyword
        node("bookmarkEnd", p, id=counter[0])
    inline(p, text)
    return p


def widths_for(rows):
    """Column widths proportional to content length, clamped, summing to the page."""
    weights = [min(max(max(len(row[i]) for row in rows), 3), 60) for i in range(len(rows[0]))]
    total = sum(weights)
    return [max(600, PAGE_WIDTH * w // total) for w in weights]


def table(parent, rows):
    widths = widths_for(rows)
    tbl = node("tbl", parent)
    props = node("tblPr", tbl)
    node("tblW", props, w=sum(widths), type="dxa")
    borders = node("tblBorders", props)
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        node(edge, borders, val="single", sz=4, color="C6D1DE")
    node("tblLayout", props, type="fixed")
    grid = node("tblGrid", tbl)
    for width in widths:
        node("gridCol", grid, w=width)
    for index, cells in enumerate(rows):
        tr = node("tr", tbl)
        tr_props = node("trPr", tr)
        node("cantSplit", tr_props)
        if index == 0:
            node("tblHeader", tr_props)
        for width, cell in zip(widths, cells):
            tc = node("tc", tr)
            tc_props = node("tcPr", tc)
            node("tcW", tc_props, w=width, type="dxa")
            if index == 0:
                node("shd", tc_props, fill="EAF0F7", val="clear")
            paragraph(tc, cell, "TableHeader" if index == 0 else "TableText")
    node("p", parent)


def spec_styles():
    root = styles()
    s = node("style", root, type="paragraph", styleId="Heading2")
    node("name", s, val="Heading2")
    node("basedOn", s, val="Normal")
    p = node("pPr", s)
    node("keepNext", p)
    node("spacing", p, before=120, after=80)
    node("outlineLvl", p, val=1)
    r = node("rPr", s)
    node("b", r)
    node("color", r, val="1F4E6B")
    node("sz", r, val=23)
    return root


def export(source, target, footer_text):
    document = node("document")
    body = node("body", document)
    lines = Path(source).read_text(encoding="utf-8").splitlines()
    pending, counter, i = [], [0], 0
    while i < len(lines):
        line = lines[i].strip()
        anchor = re.fullmatch(r'<a id="([^"]+)"></a>', line)
        if anchor:
            pending.append(anchor.group(1))
            i += 1
            continue
        if not line:
            i += 1
            continue
        if line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().split("|")[1:-1]]
                if not all(re.fullmatch(r":?-+:?", c) for c in cells):
                    rows.append(cells)
                i += 1
            assert all(len(r) == len(rows[0]) for r in rows), "table row width mismatch"
            table(body, rows)
            continue
        heading = re.match(r"^(#{1,3}) (.*)", line)
        if heading:
            style = {1: "Title", 2: "Heading1", 3: "Heading2"}[len(heading.group(1))]
            paragraph(body, heading.group(2), style, pending, counter)
            pending = []
        else:
            line = re.sub(r"^- ", "• ", line)
            paragraph(body, line, "Normal", pending, counter)
            pending = []
        i += 1
    section = node("sectPr", body)
    node("footerReference", section, type="default").set(REL_ID, "rIdFooter")
    node("pgSz", section, w=16838, h=11906, orient="landscape")
    node("pgMar", section, top=850, right=1100, bottom=850, left=1100, header=400, footer=400, gutter=0)
    footer = node("ftr")
    p = paragraph(footer, footer_text + "  |  ", "Footer", (), counter)
    node("fldSimple", p, instr="PAGE")
    package = {
        "[Content_Types].xml": b'<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/><Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/></Types>',
        "_rels/.rels": b'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rIdDocument" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
        "word/_rels/document.xml.rels": b'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rIdStyles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/><Relationship Id="rIdFooter" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/></Relationships>',
        "word/document.xml": xml(document), "word/styles.xml": xml(spec_styles()), "word/footer1.xml": xml(footer),
    }
    with ZipFile(target, "w", ZIP_DEFLATED) as archive:
        for name, content in package.items():
            archive.writestr(name, content)
    with ZipFile(target) as archive:
        assert archive.testzip() is None
        doc = E.fromstring(archive.read("word/document.xml"))
    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    marks = {b.get(w + "name") for b in doc.iter(w + "bookmarkStart")}
    links = [h.get(w + "anchor") for h in doc.iter(w + "hyperlink")]
    missing = sorted(set(links) - marks)
    assert not missing, f"links without bookmarks: {missing}"
    return {"output": str(target), "bookmarks": len(marks), "links": len(links)}


def main():
    source, target = sys.argv[1], sys.argv[2]
    title = Path(source).read_text(encoding="utf-8").splitlines()[0].lstrip("# ").strip()
    print(export(source, target, title))


if __name__ == "__main__":
    main()
