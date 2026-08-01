"""
Regenerate the 12x12 status icon PNGs in src/busyboy/assets.

Run with `uv run --frozen python tools/generate_icons.py`. Standard library
only: no image dependency is added to the project for six tiny files.

Each icon is a filled disc in a GitHub status colour with a glyph knocked out
to transparency, which reads as black on the bar's LED matrix.
"""

import math
import pathlib
import struct
import zlib

SIZE = 12
OUTPUT_DIRECTORY = pathlib.Path(__file__).resolve().parent.parent / "src" / "busyboy" / "assets"

CHECK = """
............
............
............
.........##.
........##..
..#....##...
..##..##....
...##.##....
....####....
.....##.....
............
............
"""

CROSS = """
............
............
............
...##..##...
....####....
.....##.....
.....##.....
....####....
...##..##...
............
............
............
"""

SLASH = """
............
............
............
.......##...
......##....
.....##.....
....##......
...##.......
..##........
............
............
............
"""

BAR = """
............
............
............
............
............
...######...
...######...
............
............
............
............
............
"""

Mask = list[list[bool]]


def parse(art: str) -> Mask:
    """Turn a 12-line block of '#'/'.' art into a boolean mask."""
    rows = art.strip().splitlines()
    if len(rows) != SIZE or any(len(row) != SIZE for row in rows):
        raise ValueError(f"art must be {SIZE}x{SIZE}")
    return [[char == "#" for char in row] for row in rows]


def disc(radius: float) -> Mask:
    """A filled circle centred on the grid."""
    centre = (SIZE - 1) / 2
    return [[math.hypot(x - centre, y - centre) <= radius for x in range(SIZE)] for y in range(SIZE)]


def ring(outer: float, inner: float) -> Mask:
    """An annulus centred on the grid."""
    centre = (SIZE - 1) / 2
    return [[inner <= math.hypot(x - centre, y - centre) <= outer for x in range(SIZE)] for y in range(SIZE)]


def knock_out(base: Mask, glyph: Mask) -> Mask:
    """Remove the glyph from the base shape, leaving it transparent."""
    return [[lit and not cut for lit, cut in zip(base_row, glyph_row)] for base_row, glyph_row in zip(base, glyph)]


def render(mask: Mask) -> str:
    """Draw a mask as ASCII, for eyeballing a change before committing it."""
    return "\n".join("".join("#" if lit else "." for lit in row) for row in mask)


def png_bytes(mask: Mask, colour: str) -> bytes:
    """Encode a mask as an 8-bit RGBA PNG in the given #RRGGBB colour."""
    red, green, blue = (int(colour[index : index + 2], 16) for index in (1, 3, 5))
    raw = bytearray()
    for row in mask:
        raw.append(0)  # filter type 0 (None) for every scanline
        for lit in row:
            raw.extend((red, green, blue, 255) if lit else (0, 0, 0, 0))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


# Colours are GitHub's dark-mode status palette, which is brighter than the
# light-mode one and reads better on an LED matrix.
ICONS: dict[str, tuple[Mask, str]] = {
    "success": (knock_out(disc(5.6), parse(CHECK)), "#3FB950"),
    "failure": (knock_out(disc(5.6), parse(CROSS)), "#F85149"),
    "pending": (disc(3.6), "#D29922"),
    "in_progress": (ring(5.6, 3.2), "#D29922"),
    "cancelled": (knock_out(disc(5.6), parse(SLASH)), "#8B949E"),
    "skipped": (knock_out(disc(5.6), parse(BAR)), "#8B949E"),
}


def main() -> None:
    """Write every icon, printing its ASCII rendering for review."""
    OUTPUT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    for name, (mask, colour) in ICONS.items():
        (OUTPUT_DIRECTORY / f"{name}.png").write_bytes(png_bytes(mask, colour))
        print(f"\n=== {name} ({colour}) ===")
        print(render(mask))


if __name__ == "__main__":
    main()
