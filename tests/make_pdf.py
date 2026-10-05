"""Write plain text to a simple multi-page PDF (no dependencies), with a header and
footer on every page. Used to check that the sample lease's fictional label survives
PDF text extraction exactly as an uploaded PDF would go through it."""

import textwrap

LINES_PER_PAGE = 46


def _escape(text: str) -> bytes:
    raw = text.encode("cp1252", errors="replace")  # WinAnsiEncoding: keeps the em dash
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def text_to_pdf(text: str, header: str, footer: str) -> bytes:
    lines = [wrapped for line in text.splitlines() for wrapped in (textwrap.wrap(line, 95) or [""])]
    pages = [lines[i:i + LINES_PER_PAGE] for i in range(0, len(lines), LINES_PER_PAGE)]

    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", None,
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"]
    page_ids = []
    for number, page in enumerate(pages, start=1):
        body = [b"BT /F1 9 Tf 50 760 Td (" + _escape(header) + b") Tj ET"]
        y = 735
        for line in page:
            body.append(b"BT /F1 9 Tf 50 %d Td (" % y + _escape(line) + b") Tj ET")
            y -= 15
        body.append(b"BT /F1 9 Tf 50 30 Td (" + _escape(f"{footer} (page {number} of {len(pages)})") + b") Tj ET")
        stream = b"\n".join(body)
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        content_id = len(objects)
        objects.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> "
                       b"/Contents %d 0 R >>" % content_id)
        page_ids.append(len(objects))
    objects[1] = b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % i for i in page_ids) + b"] /Count %d >>" % len(page_ids)

    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)
