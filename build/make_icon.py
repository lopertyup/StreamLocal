"""Generate the AutoFlix Windows icon.

The output is a multi-size .ico made from PNG frames using only the Python
standard library, so the build stays reproducible without extra image deps.
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "autoflix.ico"
SIZES = (16, 24, 32, 48, 64, 128, 256)


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def _mix(a: tuple[float, float, float], b: tuple[float, float, float], t: float) -> tuple[float, float, float]:
    t = _clamp(t)
    return (
        a[0] * (1.0 - t) + b[0] * t,
        a[1] * (1.0 - t) + b[1] * t,
        a[2] * (1.0 - t) + b[2] * t,
    )


def _rounded_rect_alpha(x: float, y: float, radius: float) -> float:
    qx = abs(x - 0.5) - (0.5 - radius)
    qy = abs(y - 0.5) - (0.5 - radius)
    outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
    inside = min(max(qx, qy), 0.0)
    distance = outside + inside - radius
    return _clamp(0.5 - distance * 95.0)


def _point_in_poly(x: float, y: float, points: tuple[tuple[float, float], ...]) -> bool:
    inside = False
    j = len(points) - 1
    for i, point in enumerate(points):
        xi, yi = point
        xj, yj = points[j]
        intersects = (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi
        if intersects:
            inside = not inside
        j = i
    return inside


def _polygon_alpha(x: float, y: float, points: tuple[tuple[float, float], ...]) -> float:
    return 1.0 if _point_in_poly(x, y, points) else 0.0


def _segment_alpha(x: float, y: float, ax: float, ay: float, bx: float, by: float, width: float) -> float:
    vx, vy = bx - ax, by - ay
    wx, wy = x - ax, y - ay
    length2 = vx * vx + vy * vy
    t = _clamp((wx * vx + wy * vy) / (length2 or 1e-9))
    px, py = ax + vx * t, ay + vy * t
    distance = math.hypot(x - px, y - py)
    return _clamp(0.5 - (distance - width * 0.5) * 90.0)


def _over(
    dst: tuple[float, float, float, float],
    src: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    sr, sg, sb, sa = src
    dr, dg, db, da = dst
    out_a = sa + da * (1.0 - sa)
    if out_a <= 0:
        return 0.0, 0.0, 0.0, 0.0
    return (
        (sr * sa + dr * da * (1.0 - sa)) / out_a,
        (sg * sa + dg * da * (1.0 - sa)) / out_a,
        (sb * sa + db * da * (1.0 - sa)) / out_a,
        out_a,
    )


def _sample(x: float, y: float) -> tuple[float, float, float, float]:
    base_alpha = _rounded_rect_alpha(x, y, 0.185)
    if base_alpha <= 0:
        return 0.0, 0.0, 0.0, 0.0

    top = (0.03, 0.05, 0.09)
    bottom = (0.12, 0.08, 0.18)
    bg = _mix(top, bottom, y)

    cyan_glow = _clamp(0.34 - math.hypot(x - 0.27, y - 0.24)) / 0.34
    rose_glow = _clamp(0.42 - math.hypot(x - 0.77, y - 0.78)) / 0.42
    bg = _mix(bg, (0.00, 0.74, 0.92), cyan_glow * 0.36)
    bg = _mix(bg, (0.94, 0.18, 0.46), rose_glow * 0.34)

    color = (bg[0], bg[1], bg[2], base_alpha)

    border_a = _rounded_rect_alpha(x, y, 0.185) * (1.0 - _rounded_rect_alpha((x - 0.5) / 0.86 + 0.5, (y - 0.5) / 0.86 + 0.5, 0.16))
    border = _mix((0.00, 0.86, 0.95), (1.00, 0.20, 0.44), _clamp((x + y - 0.55) / 0.9))
    color = _over(color, (border[0], border[1], border[2], border_a * 0.95))

    shadow = _polygon_alpha(x - 0.018, y + 0.026, ((0.36, 0.28), (0.36, 0.72), (0.72, 0.50)))
    color = _over(color, (0.0, 0.0, 0.0, shadow * 0.26))

    left_stroke = _segment_alpha(x, y, 0.29, 0.71, 0.45, 0.28, 0.075)
    right_stroke = _segment_alpha(x, y, 0.45, 0.28, 0.64, 0.71, 0.075)
    cross_stroke = _segment_alpha(x, y, 0.36, 0.58, 0.55, 0.58, 0.052)
    a_mark = max(left_stroke, right_stroke, cross_stroke)
    color = _over(color, (0.90, 0.98, 1.0, a_mark * 0.90))

    play = _polygon_alpha(x, y, ((0.43, 0.34), (0.43, 0.66), (0.70, 0.50)))
    play_tint = _mix((1.0, 1.0, 1.0), (0.00, 0.88, 0.96), _clamp((0.70 - x) / 0.30))
    color = _over(color, (play_tint[0], play_tint[1], play_tint[2], play * 0.98))

    shine = _segment_alpha(x, y, 0.24, 0.20, 0.76, 0.19, 0.035)
    color = _over(color, (1.0, 1.0, 1.0, shine * 0.14))

    return color


def _png_rgba(size: int) -> bytes:
    scale = 3 if size < 128 else 2
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            acc = [0.0, 0.0, 0.0, 0.0]
            for sy in range(scale):
                for sx in range(scale):
                    px = (x + (sx + 0.5) / scale) / size
                    py = (y + (sy + 0.5) / scale) / size
                    sample = _sample(px, py)
                    for i, value in enumerate(sample):
                        acc[i] += value
            factor = 1.0 / (scale * scale)
            rgba = [int(round(_clamp(value * factor) * 255)) for value in acc]
            row.extend(rgba)
        rows.append(bytes(row))

    raw = b"".join(rows)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)),
            chunk(b"IDAT", zlib.compress(raw, 9)),
            chunk(b"IEND", b""),
        ]
    )


def _ico(images: list[tuple[int, bytes]]) -> bytes:
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = []
    payloads = []
    for size, data in images:
        width_byte = 0 if size == 256 else size
        entries.append(struct.pack("<BBBBHHII", width_byte, width_byte, 0, 0, 1, 32, len(data), offset))
        payloads.append(data)
        offset += len(data)
    return header + b"".join(entries) + b"".join(payloads)


def main() -> None:
    images = [(size, _png_rgba(size)) for size in SIZES]
    OUT.write_bytes(_ico(images))
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
