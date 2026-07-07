"""Generate PWA icons with Pillow."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ICONS_DIR = Path(__file__).parent / "static" / "icons"
SIZES = [192, 512, 180]  # 180 for apple-touch-icon


def generate_icon(size: int) -> Image.Image:
    img = Image.new("RGB", (size, size), "#0d1117")
    draw = ImageDraw.Draw(img)

    # Draw a stylized "P" / paper plane motif
    margin = size // 6
    draw.rounded_rectangle(
        [margin, margin, size - margin, size - margin],
        radius=size // 8,
        fill="#1a2332",
        outline="#3b82f6",
        width=max(2, size // 64),
    )

    # Simple upward arrow (paper plane)
    cx, cy = size // 2, size // 2
    arrow_size = size // 4
    points = [
        (cx, cy - arrow_size),
        (cx - arrow_size // 2, cy + arrow_size // 4),
        (cx - arrow_size // 6, cy),
        (cx + arrow_size // 6, cy),
        (cx + arrow_size // 2, cy + arrow_size // 4),
    ]
    draw.polygon(points, fill="#3b82f6")

    return img


def main():
    ICONS_DIR.mkdir(parents=True, exist_ok=True)
    for size in SIZES:
        icon = generate_icon(size)
        if size == 180:
            path = ICONS_DIR / "apple-touch-icon.png"
        else:
            path = ICONS_DIR / f"icon-{size}.png"
        icon.save(path, "PNG")
        print(f"Generated {path}")


if __name__ == "__main__":
    main()
