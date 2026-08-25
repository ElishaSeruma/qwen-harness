#!/usr/bin/env python3
"""Render a macOS-style rounded app icon source image.

macOS does NOT round app icons for you — unlike iOS, the artwork must already
carry the rounded silhouette in its own alpha channel. An icon that ships a
full-bleed square renders in the Dock as a hard-edged square sitting among its
correctly-rounded neighbours.

Geometry was measured off the system icons this sits next to, rather than
guessed. Notes, Calculator, Reminders, Music, Freeform and Contacts all report
an identical silhouette, so these numbers reproduce it:

    canvas 1024, shape 824 centred (a 100pt margin, which is what keeps this
    icon optically the same size as its neighbours), corner radius 197.

The corner is a plain circular arc: fitting a superellipse against those same
icons drove the exponent to 2.0 (i.e. circular), so the "squircle" shaping that
iOS uses would actually be wrong here.

The source art is treated as full-bleed — scaled to *cover* the square and
centre-cropped, then the silhouette is cut out of it. That is the right
treatment for artwork that is already a filled tile, as AppLogo.png is. Art
with its own transparent margin would want fitting rather than covering.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 1024
SHAPE = 824        # Apple's macOS icon grid: 824 of 1024
RADIUS = 197       # fitted against the shipped system icons (see above)
SUPERSAMPLE = 4    # draw the mask oversized and downsample, for clean edges


def shape_mask(side, radius):
    """An L-mask of the icon silhouette, white on black, antialiased."""
    big = side * SUPERSAMPLE
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, big - 1, big - 1), radius=radius * SUPERSAMPLE, fill=255
    )
    return mask.resize((side, side), Image.Resampling.LANCZOS)


def cover(source, side):
    """Scale to fill a side x side square, centre-cropped (no distortion)."""
    ratio = max(side / source.width, side / source.height)
    resized = source.resize(
        (max(1, round(source.width * ratio)), max(1, round(source.height * ratio))),
        Image.Resampling.LANCZOS,
    )
    x = (resized.width - side) // 2
    y = (resized.height - side) // 2
    return resized.crop((x, y, x + side, y + side))


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: render_app_icon.py <source.png> <output.png>", file=sys.stderr)
        return 2

    source = Image.open(Path(sys.argv[1])).convert("RGBA")
    art = cover(source, SHAPE)

    # Cut the silhouette out of the artwork itself. The previous version built
    # the mask at full canvas size and applied it to a canvas whose corners
    # were already transparent, so it clipped nothing and the square corners
    # survived into the Dock.
    mask = shape_mask(SHAPE, RADIUS)
    art.putalpha(Image.composite(art.getchannel("A"), mask, mask))

    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    inset = (SIZE - SHAPE) // 2
    canvas.alpha_composite(art, (inset, inset))

    output_path = Path(sys.argv[2])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
