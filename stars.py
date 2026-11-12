"""
스타보드 별 기록 (칼봇 스타보드를 따라 기록)

- 칼봇 스타보드는 그대로 두고, 크리스봇은 "누가 별(⭐)을 몇 개 받았는지"만 따로 기록합니다.
- 칼봇처럼 별 10개가 안 된(스타보드에 못 오른) 메시지의 별도 셉니다. 본인이 단 별은 세지 않습니다.
- /스타관리 가져오기 : 스타보드 채널 기록 + 스타보드에 글이 올라온 적 있는 채널 전체를 한 번 훑어 지금까지의 별을 모읍니다.
  원본이 지워진 메시지는 스타보드에 남은 숫자로 셉니다.
- 그 뒤로는 ⭐ 반응이 달리고 빠질 때마다 따라 기록합니다 (별 채널 목록 안에서만).
  칼봇이 새 채널의 글을 스타보드에 올리면 그 채널도 자동으로 목록에 추가합니다.
- 칼봇 `starboard stats @유저` 답변이 올라오면 그 사람의 "Stars Received" 숫자로 자동 보정합니다 (✅ 반응으로 표시).
"""

import asyncio
import logging
import re
from typing import Optional

import discord
from discord import app_commands

import activity

logger = logging.getLogger("Chris-bot.stars")

STAR = "⭐"
CARL_BOT_ID = 235148962103951360
STAR_COUNT_RE = re.compile(r"\*\*(\d+)\*\*")
JUMP_RE = re.compile(r"discord(?:app)?\.com/channels/\d+/(\d+)/(\d+)")
AVATAR_ID_RE = re.compile(r"/(?:avatars|users)/(\d+)/")
PAGE_SIZE = 10

_importing: set[int] = set()  # 가져오기가 진행 중인 서버


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


def replace_guild_stars(guild_id: int, rows: dict[int, tuple[int, int, int]]) -> None:
    """가져오기 결과로 서버의 별 기록을 통째로 바꿉니다. rows: 메시지 ID -> (채널 ID, 작성자 ID, 별). 보정값도 초기화."""
    conn = _conn()
    conn.execute("DELETE FROM star_messages WHERE guild_id = ?", (guild_id,))
    conn.execute("DELETE FROM star_adjust WHERE guild_id = ?", (guild_id,))
    conn.executemany(
        "INSERT OR REPLACE INTO star_messages (message_id, guild_id, channel_id, author_id, stars) VALUES (?, ?, ?, ?, ?)",
        [(mid, guild_id, ch, author, max(0, st)) for mid, (ch, author, st) in rows.items()],
    )
    conn.commit()


# =====================================================================
# 칼봇 메시지 읽기 (디스코드와 무관한 순수 함수 — 테스트 가능)
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
# 실시간 기록
# =====================================================================
def _settings(guild_id: int) -> dict:
    return activity.get_db().get_settings(guild_id)


def _tracked(client: discord.Client, guild_id: int, channel_id: int, s: dict) -> bool:
    if not s.get("stars_imported") or channel_id == s.get("star_board_channel"):
        return False
    ids = set(s.get("star_channels") or [])
    if channel_id in ids:
        return True
    channel = client.get_channel(channel_id)
    return channel is not None and getattr(channel, "parent_id", None) in ids


async def handle_reaction_add(payload: discord.RawReactionActionEvent, client: discord.Client) -> None:
    if payload.guild_id is None or str(payload.emoji) != STAR:
        return
    s = _settings(payload.guild_id)
    if not _tracked(client, payload.guild_id, payload.channel_id, s):
        return
    if payload.member is not None and payload.member.bot:
        return
    author_id = getattr(payload, "message_author_id", None)
    row = get_message(payload.message_id)
    if author_id is None and row:
        author_id = row["author_id"]
    if author_id is None or payload.user_id == author_id:  # 본인 별은 세지 않음
        return
    change_stars(payload.guild_id, payload.channel_id, payload.message_id, author_id, +1)


async def handle_reaction_remove(payload: discord.RawReactionActionEvent, client: discord.Client) -> None:
    if payload.guild_id is None or str(payload.emoji) != STAR:
        return
    s = _settings(payload.guild_id)
    if not _tracked(client, payload.guild_id, payload.channel_id, s):
        return
    row = get_message(payload.message_id)
    if row is None or payload.user_id == row["author_id"]:
        return
    change_stars(payload.guild_id, payload.channel_id, payload.message_id, None, -1)


async def handle_reaction_clear(guild_id: Optional[int], message_id: int, emoji=None) -> None:
    """관리자가 반응을 모두 지운 경우"""
    if guild_id is None or (emoji is not None and str(emoji) != STAR):
        return
    row = get_message(message_id)
    if row:
        set_message_stars(row["guild_id"], row["channel_id"], message_id, row["author_id"], 0)


async def handle_message(message: discord.Message) -> None:
    """칼봇 메시지만 봅니다: 새 스타보드 글 · `starboard stats` 답변"""
    if message.guild is None or message.author.id != CARL_BOT_ID:
        return
    s = _settings(message.guild.id)
    if not s.get("stars_imported"):
        return
    if message.channel.id == s.get("star_board_channel"):
        await _on_board_post(message, s)
        return
    stats = _carl_stats_from_message(message)
    if stats:
        await _apply_carl_stats(message, stats)


async def _on_board_post(message: discord.Message, s: dict) -> None:
    p = _board_from_message(message)
    if not p:
        return
    guild = message.guild
    # 새 채널에서 올라온 글이면 그 채널도 별 채널 목록에 추가
    chans = s.setdefault("star_channels", [])
    if p["channel_id"] not in chans:
        channel = guild.get_channel_or_thread(p["channel_id"])
        parent = getattr(channel, "parent_id", None)
        if parent not in chans:
            chans.append(p["channel_id"])
            activity.get_db().save_settings(guild.id, s)
    author_id = p["author_id"]
    if author_id is None:
        row = get_message(p["message_id"])
        author_id = row["author_id"] if row else None
    if author_id is None:
        try:
            channel = guild.get_channel_or_thread(p["channel_id"]) or await guild.fetch_channel(p["channel_id"])
            author_id = (await channel.fetch_message(p["message_id"])).author.id
        except (discord.HTTPException, AttributeError):
            return
    set_message_stars(guild.id, p["channel_id"], p["message_id"], author_id, p["stars"])


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
       스타보드에 오른 메시지는 칼봇 숫자 그대로, 못 오른 메시지는 ⭐ 수에서 본인 별을 뺍니다.
    3) 원본이 지워진 메시지는 스타보드 숫자로 셉니다.
    """
    board_posts: dict[int, dict] = {}
    async for m in board.history(limit=None):
        if m.author.id != CARL_BOT_ID:
            continue
        p = _board_from_message(m)
        if p and p["message_id"] not in board_posts:  # 최신 글 우선
            p["name"] = (m.embeds[0].author.name if m.embeds[0].author else "") or ""
            board_posts[p["message_id"]] = p
    await progress(f"스타보드 글 {len(board_posts):,}개를 읽었습니다. 원래 채널을 훑는 중...")

    sources = sorted({p["channel_id"] for p in board_posts.values()})
    rows: dict[int, tuple[int, int, int]] = {}
    scanned_channels, scanned_messages = [], 0
    for i, cid in enumerate(sources, 1):
        try:
            channel = guild.get_channel_or_thread(cid) or await guild.fetch_channel(cid)
        except discord.HTTPException:
            continue  # 지워진 채널 — 스타보드 숫자로 셈
        if not hasattr(channel, "history"):
            continue
        try:
            async for m in channel.history(limit=None):
                scanned_messages += 1
                reaction = next((r for r in m.reactions if str(r.emoji) == STAR), None)
                if reaction is None or m.author.bot:
                    continue
                if m.id in board_posts:
                    stars = board_posts[m.id]["stars"]
                else:
                    stars = reaction.count - (1 if await _self_starred(reaction, m.author.id) else 0)
                if stars > 0:
                    rows[m.id] = (channel.id, m.author.id, stars)
        except discord.Forbidden:
            continue
        scanned_channels.append(cid)
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

    replace_guild_stars(guild.id, rows)
    s = _settings(guild.id)
    s["star_board_channel"] = board.id
    s["star_channels"] = sorted(set(s.get("star_channels") or []) | set(scanned_channels))
    s["stars_imported"] = True
    activity.get_db().save_settings(guild.id, s)
    return {
        "board_posts": len(board_posts),
        "channels": len(scanned_channels),
        "messages": scanned_messages,
        "starred": len(rows),
        "stars": sum(r[2] for r in rows.values()),
        "people": len({r[1] for r in rows.values()}),
        "unknown": unknown,
    }


# =====================================================================
# 명령어
# =====================================================================
def register(tree: app_commands.CommandTree) -> None:
    ensure_tables()

    @tree.command(name="스타순위", description="스타보드 별(⭐)을 많이 받은 순위를 봅니다. 한 페이지에 10명씩.")
    @app_commands.guild_only()
    @app_commands.describe(페이지="볼 페이지 (1페이지 = 1~10위)")
    async def star_ranking(interaction: discord.Interaction, 페이지: app_commands.Range[int, 1, 1000] = 1):
        gid = interaction.guild_id
        if not _settings(gid).get("stars_imported"):
            await interaction.response.send_message("📋 아직 별 기록이 없습니다. 관리자가 `/스타관리 가져오기`를 먼저 실행해야 합니다.", ephemeral=True)
            return
        total = count_star_users(gid)
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(페이지, pages)
        rows = top_stars(gid, PAGE_SIZE, (page - 1) * PAGE_SIZE)
        if not rows:
            await interaction.response.send_message("📋 아직 별 기록이 없습니다.", ephemeral=True)
            return
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = [
            f"{medals.get(i, f'`{i}.`')} <@{uid}> — {STAR} **{stars:,}** (작품 {works:,}개)"
            for i, (uid, stars, works) in enumerate(rows, start=(page - 1) * PAGE_SIZE + 1)
        ]
        await interaction.response.send_message(
            f"### {STAR} 받은 별 순위 — {page} / {pages} 페이지\n" + "\n".join(lines),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    admin = app_commands.Group(
        name="스타관리",
        description="[관리자용] 스타보드 별 기록을 관리합니다.",
        default_permissions=discord.Permissions(manage_guild=True),
        guild_only=True,
    )

    @admin.command(name="가져오기", description="칼봇 스타보드와 원래 채널을 훑어 지금까지 받은 별을 모읍니다. (몇 분~수십 분)")
    @app_commands.describe(스타보드채널="칼봇 스타보드 채널")
    async def import_cmd(interaction: discord.Interaction, 스타보드채널: discord.TextChannel):
        guild = interaction.guild
        if guild.id in _importing:
            await interaction.response.send_message("⏳ 이미 가져오는 중입니다. 끝나면 이 채널에 알려 드립니다.", ephemeral=True)
            return
        _importing.add(guild.id)
        await interaction.response.send_message(
            f"⏳ {스타보드채널.mention}과 원래 채널을 훑기 시작합니다. 진행 상황은 아래 메시지에 표시됩니다.\n"
            "다시 가져오면 이전 기록과 보정값은 지워지고 새로 계산됩니다.",
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

    @admin.command(name="칼봇맞추기", description="이 채널의 최근 칼봇 `starboard stats` 답변을 읽어 그 사람들의 별 수를 칼봇과 맞춥니다.")
    @app_commands.describe(채널="칼봇 stats 답변이 있는 채널 (비워두면 현재 채널)")
    async def carl_sync(interaction: discord.Interaction, 채널: Optional[discord.TextChannel] = None):
        guild = interaction.guild
        channel = 채널 or interaction.channel
        if not _settings(guild.id).get("stars_imported"):
            await interaction.response.send_message("❌ 먼저 `/스타관리 가져오기`를 실행해 주세요.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        results, missed = {}, 0
        async for m in channel.history(limit=300, oldest_first=False):
            if m.author.id != CARL_BOT_ID:
                continue
            stats = _carl_stats_from_message(m)
            if not stats:
                continue
            uid = stats[0]
            if uid is not None and uid in results:  # 같은 사람은 가장 최근 답변만
                continue
            r = await _apply_carl_stats(m, stats)
            if r is None:
                missed += 1
                continue
            results.setdefault(r[0], r)
        if not results:
            await interaction.followup.send("📋 칼봇 `starboard stats @유저` 답변을 찾지 못했습니다.", ephemeral=True)
            return
        lines = [f"- <@{uid}>: {before:,} → **{after:,}**" for uid, before, after in results.values()]
        text = f"✅ {len(results)}명의 별 수를 칼봇과 맞췄습니다.\n" + "\n".join(lines)
        if missed:
            text += f"\n- 누구인지 알 수 없는 답변 {missed}개는 건너뛰었습니다. `/스타관리 보정`으로 직접 맞춰 주세요."
        for chunk in activity._split_lines(text):
            await interaction.followup.send(chunk, ephemeral=True)

    @admin.command(name="보정", description="유저가 받은 별 합계를 직접 정합니다. (칼봇 숫자와 맞출 때)")
    @app_commands.describe(유저="대상 유저", 별수="받은 별 합계")
    async def adjust(interaction: discord.Interaction, 유저: discord.Member, 별수: app_commands.Range[int, 0, 1000000]):
        before = set_total(interaction.guild_id, 유저.id, 별수)
        await interaction.response.send_message(
            f"✅ {유저.mention}님의 받은 별: {before:,} → **{별수:,}**", ephemeral=True
        )

    @admin.command(name="설정목록", description="별 기록 설정과 현황을 봅니다.")
    async def show(interaction: discord.Interaction):
        gid = interaction.guild_id
        s = _settings(gid)
        board = s.get("star_board_channel")
        chans = s.get("star_channels") or []
        totals = _conn().execute(
            "SELECT COUNT(*), COALESCE(SUM(stars), 0) FROM star_messages WHERE guild_id = ? AND stars > 0", (gid,)
        ).fetchone()
        adjusted = _conn().execute("SELECT COUNT(*) FROM star_adjust WHERE guild_id = ?", (gid,)).fetchone()[0]
        text = (
            f"### {STAR} 별 기록 설정\n"
            f"- 기록: {'켜짐' if s.get('stars_imported') else '꺼짐 (`/스타관리 가져오기` 전)'}\n"
            f"- 스타보드 채널: {f'<#{board}>' if board else '없음'}\n"
            f"- 별을 세는 채널 ({len(chans)}개): {' '.join(f'<#{c}>' for c in chans) or '없음'}\n"
            f"- 별 받은 메시지 {totals[0]:,}개, 별 합계 {totals[1]:,}개 (보정한 사람 {adjusted}명)"
        )
        for i, chunk in enumerate(activity._split_lines(text)):
            if i == 0:
                await interaction.response.send_message(chunk, ephemeral=True)
            else:
                await interaction.followup.send(chunk, ephemeral=True)

    tree.add_command(admin)
