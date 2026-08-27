#!/usr/bin/env python3
"""Render a Windows .ico from the app logo.

Deliberately NOT the macOS treatment in render_app_icon.py. That script cuts a
rounded-rectangle silhouette out of the artwork because macOS does not round
app icons for you and a full-bleed square renders in the Dock as a hard-edged
tile among correctly-rounded neighbours.

Windows is the opposite case. The taskbar, Start menu and Alt-Tab all draw the
icon as supplied, and every first-party Windows 11 icon is a full-bleed square
or a shape with its own transparency. Applying the macOS silhouette here would
produce visibly rounded corners that nothing else on the taskbar has — the same
bug as the Mac one, mirrored.

So: cover-crop to square, then emit the standard .ico size ladder. 256 is the
largest size Windows reads from an .ico (as a PNG-compressed entry, which
Pillow writes automatically); 16 and 20 are what the taskbar and title bar
actually show most of the time, so they are downsampled with LANCZOS rather
than left to whatever the shell picks.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

# The sizes Windows asks for: title bar / small taskbar (16, 20), list views
# (24, 32), medium tiles (48, 64), Start and Alt-Tab (128, 256).
ICO_SIZES = [(16, 16), (20, 20), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def cover(source: Image.Image, side: int) -> Image.Image:
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
        print("usage: render_app_icon_windows.py <source.png> <output.ico>", file=sys.stderr)
        return 2

    source = Image.open(Path(sys.argv[1])).convert("RGBA")
    master = cover(source, 256)

    output_path = Path(sys.argv[2])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Pillow's ICO writer downsamples from the master for each requested size.
    # Passing an already-256px master keeps every entry sharp; handing it the
    # original (often 1024px+) makes no difference to quality and is slower.
    master.save(output_path, format="ICO", sizes=ICO_SIZES)
    print(f"wrote {output_path} ({len(ICO_SIZES)} sizes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
