import json
import logging
import os
import re
from pathlib import Path
from typing import Optional, Union

import discord
from discord import app_commands
from dotenv import load_dotenv

# .env 파일에서 환경변수 로드
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("Chris-bot")

DATA_DIR = Path(__file__).parent / "data"
CONFIG_FILE = DATA_DIR / "config.json"

GRADE_INPUT_PATTERN = re.compile(r"^([A-Za-z0-9가-힣]+?)(\++|-+)?$")


# =====================================================================
# 설정 파일 입출력 헬퍼 함수
# =====================================================================
def load_config() -> dict:
    """서버별 설정을 불러옵니다."""
    if not CONFIG_FILE.exists():
        return {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"설정 파일 로드 실패: {e}")
        return {}


def save_config(config: dict) -> None:
    """서버별 설정을 파일에 저장합니다."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def get_guild_data(config: dict, guild_id: int) -> dict:
    guild_key = str(guild_id)
    if guild_key not in config:
        config[guild_key] = {}
    g = config[guild_key]
    if "allowed_post_delete_channels" not in g:
        g["allowed_post_delete_channels"] = []
    if "grade_system" not in g:
        g["grade_system"] = {
            "grades": {},  # code -> {"code": str, "value": float, "role_id": int, "role_name": str}
            "evaluator_roles": {},  # role_id_str -> {"role_id": int, "max_grade": Optional[str], "allow_plus": bool}
            "season": {
                "active": False,
                "channel_id": None,
                "evaluations": {},  # target_id -> {"scores": {eval_id: {"grade": str, "value": float}}, "override_grade": Optional[str]}
            },
        }
    gs = g["grade_system"]
    # 이전 버전(evaluator_role_ids 리스트) 호환 변환
    if "evaluator_roles" not in gs:
        old_ids = gs.pop("evaluator_role_ids", [])
        gs["evaluator_roles"] = {
            str(rid): {"role_id": int(rid), "max_grade": None, "allow_plus": True}
            for rid in old_ids
        }
    return g


def get_allowed_channels(guild_id: int) -> list[int]:
    config = load_config()
    g = get_guild_data(config, guild_id)
    return g["allowed_post_delete_channels"]


def set_allowed_channels(guild_id: int, channel_ids: list[int]) -> None:
    config = load_config()
    g = get_guild_data(config, guild_id)
    g["allowed_post_delete_channels"] = sorted(list(set(channel_ids)))
    save_config(config)


def format_score(val: float) -> str:
    """점수를 보기 좋게 포맷팅합니다 (예: 2.0 -> 2, 2.25 -> 2.25, 2.375 -> 2.375)."""
    if float(val).is_integer():
        return str(int(val))
    return f"{val:.6f}".rstrip("0").rstrip(".")


# =====================================================================
# 등급 계산 및 점근적(+ / -) 점수 헬퍼 함수
# =====================================================================
def parse_grade_expression(expr: str, grades: dict) -> Optional[dict]:
    """
    사용자가 입력한 등급 문자열(예: 'C', 'C+', 'C++', 'B-', 'B--')을 파싱하여
    점근적 점수를 계산해 반환합니다.

    점근 공식 (간격 Δ 기준, 기본 Δ=1):
      + 가 k개일 때: base + 0.5 * Δ * (1 - 1 / 2^k)
        k=1 (+):  +0.25
        k=2 (++): +0.375
        k=3 (+++): +0.4375 ...
      - 가 k개일 때: base - 0.5 * Δ * (1 - 1 / 2^k)
        k=1 (-):  -0.25
        k=2 (--): -0.375 (예: 3 - 0.375 = 2.625)
        k=3 (---): -0.4375 ...

    반환값:
      {
        "display_code": "C++",     # 정규화된 표시 기호
        "base_code": "C",          # 기본 등급 기호
        "base_value": 2.0,         # 기본 등급 점수
        "value": 2.375,            # 최종 계산된 점수
        "modifier": "++",          # 수식어 ('', '+', '--' 등)
        "role_id": ...,            # 기본 등급의 역할 ID
        "role_name": ...,          # 기본 등급의 역할 이름
      }
    """
    cleaned = expr.strip()
    m = GRADE_INPUT_PATTERN.match(cleaned)
    if not m:
        return None

    raw_base, modifier = m.group(1), m.group(2) or ""
    matched_code = None
    for k in grades.keys():
        if k.upper() == raw_base.upper():
            matched_code = k
            break

    if matched_code is None:
        return None

    base_info = grades[matched_code]
    base_val = float(base_info["value"])

    # 인접 등급과의 간격 계산 (기본값 1.0)
    sorted_vals = sorted(set(float(g["value"]) for g in grades.values()))
    idx = sorted_vals.index(base_val)

    if not modifier:
        final_val = base_val
    elif modifier.startswith("+"):
        k = len(modifier)
        gap = (sorted_vals[idx + 1] - base_val) if (idx + 1 < len(sorted_vals)) else 1.0
        if gap <= 0:
            gap = 1.0
        offset = 0.5 * gap * (1.0 - (1.0 / (2**k)))
        final_val = base_val + offset
    else:  # modifier.startswith("-")
        k = len(modifier)
        gap = (base_val - sorted_vals[idx - 1]) if (idx - 1 >= 0) else 1.0
        if gap <= 0:
            gap = 1.0
        offset = 0.5 * gap * (1.0 - (1.0 / (2**k)))
        final_val = base_val - offset

    return {
        "display_code": f"{matched_code}{modifier}",
        "base_code": matched_code,
        "base_value": base_val,
        "value": final_val,
        "modifier": modifier,
        "role_id": base_info["role_id"],
        "role_name": base_info["role_name"],
    }


def get_member_voting_limit(
    member: discord.Member, gs: dict
) -> tuple[bool, Optional[float], Optional[str]]:
    """
    멤버가 투표 가능한지 여부와 최대 허용 점수(제한 없으면 None), 최대 등급 안내 문자열을 반환합니다.
    여러 심사위원 역할을 동시에 가지고 있으면 가장 높은 권한을 적용합니다.
    """
    if member.guild_permissions.manage_guild:
        return True, None, "제한 없음 (관리자)"

    evaluator_roles = gs.get("evaluator_roles", {})
    grades = gs.get("grades", {})

    matched_configs = []
    for role in member.roles:
        r_conf = evaluator_roles.get(str(role.id))
        if r_conf is not None:
            matched_configs.append(r_conf)

    if not matched_configs:
        return False, None, None

    # 하나라도 무제한(max_grade is None) 역할이 있으면 무제한
    for r_conf in matched_configs:
        if not r_conf.get("max_grade"):
            return True, None, "모든 등급"

    max_allowed_score = -1e18
    best_label = None

    # 정렬된 기본 등급 점수 목록 (다음 등급과의 경계 계산용)
    sorted_vals = sorted(set(float(g["value"]) for g in grades.values()))

    for r_conf in matched_configs:
        max_expr = r_conf.get("max_grade")
        allow_plus = r_conf.get("allow_plus", True)
        parsed = parse_grade_expression(max_expr, grades) if max_expr else None
        if parsed is None:
            # 등록된 등급이 삭제되었거나 찾을 수 없으면 무제한으로 폴백
            return True, None, "모든 등급"

        if not parsed["modifier"] and allow_plus:
            # 예: 최대등급이 'B'이고 플러스 허용이면 B+, B++ 등 B 계열 전체 허용 (B와 다음 등급의 중간값 미만까지)
            base_val = parsed["base_value"]
            idx = sorted_vals.index(base_val)
            gap = (
                (sorted_vals[idx + 1] - base_val)
                if (idx + 1 < len(sorted_vals))
                else 1.0
            )
            limit_score = base_val + 0.5 * gap - 1e-9
            label = f"{parsed['base_code']} 계열 ({parsed['base_code']}+ 포함)"
        else:
            # 예: 플러스 비허용이거나 직접 'B+' 등 세부 등급으로 상한을 지정한 경우
            limit_score = parsed["value"]
            label = f"{parsed['display_code']} ({format_score(limit_score)}점)"

        if limit_score > max_allowed_score:
            max_allowed_score = limit_score
            best_label = label

    return True, max_allowed_score, best_label


def find_closest_grade(avg_value: float, grades: dict) -> Optional[dict]:
    """
    산술평균 점수(avg_value)에서 가장 가까운 기본 등급을 반환합니다.
    거리가 같을 경우(예: 2점과 3점 사이 2.5점) 반올림(사사오입) 원칙에 따라 더 높은 점수의 등급을 선택합니다.
    """
    if not grades:
        return None
    sorted_grades = sorted(
        grades.values(),
        key=lambda g: (abs(float(g["value"]) - avg_value), -float(g["value"])),
    )
    return sorted_grades[0]


def calculate_target_result(
    target_data: dict, grades: dict
) -> tuple[Optional[float], Optional[dict], bool]:
    """
    대상 유저의 (산술평균 점수, 최종 결정된 등급 정보 dict, 관리자 수동수정 여부)를 반환합니다.
    """
    scores = target_data.get("scores", {})
    override_code = target_data.get("override_grade")

    avg_val = None
    if scores:
        values = [float(s["value"]) for s in scores.values()]
        avg_val = sum(values) / len(values)

    if override_code and override_code in grades:
        return avg_val, grades[override_code], True

    if avg_val is not None:
        closest = find_closest_grade(avg_val, grades)
        return avg_val, closest, False

    return None, None, False


# =====================================================================
# 1. 포스트 삭제 기능 UI
# =====================================================================
class DeleteConfirmView(discord.ui.View):
    """포스트 삭제 전 실수 방지를 위한 확인 버튼 뷰"""

    def __init__(self, requester_id: int):
        super().__init__(timeout=60.0)
        self.requester_id = requester_id

    @discord.ui.button(label="삭제 확인", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm_delete(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "❌ 본인만 누를 수 있는 버튼입니다.", ephemeral=True
            )
            return

        channel = interaction.channel
        if not isinstance(channel, discord.Thread):
            await interaction.response.edit_message(
                content="❌ 포스트(스레드) 정보를 찾을 수 없습니다.", view=None
            )
            return

        try:
            logger.info(
                f"포스트 삭제 실행: 서버={interaction.guild_id}, "
                f"채널={channel.id}({channel.name}), 작성자={interaction.user}"
            )
            await channel.delete(
                reason=f"포스트 작성자({interaction.user})의 /포스트삭제 요청"
            )
        except discord.Forbidden:
            await interaction.response.edit_message(
                content=(
                    "❌ 봇에게 **스레드 관리(Manage Threads)** 권한이 없어 포스트를 삭제하지 못했습니다.\n"
                    "서버 관리자에게 봇 권한 확인을 요청해 주세요."
                ),
                view=None,
            )
        except discord.HTTPException as e:
            await interaction.response.edit_message(
                content=f"❌ 포스트 삭제 중 오류가 발생했습니다: {e}",
                view=None,
            )

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel_delete(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if interaction.user.id != self.requester_id:
            return
        await interaction.response.edit_message(
            content="✅ 포스트 삭제를 취소했습니다.", view=None
        )


# =====================================================================
# 2. 등급 취합 최종 승인 UI
# =====================================================================
class ApproveSeasonView(discord.ui.View):
    """등급 시즌 최종 승인 확인 뷰"""

    def __init__(self, requester_id: int):
        super().__init__(timeout=120.0)
        self.requester_id = requester_id

    @discord.ui.button(
        label="최종 승인 및 역할 반영", style=discord.ButtonStyle.success, emoji="✅"
    )
    async def confirm_approve(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "❌ 명령어를 실행한 관리자만 누를 수 있습니다.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if guild is None:
            return

        config = load_config()
        g = get_guild_data(config, guild.id)
        gs = g["grade_system"]
        grades = gs["grades"]
        season = gs["season"]
        evaluations = season.get("evaluations", {})

        if not evaluations:
            await interaction.edit_original_response(
                content="❌ 취합할 평가 데이터가 없습니다.", view=None
            )
            return

        grade_role_map = {}
        for g_info in grades.values():
            rid = g_info.get("role_id")
            if rid:
                grade_role_map[int(rid)] = g_info

        announcement_lines_mention = []
        announcement_lines_username = []
        role_errors = []
        processed_count = 0

        for target_id_str, target_data in evaluations.items():
            avg_val, final_grade, _ = calculate_target_result(target_data, grades)
            if final_grade is None:
                continue

            target_id = int(target_id_str)
            member = guild.get_member(target_id)
            if member is None:
                try:
                    member = await guild.fetch_member(target_id)
                except discord.HTTPException:
                    member = None

            new_role_id = int(final_grade["role_id"])
            new_role = guild.get_role(new_role_id)
            new_role_name = new_role.name if new_role else final_grade["role_name"]

            if member is not None:
                existing_grade_roles = [
                    r for r in member.roles if r.id in grade_role_map
                ]
                existing_grade_roles.sort(
                    key=lambda r: float(grade_role_map[r.id]["value"]), reverse=True
                )

                old_role_name = (
                    existing_grade_roles[0].name if existing_grade_roles else None
                )

                roles_to_remove = [
                    r for r in existing_grade_roles if r.id != new_role_id
                ]
                try:
                    if roles_to_remove:
                        await member.remove_roles(
                            *roles_to_remove,
                            reason="등급 조정 시즌 최종 승인 (기존 등급 제거)",
                        )
                    if new_role and new_role not in member.roles:
                        await member.add_roles(
                            new_role, reason="등급 조정 시즌 최종 승인 (새 등급 부여)"
                        )
                except discord.Forbidden:
                    role_errors.append(
                        f"- {member.mention}: 봇에게 `역할 관리` 권한이 없거나 봇의 역할 순서가 등급 역할보다 낮아 변경 실패"
                    )
                except discord.HTTPException as e:
                    role_errors.append(f"- {member.mention}: 역할 변경 중 오류 ({e})")

                user_tag = f"@{member.name}"
                user_mention = f"<@{member.id}>"
            else:
                old_role_name = None
                user_tag = f"@user_{target_id}"
                user_mention = f"<@{target_id}>"

            if old_role_name:
                line_mention = f"{user_mention} {old_role_name} -> {new_role_name}"
                line_user = f"{user_tag} {old_role_name} -> {new_role_name}"
            else:
                line_mention = f"{user_mention} +{new_role_name}"
                line_user = f"{user_tag} +{new_role_name}"

            announcement_lines_mention.append(line_mention)
            announcement_lines_username.append(line_user)
            processed_count += 1

        season["active"] = False
        save_config(config)

        mention_block = "\n\n".join(announcement_lines_mention)
        username_block = "\n\n".join(announcement_lines_username)

        msg_parts = [
            f"✅ **총 {processed_count}명의 등급 반영 및 시즌 종료가 완료되었습니다!**\n",
            "📢 **[공지용 문구 1 — 복사해서 붙여넣으면 실제 멘션(@태그)이 적용됩니다]**",
            f"```text\n{mention_block}\n```",
            "📋 **[공지용 문구 2 — 아이디 텍스트 버전]**",
            f"```text\n{username_block}\n```",
        ]

        if role_errors:
            msg_parts.append(
                "\n⚠️ **일부 유저의 디스코드 역할 자동 변경에 실패했습니다:**\n"
                + "\n".join(role_errors)
                + "\n*(서버 설정 → 역할에서 `Chris Bot` 역할을 등급 역할들보다 위로 올리고 `역할 관리` 권한을 켜주세요.)*"
            )

        await interaction.edit_original_response(
            content="\n".join(msg_parts), view=None
        )

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel_approve(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if interaction.user.id != self.requester_id:
            return
        await interaction.response.edit_message(
            content="✅ 최종 승인을 취소했습니다. (아직 수정하거나 평가를 더 받을 수 있습니다.)",
            view=None,
        )


# =====================================================================
# 봇 클라이언트 정의
# =====================================================================
class ChrisBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        logger.info("슬래시 명령어 동기화 완료")

    async def on_ready(self):
        logger.info(f"로그인 성공: {self.user} (ID: {self.user.id})")


bot = ChrisBot()


# =====================================================================
# 명령어 1: /포스트삭제 및 /포스트삭제채널
# =====================================================================
@bot.tree.command(
    name="포스트삭제",
    description="내가 작성한 포스트(또는 스레드)를 통째로 삭제합니다. (허용된 채널에서만 가능)",
)
@app_commands.guild_only()
async def delete_my_post(interaction: discord.Interaction):
    channel = interaction.channel

    if not isinstance(channel, discord.Thread):
        await interaction.response.send_message(
            "❌ 이 명령어는 **포럼 포스트** 또는 **스레드** 안에서만 사용할 수 있습니다.",
            ephemeral=True,
        )
        return

    allowed_channels = get_allowed_channels(interaction.guild_id)
    if channel.parent_id not in allowed_channels:
        await interaction.response.send_message(
            "❌ 이 채널에서는 포스트 직접 삭제 기능이 허용되어 있지 않습니다.",
            ephemeral=True,
        )
        return

    owner_id = channel.owner_id
    if owner_id is None and interaction.guild is not None:
        try:
            fetched = await interaction.guild.fetch_channel(channel.id)
            if isinstance(fetched, discord.Thread):
                owner_id = fetched.owner_id
        except discord.HTTPException:
            pass

    if owner_id != interaction.user.id:
        await interaction.response.send_message(
            "❌ **본인이 작성한 포스트**만 삭제할 수 있습니다.",
            ephemeral=True,
        )
        return

    view = DeleteConfirmView(requester_id=interaction.user.id)
    await interaction.response.send_message(
        f"⚠️ **정말 `{channel.name}` 포스트를 삭제하시겠습니까?**\n"
        "포스트 안의 모든 댓글이 함께 삭제되며, 삭제 후에는 되돌릴 수 없습니다.",
        view=view,
        ephemeral=True,
    )


channel_group = app_commands.Group(
    name="포스트삭제채널",
    description="[관리자용] 작성자가 자신의 포스트를 삭제할 수 있는 채널을 관리합니다.",
    default_permissions=discord.Permissions(manage_channels=True),
    guild_only=True,
)


@channel_group.command(
    name="추가",
    description="작성자가 포스트를 스스로 삭제할 수 있도록 허용할 채널(포럼/일반채널)을 추가합니다.",
)
@app_commands.describe(채널="허용할 포럼 채널 또는 텍스트 채널")
async def add_allowed_channel(
    interaction: discord.Interaction,
    채널: Union[discord.ForumChannel, discord.TextChannel],
):
    allowed = get_allowed_channels(interaction.guild_id)
    if 채널.id in allowed:
        await interaction.response.send_message(
            f"ℹ️ {채널.mention} 채널은 이미 허용 목록에 등록되어 있습니다.",
            ephemeral=True,
        )
        return

    allowed.append(채널.id)
    set_allowed_channels(interaction.guild_id, allowed)
    await interaction.response.send_message(
        f"✅ 이제 {채널.mention} 채널에서 만든 포스트(스레드)는 작성자가 `/포스트삭제` 명령어로 직접 지울 수 있습니다.\n"
        "*(주의: 봇 역할에 `스레드 관리(Manage Threads)` 권한이 있어야 작동합니다.)*",
        ephemeral=True,
    )


@channel_group.command(
    name="제거",
    description="포스트 삭제 허용 목록에서 채널을 제외합니다.",
)
@app_commands.describe(채널="제외할 포럼 채널 또는 텍스트 채널")
async def remove_allowed_channel(
    interaction: discord.Interaction,
    채널: Union[discord.ForumChannel, discord.TextChannel],
):
    allowed = get_allowed_channels(interaction.guild_id)
    if 채널.id not in allowed:
        await interaction.response.send_message(
            f"ℹ️ {채널.mention} 채널은 허용 목록에 없습니다.",
            ephemeral=True,
        )
        return

    allowed.remove(채널.id)
    set_allowed_channels(interaction.guild_id, allowed)
    await interaction.response.send_message(
        f"✅ {채널.mention} 채널을 포스트 삭제 허용 목록에서 제외했습니다.",
        ephemeral=True,
    )


@channel_group.command(
    name="목록",
    description="현재 포스트 직접 삭제가 허용된 채널 목록을 확인합니다.",
)
async def list_allowed_channels(interaction: discord.Interaction):
    allowed = get_allowed_channels(interaction.guild_id)
    if not allowed:
        await interaction.response.send_message(
            "📋 현재 등록된 허용 채널이 없습니다. `/포스트삭제채널 추가` 명령어로 채널을 등록해 주세요.",
            ephemeral=True,
        )
        return

    lines = [f"- <#{cid}> (`{cid}`)" for cid in allowed]
    await interaction.response.send_message(
        "📋 **포스트 작성자 직접 삭제가 허용된 채널 목록:**\n" + "\n".join(lines),
        ephemeral=True,
    )


bot.tree.add_command(channel_group)


# =====================================================================
# 명령어 2: /등급평가 (심사위원용 평가 제출 명령어, + / - 점근 점수 지원)
# =====================================================================
async def base_grade_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """관리자 명령어용 기본 등급 자동완성"""
    if interaction.guild_id is None:
        return []
    config = load_config()
    g = get_guild_data(config, interaction.guild_id)
    grades = g["grade_system"]["grades"]

    sorted_grades = sorted(grades.values(), key=lambda x: float(x["value"]))
    choices = []
    for info in sorted_grades:
        code = info["code"]
        rname = info["role_name"]
        val_str = format_score(float(info["value"]))
        label = f"{code} ({rname} / 기본점수: {val_str})"
        if not current or current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label[:100], value=code))
    return choices[:25]


async def eval_grade_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """
    심사위원용 /등급평가 자동완성:
    - 심사위원 본인의 투표 가능 상한선 이하의 등급만 표시합니다.
    - 'B', 'B-', 'C++' 등을 입력하면 점근적으로 계산된 점수를 실시간으로 미리 보여줍니다.
    """
    if interaction.guild_id is None:
        return []
    config = load_config()
    g = get_guild_data(config, interaction.guild_id)
    gs = g["grade_system"]
    grades = gs["grades"]
    if not grades:
        return []

    max_score = None
    if isinstance(interaction.user, discord.Member):
        _, max_score, _ = get_member_voting_limit(interaction.user, gs)

    sorted_grades = sorted(grades.values(), key=lambda x: float(x["value"]))
    candidate_exprs = []

    typed = current.strip()
    parsed_exact = parse_grade_expression(typed, grades) if typed else None
    if parsed_exact is not None:
        base_c = parsed_exact["base_code"]
        # 사용자가 특정 등급을 입력 중이면 해당 등급과 +, ++, +++, -, --, --- 변형을 모두 제안
        variants = [
            typed.upper(),
            base_c,
            f"{base_c}+",
            f"{base_c}++",
            f"{base_c}+++",
            f"{base_c}-",
            f"{base_c}--",
            f"{base_c}---",
        ]
        for v in variants:
            if v not in candidate_exprs:
                candidate_exprs.append(v)

    # 기본 등급 및 자주 쓰는 +/- 변형 추가
    for info in sorted_grades:
        c = info["code"]
        for v in (f"{c}-", c, f"{c}+"):
            if v not in candidate_exprs:
                candidate_exprs.append(v)

    choices = []
    for expr in candidate_exprs:
        p = parse_grade_expression(expr, grades)
        if p is None:
            continue
        if max_score is not None and p["value"] > max_score + 1e-9:
            continue
        val_str = format_score(p["value"])
        label = f"{p['display_code']} ({p['role_name']} 계열 / {val_str}점)"
        if not typed or typed.lower() in label.lower() or p["base_code"].lower() in typed.lower():
            choices.append(
                app_commands.Choice(name=label[:100], value=p["display_code"])
            )

    return choices[:25]


@bot.tree.command(
    name="등급평가",
    description="등급 조정 시즌 동안 특정 유저의 등급을 평가합니다. (예: C, C+, C++, B-, B-- 입력 가능)",
)
@app_commands.guild_only()
@app_commands.describe(
    대상="등급을 평가할 대상 유저",
    등급="부여할 등급 (예: D, C+, C++, B-, B-- 등 + / - 세부 등급 지원)",
)
@app_commands.autocomplete(등급=eval_grade_autocomplete)
async def submit_grade_evaluation(
    interaction: discord.Interaction,
    대상: discord.Member,
    등급: str,
):
    config = load_config()
    g = get_guild_data(config, interaction.guild_id)
    gs = g["grade_system"]
    season = gs["season"]
    grades = gs["grades"]

    # 1. 시즌 활성화 여부 확인
    if not season.get("active"):
        await interaction.response.send_message(
            "❌ 현재 **등급 조정 시즌**이 활성화되어 있지 않습니다.",
            ephemeral=True,
        )
        return

    # 2. 지정된 채널인지 확인 (지정 채널 안의 스레드도 허용)
    designated_channel_id = season.get("channel_id")
    current_channel = interaction.channel
    parent_id = getattr(current_channel, "parent_id", None)
    if (
        current_channel.id != designated_channel_id
        and parent_id != designated_channel_id
    ):
        await interaction.response.send_message(
            f"❌ 등급 평가는 지정된 채널(<#{designated_channel_id}>)에서만 제출할 수 있습니다.",
            ephemeral=True,
        )
        return

    # 3. 심사위원 자격 및 최대 투표 가능 등급 확인
    user = interaction.user
    if not isinstance(user, discord.Member):
        return

    can_vote, max_allowed_score, limit_label = get_member_voting_limit(user, gs)
    if not can_vote:
        await interaction.response.send_message(
            "❌ 지정된 **심사위원 역할**을 가진 분만 등급 평가를 제출할 수 있습니다.",
            ephemeral=True,
        )
        return

    # 4. 등급 문자열 파싱 (C+, B-- 등 점근 점수 계산)
    parsed = parse_grade_expression(등급, grades)
    if parsed is None:
        available = ", ".join(grades.keys()) if grades else "없음"
        await interaction.response.send_message(
            f"❌ `{등급}`은(는) 올바른 등급 형식이 아닙니다.\n"
            f"- 등록된 기본 등급: `{available}`\n"
            f"- 세부 등급 예시: `C+`, `C++`, `B-`, `B--`",
            ephemeral=True,
        )
        return

    # 5. 역할별 최대 투표 가능 점수 초과 여부 검사
    if max_allowed_score is not None and parsed["value"] > max_allowed_score + 1e-9:
        await interaction.response.send_message(
            f"❌ 회원님의 심사위원 역할로는 최대 **{limit_label}**까지만 투표할 수 있습니다.\n"
            f"*(입력하신 등급: `{parsed['display_code']}` = {format_score(parsed['value'])}점)*",
            ephemeral=True,
        )
        return

    evaluations = season.setdefault("evaluations", {})
    target_key = str(대상.id)
    target_entry = evaluations.setdefault(
        target_key, {"scores": {}, "override_grade": None}
    )

    eval_key = str(user.id)
    is_update = eval_key in target_entry["scores"]
    prev_grade = target_entry["scores"][eval_key]["grade"] if is_update else None
    prev_val = (
        format_score(target_entry["scores"][eval_key]["value"]) if is_update else None
    )

    target_entry["scores"][eval_key] = {
        "grade": parsed["display_code"],
        "value": float(parsed["value"]),
        "evaluator_name": user.display_name,
    }
    save_config(config)

    total_evaluators = len(target_entry["scores"])
    val_str = format_score(parsed["value"])

    if is_update:
        msg = (
            f"✏️ {대상.mention}님에 대한 등급 평가를 수정했습니다:\n"
            f"`{prev_grade}` ({prev_val}점) ➡️ **`{parsed['display_code']}` ({parsed['role_name']} 계열, {val_str}점)**\n"
            f"*(현재 이 유저를 평가한 인원: {total_evaluators}명)*"
        )
    else:
        msg = (
            f"✅ {대상.mention}님에 대한 등급 평가를 제출했습니다:\n"
            f"**`{parsed['display_code']}` ({parsed['role_name']} 계열, {val_str}점)**\n"
            f"*(현재 이 유저를 평가한 인원: {total_evaluators}명)*"
        )

    await interaction.response.send_message(msg, ephemeral=True)


# =====================================================================
# 명령어 3: /등급관리 (관리자용 설정 · 취합 · 수정 · 최종승인 그룹)
# =====================================================================
grade_admin_group = app_commands.Group(
    name="등급관리",
    description="[관리자용] 등급 등록, 심사위원 자격 세분화, 시즌 시작/종료, 취합 수정 및 최종 승인을 관리합니다.",
    default_permissions=discord.Permissions(manage_guild=True),
    guild_only=True,
)


@grade_admin_group.command(
    name="등급등록",
    description="기본 등급 기호와 점수, 연결할 디스코드 역할을 등록합니다. (예: 기호=C, 점수=2, 역할=@[C] Good Creator)",
)
@app_commands.describe(
    등급기호="기본 등급 기호 (예: N, D, C, B, A, S)",
    점수="산술평균 계산에 사용할 기본 숫자 점수 (예: 1, 2, 3...)",
    역할="해당 등급에 부여할 디스코드 역할(Role)",
)
async def register_grade(
    interaction: discord.Interaction,
    등급기호: str,
    점수: float,
    역할: discord.Role,
):
    code = 등급기호.strip().upper()
    if "+" in code or "-" in code:
        await interaction.response.send_message(
            "❌ 기본 등급 기호에는 `+`나 `-`를 포함할 수 없습니다. 기본 등급(예: `C`, `B`)만 등록하시면 `C+`, `C++`, `B-`, `B--` 등은 자동으로 계산됩니다!",
            ephemeral=True,
        )
        return

    config = load_config()
    g = get_guild_data(config, interaction.guild_id)
    grades = g["grade_system"]["grades"]

    grades[code] = {
        "code": code,
        "value": float(점수),
        "role_id": 역할.id,
        "role_name": 역할.name,
    }
    save_config(config)

    val_str = format_score(점수)
    await interaction.response.send_message(
        f"✅ 기본 등급이 등록되었습니다: **`{code}` = {val_str}점** (연결된 역할: {역할.mention} / 표시명: `{역할.name}`)\n"
        f"*(이제 평가 시 `{code}+`, `{code}++`, `{code}-`, `{code}--` 등의 점근 점수도 자동으로 사용할 수 있습니다.)*",
        ephemeral=True,
    )


@grade_admin_group.command(
    name="등급삭제",
    description="등록되어 있는 기본 등급 기호를 삭제합니다.",
)
@app_commands.describe(등급기호="삭제할 기본 등급 기호 (예: D)")
@app_commands.autocomplete(등급기호=base_grade_autocomplete)
async def remove_grade(interaction: discord.Interaction, 등급기호: str):
    code = 등급기호.strip().upper()
    config = load_config()
    g = get_guild_data(config, interaction.guild_id)
    grades = g["grade_system"]["grades"]

    if code not in grades:
        await interaction.response.send_message(
            f"❌ `{code}` 등급은 등록되어 있지 않습니다.", ephemeral=True
        )
        return

    removed = grades.pop(code)
    save_config(config)
    await interaction.response.send_message(
        f"🗑️ `{code}` ({removed['role_name']}) 등급을 목록에서 삭제했습니다.",
        ephemeral=True,
    )


@grade_admin_group.command(
    name="심사역할지정",
    description="심사위원 역할을 등록하고, 그 역할이 투표할 수 있는 최대 등급 상한선을 설정합니다.",
)
@app_commands.describe(
    역할="심사위원으로 지정할 디스코드 역할",
    최대등급="이 역할이 투표할 수 있는 최대 등급 (예: B 입력 시 B까지만 투표 가능 / 비워두면 모든 등급 가능)",
    플러스허용="최대등급이 B일 때 B+, B++ 까지 허용할지 여부 (기본값: 허용)",
)
@app_commands.autocomplete(최대등급=base_grade_autocomplete)
@app_commands.choices(
    플러스허용=[
        app_commands.Choice(name="허용 (예: 최대 B 지정 시 B+, B++ 까지 투표 가능)", value=1),
        app_commands.Choice(name="비허용 (예: 최대 B 지정 시 정확히 B 이하만 투표 가능)", value=0),
    ]
)
async def set_evaluator_role(
    interaction: discord.Interaction,
    역할: discord.Role,
    최대등급: Optional[str] = None,
    플러스허용: int = 1,
):
    config = load_config()
    g = get_guild_data(config, interaction.guild_id)
    gs = g["grade_system"]
    grades = gs["grades"]
    eval_roles = gs.setdefault("evaluator_roles", {})

    normalized_max = None
    if 최대등급 and 최대등급.strip():
        parsed = parse_grade_expression(최대등급.strip(), grades)
        if parsed is None:
            await interaction.response.send_message(
                f"❌ `{최대등급}`은(는) 등록되지 않은 등급입니다. 먼저 `/등급관리 등급등록`으로 등급을 등록해 주세요.",
                ephemeral=True,
            )
            return
        normalized_max = parsed["display_code"]

    eval_roles[str(역할.id)] = {
        "role_id": 역할.id,
        "max_grade": normalized_max,
        "allow_plus": bool(플러스허용 == 1),
    }
    save_config(config)

    if normalized_max:
        plus_desc = (
            f"`{normalized_max}+`, `{normalized_max}++` 포함"
            if (플러스허용 == 1 and "+" not in normalized_max and "-" not in normalized_max)
            else f"`{normalized_max}` 초과 불가"
        )
        limit_msg = f"최대 **`{normalized_max}`** 등급까지만 투표 가능 ({plus_desc})"
    else:
        limit_msg = "**모든 등급** 제한 없이 투표 가능"

    await interaction.response.send_message(
        f"✅ {역할.mention} 역할을 심사위원으로 설정했습니다!\n"
        f"- **투표 권한 범위**: {limit_msg}",
        ephemeral=True,
    )


@grade_admin_group.command(
    name="심사역할해제",
    description="등록된 심사위원 역할을 심사위원 목록에서 제외합니다.",
)
@app_commands.describe(역할="심사위원에서 해제할 디스코드 역할")
async def remove_evaluator_role(
    interaction: discord.Interaction, 역할: discord.Role
):
    config = load_config()
    g = get_guild_data(config, interaction.guild_id)
    eval_roles = g["grade_system"].setdefault("evaluator_roles", {})

    if str(역할.id) not in eval_roles:
        await interaction.response.send_message(
            f"ℹ️ {역할.mention} 역할은 심사위원 목록에 등록되어 있지 않습니다.",
            ephemeral=True,
        )
        return

    eval_roles.pop(str(역할.id), None)
    save_config(config)
    await interaction.response.send_message(
        f"🗑️ {역할.mention} 역할을 심사위원 목록에서 해제했습니다.",
        ephemeral=True,
    )


@grade_admin_group.command(
    name="설정목록",
    description="현재 등록된 등급 목록, 심사위원 역할별 투표 상한선, 시즌 활성화 상태를 확인합니다.",
)
async def list_grade_settings(interaction: discord.Interaction):
    config = load_config()
    g = get_guild_data(config, interaction.guild_id)
    gs = g["grade_system"]
    grades = gs["grades"]
    eval_roles = gs.get("evaluator_roles", {})
    season = gs["season"]

    sorted_grades = sorted(grades.values(), key=lambda x: float(x["value"]))
    if sorted_grades:
        grade_lines = []
        for info in sorted_grades:
            c = info["code"]
            v_str = format_score(float(info["value"]))
            p_plus = parse_grade_expression(f"{c}+", grades)
            p_minus = parse_grade_expression(f"{c}-", grades)
            plus_str = format_score(p_plus["value"]) if p_plus else ""
            minus_str = format_score(p_minus["value"]) if p_minus else ""
            grade_lines.append(
                f"- **`{c}`** = `{v_str}점` (`{c}-`: {minus_str} / `{c}+`: {plus_str}) ➡️ <@&{info['role_id']}> (`{info['role_name']}`)"
            )
        grade_text = "\n".join(grade_lines)
    else:
        grade_text = "- *(등록된 등급이 없습니다. `/등급관리 등급등록`으로 추가해 주세요.)*"

    if eval_roles:
        eval_lines = []
        for rid_str, r_conf in eval_roles.items():
            m_grade = r_conf.get("max_grade")
            allow_p = r_conf.get("allow_plus", True)
            if m_grade:
                p_note = f" (`{m_grade}+` 포함)" if allow_p and "+" not in m_grade and "-" not in m_grade else ""
                eval_lines.append(
                    f"- <@&{rid_str}>: 최대 **`{m_grade}`** 까지 투표 가능{p_note}"
                )
            else:
                eval_lines.append(f"- <@&{rid_str}>: **모든 등급** 투표 가능")
        eval_text = "\n".join(eval_lines)
    else:
        eval_text = "- *(지정된 심사위원 역할 없음 — 현재 서버 관리자만 평가 가능)*"

    if season.get("active"):
        season_text = f"🟢 **진행 중** (지정 채널: <#{season.get('channel_id')}>)"
    else:
        season_text = "⚪ **비활성화됨**"

    msg = (
        f"### ⚙️ 등급 취합 시스템 설정 현황\n"
        f"**1. 시즌 상태**: {season_text}\n\n"
        f"**2. 심사위원 역할 및 투표 상한선**:\n{eval_text}\n\n"
        f"**3. 등록된 기본 등급 목록 (낮은 점수순)**:\n{grade_text}"
    )
    await interaction.response.send_message(msg, ephemeral=True)


@grade_admin_group.command(
    name="시즌시작",
    description="지정한 채널에서 등급 조정 시즌을 활성화하여 /등급평가 제출을 받습니다.",
)
@app_commands.describe(
    채널="등급 평가를 제출받을 채널",
    기록초기화="새 시즌을 위해 이전 평가 기록을 모두 비울지 여부 (기본값: 예)",
)
@app_commands.choices(
    기록초기화=[
        app_commands.Choice(name="예 (새로운 시즌 시작 - 이전 평가 기록 비우기)", value=1),
        app_commands.Choice(name="아니오 (기존 평가 기록 유지하고 이어서 받기)", value=0),
    ]
)
async def start_grade_season(
    interaction: discord.Interaction,
    채널: discord.TextChannel,
    기록초기화: int = 1,
):
    config = load_config()
    g = get_guild_data(config, interaction.guild_id)
    gs = g["grade_system"]

    if not gs["grades"]:
        await interaction.response.send_message(
            "⚠️ 아직 등록된 등급이 하나도 없습니다! 먼저 `/등급관리 등급등록`으로 등급(예: D=2, C=3)을 등록해 주세요.",
            ephemeral=True,
        )
        return

    season = gs["season"]
    season["active"] = True
    season["channel_id"] = 채널.id
    if 기록초기화 == 1:
        season["evaluations"] = {}

    save_config(config)

    reset_msg = (
        "이전 평가 기록을 초기화하고 새로 시작합니다."
        if 기록초기화 == 1
        else "기존 평가 기록을 유지한 채 이어서 진행합니다."
    )
    await interaction.response.send_message(
        f"🟢 **등급 조정 시즌이 활성화되었습니다!**\n"
        f"- **평가 제출 채널**: {채널.mention}\n"
        f"- **안내**: 이제 심사위원들이 {채널.mention} 채널에서 `/등급평가 [대상] [등급]` (예: `C`, `C+`, `B-`, `B--` 등) 명령어로 평가를 제출할 수 있습니다.\n"
        f"- *({reset_msg})*",
        ephemeral=True,
    )


@grade_admin_group.command(
    name="시즌종료",
    description="등급 평가 제출을 마감합니다. (취합된 데이터는 그대로 유지되어 검토/수정/최종승인 가능)",
)
async def stop_grade_season(interaction: discord.Interaction):
    config = load_config()
    g = get_guild_data(config, interaction.guild_id)
    season = g["grade_system"]["season"]
    season["active"] = False
    save_config(config)

    await interaction.response.send_message(
        "🛑 **등급 조정 시즌 평가 제출을 마감했습니다.**\n"
        "이제 `/등급관리 취합현황`으로 결과를 확인하고, 필요 시 `/등급관리 평가삭제` 또는 `/등급관리 등급수정` 후 `/등급관리 최종승인`을 진행해 주세요.",
        ephemeral=True,
    )


@grade_admin_group.command(
    name="취합현황",
    description="현재까지 제출된 등급 평가의 산술평균 취합 결과와 예상 공지 문구를 확인합니다.",
)
@app_commands.describe(대상="특정 유저의 상세 평가 내역만 보려면 선택하세요 (비워두면 전체 조회)")
async def view_aggregation_status(
    interaction: discord.Interaction,
    대상: Optional[discord.Member] = None,
):
    config = load_config()
    g = get_guild_data(config, interaction.guild_id)
    gs = g["grade_system"]
    grades = gs["grades"]
    season = gs["season"]
    evaluations = season.get("evaluations", {})

    if not evaluations:
        await interaction.response.send_message(
            "📋 아직 제출된 등급 평가가 없습니다.", ephemeral=True
        )
        return

    guild = interaction.guild
    grade_role_map = {
        int(info["role_id"]): info for info in grades.values() if info.get("role_id")
    }

    if 대상 is not None:
        t_data = evaluations.get(str(대상.id))
        if not t_data or (
            not t_data.get("scores") and not t_data.get("override_grade")
        ):
            await interaction.response.send_message(
                f"ℹ️ {대상.mention}님에 대해 제출된 평가가 없습니다.", ephemeral=True
            )
            return

        avg_val, final_grade, is_overridden = calculate_target_result(t_data, grades)
        scores = t_data.get("scores", {})
        score_lines = []
        for eval_id, s_info in scores.items():
            v_str = format_score(float(s_info["value"]))
            score_lines.append(
                f"- <@{eval_id}>: **`{s_info['grade']}`** ({v_str}점)"
            )

        avg_str = f"{avg_val:.3f}점" if avg_val is not None else "평가 없음"
        final_str = (
            f"**`{final_grade['code']}` ({final_grade['role_name']})**"
            if final_grade
            else "미정"
        )
        if is_overridden:
            final_str += " *(관리자 수동 지정됨)*"

        msg = (
            f"### 🔍 {대상.mention}님의 등급 평가 상세 내역\n"
            f"- **참여 심사위원 ({len(scores)}명)**:\n"
            + ("\n".join(score_lines) if score_lines else "  *(제출된 평가 없음)*")
            + f"\n- **산술평균 점수**: `{avg_str}`\n"
            f"- **취합 결정 등급**: {final_str}"
        )
        await interaction.response.send_message(msg, ephemeral=True)
        return

    summary_lines = []
    preview_lines = []

    for target_id_str, t_data in evaluations.items():
        avg_val, final_grade, is_overridden = calculate_target_result(t_data, grades)
        if final_grade is None:
            continue

        target_id = int(target_id_str)
        member = guild.get_member(target_id) if guild else None
        scores = t_data.get("scores", {})

        avg_str = f"{avg_val:.3f}".rstrip("0").rstrip(".") if avg_val is not None else "-"
        override_badge = " 🔧*(수동수정)*" if is_overridden else ""
        indiv = ", ".join(
            f"<@{eid}>:{s['grade']}({format_score(float(s['value']))})"
            for eid, s in scores.items()
        )
        summary_lines.append(
            f"- <@{target_id}>: 평균 **`{avg_str}점`** ({len(scores)}명: {indiv}) ➡️ **`{final_grade['code']}`** ({final_grade['role_name']}){override_badge}"
        )

        old_role_name = None
        if member:
            existing = [r for r in member.roles if r.id in grade_role_map]
            existing.sort(
                key=lambda r: float(grade_role_map[r.id]["value"]), reverse=True
            )
            if existing:
                old_role_name = existing[0].name
            user_label = f"@{member.name}"
        else:
            user_label = f"@user_{target_id}"

        if old_role_name:
            preview_lines.append(
                f"{user_label} {old_role_name} -> {final_grade['role_name']}"
            )
        else:
            preview_lines.append(f"{user_label} +{final_grade['role_name']}")

    msg = (
        f"### 📊 현재 등급 취합 현황 (총 {len(summary_lines)}명)\n"
        + "\n".join(summary_lines)
        + "\n\n**📢 [공지 문구 미리보기]**\n```text\n"
        + "\n\n".join(preview_lines)
        + "\n```"
    )
    await interaction.response.send_message(msg, ephemeral=True)


@grade_admin_group.command(
    name="평가삭제",
    description="변수 상황 발생 시 특정 심사위원의 평가를 지우거나, 특정 대상의 평가 전체를 삭제합니다.",
)
@app_commands.describe(
    대상="평가를 삭제할 대상 유저",
    심사위원="특정 심사위원이 남긴 평가만 지우려면 선택하세요 (비워두면 해당 대상의 모든 평가 삭제)",
)
async def delete_evaluation(
    interaction: discord.Interaction,
    대상: discord.Member,
    심사위원: Optional[discord.Member] = None,
):
    config = load_config()
    g = get_guild_data(config, interaction.guild_id)
    evaluations = g["grade_system"]["season"].get("evaluations", {})
    target_key = str(대상.id)

    if target_key not in evaluations:
        await interaction.response.send_message(
            f"ℹ️ {대상.mention}님에 대한 평가 기록이 없습니다.", ephemeral=True
        )
        return

    if 심사위원 is not None:
        eval_key = str(심사위원.id)
        scores = evaluations[target_key].get("scores", {})
        if eval_key not in scores:
            await interaction.response.send_message(
                f"ℹ️ {심사위원.mention}님이 {대상.mention}님에게 남긴 평가가 없습니다.",
                ephemeral=True,
            )
            return

        removed = scores.pop(eval_key)
        if not scores and not evaluations[target_key].get("override_grade"):
            evaluations.pop(target_key, None)
        save_config(config)
        await interaction.response.send_message(
            f"🗑️ {심사위원.mention}님이 {대상.mention}님에게 부여했던 **`{removed['grade']}`** 평가를 삭제했습니다.",
            ephemeral=True,
        )
    else:
        evaluations.pop(target_key, None)
        save_config(config)
        await interaction.response.send_message(
            f"🗑️ {대상.mention}님에 대한 이번 시즌 모든 평가 및 수동 수정 기록을 삭제했습니다.",
            ephemeral=True,
        )


async def override_grade_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    choices = await base_grade_autocomplete(interaction, current)
    if not current or "초기화" in current or "reset" in current.lower():
        choices.append(
            app_commands.Choice(
                name="🔄 초기화 (수동 수정을 해제하고 다시 산술평균으로 계산)",
                value="RESET",
            )
        )
    return choices[:25]


@grade_admin_group.command(
    name="등급수정",
    description="산술평균과 무관하게 특정 유저의 최종 취합 등급을 관리자가 직접 지정(또는 초기화)합니다.",
)
@app_commands.describe(
    대상="최종 등급을 수정할 대상 유저",
    변경할등급="지정할 기본 등급 기호 (수동 지정을 해제하려면 'RESET' 또는 '초기화' 선택)",
)
@app_commands.autocomplete(변경할등급=override_grade_autocomplete)
async def override_target_grade(
    interaction: discord.Interaction,
    대상: discord.Member,
    변경할등급: str,
):
    config = load_config()
    g = get_guild_data(config, interaction.guild_id)
    gs = g["grade_system"]
    grades = gs["grades"]
    evaluations = gs["season"].setdefault("evaluations", {})
    target_key = str(대상.id)

    code_input = 변경할등급.strip().upper()
    if code_input in ("RESET", "초기화"):
        if target_key in evaluations:
            evaluations[target_key]["override_grade"] = None
            if not evaluations[target_key].get("scores"):
                evaluations.pop(target_key, None)
            save_config(config)
        await interaction.response.send_message(
            f"🔄 {대상.mention}님의 수동 등급 지정을 해제했습니다. 이제 다시 산술평균으로 계산됩니다.",
            ephemeral=True,
        )
        return

    matched_code = None
    for k in grades.keys():
        if k.upper() == code_input:
            matched_code = k
            break

    if matched_code is None:
        await interaction.response.send_message(
            f"❌ `{변경할등급}`은(는) 등록되지 않은 기본 등급입니다.", ephemeral=True
        )
        return

    target_entry = evaluations.setdefault(
        target_key, {"scores": {}, "override_grade": None}
    )
    target_entry["override_grade"] = matched_code
    save_config(config)

    g_info = grades[matched_code]
    await interaction.response.send_message(
        f"🔧 {대상.mention}님의 최종 취합 등급을 **`{matched_code}` ({g_info['role_name']})**(으)로 수동 지정했습니다.",
        ephemeral=True,
    )


@grade_admin_group.command(
    name="최종승인",
    description="취합된 등급을 최종 승인하여 유저들의 역할을 일괄 변경하고 공지용 문구를 생성합니다.",
)
async def approve_season(interaction: discord.Interaction):
    config = load_config()
    g = get_guild_data(config, interaction.guild_id)
    gs = g["grade_system"]
    grades = gs["grades"]
    evaluations = gs["season"].get("evaluations", {})

    if not evaluations:
        await interaction.response.send_message(
            "❌ 승인할 평가 데이터가 없습니다.", ephemeral=True
        )
        return

    valid_count = 0
    for t_data in evaluations.values():
        _, final_grade, _ = calculate_target_result(t_data, grades)
        if final_grade is not None:
            valid_count += 1

    if valid_count == 0:
        await interaction.response.send_message(
            "❌ 유효한 등급 결과가 있는 대상자가 없습니다.", ephemeral=True
        )
        return

    view = ApproveSeasonView(requester_id=interaction.user.id)
    await interaction.response.send_message(
        f"⚠️ **총 {valid_count}명의 등급 결과를 최종 승인하시겠습니까?**\n"
        "- `[✅ 최종 승인 및 역할 반영]` 버튼을 누르면 대상 유저들의 기존 등급 역할이 새 등급 역할로 즉시 변경되며, 공지용 문구가 출력됩니다.\n"
        "- *(역할 자동 변경이 정상 작동하려면 서버 설정에서 `Chris Bot`에 **역할 관리** 권한이 있고, `Chris Bot` 역할이 등급 역할들보다 위에 있어야 합니다.)*",
        view=view,
        ephemeral=True,
    )


bot.tree.add_command(grade_admin_group)


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token or token == "여기에_디스코드_봇_토큰을_입력하세요":
        raise RuntimeError(
            ".env 파일에 유효한 DISCORD_TOKEN이 설정되어 있지 않습니다."
        )
    bot.run(token)
