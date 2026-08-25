#!/usr/bin/env python3
"""Render a macOS-style rounded app icon source image.

The source logo is kept intact inside the icon, but the final 1024x1024 canvas
gets the rounded-square alpha silhouette expected by macOS app icons.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw


SIZE = 1024
CORNER_RADIUS = 228
CONTENT_SIZE = 824


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: render_app_icon.py <source.png> <output.png>", file=sys.stderr)
        return 2

    source_path = Path(sys.argv[1])
    output_path = Path(sys.argv[2])

    source = Image.open(source_path).convert("RGBA")
    source.thumbnail((CONTENT_SIZE, CONTENT_SIZE), Image.Resampling.LANCZOS)

    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    x = (SIZE - source.width) // 2
    y = (SIZE - source.height) // 2
    canvas.alpha_composite(source, (x, y))

    mask = Image.new("L", (SIZE, SIZE), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, SIZE - 1, SIZE - 1), radius=CORNER_RADIUS, fill=255)
    canvas.putalpha(Image.composite(canvas.getchannel("A"), mask, mask))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
