"""
스타보드 (칼봇 스타보드 대체)

목표: 칼봇 스타보드를 크리스봇이 완전히 대신하는 것 (주인 결정 2026-11-12).
- 받은 별(⭐) 기록: 별 10개(기준)가 안 된 메시지의 별도 셉니다. 본인 별 · 봇의 별은 세지 않습니다.
- /스타관리 가져오기 : 칼봇 스타보드 글 + 그 글들이 나온 채널의 기록을 한 번 훑어 지금까지의 별을 모읍니다.
- 그 뒤로는 ⭐ 반응이 달리고 빠질 때마다 따라 기록합니다 (제외 채널 · 스타보드 채널 빼고 전부).
- 스타보드 기능(board_live)을 켜면 기준 이상이 된 메시지를 크리스봇이 직접 스타보드 채널에 올리고 숫자를 고칩니다.
  기준 아래로 내려가거나 원본이 지워지면 크리스봇 글을 지웁니다. 칼봇이 올린 옛 글은 그대로 둡니다.
  → 칼봇 스타보드를 먼저 끈 다음 켜야 두 번 올라가지 않습니다.
- 자동별(auto_star)을 켜면 작업물 채널에 사진 · 영상이 올라올 때 크리스봇이 ⭐을 미리 달아 둡니다 (봇의 별은 안 셈).
- 칼봇 전환 기간에는 칼봇 `starboard stats @유저` 답변으로 자동 보정, `starboard config` 답변으로 기준 · 제외 채널 복사.
"""

import asyncio
import io
import logging
import re
from typing import Optional

import discord
from discord import app_commands

import activity
import cards

logger = logging.getLogger("Chris-bot.stars")

STAR = "⭐"
CARL_BOT_ID = 235148962103951360
DEFAULT_THRESHOLD = 10
BOARD_COLOR = discord.Color.gold()
STAR_COUNT_RE = re.compile(r"\*\*(\d+)\*\*")
JUMP_RE = re.compile(r"discord(?:app)?\.com/channels/\d+/(\d+)/(\d+)")
AVATAR_ID_RE = re.compile(r"/(?:avatars|users)/(\d+)/")
CHANNEL_MENTION_RE = re.compile(r"<#(\d+)>")
PAGE_SIZE = 10

_importing: set[int] = set()  # 가져오기가 진행 중인 서버
_board_locks: dict[int, asyncio.Lock] = {}  # 같은 메시지를 두 번 올리지 않도록


# =====================================================================
# 데이터베이스 (activity 와 같은 data/chris.db)
# =====================================================================
def _conn():
    return activity.get_db().conn


def ensure_tables() -> None:
    _conn().executescript(
        """
        CREATE TABLE IF NOT EXISTS star_messages (
            message_id INTEGER PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            author_id INTEGER NOT NULL,
            stars INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_star_guild_author ON star_messages (guild_id, author_id);
        CREATE TABLE IF NOT EXISTS star_adjust (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            delta INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        );
        -- 스타보드에 올라간 글 (원본 메시지 ID → 스타보드 글 ID). by_carl=1 이면 칼봇이 올린 옛 글
        CREATE TABLE IF NOT EXISTS star_board (
            message_id INTEGER PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            board_message_id INTEGER NOT NULL,
            by_carl INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_star_board_post ON star_board (board_message_id);
        """
    )
    _conn().commit()


def get_message(message_id: int) -> Optional[dict]:
    row = _conn().execute(
        "SELECT guild_id, channel_id, author_id, stars FROM star_messages WHERE message_id = ?", (message_id,)
    ).fetchone()
    return dict(zip(("guild_id", "channel_id", "author_id", "stars"), row)) if row else None


def set_message_stars(guild_id: int, channel_id: int, message_id: int, author_id: int, stars: int) -> None:
    _conn().execute(
        "INSERT INTO star_messages (message_id, guild_id, channel_id, author_id, stars) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(message_id) DO UPDATE SET stars = excluded.stars",
        (message_id, guild_id, channel_id, author_id, max(0, stars)),
    )
    _conn().commit()


def change_stars(guild_id: int, channel_id: int, message_id: int, author_id: Optional[int], delta: int) -> None:
    """별 하나가 달리거나(+1) 빠짐(-1). 처음 보는 메시지는 작성자를 알아야 기록합니다."""
    conn = _conn()
    cur = conn.execute(
        "UPDATE star_messages SET stars = MAX(0, stars + ?) WHERE message_id = ?", (delta, message_id)
    )
    if cur.rowcount == 0 and delta > 0 and author_id:
        conn.execute(
            "INSERT INTO star_messages (message_id, guild_id, channel_id, author_id, stars) VALUES (?, ?, ?, ?, ?)",
            (message_id, guild_id, channel_id, author_id, delta),
        )
    conn.commit()


def counted_stars(guild_id: int, user_id: int) -> tuple[int, int]:
    """보정 전 (받은 별 합계, 별 받은 메시지 수)"""
    row = _conn().execute(
        "SELECT COALESCE(SUM(stars), 0), COALESCE(SUM(stars > 0), 0) FROM star_messages "
        "WHERE guild_id = ? AND author_id = ?",
        (guild_id, user_id),
    ).fetchone()
    return row[0], row[1]


def _adjust(guild_id: int, user_id: int) -> int:
    row = _conn().execute(
        "SELECT delta FROM star_adjust WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
    ).fetchone()
    return row[0] if row else 0


def user_stars(guild_id: int, user_id: int) -> tuple[int, int]:
    """(받은 별 합계 — 보정 포함, 별 받은 메시지 수)"""
    stars, works = counted_stars(guild_id, user_id)
    return max(0, stars + _adjust(guild_id, user_id)), works


def set_total(guild_id: int, user_id: int, total: int) -> int:
    """받은 별 합계가 total 이 되도록 보정값을 정합니다. 이전 합계를 돌려줍니다."""
    before = user_stars(guild_id, user_id)[0]
    counted = counted_stars(guild_id, user_id)[0]
    _conn().execute(
        "INSERT INTO star_adjust (guild_id, user_id, delta) VALUES (?, ?, ?) "
        "ON CONFLICT(guild_id, user_id) DO UPDATE SET delta = excluded.delta",
        (guild_id, user_id, total - counted),
    )
    _conn().commit()
    return before


_TOTALS_SQL = """
    SELECT uid, SUM(s) AS total, SUM(w) AS works FROM (
        SELECT author_id AS uid, stars AS s, (stars > 0) AS w FROM star_messages WHERE guild_id = :g
        UNION ALL
        SELECT user_id, delta, 0 FROM star_adjust WHERE guild_id = :g
    ) GROUP BY uid HAVING SUM(s) > 0
"""


def top_stars(guild_id: int, limit: int = PAGE_SIZE, offset: int = 0) -> list[tuple[int, int, int]]:
    """[(유저 ID, 받은 별, 별 받은 메시지 수)] 별 많은 순"""
    return _conn().execute(
        _TOTALS_SQL + " ORDER BY total DESC, uid LIMIT :l OFFSET :o",
        {"g": guild_id, "l": limit, "o": offset},
    ).fetchall()


def count_star_users(guild_id: int) -> int:
    return _conn().execute(f"SELECT COUNT(*) FROM ({_TOTALS_SQL})", {"g": guild_id}).fetchone()[0]


def star_rank(guild_id: int, total: int) -> int:
    return _conn().execute(
        f"SELECT COUNT(*) FROM ({_TOTALS_SQL}) WHERE total > :t", {"g": guild_id, "t": total}
    ).fetchone()[0] + 1


def top_works(guild_id: int, limit: int = PAGE_SIZE, offset: int = 0) -> list[tuple[int, int, int, int, Optional[int]]]:
    """[(메시지 ID, 채널 ID, 작성자 ID, 별, 스타보드 글 ID 또는 None)] 별 많은 작품 순 (같으면 먼저 올라온 순)"""
    return _conn().execute(
        "SELECT m.message_id, m.channel_id, m.author_id, m.stars, b.board_message_id FROM star_messages m "
        "LEFT JOIN star_board b ON b.message_id = m.message_id "
        "WHERE m.guild_id = ? AND m.stars > 0 ORDER BY m.stars DESC, m.message_id LIMIT ? OFFSET ?",
        (guild_id, limit, offset),
    ).fetchall()


def count_starred_works(guild_id: int) -> int:
    return _conn().execute(
        "SELECT COUNT(*) FROM star_messages WHERE guild_id = ? AND stars > 0", (guild_id,)
    ).fetchone()[0]


def replace_guild_stars(guild_id: int, rows: dict[int, tuple[int, int, int]], carl_posts: dict[int, int]) -> None:
    """
    가져오기 결과로 서버의 별 기록을 통째로 바꿉니다. 보정값도 초기화.
    rows: 메시지 ID -> (채널 ID, 작성자 ID, 별), carl_posts: 원본 메시지 ID -> 칼봇 스타보드 글 ID
    """
    conn = _conn()
    conn.execute("DELETE FROM star_messages WHERE guild_id = ?", (guild_id,))
    conn.execute("DELETE FROM star_adjust WHERE guild_id = ?", (guild_id,))
    conn.execute("DELETE FROM star_board WHERE guild_id = ? AND by_carl = 1", (guild_id,))
    conn.executemany(
        "INSERT OR REPLACE INTO star_messages (message_id, guild_id, channel_id, author_id, stars) VALUES (?, ?, ?, ?, ?)",
        [(mid, guild_id, ch, author, max(0, st)) for mid, (ch, author, st) in rows.items()],
    )
    conn.executemany(
        "INSERT OR IGNORE INTO star_board (message_id, guild_id, board_message_id, by_carl) VALUES (?, ?, ?, 1)",
        [(mid, guild_id, post) for mid, post in carl_posts.items()],
    )
    conn.commit()


def get_board(message_id: int) -> Optional[dict]:
    row = _conn().execute(
        "SELECT guild_id, board_message_id, by_carl FROM star_board WHERE message_id = ?", (message_id,)
    ).fetchone()
    return dict(zip(("guild_id", "board_message_id", "by_carl"), row)) if row else None


def get_board_by_post(board_message_id: int) -> Optional[int]:
    row = _conn().execute(
        "SELECT message_id FROM star_board WHERE board_message_id = ?", (board_message_id,)
    ).fetchone()
    return row[0] if row else None


def set_board(guild_id: int, message_id: int, board_message_id: int, by_carl: bool) -> None:
    _conn().execute(
        "INSERT INTO star_board (message_id, guild_id, board_message_id, by_carl) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(message_id) DO UPDATE SET board_message_id = excluded.board_message_id, by_carl = excluded.by_carl",
        (message_id, guild_id, board_message_id, int(by_carl)),
    )
    _conn().commit()


def remove_board(message_id: int) -> None:
    _conn().execute("DELETE FROM star_board WHERE message_id = ?", (message_id,))
    _conn().commit()


# =====================================================================
# 순수 함수 (디스코드와 무관 — 테스트 가능)
# =====================================================================
def parse_board_post(content: str, icon_url: Optional[str], field_values: list[str], footer: Optional[str]) -> Optional[dict]:
    """
    칼봇 스타보드 글 → {stars, channel_id, message_id, author_id(없으면 None)}
    예: 내용 "💫 **11** <#채널>", 필드 "[Jump!](https://discord.com/channels/서버/채널/메시지)"
    """
    m = STAR_COUNT_RE.search(content or "")
    if not m:
        return None
    jump = None
    for value in field_values:
        jump = JUMP_RE.search(value or "")
        if jump:
            break
    if not jump:
        return None
    channel_id, message_id = int(jump.group(1)), int(jump.group(2))
    if footer and footer.strip().isdigit() and int(footer.strip()) != message_id:
        return None
    uid = AVATAR_ID_RE.search(icon_url or "")
    return {
        "stars": int(m.group(1)),
        "channel_id": channel_id,
        "message_id": message_id,
        "author_id": int(uid.group(1)) if uid else None,
    }


def parse_carl_stats(fields: dict[str, str], icon_url: Optional[str]) -> Optional[tuple[Optional[int], int]]:
    """칼봇 `starboard stats @유저` 답변 → (유저 ID 또는 None, Stars Received)"""
    value = fields.get("Stars Received")
    if value is None:
        return None
    digits = value.replace(",", "").strip()
    if not digits.isdigit():
        return None
    uid = AVATAR_ID_RE.search(icon_url or "")
    return (int(uid.group(1)) if uid else None), int(digits)


def parse_carl_config(content: str) -> Optional[tuple[int, list[int]]]:
    """칼봇 `starboard config` 답변 → (기준 별 수, 제외 채널 ID 목록)"""
    m = re.search(r"\*\*Threshold:\*\*\s*(\d+)", content or "")
    if not m:
        return None
    black = re.search(r"\*\*Blacklisted channels:\*\*([^\n]*)", content)
    ids = [int(x) for x in CHANNEL_MENTION_RE.findall(black.group(1))] if black else []
    return int(m.group(1)), ids


def plan_board(stars: int, threshold: int, entry: Optional[dict]) -> Optional[str]:
    """스타보드에서 할 일: 'post'(새로 올림) · 'edit'(숫자 고침) · 'remove'(지움) · None"""
    if entry and entry["by_carl"]:
        return None  # 칼봇이 올린 옛 글은 건드리지 않음
    if stars >= threshold:
        return "edit" if entry else "post"
    return "remove" if entry else None


MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def format_work_line(rank: int, guild_id: int, message_id: int, channel_id: int, author_id: int,
                     stars: int, board_message_id: Optional[int], board_channel_id: Optional[int] = None) -> str:
    """작품 순위 한 줄: 순위 · 별 · 작성자 · 원본 바로가기 (+ 스타보드 글 바로가기 — 원본이 지워졌을 때 대비)"""
    link = f"[작품 보기](https://discord.com/channels/{guild_id}/{channel_id}/{message_id})"
    if board_message_id and board_channel_id:
        link += f" · [스타보드](https://discord.com/channels/{guild_id}/{board_channel_id}/{board_message_id})"
    date = discord.utils.snowflake_time(message_id).strftime("%Y-%m-%d")
    return f"{MEDALS.get(rank, f'`{rank}.`')} {STAR} **{stars}** — <@{author_id}> · {date} · {link}"


def board_content(stars: int, channel_id: int) -> str:
    return f"{STAR} **{stars}** <#{channel_id}>"


def is_excluded(channel_id: int, parent_id: Optional[int], s: dict) -> bool:
    if channel_id == s.get("star_board_channel"):
        return True
    excluded = set(s.get("star_excluded_channels") or [])
    return channel_id in excluded or (parent_id is not None and parent_id in excluded)


# =====================================================================
# 칼봇 메시지 읽기
# =====================================================================
def _board_from_message(message: discord.Message) -> Optional[dict]:
    if not message.embeds:
        return None
    e = message.embeds[0]
    return parse_board_post(
        message.content,
        e.author.icon_url if e.author else None,
        [f.value for f in e.fields],
        e.footer.text if e.footer else None,
    )


def _carl_stats_from_message(message: discord.Message) -> Optional[tuple[Optional[int], int, str]]:
    for e in message.embeds:
        r = parse_carl_stats({f.name: f.value for f in e.fields}, e.author.icon_url if e.author else None)
        if r:
            return r[0], r[1], (e.author.name if e.author else "") or ""
    return None


async def _member_by_display_name(guild: discord.Guild, name: str) -> Optional[discord.Member]:
    """프로필 사진이 기본이라 칼봇 글에 아이디가 없을 때, 표시 이름으로 찾습니다 (딱 한 명일 때만)."""
    if not name:
        return None
    try:
        found = await guild.query_members(query=name, limit=20, cache=False)
    except (asyncio.TimeoutError, discord.HTTPException):
        return None
    exact = [m for m in found if name in (m.display_name, m.global_name, m.name)]
    return exact[0] if len(exact) == 1 else None


# =====================================================================
# 크리스봇 스타보드 (board_live 가 켜져 있을 때)
# =====================================================================
def _settings(guild_id: int) -> dict:
    return activity.get_db().get_settings(guild_id)


def threshold_of(s: dict) -> int:
    return int(s.get("star_threshold") or DEFAULT_THRESHOLD)


async def _get_channel(guild: discord.Guild, channel_id: int):
    channel = guild.get_channel_or_thread(channel_id)
    if channel is None:
        channel = await guild.fetch_channel(channel_id)
    return channel


async def _build_board_post(message: discord.Message, stars: int) -> tuple[str, discord.Embed, list[discord.File]]:
    embed = discord.Embed(description=(message.content or "")[:4000] or None, color=BOARD_COLOR, timestamp=message.created_at)
    embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
    embed.add_field(name="원본", value=f"[바로가기]({message.jump_url})")
    embed.set_footer(text=str(message.id))
    files: list[discord.File] = []
    limit = message.guild.filesize_limit if message.guild else 8 * 1024 * 1024
    # 사진 · 영상은 다시 올려서 붙입니다 (원본 링크는 시간이 지나면 만료될 수 있음)
    for att in message.attachments:
        if not activity._is_media(att) or att.size > limit or files:
            continue
        try:
            f = await att.to_file()
        except discord.HTTPException:
            continue
        files.append(f)
        if (att.content_type or "").startswith("image/") or att.filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
            embed.set_image(url=f"attachment://{f.filename}")
    if not files:
        for e in message.embeds:  # 링크 미리보기 이미지
            url = (e.image and e.image.url) or (e.thumbnail and e.thumbnail.url)
            if url:
                embed.set_image(url=url)
                break
    return board_content(stars, message.channel.id), embed, files


async def sync_board(guild: discord.Guild, message_id: int) -> None:
    """메시지의 별 수에 맞춰 크리스봇 스타보드 글을 올리거나 · 고치거나 · 지웁니다."""
    s = _settings(guild.id)
    board_id = s.get("star_board_channel")
    if not s.get("board_live") or not board_id:
        return
    row = get_message(message_id)
    stars = row["stars"] if row else 0
    if plan_board(stars, threshold_of(s), get_board(message_id)) is None:
        return
    lock = _board_locks.setdefault(message_id, asyncio.Lock())
    async with lock:
        entry = get_board(message_id)  # 기다리는 동안 바뀌었을 수 있음
        row = get_message(message_id)
        stars = row["stars"] if row else 0
        action = plan_board(stars, threshold_of(s), entry)
        board = guild.get_channel(board_id)
        if action is None or board is None:
            return
        try:
            if action == "post":
                original = await (await _get_channel(guild, row["channel_id"])).fetch_message(message_id)
                content, embed, files = await _build_board_post(original, stars)
                post = await board.send(content, embed=embed, files=files, allowed_mentions=discord.AllowedMentions.none())
                set_board(guild.id, message_id, post.id, by_carl=False)
            elif action == "edit":
                await board.get_partial_message(entry["board_message_id"]).edit(
                    content=board_content(stars, row["channel_id"])
                )
            elif action == "remove":
                remove_board(message_id)
                await board.get_partial_message(entry["board_message_id"]).delete()
        except discord.NotFound:
            if action == "edit":  # 누가 스타보드 글을 지웠음 → 기록도 지움
                remove_board(message_id)
        except discord.HTTPException as e:
            logger.warning(f"스타보드 {action} 실패 ({message_id}): {e}")
    if not lock.locked():
        _board_locks.pop(message_id, None)


# =====================================================================
# 실시간 기록
# =====================================================================
def _tracked(client: discord.Client, channel_id: int, s: dict) -> bool:
    if not s.get("stars_imported"):
        return False
    channel = client.get_channel(channel_id)
    return not is_excluded(channel_id, getattr(channel, "parent_id", None), s)


async def handle_reaction_add(payload: discord.RawReactionActionEvent, client: discord.Client) -> None:
    if payload.guild_id is None or str(payload.emoji) != STAR:
        return
    s = _settings(payload.guild_id)
    if not _tracked(client, payload.channel_id, s):
        return
    if payload.member is not None and payload.member.bot:  # 자동별 등 봇의 별은 안 셈
        return
    author_id = getattr(payload, "message_author_id", None)
    row = get_message(payload.message_id)
    if author_id is None and row:
        author_id = row["author_id"]
    if author_id is None or payload.user_id == author_id:  # 본인 별은 세지 않음
        return
    change_stars(payload.guild_id, payload.channel_id, payload.message_id, author_id, +1)
    guild = client.get_guild(payload.guild_id)
    if guild:
        await sync_board(guild, payload.message_id)


async def handle_reaction_remove(payload: discord.RawReactionActionEvent, client: discord.Client) -> None:
    if payload.guild_id is None or str(payload.emoji) != STAR:
        return
    s = _settings(payload.guild_id)
    if not _tracked(client, payload.channel_id, s):
        return
    row = get_message(payload.message_id)
    if row is None or payload.user_id == row["author_id"] or payload.user_id == getattr(client.user, "id", None):
        return
    change_stars(payload.guild_id, payload.channel_id, payload.message_id, None, -1)
    guild = client.get_guild(payload.guild_id)
    if guild:
        await sync_board(guild, payload.message_id)


async def handle_reaction_clear(client: discord.Client, guild_id: Optional[int], message_id: int, emoji=None) -> None:
    """관리자가 반응을 모두 지운 경우"""
    if guild_id is None or (emoji is not None and str(emoji) != STAR):
        return
    row = get_message(message_id)
    if row:
        set_message_stars(row["guild_id"], row["channel_id"], message_id, row["author_id"], 0)
        guild = client.get_guild(guild_id)
        if guild:
            await sync_board(guild, message_id)


async def handle_message_delete(payload: discord.RawMessageDeleteEvent, client: discord.Client) -> None:
    """원본이 지워지면 크리스봇 스타보드 글도 지움 (받은 별 기록은 유지). 스타보드 글이 지워지면 연결만 지움."""
    if payload.guild_id is None:
        return
    entry = get_board(payload.message_id)
    if entry:
        remove_board(payload.message_id)
        s = _settings(payload.guild_id)
        board = client.get_channel(s.get("star_board_channel") or 0)
        if board is not None and not entry["by_carl"]:
            try:
                await board.get_partial_message(entry["board_message_id"]).delete()
            except discord.HTTPException:
                pass
        return
    original = get_board_by_post(payload.message_id)
    if original is not None:
        remove_board(original)


async def handle_message(message: discord.Message) -> None:
    if message.guild is None:
        return
    s = _settings(message.guild.id)
    if message.author.id == CARL_BOT_ID:
        if not s.get("stars_imported"):
            return
        if message.channel.id == s.get("star_board_channel"):
            await _on_carl_board_post(message, s)
            return
        stats = _carl_stats_from_message(message)
        if stats:
            await _apply_carl_stats(message, stats)
        return
    # 자동별: 작업물 채널에 사진 · 영상이 올라오면 ⭐을 미리 달아 둠
    if (
        s.get("auto_star")
        and not message.author.bot
        and activity._in_channels(message.channel, s.get("work_channels"))
        and any(activity._is_media(a) for a in message.attachments)
    ):
        try:
            await message.add_reaction(STAR)
        except discord.HTTPException as e:
            logger.warning(f"자동별 실패: {e}")


async def _on_carl_board_post(message: discord.Message, s: dict) -> None:
    """칼봇이 아직 켜져 있는 동안 칼봇이 올린 스타보드 글을 따라 기록"""
    p = _board_from_message(message)
    if not p:
        return
    guild = message.guild
    author_id = p["author_id"]
    if author_id is None:
        row = get_message(p["message_id"])
        author_id = row["author_id"] if row else None
    if author_id is None:
        try:
            author_id = (await (await _get_channel(guild, p["channel_id"])).fetch_message(p["message_id"])).author.id
        except (discord.HTTPException, AttributeError):
            return
    set_message_stars(guild.id, p["channel_id"], p["message_id"], author_id, p["stars"])
    if get_board(p["message_id"]) is None:
        set_board(guild.id, p["message_id"], message.id, by_carl=True)


async def _apply_carl_stats(message: discord.Message, stats: tuple[Optional[int], int, str]) -> Optional[tuple[int, int, int]]:
    """칼봇 숫자로 맞춥니다. 반환: (유저 ID, 이전 합계, 새 합계)"""
    uid, total, name = stats
    if uid is None:
        member = await _member_by_display_name(message.guild, name)
        uid = member.id if member else None
    if uid is None:
        return None
    before = set_total(message.guild.id, uid, total)
    try:
        await message.add_reaction("✅")
    except discord.HTTPException:
        pass
    return uid, before, total


# =====================================================================
# 가져오기 (지금까지의 별 모으기)
# =====================================================================
async def _self_starred(reaction: discord.Reaction, author_id: int) -> bool:
    """작성자 본인이 ⭐을 달았는지 (반응한 사람 목록은 아이디 순이라 한 명만 확인하면 됨)"""
    async for user in reaction.users(limit=1, after=discord.Object(id=author_id - 1)):
        return user.id == author_id
    return False


async def import_stars(guild: discord.Guild, board: discord.TextChannel, progress) -> dict:
    """
    1) 스타보드 채널의 칼봇 글을 모두 읽고 (원본 위치 · 별 수 · 작성자)
    2) 그 글들이 나온 채널의 기록을 처음부터 훑어 ⭐ 달린 메시지를 셉니다.
       스타보드에 오른 메시지는 칼봇 숫자 그대로, 못 오른 메시지는 ⭐ 수에서 본인 별 · 크리스봇 별을 뺍니다.
    3) 원본이 지워진 메시지는 스타보드 숫자로 셉니다.
    """
    board_posts: dict[int, dict] = {}
    async for m in board.history(limit=None):
        if m.author.id != CARL_BOT_ID:
            continue
        p = _board_from_message(m)
        if p and p["message_id"] not in board_posts:  # 최신 글 우선
            p["name"] = (m.embeds[0].author.name if m.embeds[0].author else "") or ""
            p["post_id"] = m.id
            board_posts[p["message_id"]] = p
    await progress(f"스타보드 글 {len(board_posts):,}개를 읽었습니다. 원래 채널을 훑는 중...")

    sources = sorted({p["channel_id"] for p in board_posts.values()})
    rows: dict[int, tuple[int, int, int]] = {}
    scanned_channels, scanned_messages = 0, 0
    for i, cid in enumerate(sources, 1):
        try:
            channel = await _get_channel(guild, cid)
        except discord.HTTPException:
            continue  # 지워진 채널 — 스타보드 숫자로 셈
        if not hasattr(channel, "history"):
            continue
        try:
            async for m in channel.history(limit=None):
                scanned_messages += 1
                if scanned_messages % 2000 == 0:  # 큰 채널은 오래 걸리므로 중간 진행 상황도 표시
                    await progress(
                        f"채널 {i}/{len(sources)} 훑는 중 (메시지 {scanned_messages:,}개, "
                        f"{m.created_at:%Y-%m-%d}까지, 별 받은 메시지 {len(rows):,}개)"
                    )
                reaction = next((r for r in m.reactions if str(r.emoji) == STAR), None)
                if reaction is None or m.author.bot:
                    continue
                if m.id in board_posts:
                    stars = board_posts[m.id]["stars"]
                else:
                    stars = reaction.count - (1 if reaction.me else 0)
                    if stars > 0 and await _self_starred(reaction, m.author.id):
                        stars -= 1
                if stars > 0:
                    rows[m.id] = (channel.id, m.author.id, stars)
        except discord.Forbidden:
            continue
        scanned_channels += 1
        await progress(f"채널 {i}/{len(sources)} 확인 (메시지 {scanned_messages:,}개, 별 받은 메시지 {len(rows):,}개)")

    # 원본이 지워졌거나 읽지 못한 스타보드 글
    unknown = 0
    for mid, p in board_posts.items():
        if mid in rows:
            continue
        author_id = p["author_id"]
        if author_id is None:
            member = await _member_by_display_name(guild, p["name"])
            author_id = member.id if member else None
        if author_id is None:
            unknown += 1
            continue
        rows[mid] = (p["channel_id"], author_id, p["stars"])

    replace_guild_stars(guild.id, rows, {mid: p["post_id"] for mid, p in board_posts.items()})
    s = _settings(guild.id)
    s["star_board_channel"] = board.id
    s["stars_imported"] = True
    activity.get_db().save_settings(guild.id, s)
    return {
        "board_posts": len(board_posts),
        "channels": scanned_channels,
        "messages": scanned_messages,
        "starred": len(rows),
        "stars": sum(r[2] for r in rows.values()),
        "people": len({r[1] for r in rows.values()}),
        "unknown": unknown,
    }


# =====================================================================
# 명령어
# =====================================================================
ON_OFF = [app_commands.Choice(name="켜기", value=1), app_commands.Choice(name="끄기", value=0)]


async def _send_chunks(interaction: discord.Interaction, text: str) -> None:
    for i, chunk in enumerate(activity._split_lines(text)):
        if i == 0 and not interaction.response.is_done():
            await interaction.response.send_message(chunk, ephemeral=True)
        else:
            await interaction.followup.send(chunk, ephemeral=True)


def register(tree: app_commands.CommandTree) -> None:
    ensure_tables()

    @tree.command(name="스타순위", description="별(⭐)을 많이 받은 사람 순위(이미지)를 봅니다. 한 페이지에 10명씩.")
    @app_commands.guild_only()
    @app_commands.describe(페이지="볼 페이지 (1페이지 = 1~10위)")
    async def star_ranking(interaction: discord.Interaction, 페이지: app_commands.Range[int, 1, 1000] = 1):
        gid = interaction.guild_id
        if not _settings(gid).get("stars_imported"):
            await interaction.response.send_message("📋 아직 별 기록이 없습니다. 관리자가 `/스타관리 가져오기`를 먼저 실행해야 합니다.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        total = count_star_users(gid)
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(페이지, pages)
        rows = top_stars(gid, PAGE_SIZE, (page - 1) * PAGE_SIZE)
        if not rows:
            await interaction.followup.send("📋 아직 별 기록이 없습니다.")
            return
        top = top_stars(gid, 1)[0][1]  # 막대 길이 = 1위 대비
        found = await activity.resolve_users(interaction.guild, interaction.client, [r[0] for r in rows])
        leader_rows = []
        for i, (uid, got, works) in enumerate(rows, start=(page - 1) * PAGE_SIZE + 1):
            who = found.get(uid)
            grade = await activity._grade_badge(who, with_icon=False) if isinstance(who, discord.Member) else None
            leader_rows.append(
                cards.LeaderRow(
                    rank=i,
                    name=who.display_name if who else f"알 수 없음 ({uid})",
                    avatar=await activity._avatar_bytes(who) if who else None,
                    level=0,
                    progress=got / top if top else 0,
                    color=cards.grade_color(grade),
                    label=f"별 {got:,} · 작품 {works:,}",
                )
            )
        png = await asyncio.to_thread(cards.render_leaderboard, leader_rows)
        await interaction.followup.send(
            f"{STAR} **받은 별 순위** — {page} / {pages} 페이지",
            file=discord.File(io.BytesIO(png), filename="stars.png"),
        )

    @tree.command(name="스타작품순위", description="별(⭐)을 많이 받은 작품 순위를 봅니다. 작품 바로가기 링크 포함, 10개씩.")
    @app_commands.guild_only()
    @app_commands.describe(페이지="볼 페이지 (1페이지 = 1~10위)")
    async def work_ranking(interaction: discord.Interaction, 페이지: app_commands.Range[int, 1, 1000] = 1):
        gid = interaction.guild_id
        if not _settings(gid).get("stars_imported"):
            await interaction.response.send_message("📋 아직 별 기록이 없습니다. 관리자가 `/스타관리 가져오기`를 먼저 실행해야 합니다.", ephemeral=True)
            return
        total = count_starred_works(gid)
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(페이지, pages)
        rows = top_works(gid, PAGE_SIZE, (page - 1) * PAGE_SIZE)
        if not rows:
            await interaction.response.send_message("📋 아직 별 받은 작품이 없습니다.", ephemeral=True)
            return
        board_channel = _settings(gid).get("star_board_channel")
        lines = [
            format_work_line(i, gid, *row, board_channel_id=board_channel)
            for i, row in enumerate(rows, start=(page - 1) * PAGE_SIZE + 1)
        ]
        # 링크가 길어 일반 메시지 2,000자를 넘을 수 있으므로 임베드(4,096자)에 담습니다.
        embed = discord.Embed(
            title=f"{STAR} 작품별 별 순위 — {page} / {pages} 페이지",
            description="\n".join(lines),
            color=BOARD_COLOR,
        )
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    admin = app_commands.Group(
        name="스타관리",
        description="[관리자용] 스타보드와 별 기록을 관리합니다.",
        default_permissions=discord.Permissions(manage_guild=True),
        guild_only=True,
    )

    @admin.command(name="가져오기", description="칼봇 스타보드와 원래 채널을 훑어 지금까지 받은 별을 모읍니다. (몇 분~수십 분)")
    @app_commands.describe(스타보드채널="스타보드 채널 (칼봇이 쓰던 채널 — 크리스봇도 여기에 올립니다)")
    async def import_cmd(interaction: discord.Interaction, 스타보드채널: discord.TextChannel):
        guild = interaction.guild
        if guild.id in _importing:
            await interaction.response.send_message("⏳ 이미 가져오는 중입니다. 끝나면 진행 메시지에 표시됩니다.", ephemeral=True)
            return
        _importing.add(guild.id)
        await interaction.response.send_message(
            f"⏳ {스타보드채널.mention}과 원래 채널을 훑기 시작합니다. 진행 상황은 아래 메시지에 표시됩니다.\n"
            "다시 가져오면 이전 별 기록과 보정값은 지워지고 새로 계산됩니다.",
            ephemeral=True,
        )
        status = await interaction.channel.send("⭐ 별 기록 가져오는 중...")

        async def progress(text: str) -> None:
            try:
                await status.edit(content=f"⭐ 별 기록 가져오는 중... {text}")
            except discord.HTTPException:
                pass

        try:
            r = await import_stars(guild, 스타보드채널, progress)
        except Exception:
            logger.exception("별 가져오기 실패")
            await status.edit(content="❌ 별 기록을 가져오다 오류가 났습니다. 다시 시도해 주세요.")
            return
        finally:
            _importing.discard(guild.id)
        text = (
            "✅ **별 기록 가져오기 완료**\n"
            f"- 스타보드 글 {r['board_posts']:,}개, 채널 {r['channels']}개의 메시지 {r['messages']:,}개를 확인했습니다.\n"
            f"- 별 받은 메시지 {r['starred']:,}개, 별 합계 {r['stars']:,}개, {r['people']}명\n"
            "- 이제부터 ⭐ 반응을 실시간으로 따라 기록합니다. `/스타순위`로 확인하세요."
        )
        if r["unknown"]:
            text += f"\n- 작성자를 찾지 못한 스타보드 글 {r['unknown']}개는 빠졌습니다 (원본 삭제 + 기본 프로필)."
        await status.edit(content=text)

    @admin.command(name="칼봇맞추기", description="채널의 최근 칼봇 starboard stats · config 답변을 읽어 별 수 · 기준 · 제외 채널을 맞춥니다.")
    @app_commands.describe(채널="칼봇 답변이 있는 채널 (비워두면 현재 채널)")
    async def carl_sync(interaction: discord.Interaction, 채널: Optional[discord.TextChannel] = None):
        guild = interaction.guild
        channel = 채널 or interaction.channel
        s = _settings(guild.id)
        if not s.get("stars_imported"):
            await interaction.response.send_message("❌ 먼저 `/스타관리 가져오기`를 실행해 주세요.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        results, missed, config = {}, 0, None
        async for m in channel.history(limit=300):  # 최신 메시지부터
            if m.author.id != CARL_BOT_ID:
                continue
            if config is None:
                config = parse_carl_config(m.content)
            stats = _carl_stats_from_message(m)
            if not stats or (stats[0] is not None and stats[0] in results):  # 같은 사람은 가장 최근 답변만
                continue
            r = await _apply_carl_stats(m, stats)
            if r is None:
                missed += 1
            else:
                results.setdefault(r[0], r)
        lines = []
        if config:
            s["star_threshold"], s["star_excluded_channels"] = config[0], config[1]
            activity.get_db().save_settings(guild.id, s)
            lines.append(f"✅ 칼봇 설정을 복사했습니다: 기준 **{config[0]}개**, 제외 채널 {len(config[1])}개")
        if results:
            lines.append(f"✅ {len(results)}명의 별 수를 칼봇과 맞췄습니다.")
            lines += [f"- <@{uid}>: {before:,} → **{after:,}**" for uid, before, after in results.values()]
        if missed:
            lines.append(f"- 누구인지 알 수 없는 답변 {missed}개는 건너뛰었습니다. `/스타관리 보정`으로 직접 맞춰 주세요.")
        if not lines:
            lines.append("📋 칼봇 `starboard stats @유저` · `starboard config` 답변을 찾지 못했습니다.")
        for chunk in activity._split_lines("\n".join(lines)):
            await interaction.followup.send(chunk, ephemeral=True)

    @admin.command(name="스타보드기능", description="크리스봇이 스타보드에 직접 올리는 기능을 켜거나 끕니다. (칼봇 스타보드를 먼저 끄세요)")
    @app_commands.choices(상태=ON_OFF)
    async def toggle_board(interaction: discord.Interaction, 상태: int):
        s = _settings(interaction.guild_id)
        if 상태 and not (s.get("stars_imported") and s.get("star_board_channel")):
            await interaction.response.send_message("❌ 먼저 `/스타관리 가져오기`를 실행해 주세요.", ephemeral=True)
            return
        s["board_live"] = bool(상태)
        activity.get_db().save_settings(interaction.guild_id, s)
        if 상태:
            msg = (
                f"✅ 이제 별 **{threshold_of(s)}개** 이상 받은 메시지를 크리스봇이 <#{s['star_board_channel']}>에 올립니다.\n"
                "⚠️ 칼봇 스타보드가 켜져 있으면 두 번 올라갑니다. 칼봇 스타보드를 꺼 주세요. (칼봇이 올린 옛 글은 그대로 둡니다)"
            )
        else:
            msg = "⏸️ 크리스봇 스타보드 올리기를 껐습니다. (별 기록은 계속됩니다)"
        await interaction.response.send_message(msg, ephemeral=True)

    @admin.command(name="자동별", description="작업물 채널에 사진·영상이 올라오면 크리스봇이 ⭐을 미리 달아 둡니다. (봇의 별은 안 셈)")
    @app_commands.choices(상태=ON_OFF)
    async def toggle_auto(interaction: discord.Interaction, 상태: int):
        s = _settings(interaction.guild_id)
        s["auto_star"] = bool(상태)
        activity.get_db().save_settings(interaction.guild_id, s)
        chans = s.get("work_channels") or []
        if 상태:
            msg = f"✅ 자동별을 켰습니다. 작업물 채널 {len(chans)}개 (`/레벨관리 작업물채널추가`로 정한 채널)에 적용됩니다."
            if not chans:
                msg += "\n⚠️ 작업물 채널이 없습니다. `/레벨관리 작업물채널추가`로 먼저 정해 주세요."
        else:
            msg = "⏸️ 자동별을 껐습니다."
        await interaction.response.send_message(msg, ephemeral=True)

    @admin.command(name="기준", description="스타보드에 올라갈 별 개수 기준을 정합니다. (칼봇 기본 10)")
    async def set_threshold(interaction: discord.Interaction, 개수: app_commands.Range[int, 1, 100]):
        s = _settings(interaction.guild_id)
        s["star_threshold"] = 개수
        activity.get_db().save_settings(interaction.guild_id, s)
        await interaction.response.send_message(f"✅ 스타보드 기준을 별 **{개수}개**로 정했습니다.", ephemeral=True)

    @admin.command(name="제외채널추가", description="별을 세지 않고 스타보드에도 올리지 않을 채널을 추가합니다.")
    async def add_excluded(interaction: discord.Interaction, 채널: discord.abc.GuildChannel):
        s = _settings(interaction.guild_id)
        chans = s.setdefault("star_excluded_channels", [])
        if 채널.id not in chans:
            chans.append(채널.id)
            activity.get_db().save_settings(interaction.guild_id, s)
        await interaction.response.send_message(f"✅ {채널.mention}을 별 제외 채널에 추가했습니다.", ephemeral=True)

    @admin.command(name="제외채널제거", description="별 제외 채널 목록에서 뺍니다.")
    async def remove_excluded(interaction: discord.Interaction, 채널: discord.abc.GuildChannel):
        s = _settings(interaction.guild_id)
        chans = s.setdefault("star_excluded_channels", [])
        if 채널.id in chans:
            chans.remove(채널.id)
            activity.get_db().save_settings(interaction.guild_id, s)
        await interaction.response.send_message(f"🗑️ {채널.mention}을 별 제외 채널에서 뺐습니다.", ephemeral=True)

    @admin.command(name="보정", description="유저가 받은 별 합계를 직접 정합니다. (칼봇 숫자와 맞출 때)")
    @app_commands.describe(유저="대상 유저", 별수="받은 별 합계")
    async def adjust(interaction: discord.Interaction, 유저: discord.Member, 별수: app_commands.Range[int, 0, 1000000]):
        before = set_total(interaction.guild_id, 유저.id, 별수)
        await interaction.response.send_message(
            f"✅ {유저.mention}님의 받은 별: {before:,} → **{별수:,}**", ephemeral=True
        )

    @admin.command(name="설정목록", description="스타보드 설정과 별 기록 현황을 봅니다.")
    async def show(interaction: discord.Interaction):
        gid = interaction.guild_id
        s = _settings(gid)
        board = s.get("star_board_channel")
        excluded = s.get("star_excluded_channels") or []
        totals = _conn().execute(
            "SELECT COUNT(*), COALESCE(SUM(stars), 0) FROM star_messages WHERE guild_id = ? AND stars > 0", (gid,)
        ).fetchone()
        adjusted = _conn().execute("SELECT COUNT(*) FROM star_adjust WHERE guild_id = ?", (gid,)).fetchone()[0]
        posts = _conn().execute(
            "SELECT COALESCE(SUM(by_carl = 0), 0), COALESCE(SUM(by_carl = 1), 0) FROM star_board WHERE guild_id = ?", (gid,)
        ).fetchone()
        text = (
            f"### {STAR} 스타보드 설정\n"
            f"- 별 기록: {'켜짐' if s.get('stars_imported') else '꺼짐 (`/스타관리 가져오기` 전)'}\n"
            f"- 크리스봇 스타보드 올리기: {'켜짐' if s.get('board_live') else '꺼짐'} · 자동별: {'켜짐' if s.get('auto_star') else '꺼짐'}\n"
            f"- 스타보드 채널: {f'<#{board}>' if board else '없음'} · 기준: 별 {threshold_of(s)}개\n"
            f"- 제외 채널 ({len(excluded)}개): {' '.join(f'<#{c}>' for c in excluded) or '없음'}\n"
            f"- 별 받은 메시지 {totals[0]:,}개, 별 합계 {totals[1]:,}개 (보정한 사람 {adjusted}명)\n"
            f"- 스타보드 글: 크리스봇 {posts[0]:,}개 · 칼봇(옛 글) {posts[1]:,}개"
        )
        await _send_chunks(interaction, text)

    tree.add_command(admin)
