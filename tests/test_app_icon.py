"""The app icon sits on Apple's macOS icon grid (tile at 824/1024, clear margin)."""
import struct
import unittest
import zlib
from pathlib import Path

ICNS = Path(__file__).resolve().parent.parent / "myOS.app" / "Contents" / "Resources" / "AppIcon.icns"


def _png_1024(icns):
    """The 1024px PNG (`ic10` entry) inside an .icns file."""
    data = icns.read_bytes()
    i = 8
    while i < len(data):
        kind, size = data[i:i + 4], struct.unpack(">I", data[i + 4:i + 8])[0]
        if kind == b"ic10":
            return data[i + 8:i + size]
        i += size
    raise AssertionError("no 1024px image in AppIcon.icns")


def _alpha_rows(png):
    """Alpha channel rows of an 8-bit RGBA, non-interlaced PNG."""
    pos, idat, w = 8, b"", 0
    while pos < len(png):
        n = struct.unpack(">I", png[pos:pos + 4])[0]
        kind, body = png[pos + 4:pos + 8], png[pos + 8:pos + 8 + n]
        if kind == b"IHDR":
            w, h, depth, ctype = struct.unpack(">IIBB", body[:10])
            assert (depth, ctype) == (8, 6), "expected 8-bit RGBA"
        elif kind == b"IDAT":
            idat += body
        pos += n + 12
    raw, bpp, stride = zlib.decompress(idat), 4, w * 4
    rows, prev = [], bytearray(stride)
    for r in range(h):
        f, line = raw[r * (stride + 1)], bytearray(raw[r * (stride + 1) + 1:(r + 1) * (stride + 1)])
        for x in range(stride):
            a = line[x - bpp] if x >= bpp else 0
            b, c = prev[x], prev[x - bpp] if x >= bpp else 0
            if f == 1:
                line[x] = (line[x] + a) & 255
            elif f == 2:
                line[x] = (line[x] + b) & 255
            elif f == 3:
                line[x] = (line[x] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        rows.append(line[3::4])
        prev = line
    return rows


class AppIcon(unittest.TestCase):
    def test_icon_keeps_apples_margin(self):
        alpha = _alpha_rows(_png_1024(ICNS))
        self.assertEqual(alpha[512][40], 0, "left margin must be clear")
        self.assertEqual(alpha[40][512], 0, "top margin must be clear")
        self.assertEqual(alpha[512][984], 0, "right margin must be clear")
        self.assertEqual(alpha[512][512], 255, "tile centre must be opaque")
        self.assertEqual(alpha[512][110], 255, "tile starts at 100px")


if __name__ == "__main__":
    unittest.main()
