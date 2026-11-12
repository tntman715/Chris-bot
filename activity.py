"""
활동량 · 레벨 · 재화 시스템

- 채팅을 감지해 경험치(레벨)와 재화(코인)를 적립합니다.
- 글 내용은 저장하지 않습니다. 길이(글자 수)와 사진·영상 첨부 여부만 사용하고 바로 버립니다.
- 데이터는 data/chris.db (SQLite)에 유저 ID 기준으로 저장하므로, 서버를 나갔다 들어와도 유지됩니다.
- 레벨 공식 · 경험치 지급량은 이 서버의 기존 Arcane 설정과 똑같이 맞췄습니다 (Arcane 순위표 100명으로 검증).
  Arcane "Linear, Multiplier 1" = 레벨 L → L+1 에 필요한 경험치 100L + 75
"""

import asyncio
import io
import json
import logging
import random
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Union

import discord
from discord import app_commands

import cards

logger = logging.getLogger("Chris-bot.activity")

DB_FILE = Path(__file__).parent / "data" / "chris.db"
ARCANE_IMPORT_FILE = Path(__file__).parent / "data" / "arcane_import.json"
GRADE_CONFIG_FILE = Path(__file__).parent / "data" / "config.json"  # bot.py 등급 시스템 설정 (읽기 전용)
MAX_BADGES = 10
KST = timezone(timedelta(hours=9))

# ---------------------------------------------------------------------
# 조정 가능한 숫자들 (경험치 관련 값은 기존 Arcane 설정과 동일)
# ---------------------------------------------------------------------
XP_COOLDOWN_SECONDS = 60  # 같은 사람은 이 시간(초)에 한 번만 경험치/코인 지급 (도배 방지)
XP_MIN, XP_MAX = 15, 40  # 채팅 1회 지급 경험치
REACTION_XP = 25  # 반응(이모지)을 단 사람에게 주는 경험치
REACTION_COOLDOWN_SECONDS = 300  # 반응 경험치는 5분에 한 번
LEVEL_BASE, LEVEL_STEP = 75, 100  # 레벨 L → L+1 필요 경험치 = LEVEL_STEP × L + LEVEL_BASE
COIN_BASE = 1  # 1회 지급 기본 코인
COIN_LENGTH_BONUS = ((150, 2), (50, 1))  # (최소 글자 수, 보너스) — 긴 글 보너스 (큰 값부터)
COIN_MEDIA_BONUS = 2  # 사진·영상 첨부 보너스
CURRENCY_NAME = "코인"
CURRENCY_EMOJI = "🪙"

MEDIA_EXTENSIONS = {
    "png", "jpg", "jpeg", "gif", "webp", "bmp", "heic", "avif",
    "mp4", "mov", "webm", "mkv", "avi", "m4v",
}

DEFAULT_SETTINGS = {
    "live": False,  # 켜져 있어야 레벨업 알림 · 레벨 역할 지급이 작동 (Arcane에서 옮기기 전에는 꺼 둠)
    "announce_channel_id": None,  # 레벨업 알림 채널 (없으면 채팅한 채널)
    "excluded_channels": [],  # 활동량을 세지 않을 채널 (스레드는 상위 채널 기준)
    "level_roles": {},  # "레벨" -> 역할 ID
    "badge_roles": [],  # /랭크 카드에 뱃지로 보여 줄 역할 ID (등록 순서대로 표시)
}


# =====================================================================
# 레벨 공식 (Arcane Linear: 레벨 L -> L+1 에 필요한 경험치 = 100L + 75)
# =====================================================================
def xp_to_next(level: int) -> int:
    return LEVEL_STEP * level + LEVEL_BASE


def total_xp_for_level(level: int) -> int:
    """레벨 level 이 시작되는 누적 경험치 (= 50L² + 25L)"""
    return LEVEL_STEP * level * (level - 1) // 2 + LEVEL_BASE * level


def level_from_xp(xp: int) -> int:
    level = 0
    while xp >= xp_to_next(level):
        xp -= xp_to_next(level)
        level += 1
    return level


def level_progress(xp: int) -> tuple[int, int, int]:
    """(레벨, 현재 레벨에서 쌓은 경험치, 다음 레벨까지 필요한 경험치)"""
    level = level_from_xp(xp)
    return level, xp - total_xp_for_level(level), xp_to_next(level)


def coins_for_message(length: int, has_media: bool) -> int:
    coins = COIN_BASE
    for min_len, bonus in COIN_LENGTH_BONUS:
        if length >= min_len:
            coins += bonus
            break
    if has_media:
        coins += COIN_MEDIA_BONUS
    return coins


def today_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def day_kst(days_ago: int) -> str:
    return (datetime.now(KST) - timedelta(days=days_ago)).strftime("%Y-%m-%d")


# =====================================================================
# 데이터베이스 (SQLite)
# =====================================================================
class ActivityDB:
    def __init__(self, path: Path = DB_FILE):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS members (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                xp INTEGER NOT NULL DEFAULT 0,
                coins INTEGER NOT NULL DEFAULT 0,
                contribution INTEGER NOT NULL DEFAULT 0,
                messages INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS daily (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                messages INTEGER NOT NULL DEFAULT 0,
                chars INTEGER NOT NULL DEFAULT 0,
                media INTEGER NOT NULL DEFAULT 0,
                xp INTEGER NOT NULL DEFAULT 0,
                coins INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id, day)
            );
            CREATE INDEX IF NOT EXISTS idx_daily_guild_day ON daily (guild_id, day);
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                data TEXT NOT NULL
            );
            """
        )
        self.conn.commit()
        self._settings_cache: dict[int, dict] = {}

    # ---------------- 설정 ----------------
    def get_settings(self, guild_id: int) -> dict:
        if guild_id in self._settings_cache:
            return self._settings_cache[guild_id]
        row = self.conn.execute(
            "SELECT data FROM guild_settings WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        settings = json.loads(json.dumps(DEFAULT_SETTINGS))
        if row:
            settings.update(json.loads(row[0]))
        self._settings_cache[guild_id] = settings
        return settings

    def save_settings(self, guild_id: int, settings: dict) -> None:
        self.conn.execute(
            "INSERT INTO guild_settings (guild_id, data) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET data = excluded.data",
            (guild_id, json.dumps(settings, ensure_ascii=False)),
        )
        self.conn.commit()
        self._settings_cache[guild_id] = settings

    # ---------------- 활동 기록 ----------------
    def record_message(
        self,
        guild_id: int,
        user_id: int,
        length: int,
        has_media: bool,
        xp_gain: int,
        coin_gain: int,
    ) -> tuple[int, int]:
        """메시지 1개를 기록하고 (이전 경험치, 새 경험치)를 반환합니다."""
        cur = self.conn.cursor()
        row = cur.execute(
            "SELECT xp FROM members WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
        old_xp = row[0] if row else 0
        cur.execute(
            "INSERT INTO members (guild_id, user_id, xp, coins, messages) VALUES (?, ?, ?, ?, 1) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET "
            "xp = xp + excluded.xp, coins = coins + excluded.coins, messages = messages + 1",
            (guild_id, user_id, xp_gain, coin_gain),
        )
        cur.execute(
            "INSERT INTO daily (guild_id, user_id, day, messages, chars, media, xp, coins) "
            "VALUES (?, ?, ?, 1, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id, user_id, day) DO UPDATE SET "
            "messages = messages + 1, chars = chars + excluded.chars, media = media + excluded.media, "
            "xp = xp + excluded.xp, coins = coins + excluded.coins",
            (guild_id, user_id, today_kst(), length, 1 if has_media else 0, xp_gain, coin_gain),
        )
        self.conn.commit()
        return old_xp, old_xp + xp_gain

    def get_member(self, guild_id: int, user_id: int) -> dict:
        row = self.conn.execute(
            "SELECT xp, coins, contribution, messages FROM members WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
        xp, coins, contribution, messages = row if row else (0, 0, 0, 0)
        return {"xp": xp, "coins": coins, "contribution": contribution, "messages": messages}

    def messages_since(self, guild_id: int, user_id: int, since_day: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(messages), 0) FROM daily WHERE guild_id = ? AND user_id = ? AND day >= ?",
            (guild_id, user_id, since_day),
        ).fetchone()
        return row[0]

    def xp_rank(self, guild_id: int, xp: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM members WHERE guild_id = ? AND xp > ?", (guild_id, xp)
        ).fetchone()
        return row[0] + 1

    def top_members(
        self, guild_id: int, column: str, limit: int = 10, offset: int = 0
    ) -> list[tuple[int, int]]:
        assert column in ("xp", "coins", "contribution")
        return self.conn.execute(
            f"SELECT user_id, {column} FROM members WHERE guild_id = ? AND {column} > 0 "
            f"ORDER BY {column} DESC LIMIT ? OFFSET ?",
            (guild_id, limit, offset),
        ).fetchall()

    def count_ranked(self, guild_id: int) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM members WHERE guild_id = ? AND xp > 0", (guild_id,)
        ).fetchone()[0]

    def top_recent(self, guild_id: int, since_day: str, limit: int = 10) -> list[tuple[int, int]]:
        return self.conn.execute(
            "SELECT user_id, SUM(messages) AS m FROM daily WHERE guild_id = ? AND day >= ? "
            "GROUP BY user_id ORDER BY m DESC LIMIT ?",
            (guild_id, since_day, limit),
        ).fetchall()

    def set_xp(self, guild_id: int, user_id: int, xp: int) -> int:
        """경험치를 지정한 값으로 바꾸고 이전 경험치를 반환합니다."""
        old = self.get_member(guild_id, user_id)["xp"]
        self.conn.execute(
            "INSERT INTO members (guild_id, user_id, xp) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET xp = excluded.xp",
            (guild_id, user_id, xp),
        )
        self.conn.commit()
        return old

    def add_xp(self, guild_id: int, user_id: int, amount: int) -> tuple[int, int]:
        """경험치만 더하고 (이전 경험치, 새 경험치)를 반환합니다. (반응 경험치, Arcane 가져오기용)"""
        old = self.get_member(guild_id, user_id)["xp"]
        self.conn.execute(
            "INSERT INTO members (guild_id, user_id, xp) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET xp = xp + excluded.xp",
            (guild_id, user_id, amount),
        )
        self.conn.execute(
            "INSERT INTO daily (guild_id, user_id, day, xp) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id, user_id, day) DO UPDATE SET xp = xp + excluded.xp",
            (guild_id, user_id, today_kst(), amount),
        )
        self.conn.commit()
        return old, old + amount

    def add_coins(self, guild_id: int, user_id: int, amount: int) -> int:
        """코인을 더하거나 빼고(0 미만으로는 내려가지 않음) 새 잔액을 반환합니다."""
        current = self.get_member(guild_id, user_id)["coins"]
        new_balance = max(0, current + amount)
        self.conn.execute(
            "INSERT INTO members (guild_id, user_id, coins) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET coins = excluded.coins",
            (guild_id, user_id, new_balance),
        )
        self.conn.commit()
        return new_balance


db: Optional[ActivityDB] = None
_last_award: dict[tuple[int, int], float] = {}


def get_db() -> ActivityDB:
    global db
    if db is None:
        db = ActivityDB()
    return db


# =====================================================================
# 메시지 감지
# =====================================================================
def _is_media(attachment: discord.Attachment) -> bool:
    ctype = (attachment.content_type or "").lower()
    if ctype.startswith(("image/", "video/")):
        return True
    ext = attachment.filename.rsplit(".", 1)[-1].lower() if "." in attachment.filename else ""
    return ext in MEDIA_EXTENSIONS


def _is_excluded(channel, settings: dict) -> bool:
    excluded = set(settings.get("excluded_channels", []))
    if not excluded:
        return False
    return channel.id in excluded or getattr(channel, "parent_id", None) in excluded


async def handle_message(message: discord.Message) -> None:
    """모든 채팅마다 호출됩니다. 내용은 저장하지 않고 길이와 첨부 여부만 사용합니다."""
    if message.author.bot or message.guild is None:
        return
    if message.type not in (discord.MessageType.default, discord.MessageType.reply):
        return
    if not isinstance(message.author, discord.Member):
        return

    database = get_db()
    guild_id = message.guild.id
    settings = database.get_settings(guild_id)
    if _is_excluded(message.channel, settings):
        return

    length = len(message.content or "")
    has_media = any(_is_media(a) for a in message.attachments)

    key = (guild_id, message.author.id)
    now = time.monotonic()
    awarded = now - _last_award.get(key, -1e9) >= XP_COOLDOWN_SECONDS
    xp_gain = coin_gain = 0
    if awarded:
        _last_award[key] = now
        xp_gain = random.randint(XP_MIN, XP_MAX)
        coin_gain = coins_for_message(length, has_media)

    old_xp, new_xp = database.record_message(
        guild_id, message.author.id, length, has_media, xp_gain, coin_gain
    )

    if awarded and settings.get("live"):
        old_level, new_level = level_from_xp(old_xp), level_from_xp(new_xp)
        if new_level > old_level:
            await _on_level_up(message.channel, message.author, new_level, settings)


_last_reaction_award: dict[tuple[int, int], float] = {}


async def handle_reaction(payload: discord.RawReactionActionEvent, client: discord.Client) -> None:
    """반응(이모지)을 단 사람에게 5분에 한 번 경험치를 줍니다 (Arcane 'Reactor' 설정과 동일, 코인은 없음)."""
    member = payload.member
    if payload.guild_id is None or member is None or member.bot:
        return
    database = get_db()
    settings = database.get_settings(payload.guild_id)
    channel = client.get_channel(payload.channel_id)
    if channel is not None and _is_excluded(channel, settings):
        return

    key = (payload.guild_id, member.id)
    now = time.monotonic()
    if now - _last_reaction_award.get(key, -1e9) < REACTION_COOLDOWN_SECONDS:
        return
    _last_reaction_award[key] = now

    old_xp, new_xp = database.add_xp(payload.guild_id, member.id, REACTION_XP)
    if settings.get("live") and level_from_xp(new_xp) > level_from_xp(old_xp) and channel is not None:
        await _on_level_up(channel, member, level_from_xp(new_xp), settings)


async def sync_level_roles(member: discord.Member, level: int, settings: dict) -> Optional[discord.Role]:
    """도달한 가장 높은 레벨 역할 하나만 남기고 나머지 레벨 역할은 제거합니다. 새로 준 역할을 반환."""
    level_roles = {int(k): int(v) for k, v in settings.get("level_roles", {}).items()}
    if not level_roles:
        return None
    reached = [lv for lv in level_roles if lv <= level]
    target_id = level_roles[max(reached)] if reached else None
    all_ids = set(level_roles.values())

    to_remove = [r for r in member.roles if r.id in all_ids and r.id != target_id]
    target_role = member.guild.get_role(target_id) if target_id else None
    added = None
    try:
        if to_remove:
            await member.remove_roles(*to_remove, reason="레벨 역할 갱신")
        if target_role and target_role not in member.roles:
            await member.add_roles(target_role, reason=f"레벨 {level} 달성")
            added = target_role
    except discord.HTTPException as e:
        logger.warning(f"레벨 역할 변경 실패 ({member}): {e}")
    return added


async def _on_level_up(
    source_channel, member: discord.Member, new_level: int, settings: dict
) -> None:
    new_role = await sync_level_roles(member, new_level, settings)
    # 기존 Arcane 알림 문구와 같은 형식 (멘션 알림은 울리지 않음)
    text = f"{member.mention} 이(가) **{new_level}** 레벨을 달성했습니다"
    if new_role:
        text += f" ({new_role.mention} 역할 획득)"

    channel = None
    if settings.get("announce_channel_id"):
        channel = member.guild.get_channel(int(settings["announce_channel_id"]))
    if channel is None:
        channel = source_channel
    try:
        await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as e:
        logger.warning(f"레벨업 알림 전송 실패: {e}")


def _normalize_name(name: str) -> str:
    return name.lower().replace("_", "").replace(".", "")


async def _find_member_by_username(guild: discord.Guild, username: str) -> Optional[discord.Member]:
    """Arcane 순위표의 아이디로 서버 멤버를 찾습니다. (정확히 일치 → 밑줄·점 무시 비교 순)"""
    try:
        found = await guild.query_members(query=username, limit=10, cache=False)
        for m in found:
            if m.name.lower() == username.lower():
                return m
        key = _normalize_name(username)
        prefix = username.strip("_.")[:3] or username[:3]
        found = await guild.query_members(query=prefix, limit=100, cache=False)
        for m in found:
            if _normalize_name(m.name) == key:
                return m
    except (asyncio.TimeoutError, discord.HTTPException) as e:
        logger.warning(f"멤버 검색 실패 ({username}): {e}")
    return None


# ---------------------------------------------------------------------
# 이미지 카드용 정보 모으기
# ---------------------------------------------------------------------
_avatar_cache: dict[str, bytes] = {}


async def _avatar_bytes(user: Union[discord.Member, discord.User]) -> Optional[bytes]:
    asset = user.display_avatar.replace(size=128, static_format="png")
    url = str(asset.url)
    if url in _avatar_cache:
        return _avatar_cache[url]
    try:
        data = await asset.read()
    except discord.HTTPException:
        return None
    if len(_avatar_cache) > 300:
        _avatar_cache.clear()
    _avatar_cache[url] = data
    return data


async def _role_badge(role: discord.Role) -> cards.Badge:
    icon = None
    if isinstance(role.display_icon, discord.Asset):
        try:
            icon = await role.display_icon.replace(size=64, static_format="png").read()
        except discord.HTTPException:
            icon = None
    color = role.color.to_rgb() if role.color.value else cards.GRAY
    return cards.Badge(role.name, color, icon)


async def _grade_badge(member: discord.Member) -> Optional[cards.Badge]:
    """등급 시스템(/등급관리)에 등록된 등급 역할 중 이 멤버가 가진 가장 높은 등급"""
    try:
        config = json.loads(GRADE_CONFIG_FILE.read_text(encoding="utf-8"))
        grades = config.get(str(member.guild.id), {}).get("grade_system", {}).get("grades", {})
    except (OSError, ValueError):
        return None
    best = None
    member_role_ids = {r.id for r in member.roles}
    for info in grades.values():
        rid = int(info.get("role_id") or 0)
        if rid in member_role_ids and (best is None or float(info["value"]) > float(best["value"])):
            best = info
    if best is None:
        return None
    role = member.guild.get_role(int(best["role_id"]))
    return await _role_badge(role) if role else cards.Badge(best.get("role_name", best["code"]))


async def _member_badges(member: discord.Member, settings: dict) -> list[cards.Badge]:
    member_role_ids = {r.id for r in member.roles}
    badges = []
    for rid in settings.get("badge_roles", []):
        if rid in member_role_ids:
            role = member.guild.get_role(rid)
            if role:
                badges.append(await _role_badge(role))
    return badges


def _split_lines(text: str, limit: int = 1900) -> list[str]:
    chunks, current = [], ""
    for line in text.split("\n"):
        if current and len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


# =====================================================================
# 명령어
# =====================================================================
def _progress_bar(current: int, total: int, width: int = 12) -> str:
    filled = int(width * current / total) if total else 0
    return "█" * filled + "░" * (width - filled)


def register(tree: app_commands.CommandTree) -> None:
    @tree.command(name="활동량", description="레벨, 경험치, 보유 코인, 채팅 활동량을 확인합니다.")
    @app_commands.guild_only()
    @app_commands.describe(유저="확인할 유저 (비워두면 본인)")
    async def show_activity(interaction: discord.Interaction, 유저: Optional[discord.Member] = None):
        target = 유저 or interaction.user
        database = get_db()
        gid = interaction.guild_id
        m = database.get_member(gid, target.id)
        level, cur, need = level_progress(m["xp"])
        rank = database.xp_rank(gid, m["xp"])
        today = database.messages_since(gid, target.id, today_kst())
        week = database.messages_since(gid, target.id, day_kst(6))
        month = database.messages_since(gid, target.id, day_kst(29))

        msg = (
            f"### 📊 {target.mention}님의 활동량\n"
            f"- **레벨 {level}** (서버 {rank}위)\n"
            f"  `{_progress_bar(cur, need)}` {cur} / {need} XP (누적 {m['xp']} XP)\n"
            f"- **보유 {CURRENCY_NAME}**: {m['coins']} {CURRENCY_EMOJI}\n"
            f"- **기여 점수**: {m['contribution']}\n"
            f"- **채팅 수**: 오늘 {today} · 최근 7일 {week} · 최근 30일 {month} · 전체 {m['messages']}"
        )
        await interaction.response.send_message(msg, ephemeral=True)

    @tree.command(name="순위", description="서버 활동 순위(상위 10명)를 확인합니다.")
    @app_commands.guild_only()
    @app_commands.describe(기준="어떤 기준으로 순위를 볼지 선택하세요")
    @app_commands.choices(
        기준=[
            app_commands.Choice(name="레벨", value="xp"),
            app_commands.Choice(name=f"보유 {CURRENCY_NAME}", value="coins"),
            app_commands.Choice(name="기여 점수", value="contribution"),
            app_commands.Choice(name="최근 7일 채팅 수", value="week"),
            app_commands.Choice(name="최근 30일 채팅 수", value="month"),
        ]
    )
    async def show_ranking(interaction: discord.Interaction, 기준: str = "xp"):
        database = get_db()
        gid = interaction.guild_id
        if 기준 == "week":
            rows, title, fmt = database.top_recent(gid, day_kst(6)), "최근 7일 채팅 수", lambda v: f"{v}개"
        elif 기준 == "month":
            rows, title, fmt = database.top_recent(gid, day_kst(29)), "최근 30일 채팅 수", lambda v: f"{v}개"
        elif 기준 == "coins":
            rows, title, fmt = database.top_members(gid, "coins"), f"보유 {CURRENCY_NAME}", lambda v: f"{v} {CURRENCY_EMOJI}"
        elif 기준 == "contribution":
            rows, title, fmt = database.top_members(gid, "contribution"), "기여 점수", lambda v: f"{v}점"
        else:
            rows, title, fmt = database.top_members(gid, "xp"), "레벨", lambda v: f"레벨 {level_from_xp(v)} ({v} XP)"

        if not rows:
            await interaction.response.send_message("📋 아직 기록이 없습니다.", ephemeral=True)
            return
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = [f"{medals.get(i, f'{i}.')} <@{uid}> — {fmt(v)}" for i, (uid, v) in enumerate(rows, 1)]
        await interaction.response.send_message(
            f"### 🏆 {title} 순위\n" + "\n".join(lines), ephemeral=True
        )

    @tree.command(name="랭크", description="레벨 카드(이미지)를 보여줍니다. 서버 등급과 뱃지도 함께 표시됩니다.")
    @app_commands.guild_only()
    @app_commands.describe(유저="확인할 유저 (비워두면 본인)")
    async def rank_card(interaction: discord.Interaction, 유저: Optional[discord.Member] = None):
        target = 유저 or interaction.user
        if not isinstance(target, discord.Member):
            return
        await interaction.response.defer(thinking=True)
        database = get_db()
        gid = interaction.guild_id
        m = database.get_member(gid, target.id)
        level, cur, need = level_progress(m["xp"])
        settings = database.get_settings(gid)
        data = cards.RankCardData(
            display_name=target.display_name,
            username=target.name,
            avatar=await _avatar_bytes(target),
            level=level,
            xp_in_level=cur,
            xp_needed=need,
            rank=database.xp_rank(gid, m["xp"]),
            coins=m["coins"],
            accent=target.color.to_rgb() if target.color.value else cards.DEFAULT_ACCENT,
            grade=await _grade_badge(target),
            badges=await _member_badges(target, settings),
        )
        png = await asyncio.to_thread(cards.render_rank_card, data)
        await interaction.followup.send(file=discord.File(io.BytesIO(png), filename="rank.png"))

    @tree.command(name="리더보드", description="레벨 순위표(이미지)를 보여줍니다. 한 페이지에 10명씩 표시됩니다.")
    @app_commands.guild_only()
    @app_commands.describe(페이지="볼 페이지 (1페이지 = 1~10위)")
    async def leaderboard(interaction: discord.Interaction, 페이지: app_commands.Range[int, 1, 1000] = 1):
        await interaction.response.defer(thinking=True)
        database = get_db()
        guild = interaction.guild
        total = database.count_ranked(guild.id)
        pages = max(1, (total + 9) // 10)
        page = min(페이지, pages)
        offset = (page - 1) * 10
        rows = database.top_members(guild.id, "xp", 10, offset)
        if not rows:
            await interaction.followup.send("📋 아직 레벨 기록이 없습니다.")
            return

        ids = [uid for uid, _ in rows]
        found: dict[int, Union[discord.Member, discord.User]] = {}
        try:
            for mem in await guild.query_members(user_ids=ids, limit=100, cache=True):
                found[mem.id] = mem
        except (asyncio.TimeoutError, discord.HTTPException) as e:
            logger.warning(f"리더보드 멤버 조회 실패: {e}")
        for uid in ids:
            if uid not in found:
                user = interaction.client.get_user(uid)
                if user is None:
                    try:
                        user = await interaction.client.fetch_user(uid)
                    except discord.HTTPException:
                        user = None
                if user is not None:
                    found[uid] = user

        leader_rows = []
        for i, (uid, xp) in enumerate(rows, start=offset + 1):
            who = found.get(uid)
            level, cur, need = level_progress(xp)
            leader_rows.append(
                cards.LeaderRow(
                    rank=i,
                    name=who.display_name if who else f"알 수 없음 ({uid})",
                    avatar=await _avatar_bytes(who) if who else None,
                    level=level,
                    progress=cur / need if need else 0,
                )
            )
        png = await asyncio.to_thread(cards.render_leaderboard, leader_rows)
        await interaction.followup.send(
            f"🏆 **레벨 순위** — {page} / {pages} 페이지",
            file=discord.File(io.BytesIO(png), filename="leaderboard.png"),
        )

    admin = app_commands.Group(
        name="레벨관리",
        description="[관리자용] 레벨 · 활동량 · 코인 시스템을 설정합니다.",
        default_permissions=discord.Permissions(manage_guild=True),
        guild_only=True,
    )

    @admin.command(name="레벨기능", description="레벨업 알림과 레벨 역할 지급을 켜거나 끕니다. (Arcane에서 옮긴 뒤에 켜세요)")
    @app_commands.choices(
        상태=[
            app_commands.Choice(name="켜기", value=1),
            app_commands.Choice(name="끄기", value=0),
        ]
    )
    async def toggle_live(interaction: discord.Interaction, 상태: int):
        database = get_db()
        s = database.get_settings(interaction.guild_id)
        s["live"] = bool(상태)
        database.save_settings(interaction.guild_id, s)
        if 상태:
            text = "🟢 레벨업 알림과 레벨 역할 지급을 **켰습니다**. *(Arcane의 레벨 기능은 꺼 주세요. 둘 다 켜져 있으면 알림이 두 번 나옵니다.)*"
        else:
            text = "⚪ 레벨업 알림과 레벨 역할 지급을 **껐습니다**. *(경험치와 코인 적립은 계속됩니다.)*"
        await interaction.response.send_message(text, ephemeral=True)

    @admin.command(name="알림채널", description="레벨업 알림을 보낼 채널을 정합니다. (비워두면 채팅한 채널에 알림)")
    async def set_announce(interaction: discord.Interaction, 채널: Optional[discord.TextChannel] = None):
        database = get_db()
        s = database.get_settings(interaction.guild_id)
        s["announce_channel_id"] = 채널.id if 채널 else None
        database.save_settings(interaction.guild_id, s)
        where = 채널.mention if 채널 else "채팅한 그 채널"
        await interaction.response.send_message(f"✅ 레벨업 알림을 {where}에 보냅니다.", ephemeral=True)

    @admin.command(name="제외채널추가", description="활동량(경험치·코인)을 세지 않을 채널을 추가합니다.")
    async def add_excluded(
        interaction: discord.Interaction,
        채널: discord.abc.GuildChannel,
    ):
        database = get_db()
        s = database.get_settings(interaction.guild_id)
        if 채널.id not in s["excluded_channels"]:
            s["excluded_channels"].append(채널.id)
            database.save_settings(interaction.guild_id, s)
        await interaction.response.send_message(
            f"✅ {채널.mention} 채널(안의 스레드 포함)은 이제 활동량을 세지 않습니다.", ephemeral=True
        )

    @admin.command(name="제외채널제거", description="활동량 제외 목록에서 채널을 뺍니다.")
    async def remove_excluded(
        interaction: discord.Interaction,
        채널: discord.abc.GuildChannel,
    ):
        database = get_db()
        s = database.get_settings(interaction.guild_id)
        if 채널.id in s["excluded_channels"]:
            s["excluded_channels"].remove(채널.id)
            database.save_settings(interaction.guild_id, s)
        await interaction.response.send_message(
            f"✅ {채널.mention} 채널을 다시 활동량에 포함합니다.", ephemeral=True
        )

    @admin.command(name="레벨역할추가", description="특정 레벨에 도달하면 줄 역할을 정합니다.")
    @app_commands.describe(레벨="역할을 줄 레벨 (예: 10)", 역할="그 레벨에 줄 역할")
    async def add_level_role(
        interaction: discord.Interaction,
        레벨: app_commands.Range[int, 1, 1000],
        역할: discord.Role,
    ):
        database = get_db()
        s = database.get_settings(interaction.guild_id)
        s["level_roles"][str(레벨)] = 역할.id
        database.save_settings(interaction.guild_id, s)
        await interaction.response.send_message(
            f"✅ **레벨 {레벨}**에 도달하면 {역할.mention} 역할을 줍니다. "
            "*(가장 높은 레벨 역할 하나만 남기고 아래 레벨 역할은 회수합니다.)*",
            ephemeral=True,
        )

    @admin.command(name="레벨역할제거", description="레벨 역할 설정을 지웁니다.")
    async def remove_level_role(interaction: discord.Interaction, 레벨: int):
        database = get_db()
        s = database.get_settings(interaction.guild_id)
        removed = s["level_roles"].pop(str(레벨), None)
        database.save_settings(interaction.guild_id, s)
        text = f"🗑️ 레벨 {레벨} 역할 설정을 지웠습니다." if removed else f"ℹ️ 레벨 {레벨}에는 설정된 역할이 없습니다."
        await interaction.response.send_message(text, ephemeral=True)

    @admin.command(name="뱃지추가", description="역할을 뱃지로 등록합니다. 그 역할을 가진 사람의 /랭크 카드에 표시됩니다.")
    @app_commands.describe(역할="뱃지로 보여 줄 역할 (역할 아이콘이 있으면 아이콘이, 없으면 역할 색이 표시됨)")
    async def add_badge(interaction: discord.Interaction, 역할: discord.Role):
        database = get_db()
        s = database.get_settings(interaction.guild_id)
        badges = s.setdefault("badge_roles", [])
        if 역할.id in badges:
            text = f"ℹ️ {역할.mention} 역할은 이미 뱃지로 등록되어 있습니다."
        elif len(badges) >= 25:
            text = "❌ 뱃지는 최대 25개까지 등록할 수 있습니다."
        else:
            badges.append(역할.id)
            database.save_settings(interaction.guild_id, s)
            text = (
                f"✅ {역할.mention} 역할을 뱃지로 등록했습니다. 이 역할을 가진 사람의 `/랭크` 카드에 표시됩니다.\n"
                f"*(카드에는 등급 다음에 등록한 순서대로 보이며, 자리가 모자라면 `+N`으로 표시됩니다.)*"
            )
        await interaction.response.send_message(text, ephemeral=True)

    @admin.command(name="뱃지제거", description="뱃지로 등록한 역할을 뺍니다.")
    async def remove_badge(interaction: discord.Interaction, 역할: discord.Role):
        database = get_db()
        s = database.get_settings(interaction.guild_id)
        badges = s.setdefault("badge_roles", [])
        if 역할.id in badges:
            badges.remove(역할.id)
            database.save_settings(interaction.guild_id, s)
            text = f"🗑️ {역할.mention} 역할을 뱃지 목록에서 뺐습니다."
        else:
            text = f"ℹ️ {역할.mention} 역할은 뱃지로 등록되어 있지 않습니다."
        await interaction.response.send_message(text, ephemeral=True)

    @admin.command(name="설정목록", description="레벨 · 활동량 시스템의 현재 설정을 확인합니다.")
    async def show_settings(interaction: discord.Interaction):
        s = get_db().get_settings(interaction.guild_id)
        live = "🟢 켜짐" if s.get("live") else "⚪ 꺼짐 (경험치·코인은 적립 중)"
        announce = f"<#{s['announce_channel_id']}>" if s.get("announce_channel_id") else "채팅한 채널"
        excluded = ", ".join(f"<#{c}>" for c in s.get("excluded_channels", [])) or "없음"
        roles = sorted(((int(k), v) for k, v in s.get("level_roles", {}).items()))
        role_text = "\n".join(f"  - 레벨 {lv}: <@&{rid}>" for lv, rid in roles) or "  - 없음"
        bonus = ", ".join(f"{n}자 이상 +{b}" for n, b in sorted(COIN_LENGTH_BONUS))
        msg = (
            "### ⚙️ 레벨 · 활동량 설정\n"
            f"- **레벨업 알림 · 역할 지급**: {live}\n"
            f"- **레벨업 알림 채널**: {announce}\n"
            f"- **활동량 제외 채널**: {excluded}\n"
            f"- **레벨 역할**:\n{role_text}\n"
            f"- **뱃지 역할** (`/랭크` 카드에 표시): "
            f"{', '.join(f'<@&{r}>' for r in s.get('badge_roles', [])) or '없음'}\n"
            f"- **지급 규칙**: 채팅은 {XP_COOLDOWN_SECONDS}초에 한 번 경험치 {XP_MIN}~{XP_MAX} + "
            f"{CURRENCY_NAME} {COIN_BASE}개 (긴 글: {bonus}, 사진·영상 +{COIN_MEDIA_BONUS}) / "
            f"반응은 {REACTION_COOLDOWN_SECONDS // 60}분에 한 번 경험치 {REACTION_XP}\n"
            f"- **Arcane 기록 가져오기**: {'완료 (' + str(len(s.get('arcane_imported', []))) + '명)' if s.get('arcane_imported') else '아직 안 함'}"
        )
        await interaction.response.send_message(msg, ephemeral=True)

    @admin.command(
        name="아케인가져오기",
        description="Arcane 순위표 1~100위 기록(경험치)을 크리스봇 경험치에 더합니다. (한 번만 실행)",
    )
    async def import_arcane(interaction: discord.Interaction):
        guild = interaction.guild
        database = get_db()
        settings = database.get_settings(guild.id)
        if settings.get("arcane_imported"):
            await interaction.response.send_message(
                f"ℹ️ 이미 Arcane 기록을 가져왔습니다 ({len(settings['arcane_imported'])}명). 두 번 더해지지 않도록 막았습니다.",
                ephemeral=True,
            )
            return
        if not ARCANE_IMPORT_FILE.exists():
            await interaction.response.send_message(
                "❌ 서버에 `data/arcane_import.json` 파일이 없습니다.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        entries = json.loads(ARCANE_IMPORT_FILE.read_text(encoding="utf-8"))
        imported: list[int] = []
        lines: list[str] = []
        missing: list[str] = []
        for e in entries:
            member = await _find_member_by_username(guild, e["username"])
            if member is None:
                missing.append(f"{e['rank']}위 `{e['username']}` (레벨 {e['level']})")
            else:
                old_xp, new_xp = database.add_xp(guild.id, member.id, int(e["xp"]))
                imported.append(member.id)
                lines.append(
                    f"{e['rank']}. {member.mention} 레벨 {level_from_xp(old_xp)} → **{level_from_xp(new_xp)}**"
                )
            await asyncio.sleep(0.7)  # 디스코드 요청 속도 제한 보호

        settings["arcane_imported"] = imported
        database.save_settings(guild.id, settings)

        report = (
            f"### 📥 Arcane 기록 가져오기 완료: {len(imported)}명\n" + "\n".join(lines)
        )
        if missing:
            report += (
                f"\n\n⚠️ **서버에서 찾지 못한 {len(missing)}명** "
                "(서버를 나갔거나 아이디가 바뀜 — 필요하면 `/레벨관리 레벨설정`으로 직접 지정):\n"
                + "\n".join(missing)
            )
        for chunk in _split_lines(report):
            await interaction.followup.send(chunk, ephemeral=True)

    @admin.command(name="레벨설정", description="유저의 레벨(경험치)을 직접 지정합니다. (Arcane 기록 보정 등)")
    @app_commands.describe(
        유저="레벨을 지정할 유저",
        레벨="지정할 레벨",
        추가경험치="그 레벨 안에서 이미 쌓은 경험치 (모르면 비워두세요)",
    )
    async def set_level(
        interaction: discord.Interaction,
        유저: discord.Member,
        레벨: app_commands.Range[int, 0, 1000],
        추가경험치: app_commands.Range[int, 0, 10_000_000] = 0,
    ):
        database = get_db()
        new_xp = total_xp_for_level(레벨) + min(추가경험치, xp_to_next(레벨) - 1)
        old_xp = database.set_xp(interaction.guild_id, 유저.id, new_xp)
        settings = database.get_settings(interaction.guild_id)
        role_note = ""
        if settings.get("live"):
            await sync_level_roles(유저, 레벨, settings)
            role_note = " 레벨 역할도 맞춰 갱신했습니다."
        await interaction.response.send_message(
            f"✅ {유저.mention}님을 **레벨 {level_from_xp(old_xp)} → 레벨 {레벨}** "
            f"(누적 {new_xp} XP)로 지정했습니다.{role_note}",
            ephemeral=True,
        )

    @admin.command(name="코인조정", description=f"유저의 {CURRENCY_NAME}을 더하거나 뺍니다. (빼려면 음수 입력)")
    @app_commands.describe(유저="대상 유저", 양="더할 양 (빼려면 -10 처럼 음수)")
    async def adjust_coins(interaction: discord.Interaction, 유저: discord.Member, 양: int):
        balance = get_db().add_coins(interaction.guild_id, 유저.id, 양)
        sign = "+" if 양 >= 0 else ""
        await interaction.response.send_message(
            f"✅ {유저.mention}님의 {CURRENCY_NAME} {sign}{양} → 현재 **{balance} {CURRENCY_EMOJI}**",
            ephemeral=True,
        )

    tree.add_command(admin)
