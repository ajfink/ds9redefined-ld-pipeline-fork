"""Contact sheet of the worst moments: a labelled montage of frames from the delivered file."""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw

THUMB_W = 480
LABEL_H = 34


def grab_frame(ffmpeg, video: Path, t: float) -> Image.Image | None:
    """One decoded frame at time ``t`` (ffmpeg, PNG over a temp file, no scaling: judged at native pixels)."""
    tmp = Path(str(video) + f".contact-{int(t * 1000)}.png")
    try:
        ffmpeg.run(["-y", "-v", "error", "-ss", f"{max(t, 0):.3f}", "-i", str(video), "-frames:v", "1", str(tmp)],
                   name="contact-frame")
        if not tmp.is_file():
            return None
        with Image.open(io.BytesIO(tmp.read_bytes())) as im:
            return im.convert("RGB").copy()
    finally:
        tmp.unlink(missing_ok=True)


def montage(tiles: list[tuple[Image.Image, str]], *, cols: int = 3) -> Image.Image:
    """Arrange (image, label) tiles in a grid, each scaled to THUMB_W wide (aspect kept)."""
    if not tiles:
        raise ValueError("no frames for the contact sheet")
    scaled = []
    for im, label in tiles:
        h = max(1, round(im.height * THUMB_W / im.width))
        scaled.append((im.resize((THUMB_W, h), Image.LANCZOS), label))
    th = max(im.height for im, _ in scaled)
    rows = -(-len(scaled) // cols)
    sheet = Image.new("RGB", (cols * THUMB_W, rows * (th + LABEL_H)), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    for i, (im, label) in enumerate(scaled):
        x, y = (i % cols) * THUMB_W, (i // cols) * (th + LABEL_H)
        sheet.paste(im, (x, y))
        draw.text((x + 6, y + th + 4), label[:70], fill=(230, 230, 230))
    return sheet


def contact_sheet(ffmpeg, video: Path, moments: list[dict], out: Path) -> int:
    """Write the sheet; returns how many frames it holds (0 = nothing to show, no file written)."""
    tiles = []
    for m in moments:
        im = grab_frame(ffmpeg, video, m["time_s"])
        if im is not None:
            tiles.append((im, f"{m['time_s']:.1f}s  {m['reason']}"))
    if not tiles:
        return 0
    tmp = out.with_name(out.stem + ".tmp.png")
    montage(tiles).save(tmp, "PNG")
    tmp.replace(out)
    return len(tiles)
