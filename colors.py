"""
서버 부스터 닉네임 색 (/닉네임색).

- 서버 부스터만 자기 닉네임 색을 정할 수 있습니다. 색을 두 개 넣으면 그라데이션.
- 그라데이션은 서버에 그라데이션 역할 색 기능(ENHANCED_ROLE_COLORS, 부스트 레벨)이 열려 있을 때만 됩니다.
- 색마다 역할 하나("🎨 #FF8800" / "🎨 #FF8800 → #00AAFF")를 만들어 같은 색을 고른 사람끼리 같이 씁니다.
  아무도 안 쓰게 된 색 역할은 지웁니다. 역할은 크리스봇 역할 바로 아래에 두어 다른 색 역할보다 위에 보이게 합니다.
- 부스트가 끝난 사람은 BOOST_CHECK_MINUTES 마다 확인해 색 역할을 회수합니다 (Server Members Intent 없이 REST로 확인).
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

logger = logging.getLogger("chris-bot")

BOOST_CHECK_MINUTES = 30
ROLE_PREFIX = "🎨 "
GRADIENT_FEATURE = "ENHANCED_ROLE_COLORS"

# 이름으로 고를 수 있는 색 (자동완성). 디스코드 어두운 화면 · 밝은 화면 모두에서 잘 보이는 색 위주.
PRESETS: dict[str, int] = {
    "빨강": 0xFF4D4D,
    "다홍": 0xFF6B4A,
    "주황": 0xFF9F43,
    "살구": 0xFFB38A,
    "금색": 0xFFC83D,
    "노랑": 0xFFE45C,
    "연두": 0xA3E635,
    "초록": 0x2ECC71,
    "민트": 0x3EECC4,
    "청록": 0x1ABC9C,
    "하늘": 0x5BC0FF,
    "파랑": 0x3D7BFF,
    "남색": 0x4B4BD8,
    "보라": 0x9B59FF,
    "연보라": 0xC9A7FF,
    "분홍": 0xFF7EB6,
    "핫핑크": 0xFF3D9A,
    "갈색": 0xA0673A,
    "은색": 0xC0C7D1,
    "회색": 0x9AA0A6,
    "흰색": 0xFFFFFF,
    "검정": 0x1A1A1A,
}

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6}|[0-9a-fA-F]{3})$")
_locks: dict[int, asyncio.Lock] = {}


# =====================================================================
# 순수 함수 (테스트 가능)
# =====================================================================
def parse_color(text: Optional[str]) -> Optional[int]:
    """'#FF8800', 'ff8800', '#f80', '빨강' → 0xFF8800. 알 수 없으면 None.
    디스코드에서 0(검정)은 '색 없음'이라 0x010101로 바꿉니다."""
    if text is None:
        return None
    t = text.strip().replace(" ", "")
    if not t:
        return None
    if t in PRESETS:
        value = PRESETS[t]
    else:
        m = _HEX_RE.match(t)
        if not m:
            return None
        h = m.group(1)
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        value = int(h, 16)
    return value or 0x010101


def hex_text(value: int) -> str:
    return f"#{value:06X}"


def role_name(color1: int, color2: Optional[int]) -> str:
    if color2 is None:
        return f"{ROLE_PREFIX}{hex_text(color1)}"
    return f"{ROLE_PREFIX}{hex_text(color1)} → {hex_text(color2)}"


def color_choices(current: str) -> list[tuple[str, str]]:
    """자동완성 목록 [(보이는 이름, 값)] — 최대 25개"""
    out: list[tuple[str, str]] = []
    cur = (current or "").strip()
    direct = parse_color(cur) if cur and cur.replace(" ", "") not in PRESETS else None
    if direct is not None:
        out.append((f"직접 입력: {hex_text(direct)}", hex_text(direct)))
    for name, value in PRESETS.items():
        if not cur or cur in name or cur.lower().lstrip("#") in hex_text(value).lower():
            out.append((f"{name} ({hex_text(value)})", hex_text(value)))
    return out[:25]


def is_booster(member: discord.Member) -> bool:
    if getattr(member, "premium_since", None) is not None:
        return True
    role = member.guild.premium_subscriber_role
    return role is not None and role in member.roles


# =====================================================================
# 데이터베이스 (activity 와 같은 data/chris.db)
# =====================================================================
def _conn():
    return activity.get_db().conn


def ensure_tables() -> None:
    _conn().executescript(
        """
        -- 색 역할: 같은 색(1색 또는 2색 그라데이션)을 고른 사람끼리 같이 씀
        CREATE TABLE IF NOT EXISTS color_roles (
            role_id INTEGER PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            color1 INTEGER NOT NULL,
            color2 INTEGER  -- NULL = 한 가지 색
        );
        CREATE INDEX IF NOT EXISTS idx_color_roles_guild ON color_roles (guild_id, color1, color2);
        -- 누가 어떤 색 역할을 쓰는지
        CREATE TABLE IF NOT EXISTS member_colors (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role_id INTEGER NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        );
        """
    )
    _conn().commit()


def find_color_role(guild_id: int, color1: int, color2: Optional[int]) -> Optional[int]:
    row = _conn().execute(
        "SELECT role_id FROM color_roles WHERE guild_id = ? AND color1 = ? AND color2 IS ?",
        (guild_id, color1, color2),
    ).fetchone()
    return row[0] if row else None


def add_color_role(guild_id: int, role_id: int, color1: int, color2: Optional[int]) -> None:
    _conn().execute(
        "INSERT OR REPLACE INTO color_roles (role_id, guild_id, color1, color2) VALUES (?, ?, ?, ?)",
        (role_id, guild_id, color1, color2),
    )
    _conn().commit()


def forget_color_role(role_id: int) -> None:
    _conn().execute("DELETE FROM color_roles WHERE role_id = ?", (role_id,))
    _conn().execute("DELETE FROM member_colors WHERE role_id = ?", (role_id,))
    _conn().commit()


def member_role(guild_id: int, user_id: int) -> Optional[int]:
    row = _conn().execute(
        "SELECT role_id FROM member_colors WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
    ).fetchone()
    return row[0] if row else None


def set_member_role(guild_id: int, user_id: int, role_id: Optional[int]) -> None:
    if role_id is None:
        _conn().execute("DELETE FROM member_colors WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
    else:
        _conn().execute(
            "INSERT OR REPLACE INTO member_colors (guild_id, user_id, role_id) VALUES (?, ?, ?)",
            (guild_id, user_id, role_id),
        )
    _conn().commit()


def holders(role_id: int) -> int:
    return _conn().execute("SELECT COUNT(*) FROM member_colors WHERE role_id = ?", (role_id,)).fetchone()[0]


def all_members() -> list[tuple[int, int, int]]:
    return _conn().execute("SELECT guild_id, user_id, role_id FROM member_colors").fetchall()


def unused_roles() -> list[tuple[int, int]]:
    """아무도 안 쓰는 색 역할 [(guild_id, role_id)]"""
    return _conn().execute(
        "SELECT guild_id, role_id FROM color_roles WHERE role_id NOT IN (SELECT role_id FROM member_colors)"
    ).fetchall()


# =====================================================================
# 역할 다루기
# =====================================================================
def _lock(guild_id: int) -> asyncio.Lock:
    return _locks.setdefault(guild_id, asyncio.Lock())


async def _delete_if_unused(guild: discord.Guild, role_id: int) -> None:
    if holders(role_id):
        return
    role = guild.get_role(role_id)
    forget_color_role(role_id)
    if role is not None:
        try:
            await role.delete(reason="닉네임색: 아무도 쓰지 않는 색 역할 정리")
        except discord.HTTPException:
            logger.warning("색 역할 삭제 실패: %s", role_id)


async def _get_or_create_role(guild: discord.Guild, color1: int, color2: Optional[int]) -> discord.Role:
    role_id = find_color_role(guild.id, color1, color2)
    role = guild.get_role(role_id) if role_id else None
    if role_id and role is None:  # 관리자가 손으로 지운 경우
        forget_color_role(role_id)
    if role is None:
        kwargs = {"name": role_name(color1, color2), "colour": discord.Colour(color1),
                  "permissions": discord.Permissions.none(), "mentionable": False, "hoist": False,
                  "reason": "닉네임색: 부스터 색 역할"}
        if color2 is not None:
            kwargs["secondary_colour"] = discord.Colour(color2)
        role = await guild.create_role(**kwargs)
        add_color_role(guild.id, role.id, color1, color2)
    return role


def color_role_ids(guild_id: int) -> set[int]:
    return {r[0] for r in _conn().execute("SELECT role_id FROM color_roles WHERE guild_id = ?", (guild_id,))}


def plan_positions(roles: list[tuple[int, int]], color_ids: set[int], bot_role_ids: set[int]) -> Optional[dict[int, int]]:
    """색 역할들을 크리스봇의 가장 높은 역할 바로 아래로 모으는 새 위치 {역할 ID: 위치} (바뀌는 것만).
    roles = [(역할 ID, 지금 위치)] (@everyone 제외). 옮길 수 없으면 None.
    디스코드는 위치가 같으면 ID가 큰(새) 역할을 아래로 봅니다."""
    order = sorted(roles, key=lambda r: (r[1], -r[0]))  # 아래 → 위
    tops = [i for i, (rid, _) in enumerate(order) if rid in bot_role_ids]
    if not tops:
        return None
    top = tops[-1]
    bot_position = order[top][1]
    below = [rid for rid, _ in order[:top]]
    new = [rid for rid in below if rid not in color_ids] + [rid for rid in below if rid in color_ids]
    if len(new) >= bot_position:  # 위치 번호가 겹쳐 있어 크리스봇 역할 아래에 다 못 넣음
        return None
    now = dict(roles)
    return {rid: i + 1 for i, rid in enumerate(new) if now[rid] != i + 1}


async def arrange_roles(guild: discord.Guild) -> dict[int, int]:
    """색 역할들을 크리스봇 역할 바로 아래로 옮깁니다 (다른 색 역할보다 위에 있어야 그 색이 보임).
    디스코드에서 최신 역할 순서를 받아 계산하며, 정리된 뒤의 {역할 ID: 위치}를 돌려줍니다."""
    roles = await guild.fetch_roles()
    pairs = [(r.id, r.position) for r in roles if not r.is_default()]
    bot_ids = {r.id for r in guild.me.roles if not r.is_default()}
    plan = plan_positions(pairs, color_role_ids(guild.id), bot_ids)
    final = dict(pairs)
    if plan is None:
        logger.warning("닉네임색: 크리스봇 역할 아래로 색 역할을 옮길 수 없음 (guild %s)", guild.id)
        return final
    if plan:
        by_id = {r.id: r for r in roles}
        await guild.edit_role_positions(positions={by_id[rid]: pos for rid, pos in plan.items()},
                                        reason="닉네임색: 색 역할을 크리스봇 역할 바로 아래로")
        final.update(plan)
    return final


async def apply_color(member: discord.Member, color1: int, color2: Optional[int]) -> tuple[discord.Role, list[str]]:
    """색 역할을 주고 (역할, 그보다 위에 있어 색을 가리는 역할 이름들)을 돌려줍니다."""
    guild = member.guild
    async with _lock(guild.id):
        role = await _get_or_create_role(guild, color1, color2)
        old_id = member_role(guild.id, member.id)
        if role not in member.roles:
            await member.add_roles(role, reason="닉네임색 설정")
        set_member_role(guild.id, member.id, role.id)
        if old_id and old_id != role.id:
            old = guild.get_role(old_id)
            if old is not None and old in member.roles:
                await member.remove_roles(old, reason="닉네임색 변경")
            await _delete_if_unused(guild, old_id)
        hiding: list[str] = []
        try:
            final = await arrange_roles(guild)
            mine = final.get(role.id, 0)
            hiding = [r.name for r in member.roles
                      if r.id != role.id and r.colour.value and final.get(r.id, 0) > mine]
        except discord.HTTPException:
            logger.exception("닉네임색: 역할 위치 정리 실패")
        return role, hiding


async def clear_color(guild: discord.Guild, user_id: int, member: Optional[discord.Member], reason: str) -> bool:
    async with _lock(guild.id):
        old_id = member_role(guild.id, user_id)
        if not old_id:
            return False
        set_member_role(guild.id, user_id, None)
        old = guild.get_role(old_id)
        if member is not None and old is not None and old in member.roles:
            try:
                await member.remove_roles(old, reason=reason)
            except discord.HTTPException:
                logger.warning("색 역할 회수 실패: %s", user_id)
        await _delete_if_unused(guild, old_id)
        return True


async def boost_watch(client: discord.Client) -> None:
    """부스트가 끝났거나 서버를 나간 사람의 색 역할을 주기적으로 회수합니다."""
    await client.wait_until_ready()
    while not client.is_closed():
        try:
            for guild_id, user_id, _role_id in all_members():
                guild = client.get_guild(guild_id)
                if guild is None:
                    continue
                member = guild.get_member(user_id)
                if member is None:
                    try:
                        member = await guild.fetch_member(user_id)
                    except discord.NotFound:
                        member = None
                if member is None or not is_booster(member):
                    await clear_color(guild, user_id, member, "닉네임색: 서버 부스트 종료")
                    logger.info("부스트가 끝나 닉네임색 회수: %s", user_id)
                await asyncio.sleep(1)
            for guild_id, role_id in unused_roles():
                guild = client.get_guild(guild_id)
                if guild is not None:
                    async with _lock(guild_id):
                        await _delete_if_unused(guild, role_id)
            # 누가 역할 순서를 바꿔 색 역할이 내려갔으면 다시 크리스봇 역할 바로 아래로
            guild_ids = {r[0] for r in _conn().execute("SELECT DISTINCT guild_id FROM color_roles")}
            for guild_id in guild_ids:
                guild = client.get_guild(guild_id)
                if guild is not None:
                    async with _lock(guild_id):
                        await arrange_roles(guild)
        except Exception:
            logger.exception("닉네임색 부스트 확인 중 오류")
        await asyncio.sleep(BOOST_CHECK_MINUTES * 60)


# =====================================================================
# 명령어
# =====================================================================
def register(tree: app_commands.CommandTree) -> None:
    ensure_tables()

    group = app_commands.Group(name="닉네임색", description="서버 부스터 전용: 내 닉네임 색을 정합니다. (두 색을 넣으면 그라데이션)",
                               guild_only=True)

    async def _autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return [app_commands.Choice(name=n, value=v) for n, v in color_choices(current)]

    def _preview(member: discord.Member, color1: int, color2: Optional[int]) -> discord.File:
        png = cards.render_name_color(member.display_name, color1, color2)
        return discord.File(io.BytesIO(png), filename="name_color.png")

    async def _parse(interaction: discord.Interaction, 색: str, 그라데이션색: Optional[str]):
        color1 = parse_color(색)
        color2 = parse_color(그라데이션색) if 그라데이션색 else None
        bad = [t for t, v in ((색, color1), (그라데이션색, color2)) if t and v is None]
        if bad:
            await interaction.response.send_message(
                f"❌ `{bad[0]}`은(는) 알 수 없는 색입니다. 목록에서 고르거나 `#FF8800`처럼 헥스 코드를 넣어 주세요.\n"
                "헥스 코드는 https://htmlcolorcodes.com/color-picker/ 에서 색을 눌러 복사할 수 있습니다.",
                ephemeral=True,
            )
            return None
        if color2 is not None and GRADIENT_FEATURE not in interaction.guild.features:
            await interaction.response.send_message(
                "❌ 이 서버는 아직 **그라데이션 역할 색**이 열려 있지 않습니다 (서버 부스트 레벨 부족).\n"
                "`그라데이션색` 칸을 비우고 한 가지 색으로 정해 주세요.",
                ephemeral=True,
            )
            return None
        return color1, color2

    @group.command(name="설정", description="내 닉네임 색을 정합니다. 그라데이션색까지 넣으면 두 색 그라데이션이 됩니다.")
    @app_commands.describe(색="색 이름을 고르거나 헥스 코드 입력 (예: #FF8800)",
                           그라데이션색="(선택) 두 번째 색 — 넣으면 왼쪽 → 오른쪽 그라데이션")
    @app_commands.autocomplete(색=_autocomplete, 그라데이션색=_autocomplete)
    async def set_color(interaction: discord.Interaction, 색: str, 그라데이션색: Optional[str] = None):
        member = interaction.user
        if not isinstance(member, discord.Member) or not is_booster(member):
            await interaction.response.send_message(
                "❌ **서버 부스터**만 닉네임 색을 정할 수 있습니다. 💎\n"
                "`/닉네임색 미리보기`로 어떻게 보일지는 누구나 볼 수 있어요.",
                ephemeral=True,
            )
            return
        parsed = await _parse(interaction, 색, 그라데이션색)
        if parsed is None:
            return
        color1, color2 = parsed
        if color2 == color1:
            color2 = None
        me = interaction.guild.me
        if not me.guild_permissions.manage_roles:
            await interaction.response.send_message("❌ 크리스봇에게 **역할 관리** 권한이 없습니다. 관리자에게 알려 주세요.",
                                                    ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            role, hiding = await apply_color(member, color1, color2)
        except discord.Forbidden:
            await interaction.followup.send("❌ 역할을 만들거나 줄 권한이 없습니다. 관리자에게 알려 주세요.", ephemeral=True)
            return
        except discord.HTTPException as e:
            logger.exception("닉네임색 설정 실패")
            await interaction.followup.send(f"❌ 색을 바꾸지 못했습니다: {e.text or e}", ephemeral=True)
            return
        text = f"✅ 닉네임 색을 **{role_name(color1, color2)[len(ROLE_PREFIX):]}**(으)로 바꿨습니다!"
        # 더 위에 있는 다른 색 역할이 있으면 그 색이 먼저 보임
        if hiding:
            text += f"\n⚠️ 더 위에 있는 `{hiding[0]}` 역할 색이 먼저 보일 수 있습니다. 관리자에게 알려 주세요."
        text += "\n색을 없애려면 `/닉네임색 해제`."
        await interaction.followup.send(text, file=_preview(member, color1, color2), ephemeral=True)
        logger.info("닉네임색 설정: %s → %s", member.id, role.name)

    @group.command(name="해제", description="내 닉네임 색을 없앱니다.")
    async def clear(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        done = await clear_color(interaction.guild, interaction.user.id, interaction.user, "닉네임색 해제")
        await interaction.followup.send(
            "✅ 닉네임 색을 없앴습니다." if done else "정해 둔 닉네임 색이 없습니다.", ephemeral=True
        )

    @group.command(name="미리보기", description="색을 정하기 전에 내 닉네임이 어떻게 보일지 봅니다. (누구나)")
    @app_commands.describe(색="색 이름을 고르거나 헥스 코드 입력 (예: #FF8800)",
                           그라데이션색="(선택) 두 번째 색")
    @app_commands.autocomplete(색=_autocomplete, 그라데이션색=_autocomplete)
    async def preview(interaction: discord.Interaction, 색: str, 그라데이션색: Optional[str] = None):
        color1 = parse_color(색)
        color2 = parse_color(그라데이션색) if 그라데이션색 else None
        if color1 is None or (그라데이션색 and color2 is None):
            parsed = await _parse(interaction, 색, 그라데이션색)  # 오류 안내
            if parsed is None:
                return
        note = ""
        if color2 is not None and GRADIENT_FEATURE not in interaction.guild.features:
            note = "\n⚠️ 이 서버는 아직 그라데이션 역할 색이 열려 있지 않아 실제로는 첫 번째 색만 쓸 수 있습니다."
        await interaction.response.send_message(
            f"🎨 미리보기 (위: 어두운 화면, 아래: 밝은 화면){note}",
            file=_preview(interaction.user, color1, color2), ephemeral=True,
        )

    tree.add_command(group)
