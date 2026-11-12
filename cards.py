"""
레벨 카드 · 리더보드 이미지를 그립니다 (Pillow).

디스코드와 무관한 순수 그림 함수만 있으므로, 서버 없이도 테스트할 수 있습니다.
한글 글꼴이 필요합니다: 서버는 `sudo apt install fonts-nanum`, 윈도우는 맑은 고딕을 사용합니다.
"""

import io
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).parent

BOLD_FONTS = [
    HERE / "assets" / "fonts" / "bold.ttf",
    Path("/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf"),
    Path("/usr/share/fonts/truetype/nanum/NanumBarunGothicBold.ttf"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
    Path("C:/Windows/Fonts/malgunbd.ttf"),
]
REGULAR_FONTS = [
    HERE / "assets" / "fonts" / "regular.ttf",
    Path("/usr/share/fonts/truetype/nanum/NanumGothic.ttf"),
    Path("/usr/share/fonts/truetype/nanum/NanumBarunGothic.ttf"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("C:/Windows/Fonts/malgun.ttf"),
]

# 색상
BG = (32, 35, 40)
ROW_BG = (40, 44, 50)
TRACK = (70, 74, 82)
WHITE = (240, 242, 245)
GRAY = (160, 165, 175)
DEFAULT_ACCENT = (72, 187, 180)
NO_GRADE = (128, 133, 142)  # 등급이 없을 때의 강조색 (회색)
MEDALS = {1: (245, 196, 66), 2: (190, 196, 205), 3: (205, 127, 50)}

# 글꼴에 없는 이모지 등은 네모(□)로 나오므로 지웁니다.
_UNSUPPORTED = re.compile("[\U00010000-\U0010FFFF\uFE0F\u200D\u20E3]")


@dataclass
class Badge:
    name: str
    color: tuple[int, int, int] = GRAY
    icon: Optional[bytes] = None  # 역할 아이콘 이미지 (있으면)


@dataclass
class RankCardData:
    display_name: str
    username: str
    avatar: Optional[bytes]
    level: int
    xp_in_level: int
    xp_needed: int
    rank: int
    coins: int = 0
    grade: Optional[Badge] = None  # 서버 등급 (카드 오른쪽에 크게 표시, 카드 색도 이 등급 색)
    badges: list[Badge] = field(default_factory=list)


@dataclass
class LeaderRow:
    rank: int
    name: str
    avatar: Optional[bytes]
    level: int
    progress: float  # 현재 레벨 안에서의 진행도 0~1
    color: tuple[int, int, int] = NO_GRADE  # 막대 색 = 등급 색 (등급 없으면 회색)


# ---------------------------------------------------------------------
# 도구
# ---------------------------------------------------------------------
@lru_cache(maxsize=32)
def font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    for path in BOLD_FONTS if bold else REGULAR_FONTS:
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default(size)


def clean(text: str) -> str:
    return _UNSUPPORTED.sub("", text).strip() or "?"


def short_number(n: float) -> str:
    """5700 -> 5.7K, 1100000 -> 1.1M (Arcane과 같은 표기)"""
    if n < 1000:
        return str(int(n))
    for unit, div in (("M", 1_000_000), ("K", 1_000)):
        if n >= div:
            return f"{n / div:.1f}".rstrip("0").rstrip(".") + unit
    return str(int(n))


def fit(draw: ImageDraw.ImageDraw, text: str, f, max_width: int) -> str:
    if draw.textlength(text, font=f) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=f) > max_width:
        text = text[:-1]
    return text + "…"


def load_image(data: Optional[bytes], size: int) -> Image.Image:
    if data:
        try:
            return Image.open(io.BytesIO(data)).convert("RGBA").resize((size, size), Image.LANCZOS)
        except Exception:
            pass
    img = Image.new("RGBA", (size, size), (88, 101, 242, 255))
    return img


def circle(img: Image.Image) -> Image.Image:
    """정사각형 이미지를 매끈한 원 모양으로 자릅니다."""
    size = img.size[0]
    big = size * 4
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, big - 1, big - 1), fill=255)
    mask = mask.resize((size, size), Image.LANCZOS)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def progress_bar(draw, box, ratio: float, color, track=TRACK):
    x0, y0, x1, y1 = box
    r = (y1 - y0) // 2
    draw.rounded_rectangle(box, radius=r, fill=track)
    ratio = max(0.0, min(1.0, ratio))
    if ratio > 0:
        fill_x = x0 + max(2 * r, int((x1 - x0) * ratio))
        draw.rounded_rectangle((x0, y0, fill_x, y1), radius=r, fill=color)


def readable_on(color) -> tuple[int, int, int]:
    """배경색 위에서 잘 보이는 글자색 (밝으면 검정, 어두우면 흰색)"""
    r, g, b = color[:3]
    return (25, 25, 30) if (0.299 * r + 0.587 * g + 0.114 * b) > 160 else WHITE


def draw_badge(img: Image.Image, draw, x: int, y: int, badge: Badge, height: int = 36, prefix: str = "") -> int:
    """알약 모양 뱃지를 그리고 그 너비를 반환합니다."""
    f = font(20, bold=True)
    label = clean(prefix + badge.name)
    pad = 14
    icon_size = height - 10
    text_w = int(draw.textlength(label, font=f))
    width = pad + icon_size + 8 + text_w + pad
    draw.rounded_rectangle((x, y, x + width, y + height), radius=height // 2, fill=(55, 59, 66))
    cx, cy = x + pad, y + 5
    if badge.icon:
        icon = load_image(badge.icon, icon_size)
        img.paste(icon, (cx, cy), icon)
    else:
        draw.ellipse((cx + 4, cy + 4, cx + icon_size - 4, cy + icon_size - 4), fill=badge.color)
    draw.text((cx + icon_size + 8, y + height // 2), label, font=f, fill=WHITE, anchor="lm")
    return width


def to_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG", optimize=True)
    return buf.getvalue()


# ---------------------------------------------------------------------
# 레벨 카드
# ---------------------------------------------------------------------
BADGE_H, BADGE_GAP_X, BADGE_GAP_Y = 36, 10, 10


def badge_width(draw, badge: Badge, prefix: str = "") -> int:
    return 14 + (BADGE_H - 10) + 8 + int(draw.textlength(clean(prefix + badge.name), font=font(20))) + 14


def layout_badges(items, left: int, max_x: int) -> list[list[tuple[Badge, str, int]]]:
    """뱃지를 줄 단위로 나눕니다. 한 줄에 안 들어가면 다음 줄로 넘깁니다 (줄 수 제한 없음)."""
    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    rows: list[list[tuple[Badge, str, int]]] = [[]]
    x = left
    for badge, prefix in items:
        w = min(badge_width(measure, badge, prefix), max_x - left)
        if rows[-1] and x + w > max_x:
            rows.append([])
            x = left
        rows[-1].append((badge, prefix, w))
        x += w + BADGE_GAP_X
    return rows if rows[0] else []


def grade_color(grade: Optional[Badge]) -> tuple[int, int, int]:
    """카드 강조색 = 등급 역할 색 (등급이 없거나 역할 색이 없으면 회색)"""
    if grade and grade.color and grade.color != GRAY:
        return tuple(grade.color[:3])
    return NO_GRADE


def render_rank_card(d: RankCardData) -> bytes:
    W = 920
    ax, av_size = 44, 196
    left = ax + av_size + 40
    panel_left = W - 230  # 오른쪽 등급 영역
    badge_rows = layout_badges([(b, "") for b in d.badges], left, W - 44)
    # 뱃지가 두 줄 이상이면 그만큼 카드를 아래로 늘립니다. (뱃지가 없으면 그 자리는 비워 둠)
    extra = max(0, len(badge_rows) - 1) * (BADGE_H + BADGE_GAP_Y)
    H = 300 + extra
    img = Image.new("RGBA", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # 오른쪽 대각선 장식 (등급 색)
    accent = grade_color(d.grade)
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(overlay).polygon([(panel_left, 0), (W, 0), (W, H), (W - 60, H)], fill=accent + (70,))
    ImageDraw.Draw(overlay).polygon([(W - 120, 0), (W, 0), (W, H)], fill=accent + (120,))
    img.alpha_composite(overlay)

    # 등급 (뱃지와 분리해서 오른쪽에 크게)
    gx = W - 100
    draw.text((gx, 72), "등급", font=font(22, False), fill=(225, 228, 232), anchor="mm")
    if d.grade:
        gfont = font(44)
        gname = fit(draw, clean(d.grade.name), gfont, 170)
        if draw.textlength(gname, font=gfont) > 170 or gname.endswith("…"):
            gfont = font(30)
            gname = fit(draw, clean(d.grade.name), gfont, 170)
        draw.text((gx + 2, 122), gname, font=gfont, fill=(0, 0, 0), anchor="mm")  # 그림자
        draw.text((gx, 120), gname, font=gfont, fill=WHITE, anchor="mm")
    else:
        draw.text((gx, 120), "없음", font=font(34), fill=(200, 203, 208), anchor="mm")

    # 아바타 (테두리 원) — 카드가 길어져도 위쪽 자리에 고정
    ay = (300 - av_size) // 2
    draw.ellipse((ax - 6, ay - 6, ax + av_size + 6, ay + av_size + 6), fill=accent)
    avatar = circle(load_image(d.avatar, av_size))
    img.paste(avatar, (ax, ay), avatar)

    right = panel_left - 20
    width = right - left

    # 이름 · 아이디 · 코인
    name = fit(draw, clean(d.display_name), font(42), width)
    draw.text((left, 34), name, font=font(42), fill=WHITE)
    coin_text = f"코인 {d.coins:,}"
    coin_w = int(draw.textlength(coin_text, font=font(22)))
    draw.text((left, 92), fit(draw, "@" + clean(d.username), font(22, False), width - coin_w - 20), font=font(22, False), fill=GRAY)
    draw.text((right, 92), coin_text, font=font(22), fill=GRAY, anchor="ra")

    # 레벨 · 순위 · 경험치
    y = 134
    x = left
    for label, value, color in (
        ("레벨", str(d.level), accent),
        ("순위", f"#{d.rank}", WHITE),
        ("XP", f"{short_number(d.xp_in_level)} / {short_number(d.xp_needed)}", WHITE),
    ):
        draw.text((x, y), label, font=font(22, False), fill=GRAY)
        x += int(draw.textlength(label, font=font(22, False))) + 8
        draw.text((x, y - 6), value, font=font(30), fill=color)
        x += int(draw.textlength(value, font=font(30))) + 26

    # 뱃지 (여러 줄)
    y = 186
    for row in badge_rows:
        x = left
        for badge, prefix, w in row:
            label_badge = badge
            if badge_width(draw, badge, prefix) > w:  # 한 줄보다 긴 이름은 줄임표 처리
                label_badge = Badge(fit(draw, clean(badge.name), font(20), w - 62), badge.color, badge.icon)
            draw_badge(img, draw, x, y, label_badge, height=BADGE_H, prefix=prefix)
            x += w + BADGE_GAP_X
        y += BADGE_H + BADGE_GAP_Y

    # 경험치 막대
    ratio = d.xp_in_level / d.xp_needed if d.xp_needed else 0
    progress_bar(draw, (left, 244 + extra, W - 44, 272 + extra), ratio, accent)

    return to_png(img)


# ---------------------------------------------------------------------
# 리더보드
# ---------------------------------------------------------------------
def render_leaderboard(rows: list[LeaderRow]) -> bytes:
    W = 720
    row_h, gap = 76, 6
    H = max(1, len(rows)) * (row_h + gap) - gap
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    for i, r in enumerate(rows):
        y = i * (row_h + gap)
        draw.rounded_rectangle((0, y, W - 1, y + row_h - 1), radius=8, fill=ROW_BG)

        av = load_image(r.avatar, row_h)
        mask = Image.new("L", (row_h, row_h), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, row_h - 1, row_h - 1), radius=8, fill=255)
        img.paste(av, (0, y), mask)

        x = row_h + 16
        cy = y + 32
        rank_text = f"#{r.rank}"
        draw.text((x, cy), rank_text, font=font(28), fill=MEDALS.get(r.rank, WHITE), anchor="lm")
        x += int(draw.textlength(rank_text, font=font(28))) + 12
        draw.text((x, cy), "•", font=font(22), fill=GRAY, anchor="lm")
        x += 20

        level_text = f"레벨 {r.level}"
        lw = int(draw.textlength(level_text, font=font(26)))
        name = fit(draw, clean(r.name), font(26), W - 24 - lw - 30 - x)
        draw.text((x, cy), name, font=font(26), fill=WHITE, anchor="lm")
        x += int(draw.textlength(name, font=font(26))) + 12
        draw.text((x, cy), "•", font=font(22), fill=GRAY, anchor="lm")
        x += 20
        draw.text((x, cy), level_text, font=font(26), fill=WHITE, anchor="lm")

        progress_bar(draw, (row_h + 16, y + row_h - 16, W - 24, y + row_h - 11), r.progress, r.color)

    out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    out.alpha_composite(img)
    buf = io.BytesIO()
    out.save(buf, "PNG", optimize=True)  # 바깥은 투명하게 (디스코드 배경에 자연스럽게)
    return buf.getvalue()
