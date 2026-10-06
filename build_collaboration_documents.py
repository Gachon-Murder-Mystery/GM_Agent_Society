"""Export the three recipient proposals as editable DOCX, using the stdlib."""
import json
import re
from pathlib import Path
from xml.etree import ElementTree as E
from zipfile import ZIP_DEFLATED, ZipFile


NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
E.register_namespace("w", NS)
BASE = Path(__file__).with_name("docs") / "collaboration_20261006"
FILES = ("GM_common_proposal", "A_engine_proposal", "B_agent_proposal")


def node(name, parent=None, **attrs):
    value = E.Element(f"{{{NS}}}{name}", {f"{{{NS}}}{key}": str(val) for key, val in attrs.items()})
    if parent is not None:
        parent.append(value)
    return value


def plain(text):
    return re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", text).replace("**", "").replace("`", "")


def paragraph(parent, text, style="Normal"):
    p = node("p", parent)
    properties = node("pPr", p)
    node("pStyle", properties, val=style)
    run = node("r", p)
    t = node("t", run)
    t.text = plain(text)
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    return p


def table(parent, rows):
    count = len(rows[0])
    widths = [1100, 4500, 4200, 4800] if count == 4 else [4866] * count
    tbl = node("tbl", parent)
    properties = node("tblPr", tbl)
    node("tblW", properties, w=sum(widths), type="dxa")
    borders = node("tblBorders", properties)
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        node(edge, borders, val="single", sz=4, color="C6D1DE")
    node("tblLayout", properties, type="fixed")
    margins = node("tblCellMar", properties)
    for edge in ("top", "left", "bottom", "right"):
        node(edge, margins, w=100, type="dxa")
    grid = node("tblGrid", tbl)
    for width in widths:
        node("gridCol", grid, w=width)
    for row_index, cells in enumerate(rows):
        tr = node("tr", tbl)
        tr_props = node("trPr", tr)
        node("cantSplit", tr_props)
        if row_index == 0:
            node("tblHeader", tr_props)
        for width, text in zip(widths, cells):
            tc = node("tc", tr)
            props = node("tcPr", tc)
            node("tcW", props, w=width, type="dxa")
            if row_index == 0:
                node("shd", props, fill="EAF0F7", val="clear")
            node("vAlign", props, val="top")
            paragraph(tc, text, "TableHeader" if row_index == 0 else "TableText")
    paragraph(parent, "")


def styles():
    root = node("styles")
    defaults = node("docDefaults", root)
    run = node("rPr", node("rPrDefault", defaults))
    node("rFonts", run, ascii="Calibri", hAnsi="Calibri", eastAsia="맑은 고딕")
    node("lang", run, val="ko-KR", eastAsia="ko-KR")
    definitions = (("Normal", 22, False), ("Title", 34, True), ("Heading1", 26, True),
                   ("TableText", 19, False), ("TableHeader", 19, True), ("Footer", 18, False))
    for name, size, bold in definitions:
        s = node("style", root, type="paragraph", styleId=name)
        node("name", s, val=name)
        if name != "Normal":
            node("basedOn", s, val="Normal")
        p = node("pPr", s)
        if name in ("Title", "Heading1", "TableHeader"):
            node("keepNext", p)
        node("spacing", p, before=160 if name == "Heading1" else 0,
             after=110 if name in ("Normal", "Title", "Heading1") else 35, line=265, lineRule="auto")
        if name == "Heading1":
            node("outlineLvl", p, val=0)
        r = node("rPr", s)
        if bold:
            node("b", r)
            node("color", r, val="234263")
        node("sz", r, val=size)
        node("szCs", r, val=size)
    return root


def xml(root):
    return E.tostring(root, encoding="utf-8", xml_declaration=True)


def export(source, target):
    document = node("document")
    body = node("body", document)
    lines = source.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        if line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [value.strip() for value in lines[i].strip().split("|")[1:-1]]
                if not all(re.fullmatch(r":?-+:?", value) for value in cells):
                    rows.append(cells)
                else:
                    assert len(cells) == len(rows[0]), "table header/delimiter mismatch"
                i += 1
            assert all(len(row) == len(rows[0]) for row in rows), "table row width mismatch"
            table(body, rows)
            continue
        style = "Title" if line.startswith("# ") else "Heading1" if line.startswith("## ") else "Normal"
        paragraph(body, re.sub(r"^#{1,2} ", "", line), style)
        i += 1
    section = node("sectPr", body)
    footer_reference = node("footerReference", section, type="default")
    footer_reference.set("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id", "rIdFooter")
    node("pgSz", section, w=16838, h=11906, orient="landscape")
    node("pgMar", section, top=850, right=1100, bottom=850, left=1100, header=400, footer=400, gutter=0)
    footer = node("ftr")
    p = paragraph(footer, "GM 연결 협의 제안 · 2026-10-06  |  ", "Footer")
    node("fldSimple", p, instr="PAGE")
    package = {
        "[Content_Types].xml": b'''<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/><Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/></Types>''',
        "_rels/.rels": b'''<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rIdDocument" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>''',
        "word/_rels/document.xml.rels": b'''<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rIdStyles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/><Relationship Id="rIdFooter" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/></Relationships>''',
        "word/document.xml": xml(document), "word/styles.xml": xml(styles()), "word/footer1.xml": xml(footer),
    }
    with ZipFile(target, "w", ZIP_DEFLATED) as archive:
        for name, content in package.items():
            archive.writestr(name, content)
    with ZipFile(target) as archive:
        assert archive.testzip() is None
        for name in archive.namelist():
            E.fromstring(archive.read(name))
    return {"source": source.name, "output": target.name, "bytes": target.stat().st_size,
            "validation": "DOCX package/XML and table structure; native Word rendering not included"}


def main():
    records = [export(BASE / f"{name}.md", BASE / f"{name}.docx") for name in FILES]
    print(json.dumps(records, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
