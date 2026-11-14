"""
역할 유지 (주인 요청 2026-11-14)

서버를 나갔다가 다시 들어온 사람에게, 나가기 직전에 갖고 있던 역할을 돌려줍니다.
디스코드는 서버를 나가면 그 사람의 역할을 모두 지우기 때문입니다 (레벨 · 등급 역할 포함).

- 서버마다 켜고 끕니다: /역할유지 기능 켜기·끄기 (기본: 꺼짐).
- 디스코드 개발자 포털 → Bot → "Server Members Intent"가 켜져 있어야 동작합니다.
  (들어오고 나가는 것을 알려면 필요. 켜져 있지 않으면 bot.py가 알아서 이 인텐트 없이 접속하고, 이 기능만 쉬게 됩니다.)
- 기록 (`kept_roles` 테이블, 역할 ID 목록만 저장):
  · 역할이 바뀔 때마다(on_member_update) · 나갈 때(on_raw_member_remove) 최신 역할을 저장
  · 봇이 켜질 때 · 기능을 켤 때 서버 전체를 한 번 저장
  → 봇이 꺼져 있는 동안 나간 사람도 마지막으로 본 역할이 남아 있음.
- 돌려주기: 들어올 때(on_member_join). 봇이 꺼져 있는 동안 나갔다 들어온 사람은
  봇이 켜질 때 "들어온 시각이 기록보다 나중"인 것으로 알아보고 돌려줌.
- 돌려주지 않는 역할(순수 함수 `restorable_roles`): @everyone, 디스코드가 관리하는 역할(Server Booster · 봇 역할),
  크리스봇 역할보다 위이거나 같은 역할(봇이 줄 수 없음), 닉네임색 역할(부스터 전용), /역할유지 제외추가 로 뺀 역할,
  이제 서버에 없는 역할.
"""

import json
import logging
import time
from datetime import datetime, timezone
from typing import Iterable, Optional

import discord
from discord import app_commands

import activity
import colors

logger = logging.getLogger("Chris-bot.keeproles")

# bot.py가 접속할 때 Server Members Intent를 쓸 수 있었는지 (False면 이 기능은 쉼)
members_intent_ok = False


# =====================================================================
# 데이터베이스 (data/chris.db)
# =====================================================================
def _conn():
    return activity.get_db().conn


def ensure_tables() -> None:
    _conn().executescript(
        """
        -- 멤버가 마지막으로 가진 역할 (역할 유지가 켜진 서버만 기록)
        CREATE TABLE IF NOT EXISTS kept_roles (
            guild_id   INTEGER NOT NULL,
            user_id    INTEGER NOT NULL,
            role_ids   TEXT NOT NULL,      -- JSON 배열
            updated_at REAL NOT NULL,      -- UTC 유닉스 시각
            PRIMARY KEY (guild_id, user_id)
        );
        """
    )
    _conn().commit()


def save_roles(guild_id: int, user_id: int, role_ids: Iterable[int], when: Optional[float] = None) -> None:
    _conn().execute(
        "INSERT INTO kept_roles (guild_id, user_id, role_ids, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(guild_id, user_id) DO UPDATE SET role_ids = excluded.role_ids, updated_at = excluded.updated_at",
        (guild_id, user_id, json.dumps(sorted(role_ids)), when if when is not None else time.time()),
    )
    _conn().commit()


def saved_roles(guild_id: int, user_id: int) -> Optional[tuple[list[int], float]]:
    row = _conn().execute(
        "SELECT role_ids, updated_at FROM kept_roles WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
    ).fetchone()
    return (json.loads(row[0]), row[1]) if row else None


def saved_count(guild_id: int) -> int:
    return _conn().execute("SELECT COUNT(*) FROM kept_roles WHERE guild_id = ?", (guild_id,)).fetchone()[0]


def _settings(guild_id: int) -> dict:
    return activity.get_db().get_settings(guild_id)


def is_on(guild_id: int) -> bool:
    return members_intent_ok and bool(_settings(guild_id).get("keep_roles"))


def _role_ids(member: discord.Member) -> list[int]:
    return [r.id for r in member.roles if not r.is_default()]


# =====================================================================
# 판단 (순수 함수)
# =====================================================================
def restorable_roles(saved: Iterable[int], current: Iterable[int], roles: dict, bot_top: int,
                     excluded: Iterable[int], color_ids: Iterable[int]) -> list[int]:
    """돌려줄 역할 ID들.

    roles: 서버의 역할 {id: (위치, 디스코드가 관리하는 역할인지, @everyone인지)}
    bot_top: 크리스봇의 가장 높은 역할 위치 (그보다 아래 역할만 줄 수 있음)
    """
    skip = set(current) | set(excluded) | set(color_ids)
    out = []
    for rid in saved:
        info = roles.get(rid)
        if info is None or rid in skip:
            continue
        position, managed, default = info
        if managed or default or position >= bot_top:
            continue
        out.append(rid)
    return out


# =====================================================================
# 동작
# =====================================================================
async def restore(member: discord.Member, saved: list[int]) -> list[discord.Role]:
    guild = member.guild
    me = guild.me
    if me is None or not me.guild_permissions.manage_roles:
        logger.warning(f"역할 유지: {guild.name}에서 역할 관리 권한이 없어 돌려주지 못함")
        return []
    roles = {r.id: (r.position, r.managed, r.is_default()) for r in guild.roles}
    ids = restorable_roles(saved, _role_ids(member), roles, me.top_role.position,
                           _settings(guild.id).get("keep_roles_excluded", []), colors.color_role_ids(guild.id))
    give = [guild.get_role(rid) for rid in ids]
    give = [r for r in give if r is not None]
    if not give:
        return []
    try:
        await member.add_roles(*give, reason="역할 유지: 다시 들어온 멤버에게 예전 역할 돌려줌")
    except discord.HTTPException as e:
        logger.warning(f"역할 유지: {member} 역할 돌려주기 실패: {e}")
        return []
    logger.info(f"역할 유지: {guild.name} {member.id}에게 역할 {len(give)}개 돌려줌")
    return give


async def handle_join(member: discord.Member) -> None:
    if member.bot or not is_on(member.guild.id):
        return
    snap = saved_roles(member.guild.id, member.id)  # 다른 봇이 역할을 주기 전에 먼저 읽어 둠
    if snap and snap[0]:
        await restore(member, snap[0])


def handle_update(before: discord.Member, after: discord.Member) -> None:
    if after.bot or not is_on(after.guild.id) or before.roles == after.roles:
        return
    save_roles(after.guild.id, after.id, _role_ids(after))


def handle_remove(payload: discord.RawMemberRemoveEvent) -> None:
    user = payload.user
    if user.bot or not is_on(payload.guild_id):
        return
    if isinstance(user, discord.Member):  # 캐시에 있으면 나가기 직전 역할 (없으면 마지막 기록을 그대로 씀)
        save_roles(payload.guild_id, user.id, _role_ids(user))


def _ts(dt: Optional[datetime]) -> Optional[float]:
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


async def sync_guild(guild: discord.Guild) -> tuple[int, int]:
    """서버 전체의 지금 역할을 저장합니다. 봇이 꺼져 있는 동안 다시 들어온 사람은 먼저 돌려줌.
    결과: (저장한 사람 수, 역할을 돌려준 사람 수)"""
    if not is_on(guild.id):
        return 0, 0
    restored = 0
    rows = []
    now = time.time()
    for member in guild.members:
        if member.bot:
            continue
        snap = saved_roles(guild.id, member.id)
        joined = _ts(member.joined_at)
        if snap and snap[0] and joined is not None and joined > snap[1]:
            if await restore(member, snap[0]):
                restored += 1
                continue  # 역할이 바뀌면 on_member_update가 새로 저장
        rows.append((guild.id, member.id, json.dumps(sorted(_role_ids(member))), now))
    _conn().executemany(
        "INSERT INTO kept_roles (guild_id, user_id, role_ids, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(guild_id, user_id) DO UPDATE SET role_ids = excluded.role_ids, updated_at = excluded.updated_at",
        rows,
    )
    _conn().commit()
    return len(rows), restored


async def sync_all(client: discord.Client) -> None:
    for guild in client.guilds:
        if not is_on(guild.id):
            continue
        try:
            if not guild.chunked:
                await guild.chunk()
            saved, restored = await sync_guild(guild)
            logger.info(f"역할 유지: {guild.name} 멤버 {saved}명 저장, {restored}명에게 역할 돌려줌")
        except Exception:
            logger.exception(f"역할 유지: {guild.name} 전체 저장 실패")


# =====================================================================
# 명령어
# =====================================================================
INTENT_HELP = (
    "⚠️ 지금은 크리스봇이 **Server Members Intent** 없이 접속해 있어 이 기능이 동작하지 않습니다.\n"
    "디스코드 개발자 포털(https://discord.com/developers/applications) → 크리스봇 → **Bot** → "
    "**Server Members Intent** 를 켜고 저장한 뒤, 봇을 다시 시작해 주세요."
)


def register(tree: app_commands.CommandTree) -> None:
    ensure_tables()

    group = app_commands.Group(
        name="역할유지",
        description="[관리자용] 서버를 나갔다 다시 들어온 사람에게 예전 역할을 돌려줍니다.",
        default_permissions=discord.Permissions(manage_roles=True),
        guild_only=True,
    )

    @group.command(name="기능", description="역할 유지를 켜거나 끕니다.")
    @app_commands.choices(상태=[app_commands.Choice(name="켜기", value=1), app_commands.Choice(name="끄기", value=0)])
    async def toggle(interaction: discord.Interaction, 상태: int):
        database = activity.get_db()
        s = database.get_settings(interaction.guild_id)
        s["keep_roles"] = bool(상태)
        database.save_settings(interaction.guild_id, s)
        if not 상태:
            await interaction.response.send_message(
                "⚪ 역할 유지를 **껐습니다**. 다시 들어온 사람에게 역할을 돌려주지 않습니다. *(저장해 둔 기록은 남아 있어요.)*",
                ephemeral=True,
            )
            return
        if not members_intent_ok:
            await interaction.response.send_message("🟡 역할 유지를 켜 두었습니다.\n" + INTENT_HELP, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        if not guild.chunked:
            await guild.chunk()
        saved, restored = await sync_guild(guild)
        text = (
            f"🟢 역할 유지를 **켰습니다**. 지금 멤버 **{saved:,}명**의 역할을 저장했습니다.\n"
            "이제 누가 나갔다 다시 들어오면 나가기 직전 역할을 돌려줍니다."
        )
        if restored:
            text += f"\n(예전 기록이 있는 {restored}명에게는 바로 역할을 돌려줬습니다.)"
        if not guild.me.guild_permissions.manage_roles:
            text += "\n⚠️ 크리스봇에 **역할 관리** 권한이 없어 지금은 돌려줄 수 없습니다."
        text += "\n*(크리스봇 역할보다 위에 있는 역할, Server Booster 같은 자동 역할, 닉네임색 역할은 돌려주지 않습니다.)*"
        await interaction.followup.send(text, ephemeral=True)

    @group.command(name="제외추가", description="다시 들어와도 돌려주지 않을 역할을 추가합니다.")
    async def add_excluded(interaction: discord.Interaction, 역할: discord.Role):
        database = activity.get_db()
        s = database.get_settings(interaction.guild_id)
        ids = s.setdefault("keep_roles_excluded", [])
        if 역할.id not in ids:
            ids.append(역할.id)
        database.save_settings(interaction.guild_id, s)
        await interaction.response.send_message(
            f"✅ {역할.mention} 역할은 다시 들어와도 돌려주지 않습니다.", ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @group.command(name="제외제거", description="돌려주지 않을 역할 목록에서 뺍니다.")
    async def remove_excluded(interaction: discord.Interaction, 역할: discord.Role):
        database = activity.get_db()
        s = database.get_settings(interaction.guild_id)
        ids = s.get("keep_roles_excluded", [])
        if 역할.id in ids:
            ids.remove(역할.id)
        database.save_settings(interaction.guild_id, s)
        await interaction.response.send_message(
            f"✅ {역할.mention} 역할도 다시 돌려줍니다.", ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @group.command(name="설정", description="역할 유지 설정과 저장 상태를 봅니다.")
    async def show(interaction: discord.Interaction):
        s = _settings(interaction.guild_id)
        on = bool(s.get("keep_roles"))
        excluded = [f"<@&{rid}>" for rid in s.get("keep_roles_excluded", [])]
        lines = [
            f"**역할 유지**: {'🟢 켜짐' if on else '⚪ 꺼짐'}",
            f"**저장된 멤버**: {saved_count(interaction.guild_id):,}명",
            f"**돌려주지 않는 역할**: {', '.join(excluded) if excluded else '없음'}",
            "*(그 밖에 크리스봇 역할보다 위 역할 · Server Booster 같은 자동 역할 · 닉네임색 역할도 돌려주지 않음)*",
        ]
        if on and not members_intent_ok:
            lines.append(INTENT_HELP)
        await interaction.response.send_message(
            "\n".join(lines), ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    tree.add_command(group)
