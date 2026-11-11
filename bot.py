import json
import logging
import os
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


def load_config() -> dict:
    """서버별 설정(허용된 채널 목록 등)을 불러옵니다."""
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


def get_allowed_channels(guild_id: int) -> list[int]:
    config = load_config()
    guild_conf = config.get(str(guild_id), {})
    return guild_conf.get("allowed_post_delete_channels", [])


def set_allowed_channels(guild_id: int, channel_ids: list[int]) -> None:
    config = load_config()
    guild_key = str(guild_id)
    if guild_key not in config:
        config[guild_key] = {}
    config[guild_key]["allowed_post_delete_channels"] = sorted(list(set(channel_ids)))
    save_config(config)


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


class ChrisBot(discord.Client):
    def __init__(self):
        # 슬래시 명령어 및 기본 길드 이벤트만 사용하므로 메모리 사용을 최소화합니다.
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        logger.info("슬래시 명령어 동기화 완료")

    async def on_ready(self):
        logger.info(f"로그인 성공: {self.user} (ID: {self.user.id})")


bot = ChrisBot()


@bot.tree.command(
    name="포스트삭제",
    description="내가 작성한 포스트(또는 스레드)를 통째로 삭제합니다. (허용된 채널에서만 가능)",
)
@app_commands.guild_only()
async def delete_my_post(interaction: discord.Interaction):
    channel = interaction.channel

    # 1. 현재 위치가 포럼 포스트 또는 스레드인지 확인
    if not isinstance(channel, discord.Thread):
        await interaction.response.send_message(
            "❌ 이 명령어는 **포럼 포스트** 또는 **스레드** 안에서만 사용할 수 있습니다.",
            ephemeral=True,
        )
        return

    # 2. 상위 채널이 허용된 채널 목록에 있는지 확인
    allowed_channels = get_allowed_channels(interaction.guild_id)
    if channel.parent_id not in allowed_channels:
        await interaction.response.send_message(
            "❌ 이 채널에서는 포스트 직접 삭제 기능이 허용되어 있지 않습니다.",
            ephemeral=True,
        )
        return

    # 3. 포스트 작성자(owner_id) 확인 (캐시에 없으면 API로 조회)
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

    # 4. 실수 방지를 위한 본인 전용 확인 버튼 표시
    view = DeleteConfirmView(requester_id=interaction.user.id)
    await interaction.response.send_message(
        f"⚠️ **정말 `{channel.name}` 포스트를 삭제하시겠습니까?**\n"
        "포스트 안의 모든 댓글이 함께 삭제되며, 삭제 후에는 되돌릴 수 없습니다.",
        view=view,
        ephemeral=True,
    )


# --- 관리자 전용 설정 명령어 그룹 ---
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


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token or token == "여기에_디스코드_봇_토큰을_입력하세요":
        raise RuntimeError(
            ".env 파일에 유효한 DISCORD_TOKEN이 설정되어 있지 않습니다."
        )
    bot.run(token)
