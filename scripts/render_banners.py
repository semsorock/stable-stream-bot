"""Regenerate bundled post banners (development tool; requires Pillow only)."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

ASSETS = Path(__file__).resolve().parents[1] / "bot" / "assets"
FONT = ASSETS / "fonts" / "LiberationSans-Bold.ttf"
SIZE = (1200, 400)
SCALE = 3
BACKGROUND = "#080F16"
FOREGROUND = "#F4F5FA"
BURN_COLOR = "#D94238"
TOKENS = {
    "USDM": "#1B52FF",
    "USDCx": "#2775CA",
    "USDA": "#17B6C2",
}


def render_banner(ticker: str, action: str, accent: str) -> Image.Image:
    """Mirror the reference layout with original registry artwork and crisp type."""
    image = Image.new("RGB", (SIZE[0] * SCALE, SIZE[1] * SCALE), BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 12 * SCALE - 1, SIZE[1] * SCALE), fill=accent)
    action_color = accent if action == "minted" else BURN_COLOR
    draw.rectangle((1056 * SCALE, 0, SIZE[0] * SCALE, SIZE[1] * SCALE), fill=action_color)

    with Image.open(ASSETS / "logos" / f"{ticker.lower()}.png") as source:
        logo = source.convert("RGBA")
        logo = logo.crop(logo.getbbox())
        logo = ImageOps.contain(logo, (176 * SCALE, 176 * SCALE), Image.Resampling.LANCZOS)
    image.paste(logo, (208 * SCALE - logo.width // 2, 200 * SCALE - logo.height // 2), logo)

    font = ImageFont.truetype(str(FONT), 144 * SCALE)
    bounds = draw.textbbox((0, 0), ticker, font=font)
    draw.text((354 * SCALE, 200 * SCALE - (bounds[3] + bounds[1]) // 2), ticker, font=font, fill=FOREGROUND)

    label_font = ImageFont.truetype(str(FONT), 44 * SCALE)
    label = action.upper()
    tracking = 7 * SCALE
    widths = [label_font.getlength(letter) for letter in label]
    label_width = round(sum(widths) + tracking * (len(label) - 1))
    label_image = Image.new("RGBA", (label_width + 20 * SCALE, 70 * SCALE))
    label_draw = ImageDraw.Draw(label_image)
    x = 10 * SCALE
    for letter, width in zip(label, widths, strict=True):
        label_draw.text((round(x), 0), letter, font=label_font, fill=FOREGROUND)
        x += width + tracking
    label_image = label_image.crop(label_image.getbbox()).rotate(90, expand=True)
    image.paste(
        label_image,
        (1128 * SCALE - label_image.width // 2, 200 * SCALE - label_image.height // 2),
        label_image,
    )
    return image.resize(SIZE, Image.Resampling.LANCZOS)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", type=Path, help="Optional contact sheet output path")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    output = ASSETS / "banners"
    output.mkdir(parents=True, exist_ok=True)
    preview = Image.new("RGB", (1200, len(TOKENS) * 200), "#171717") if args.preview else None
    for row, (ticker, accent) in enumerate(TOKENS.items()):
        for column, action in enumerate(("minted", "burned")):
            image = render_banner(ticker, action, accent)
            path = output / f"{ticker.lower()}-{action}.png"
            image.save(path, optimize=True)
            logging.info("Wrote %s", path)
            if preview is not None:
                preview.paste(image.resize((600, 200), Image.Resampling.LANCZOS), (column * 600, row * 200))
    if preview is not None:
        preview.save(args.preview)


if __name__ == "__main__":
    main()
