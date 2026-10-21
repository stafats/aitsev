"""Generates every Aitsev IDE icon and installer image from one drawing (needs Pillow).

    python ide/branding/make_icons.py

Replace draw_mark() with your own logo to rebrand everything at once.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).parent
OUT = HERE / "generated"
BLUE_TOP = (32, 92, 222)
BLUE_BOTTOM = (18, 52, 160)


def draw_mark(size: int) -> Image.Image:
    s = size / 1024
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    bg = Image.new("RGBA", (size, size))
    grad = ImageDraw.Draw(bg)
    for y in range(size):
        t = y / max(1, size - 1)
        grad.line(
            [(0, y), (size, y)],
            fill=tuple(int(a + (b - a) * t) for a, b in zip(BLUE_TOP, BLUE_BOTTOM, strict=True)) + (255,),
        )
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([40 * s, 40 * s, size - 40 * s, size - 40 * s], radius=220 * s, fill=255)
    img.paste(bg, (0, 0), mask)
    d = ImageDraw.Draw(img)
    w = max(2, int(96 * s))
    d.line([(300 * s, 800 * s), (512 * s, 220 * s)], fill="white", width=w)
    d.line([(512 * s, 220 * s), (724 * s, 800 * s)], fill="white", width=w)
    d.ellipse([512 * s - w / 2, 220 * s - w / 2, 512 * s + w / 2, 220 * s + w / 2], fill="white")
    d.line([(392 * s, 600 * s), (632 * s, 600 * s)], fill="white", width=max(2, int((96 - 20) * s)))
    for x, y, c in [(250, 330, (255, 196, 61)), (774, 330, (91, 224, 170)), (512, 870, (255, 122, 122))]:
        r = 46 * s
        d.ellipse([x * s - r, y * s - r, x * s + r, y * s + r], fill=c)
    return img


def installer_image(width: int, height: int, small: bool) -> Image.Image:
    canvas = Image.new("RGB", (width, height), (255, 255, 255) if small else BLUE_BOTTOM)
    if not small:
        top = Image.new("RGB", (width, height))
        g = ImageDraw.Draw(top)
        for y in range(height):
            t = y / max(1, height - 1)
            g.line([(0, y), (width, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(BLUE_TOP, BLUE_BOTTOM, strict=True)))
        canvas = top
    mark_size = int(min(width, height) * (0.9 if small else 0.62))
    mark = draw_mark(mark_size)
    x = (width - mark_size) // 2
    y = (height - mark_size) // 2 if small else int(height * 0.18)
    canvas.paste(mark, (x, y), mark)
    return canvas


def main() -> None:
    OUT.mkdir(exist_ok=True)
    big = draw_mark(1024)
    big.save(OUT / "code.png")  # Linux
    big.resize((150, 150), Image.LANCZOS).save(OUT / "code_150x150.png")
    big.resize((70, 70), Image.LANCZOS).save(OUT / "code_70x70.png")
    big.save(OUT / "code.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    big.save(OUT / "code.icns")
    for scale, (w, h) in {100: (164, 314), 125: (192, 386), 150: (246, 459), 175: (273, 556), 200: (328, 604), 225: (355, 700), 250: (410, 797)}.items():
        installer_image(w, h, small=False).save(OUT / f"inno-big-{scale}.bmp")
    for scale, (w, h) in {100: (55, 55), 125: (64, 68), 150: (83, 80), 175: (92, 97), 200: (110, 106), 225: (119, 123), 250: (138, 140)}.items():
        installer_image(w, h, small=True).save(OUT / f"inno-small-{scale}.bmp")
    # Watermark shown in an empty editor area (one per theme).
    for theme, color, opacity in [("dark", "#ffffff", "0.08"), ("light", "#000000", "0.08"), ("hcDark", "#ffffff", "0.6"), ("hcLight", "#000000", "0.6")]:
        (OUT / f"letterpress-{theme}.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">'
            f'<g fill="none" stroke="{color}" stroke-opacity="{opacity}" stroke-width="96" stroke-linecap="round">'
            '<path d="M300 800 L512 220 L724 800"/><path d="M392 600 H632"/></g>'
            f'<g fill="{color}" fill-opacity="{opacity}"><circle cx="250" cy="330" r="46"/>'
            '<circle cx="774" cy="330" r="46"/><circle cx="512" cy="870" r="46"/></g></svg>\n'
        )
    print(f"Icons written to {OUT}")


if __name__ == "__main__":
    main()
