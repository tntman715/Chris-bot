"""
활동량 · 레벨 · 재화 시스템

- 채팅을 감지해 경험치(레벨)와 재화(코인)를 적립합니다.
- 글 내용은 저장하지 않습니다. 길이(글자 수)와 사진·영상 첨부 여부만 사용하고 바로 버립니다.
- 데이터는 data/chris.db (SQLite)에 유저 ID 기준으로 저장하므로, 서버를 나갔다 들어와도 유지됩니다.
- 레벨 공식은 Arcane/MEE6과 같은 공식을 사용합니다 (나중에 Arcane 데이터를 그대로 옮기기 위함).
"""

import json
import logging
import random
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands

logger = logging.getLogger("Chris-bot.activity")

DB_FILE = Path(__file__).parent / "data" / "chris.db"
KST = timezone(timedelta(hours=9))

# ---------------------------------------------------------------------
# 조정 가능한 숫자들
# ---------------------------------------------------------------------
XP_COOLDOWN_SECONDS = 60  # 같은 사람은 이 시간(초)에 한 번만 경험치/코인 지급 (도배 방지)
XP_MIN, XP_MAX = 15, 25  # 1회 지급 경험치 (Arcane/MEE6 기본값과 동일)
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
}


# =====================================================================
# 레벨 공식 (Arcane/MEE6 호환: 레벨 L -> L+1 에 필요한 경험치 = 5L² + 50L + 100)
# =====================================================================
def xp_to_next(level: int) -> int:
    return 5 * level * level + 50 * level + 100


def total_xp_for_level(level: int) -> int:
    """레벨 level 이 시작되는 누적 경험치"""
    return sum(xp_to_next(i) for i in range(level))


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

    def top_members(self, guild_id: int, column: str, limit: int = 10) -> list[tuple[int, int]]:
        assert column in ("xp", "coins", "contribution")
        return self.conn.execute(
            f"SELECT user_id, {column} FROM members WHERE guild_id = ? AND {column} > 0 "
            f"ORDER BY {column} DESC LIMIT ?",
            (guild_id, limit),
        ).fetchall()

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
            await _on_level_up(message, message.author, new_level, settings)


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
    message: discord.Message, member: discord.Member, new_level: int, settings: dict
) -> None:
    new_role = await sync_level_roles(member, new_level, settings)
    text = f"🎉 {member.mention}님이 **레벨 {new_level}**이 되었습니다!"
    if new_role:
        text += f" ({new_role.mention} 역할 획득)"

    channel = None
    if settings.get("announce_channel_id"):
        channel = member.guild.get_channel(int(settings["announce_channel_id"]))
    if channel is None:
        channel = message.channel
    try:
        await channel.send(text, allowed_mentions=discord.AllowedMentions(users=[member], roles=False))
    except discord.HTTPException as e:
        logger.warning(f"레벨업 알림 전송 실패: {e}")


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
            f"- **지급 규칙**: {XP_COOLDOWN_SECONDS}초에 한 번, 경험치 {XP_MIN}~{XP_MAX} / "
            f"{CURRENCY_NAME} {COIN_BASE}개 (긴 글: {bonus}, 사진·영상 +{COIN_MEDIA_BONUS})"
        )
        await interaction.response.send_message(msg, ephemeral=True)

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
