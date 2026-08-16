"""Generate docs/claude-monitor.ico - a sunburst mark, no dependencies.

Run this only when the icon design changes; the .ico it produces is committed.
Borrowing an icon from an installed app is not an option: those live in
version-stamped folders that vanish on the next update, leaving a blank tile.

    python tools/make_icon.py
"""
import math
import os
import struct
import zlib

ORANGE = (217, 119, 87)
SIZES = (16, 24, 32, 48, 64, 128, 256)
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "docs", "claude-monitor.ico")


def spokes(n=10):
    """Endpoints of the mark's radiating arms, on a unit circle."""
    out = []
    for i in range(n):
        a = math.pi * 2 * i / n
        out.append((math.cos(a), math.sin(a)))
    return out


def render(size, ss):
    """Anti-aliased RGBA raster, supersampled by `ss` then box-filtered."""
    w = size * ss
    half, radius, inner = w / 2.0, w * 0.44, w * 0.44 * 0.28
    thick = max(1.0, w * 0.052)
    arms = [((half + dx * inner, half + dy * inner),
             (half + dx * radius, half + dy * radius)) for dx, dy in spokes()]

    cover = [0.0] * (w * w)
    for (x0, y0), (x1, y1) in arms:
        vx, vy = x1 - x0, y1 - y0
        span = vx * vx + vy * vy
        lo_x, hi_x = int(min(x0, x1) - thick - 1), int(max(x0, x1) + thick + 2)
        lo_y, hi_y = int(min(y0, y1) - thick - 1), int(max(y0, y1) + thick + 2)
        for py in range(max(0, lo_y), min(w, hi_y)):
            for px in range(max(0, lo_x), min(w, hi_x)):
                t = 0.0 if span == 0 else \
                    ((px + .5 - x0) * vx + (py + .5 - y0) * vy) / span
                t = 0.0 if t < 0 else (1.0 if t > 1 else t)
                dx = px + .5 - (x0 + t * vx)
                dy = py + .5 - (y0 + t * vy)
                if dx * dx + dy * dy <= thick * thick:
                    cover[py * w + px] = 1.0

    # box-filter the supersampled coverage down to the target size
    out = bytearray(size * size * 4)
    area = float(ss * ss)
    for y in range(size):
        for x in range(size):
            hit = 0.0
            for sy in range(ss):
                row = (y * ss + sy) * w + x * ss
                for sx in range(ss):
                    hit += cover[row + sx]
            i = (y * size + x) * 4
            out[i:i + 3] = bytes(ORANGE)
            out[i + 3] = int(round(255 * hit / area))
    return bytes(out)


def png(size, rgba):
    def chunk(tag, body):
        c = struct.pack(">I", len(body)) + tag + body
        return c + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)

    raw = b"".join(b"\0" + rgba[y * size * 4:(y + 1) * size * 4]
                   for y in range(size))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def dib(size, rgba):
    """Classic BMP icon entry. Only 256px may be PNG-compressed; older
    consumers (and System.Drawing) reject PNG at the smaller sizes."""
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                         size * size * 4, 0, 0, 0, 0)
    rows = []
    for y in range(size - 1, -1, -1):          # DIBs are stored bottom-up
        row = bytearray()
        for x in range(size):
            r, g, b, a = rgba[(y * size + x) * 4:(y * size + x) * 4 + 4]
            row += bytes((b, g, r, a))
        rows.append(bytes(row))
    mask = b"\0" * (((size + 31) // 32) * 4 * size)
    return header + b"".join(rows) + mask


def main():
    images = []
    for size in SIZES:
        ss = 2 if size >= 128 else 4
        rgba = render(size, ss)
        images.append((size, png(size, rgba) if size == 256 else dib(size, rgba)))
        print("  rendered %dx%d" % (size, size))

    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for size, blob in images:
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0,
                               1, 32, len(blob), offset)
        offset += len(blob)
        blobs += blob

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "wb") as f:
        f.write(header + entries + blobs)
    print("wrote %s (%d bytes)" % (OUT, os.path.getsize(OUT)))


if __name__ == "__main__":
    main()
