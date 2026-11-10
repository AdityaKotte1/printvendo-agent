#!/usr/bin/env python3
"""Build a four-page PDF that tells a working print path from a broken one.

"Some PDFs print gibberish" is not a fault anybody can chase. Each page here
leans on one thing a printer's page-language interpreter can get wrong, so a
garbled page names its own cause:

  page 1  base-14 text only -- no font data is sent at all. Garbled here means
          the queue's page language is simply wrong (PostScript sent to a
          printer in PCL mode, or the reverse).
  page 2  an embedded TrueType face. This is what a PostScript emulation
          usually chokes on, and the commonest cause of a page of symbols.
  page 3  transparency. It has to be flattened before it reaches a PCL or PS
          printer; a filter that skips that step drops or blackens the shapes.
  page 4  vector art, hairlines and rotated text -- the raster path's stress.

Every page carries a sentence to read back, so "correct" is a thing you can
check at a glance rather than a judgement.

Standard library only: the kiosk has python3 and nothing else.

    python3 make-print-test-pdf.py print-test.pdf
    lp -d mono-1 print-test.pdf
"""

from __future__ import annotations

import math
import struct
import sys
import zlib
from pathlib import Path

PAGE_W, PAGE_H = 595, 842  # A4 in points

# Faces to embed, best first. A kiosk built from KIOSK-UBUNTU.md has DejaVu;
# the others are here so this script can be checked on a workstation.
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
]

FIRST_CHAR, LAST_CHAR = 32, 126


# ── reading the TrueType file ─────────────────────────────────────────────
# Widths come out of the font rather than being guessed at 600 each: a page
# whose letters overlap looks broken, and this page exists to answer whether
# the printer broke it.


def _tables(data: bytes) -> dict[str, tuple[int, int]]:
    count = struct.unpack(">H", data[4:6])[0]
    out = {}
    for i in range(count):
        at = 12 + 16 * i
        tag = data[at : at + 4].decode("latin-1")
        start, length = struct.unpack(">II", data[at + 8 : at + 16])
        out[tag] = (start, length)
    return out


def _cmap_format4(data: bytes, at: int) -> dict[int, int]:
    seg_x2 = struct.unpack(">H", data[at + 6 : at + 8])[0]
    segs = seg_x2 // 2
    ends = at + 14
    starts = ends + seg_x2 + 2
    deltas = starts + seg_x2
    ranges = deltas + seg_x2

    mapping = {}
    for s in range(segs):
        end = struct.unpack(">H", data[ends + 2 * s : ends + 2 * s + 2])[0]
        start = struct.unpack(">H", data[starts + 2 * s : starts + 2 * s + 2])[0]
        delta = struct.unpack(">h", data[deltas + 2 * s : deltas + 2 * s + 2])[0]
        r_off = struct.unpack(">H", data[ranges + 2 * s : ranges + 2 * s + 2])[0]
        if start > LAST_CHAR or end < FIRST_CHAR:
            continue
        for code in range(max(start, FIRST_CHAR), min(end, LAST_CHAR) + 1):
            if r_off == 0:
                gid = (code + delta) & 0xFFFF
            else:
                glyph_at = ranges + 2 * s + r_off + 2 * (code - start)
                gid = struct.unpack(">H", data[glyph_at : glyph_at + 2])[0]
                if gid:
                    gid = (gid + delta) & 0xFFFF
            if gid:
                mapping[code] = gid
    return mapping


def _unicode_cmap(data: bytes, start: int) -> dict[int, int]:
    count = struct.unpack(">H", data[start + 2 : start + 4])[0]
    best = None
    for i in range(count):
        at = start + 4 + 8 * i
        plat, enc = struct.unpack(">HH", data[at : at + 4])
        off = struct.unpack(">I", data[at + 4 : at + 8])[0]
        fmt = struct.unpack(">H", data[start + off : start + off + 2])[0]
        if fmt != 4:
            continue
        rank = {(3, 1): 0, (3, 10): 1, (0, 3): 2, (0, 4): 2, (0, 6): 2}.get((plat, enc))
        if rank is not None and (best is None or rank < best[0]):
            best = (rank, start + off)
    if best is None:
        raise SystemExit("font has no format-4 Unicode cmap; pick another face")
    return _cmap_format4(data, best[1])


class Face:
    """Just enough of a TrueType file to embed it and space its letters."""

    def __init__(self, path: Path):
        self.path = path
        self.data = path.read_bytes()
        tabs = _tables(self.data)
        for need in ("head", "hhea", "hmtx", "cmap", "glyf", "loca"):
            if need not in tabs:
                raise SystemExit(f"{path.name}: no {need} table (a CFF font? use a .ttf)")

        head = tabs["head"][0]
        self.units = struct.unpack(">H", self.data[head + 18 : head + 20])[0]
        self.bbox = struct.unpack(">hhhh", self.data[head + 36 : head + 44])

        hhea = tabs["hhea"][0]
        self.ascent, self.descent = struct.unpack(">hh", self.data[hhea + 4 : hhea + 8])
        metrics = struct.unpack(">H", self.data[hhea + 34 : hhea + 36])[0]

        hmtx = tabs["hmtx"][0]
        self.advances = [
            struct.unpack(">H", self.data[hmtx + 4 * i : hmtx + 4 * i + 2])[0]
            for i in range(metrics)
        ]
        self.cmap = _unicode_cmap(self.data, tabs["cmap"][0])

    def _scale(self, v: int) -> int:
        return round(v * 1000 / self.units)

    def widths(self) -> list[int]:
        out = []
        for code in range(FIRST_CHAR, LAST_CHAR + 1):
            gid = self.cmap.get(code, 0)
            adv = self.advances[gid] if gid < len(self.advances) else self.advances[-1]
            out.append(self._scale(adv))
        return out

    def descriptor_numbers(self) -> dict[str, int]:
        x0, y0, x1, y1 = (self._scale(v) for v in self.bbox)
        return {
            "bbox": (x0, y0, x1, y1),
            "ascent": self._scale(self.ascent),
            "descent": self._scale(self.descent),
        }

    @property
    def ps_name(self) -> str:
        # The name in the PDF need only be self-consistent; the bytes are here.
        return "Embedded" + "".join(c for c in self.path.stem if c.isalnum())


def find_face(explicit: str | None) -> Face:
    if explicit:
        return Face(Path(explicit))
    for candidate in FONT_CANDIDATES:
        p = Path(candidate)
        if p.is_file():
            return Face(p)
    raise SystemExit(
        "no TrueType face found. Install one:\n"
        "  sudo apt-get install -y fonts-dejavu-core\n"
        "or pass a path: python3 make-print-test-pdf.py out.pdf /path/to/font.ttf"
    )


# ── writing the PDF ───────────────────────────────────────────────────────


def esc(s: str) -> bytes:
    out = s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    return out.encode("latin-1", "replace")


def text(x: float, y: float, size: float, font: str, s: str) -> bytes:
    return b"BT /%s %g Tf 1 0 0 1 %g %g Tm (%s) Tj ET\n" % (
        font.encode(),
        size,
        x,
        y,
        esc(s),
    )


def lines(x: float, y: float, size: float, font: str, leading: float, rows) -> bytes:
    out = b"BT /%s %g Tf %g TL 1 0 0 1 %g %g Tm\n" % (
        font.encode(),
        size,
        leading,
        x,
        y,
    )
    for row in rows:
        out += b"(%s) Tj T*\n" % esc(row)
    return out + b"ET\n"


class Pdf:
    def __init__(self) -> None:
        self.objs: list[bytes | None] = []

    def reserve(self) -> int:
        self.objs.append(None)
        return len(self.objs)

    def put(self, num: int, body: bytes) -> int:
        self.objs[num - 1] = body
        return num

    def add(self, body: bytes) -> int:
        return self.put(self.reserve(), body)

    def stream(self, extra: bytes, payload: bytes, compress: bool = True) -> bytes:
        if compress:
            payload = zlib.compress(payload, 9)
            extra += b" /Filter /FlateDecode"
        return (
            b"<< /Length %d%s >>\nstream\n" % (len(payload), extra)
            + payload
            + b"\nendstream"
        )

    def build(self, root: int) -> bytes:
        out = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0] * (len(self.objs) + 1)
        for i, body in enumerate(self.objs, start=1):
            if body is None:
                raise AssertionError(f"object {i} was reserved and never written")
            offsets[i] = len(out)
            out += b"%d 0 obj\n" % i + body + b"\nendobj\n"

        xref_at = len(out)
        out += b"xref\n0 %d\n" % (len(self.objs) + 1)
        out += b"0000000000 65535 f \n"
        for i in range(1, len(self.objs) + 1):
            out += b"%010d 00000 n \n" % offsets[i]
        out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
            len(self.objs) + 1,
            root,
            xref_at,
        )
        return bytes(out)


# ── the four pages ────────────────────────────────────────────────────────

SENTINEL = "If you can read this line, this page printed correctly."


def page1(base: str) -> bytes:
    body = text(50, PAGE_H - 70, 22, base, "PAGE 1 of 4  -  plain text, no embedded font")
    body += lines(
        50,
        PAGE_H - 110,
        11,
        base,
        15,
        [
            "This page uses Helvetica, one of the 14 fonts every PostScript",
            "printer is required to have. No font data is sent with the job.",
            "",
            SENTINEL,
            "",
            "Garbled here means the queue is speaking the wrong language",
            "altogether: PostScript sent to a printer in PCL mode, or PCL sent",
            "to one in PostScript mode. Check the printer panel's",
            "Emulation setting and set it to Auto, then reprint.",
            "",
            "Reference strings -- compare these character for character:",
            "  The quick brown fox jumps over the lazy dog.",
            "  ABCDEFGHIJKLMNOPQRSTUVWXYZ abcdefghijklmnopqrstuvwxyz",
            "  0123456789  !\"#$%&'*+,-./:;<=>?@[]^_{|}~",
            "",
            "Rules below. A broken raster path loses the thin ones first.",
        ],
    )
    y = PAGE_H - 360
    for i, w in enumerate((0.25, 0.5, 1, 2, 4)):
        body += b"%g w 50 %g m 545 %g l S\n" % (w, y - i * 22, y - i * 22)
        body += text(50, y - i * 22 + 5, 8, base, f"{w} pt rule")
    return body


def page2(base: str, embedded: str) -> bytes:
    body = text(50, PAGE_H - 70, 22, base, "PAGE 2 of 4  -  embedded TrueType font")
    body += lines(
        50,
        PAGE_H - 100,
        10,
        base,
        13,
        [
            "The heading above is Helvetica. Everything below is a TrueType face",
            "sent inside the job -- which is what nearly every real document does.",
        ],
    )
    body += lines(
        50,
        PAGE_H - 150,
        13,
        embedded,
        18,
        [
            SENTINEL,
            "",
            "The quick brown fox jumps over the lazy dog, 0123456789.",
            "Jackdaws love my big sphinx of quartz.",
            "",
            "Full printable ASCII, in four rows:",
            " !\"#$%&'()*+,-./0123456789:;<=>?",
            "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_",
            "`abcdefghijklmnopqrstuvwxyz{|}~",
            "",
            "Rs 3.00 per page - 10 pages - Rs 30.00 total",
            "",
            "A page of symbols, boxes or stray letters here, with page 1 correct,",
            "means the printer could not take the font. That is the gibberish",
            "you have been seeing. Switch the queue to a driver that rasterises",
            "(brlaser) or to the printer's native PCL-XL, and reprint.",
        ],
    )
    body += lines(
        50,
        PAGE_H - 470,
        9,
        embedded,
        11,
        [
            "Small sizes stress it hardest -- 9 point, then 7, then 6:",
            "   Shakespeare wrote: the course of true love never did run smooth.",
        ],
    )
    body += text(50, PAGE_H - 500, 7, embedded, "   Seven point: " + SENTINEL)
    body += text(50, PAGE_H - 514, 6, embedded, "   Six point: " + SENTINEL)
    return body


def page3(base: str, gs: str) -> bytes:
    body = text(50, PAGE_H - 70, 22, base, "PAGE 3 of 4  -  transparency")
    body += lines(
        50,
        PAGE_H - 105,
        10,
        base,
        13,
        [
            "Three squares overlap at half opacity. A printer cannot do this:",
            "the host has to flatten it into ordinary ink first. If the squares",
            "are missing, solid, or a black block, the filter chain skipped that",
            "step -- the usual sign of a raw queue with no driver attached.",
            "",
            "Correct: each overlap is visibly darker than the squares themselves,",
            "and the text under them stays readable.",
        ],
    )
    # Drawn first, so the squares land on top of it: half-opacity ink over
    # text is the part a flattener gets wrong, and the sentence says whether
    # it did.
    body += text(100, 500, 11, base, "under the squares: " + SENTINEL)
    body += b"q /%s gs\n" % gs.encode()
    body += b"0.1 0.1 0.1 rg 90 420 170 170 re f\n"
    body += b"0.45 0.45 0.45 rg 190 480 170 170 re f\n"
    body += b"0.75 0.75 0.75 rg 290 420 170 170 re f\n"
    body += b"Q\n"
    body += lines(
        50,
        380,
        10,
        base,
        14,
        [
            "Below: a grey ramp in 5% steps, 0% to 100%.",
            "Correct is a smooth climb with both ends distinguishable from their",
            "neighbours. Banding or a jump straight to black is a driver problem,",
            "not a toner problem.",
        ],
    )
    for i in range(21):
        shade = i / 20
        body += b"%g g %g 260 23 60 re f\n" % (1 - shade, 50 + i * 23.5)
    body += b"0 g\n"
    return body


def page4(base: str) -> bytes:
    body = text(50, PAGE_H - 70, 22, base, "PAGE 4 of 4  -  vectors and rotated text")
    body += lines(
        50,
        PAGE_H - 100,
        10,
        base,
        13,
        [
            "Curves, a fan of hairlines, a hatch and text at every angle.",
            "Correct: both circles are round, the fan fills the ring evenly with",
            "no gaps and no line crossing either circle, the hatch below is an",
            "even grid, and all twelve angle labels read cleanly.",
        ],
    )

    # A fan of hairlines filling the ring between the two circles. Every line
    # starts and ends on a circle, so a dropped or shortened path shows up as
    # a gap against its neighbours rather than having to be measured.
    cx, cy = 300, 430
    hub, rim = 92, 170
    body += b"0.4 w\n"
    for i in range(72):
        ang = math.radians(i * 5)
        c, sn = math.cos(ang), math.sin(ang)
        body += b"%g %g m %g %g l S\n" % (
            cx + hub * c,
            cy + hub * sn,
            cx + rim * c,
            cy + rim * sn,
        )

    # Two circles, each four Bezier quarters. A quarter's handles sit k of the
    # radius along the tangent at either end; anything else draws a shape that
    # is visibly not a circle, and this page is read by eye.
    k = 0.5523
    quarters = [(1, 0), (0, 1), (-1, 0), (0, -1), (1, 0)]
    for r in (hub, rim):
        body += b"0.9 w %g %g m\n" % (cx + r, cy)
        for (ax, ay), (bx, by) in zip(quarters, quarters[1:]):
            body += b"%g %g %g %g %g %g c\n" % (
                cx + r * (ax - k * ay),
                cy + r * (ay + k * ax),
                cx + r * (bx + k * by),
                cy + r * (by - k * bx),
                cx + r * bx,
                cy + r * by,
            )
        body += b"S\n"

    for i in range(12):
        ang = math.radians(i * 30)
        c, sn = math.cos(ang), math.sin(ang)
        body += b"BT /%s 9 Tf %g %g %g %g %g %g Tm (%s) Tj ET\n" % (
            base.encode(),
            c,
            sn,
            -sn,
            c,
            cx + 55 * c,
            cy + 55 * sn,
            esc(f"{i * 30}"),
        )

    body += b"0.15 w\n"
    for i in range(40):
        body += b"50 %g m 545 %g l S\n" % (120 + i * 2.2, 120 + i * 2.2)
    for i in range(100):
        body += b"%g 120 m %g 206 l S\n" % (50 + i * 5, 50 + i * 5)
    body += text(60, 100, 11, base, SENTINEL)
    return body


def main(argv: list[str]) -> int:
    out_path = Path(argv[1] if len(argv) > 1 else "print-test.pdf")
    face = find_face(argv[2] if len(argv) > 2 else None)

    pdf = Pdf()
    catalog = pdf.reserve()
    pages = pdf.reserve()

    helv = pdf.add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                   b"/Encoding /WinAnsiEncoding >>")

    file_obj = pdf.add(
        pdf.stream(b" /Length1 %d" % len(face.data), face.data)
    )
    nums = face.descriptor_numbers()
    x0, y0, x1, y1 = nums["bbox"]
    descriptor = pdf.add(
        b"<< /Type /FontDescriptor /FontName /%s /Flags 32 "
        b"/FontBBox [%d %d %d %d] /ItalicAngle 0 /Ascent %d /Descent %d "
        b"/CapHeight %d /StemV 80 /FontFile2 %d 0 R >>"
        % (
            face.ps_name.encode(),
            x0,
            y0,
            x1,
            y1,
            nums["ascent"],
            nums["descent"],
            round(nums["ascent"] * 0.72),
            file_obj,
        )
    )
    widths = b" ".join(b"%d" % w for w in face.widths())
    embedded = pdf.add(
        b"<< /Type /Font /Subtype /TrueType /BaseFont /%s /FirstChar %d "
        b"/LastChar %d /Widths [%s] /Encoding /WinAnsiEncoding "
        b"/FontDescriptor %d 0 R >>"
        % (face.ps_name.encode(), FIRST_CHAR, LAST_CHAR, widths, descriptor)
    )
    alpha = pdf.add(b"<< /Type /ExtGState /ca 0.5 /CA 0.5 /BM /Multiply >>")

    resources = (
        b"<< /Font << /F1 %d 0 R /F2 %d 0 R >> /ExtGState << /GS1 %d 0 R >> >>"
        % (helv, embedded, alpha)
    )

    bodies = [
        page1("F1"),
        page2("F1", "F2"),
        page3("F1", "GS1"),
        page4("F1"),
    ]
    page_objs = []
    for body in bodies:
        content = pdf.add(pdf.stream(b"", body))
        page_objs.append(
            pdf.add(
                b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %d %d] "
                b"/Resources %s /Contents %d 0 R >>"
                % (pages, PAGE_W, PAGE_H, resources, content)
            )
        )

    kids = b" ".join(b"%d 0 R" % n for n in page_objs)
    pdf.put(
        pages,
        b"<< /Type /Pages /Count %d /Kids [%s] >>" % (len(page_objs), kids),
    )
    pdf.put(catalog, b"<< /Type /Catalog /Pages %d 0 R >>" % pages)

    data = pdf.build(catalog)
    out_path.write_bytes(data)

    # Read our own xref back: an offset that is out by a byte is a file some
    # viewers repair silently and a printer rejects, which is exactly the
    # failure this script exists to rule out.
    start = int(data.rsplit(b"startxref", 1)[1].split()[0])
    table = data[start:].split(b"\n")
    for i, row in enumerate(table[3 : 3 + len(pdf.objs)], start=1):
        at = int(row.split()[0])
        head = b"%d 0 obj" % i
        assert data[at : at + len(head)] == head, f"xref row {i} points at nothing"

    print(f"{out_path} -- {len(data)} bytes, 4 pages, font {face.path}")
    print("  page 1  base-14 text, no font sent")
    print("  page 2  embedded TrueType -- the usual cause of gibberish")
    print("  page 3  transparency and a grey ramp")
    print("  page 4  vectors, hairlines, rotated text")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
