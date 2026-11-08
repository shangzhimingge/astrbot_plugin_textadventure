"""Render adventure journal pages locally without external assets."""

import re
from io import BytesIO
from math import cos, pi, sin

from PIL import Image, ImageDraw, ImageFont

WIDTH = 880
LEFT = 76
BODY_WIDTH = WIDTH - LEFT * 2
ROWS_PER_PAGE = 20
FRAME = "#142724"
HEADER = "#203b34"
PAPER = "#fbf2df"
INK = "#292821"
MUTED = "#665b48"
GOLD = "#d9af70"
LINE = "#dfcaa6"

HEADING = re.compile(r"^(?:#{1,6}\s+(.+)|\*\*(.+)\*\*|__(.+)__)$")
INLINE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__|\x60([^\x60]+)\x60")


def _compass(draw: ImageDraw.ImageDraw) -> None:
    x, y = 738, 125
    for radius in (38, 54, 62):
        color = GOLD if radius == 54 else "#587068"
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), outline=color)
    for point in range(8):
        angle = point * pi / 4
        start = (x + 57 * cos(angle), y + 57 * sin(angle))
        end = (x + 67 * cos(angle), y + 67 * sin(angle))
        draw.line((start, end), fill=GOLD, width=2)
    draw.polygon([(x, y - 41), (x + 10, y - 8), (x, y), (x - 10, y - 8)], fill=GOLD)
    draw.polygon([(x, y + 41), (x + 10, y + 8), (x, y), (x - 10, y + 8)], fill="#91a99a")
    draw.polygon([(x - 35, y), (x, y - 9), (x + 35, y), (x, y + 9)], outline=GOLD, width=2)
    draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill=PAPER)


def render_story_cards(
    story: str, action_example: str, player_id: str, chapter: int, saved_name: str = "",
) -> list[bytes]:
    """Return PNG pages with a fixed readable width and a bounded height."""
    from astrbot.core.utils.t2i.local_strategy import FontManager, TextMeasurer

    def font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
        face = FontManager.get_font(size, bold=bold)
        if not isinstance(face, ImageFont.FreeTypeFont):
            raise RuntimeError("没有可用的文字冒险图片字体")
        if bytes(face.getmask("冒")) == bytes(face.getmask("\u0378")):
            raise RuntimeError("文字冒险图片需要支持中文的字体")
        return face.font_variant()

    body = font(30)
    heading = font(32, bold=True)
    title = font(60, bold=True)
    label = font(23, bold=True)
    small = font(22)

    def fit(text: str, face: ImageFont.FreeTypeFont, width: int = BODY_WIDTH) -> list[str]:
        return TextMeasurer.split_text_to_fit_width(text, face, width)

    def first_line(text: str, face: ImageFont.FreeTypeFont, width: int) -> str:
        lines = fit(text, face, width - 24)
        return lines[0] + ("…" if len(lines) > 1 else "")

    rows = []
    for raw_line in story.strip().splitlines():
        line = raw_line.strip()
        if not line:
            rows.append(("", body, INK, 24))
            continue

        heading_match = HEADING.fullmatch(line)
        if heading_match:
            line = next(part for part in heading_match.groups() if part)
        line = INLINE.sub(lambda match: next(part for part in match.groups() if part), line)
        line = re.sub(r"^[-*+]\s+", "•  ", line)
        line = re.sub(r"^>\s?", "│ ", line)
        face, color, step = (heading, HEADER, 55) if heading_match else (body, INK, 48)
        rows.extend((part, face, color, step) for part in fit(line, face))

    if not rows:
        rows.append(("", body, INK, 48))
    pages = [rows[i:i + ROWS_PER_PAGE] for i in range(0, len(rows), ROWS_PER_PAGE)]
    eyebrow = "ARCHIVE  /  读档" if saved_name else "FIELD NOTES  /  剧情记录"
    chapter_label = f"存档 · {saved_name}" if saved_name else ("序章" if chapter <= 1 else f"第 {chapter - 1:02d} 回合")
    images = []

    for index, page in enumerate(pages, 1):
        footer = 261 + max(76, sum(row[3] for row in page)) + 25
        height = footer + 174
        image = Image.new("RGB", (WIDTH, height), FRAME)
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((26, 25, WIDTH - 26, height - 25), radius=28, fill=PAPER)
        draw.rounded_rectangle((26, 25, WIDTH - 26, 227), radius=28, fill=HEADER)
        draw.rectangle((26, 197, WIDTH - 26, 227), fill=HEADER)
        draw.rounded_rectangle((38, 37, WIDTH - 38, height - 37), radius=21, outline="#a88a57")
        draw.rectangle((LEFT, 225, WIDTH - LEFT, 228), fill=GOLD)
        _compass(draw)

        draw.text((LEFT, 62), eyebrow, font=label, fill=GOLD, anchor="lt")
        draw.text((LEFT, 103), "冒险日志", font=title, fill=PAPER, anchor="lt")
        draw.text(
            (LEFT, 181), first_line(chapter_label, label, 525),
            font=label, fill="#d3dfcd", anchor="lt",
        )

        y = 261
        for text, face, color, step in page:
            draw.text((LEFT, y), text, font=face, fill=color, anchor="lt")
            y += step

        draw.line((LEFT, footer, WIDTH - LEFT, footer), fill=LINE, width=2)
        draw.rounded_rectangle(
            (LEFT, footer + 19, WIDTH - LEFT, footer + 69), radius=12, fill="#eee0c1",
        )
        draw.text(
            (LEFT + 15, footer + 31),
            first_line(f"行动示例   {action_example}", label, BODY_WIDTH - 30),
            font=label, fill=HEADER, anchor="lt",
        )
        draw.text(
            (LEFT, footer + 96), first_line(f"玩家 ID  {player_id}", small, BODY_WIDTH - 125),
            font=small, fill=MUTED, anchor="lt",
        )
        draw.text(
            (WIDTH - LEFT, footer + 96), f"{index:02d} / {len(pages):02d}",
            font=small, fill=MUTED, anchor="rt",
        )
        with BytesIO() as buffer:
            image.save(buffer, format="PNG")
            images.append(buffer.getvalue())

    return images
