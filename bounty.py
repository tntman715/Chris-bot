"""
작품신청 현상금 시스템

- 관리자가 작품신청 포럼 채널을 정하면, 그 안의 포스트(스레드)가 신청 하나입니다. 신청은 무료.
- 신청자(포스트 작성자)는 /현상금 으로 코인을 걸 수 있고, 건 코인은 완료 · 취소 때까지 봇이 맡아 둡니다.
- /작품완료 : 신청자가 처리자 한 명을 고르면, 걸린 코인은 처리자의 기여 점수로 바뀝니다 (기본 10점 + 걸린 코인 × 2).
  처리자에게 코인은 가지 않고, 걸린 코인은 사라집니다 (주인 결정 2026-11-14 — 코인 환전도 없앰).
  처리자는 그 포스트에 사진 · 영상을 올린 사람 중에서만 고를 수 있습니다 (본인 제외).
- /작품완료요청 : 신청자가 완료 처리를 잊었을 때, 작품을 올린 작업자가 신청자를 불러 [완료 처리] 버튼을 띄웁니다.
  버튼은 신청자만 누를 수 있고, 누르면 그 작업자를 처리자로 확정하는 확인 창이 나옵니다 (봇을 다시 켜도 버튼 유지).
- /작품취소 또는 포스트 삭제 시 걸린 코인을 신청자에게 돌려줍니다. 오래된 신청을 자동으로 정리하지는 않습니다.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands

import activity

logger = logging.getLogger("Chris-bot.bounty")

CONTRIBUTION_MULTIPLIER = 2  # 기여 점수 = 걸린 코인 × 2 (주인 결정 2026-11-12)
BASE_CONTRIBUTION = 10  # 작품을 처리하면 현상금과 상관없이 받는 기본 기여 점수 (주인 요청 2026-11-14)


def contribution_points(amount: int) -> int:
    """처리자가 받는 기여 점수: 기본 10점 + 걸린 코인 × 2 (현상금 없는 작업은 10점)."""
    return BASE_CONTRIBUTION + amount * CONTRIBUTION_MULTIPLIER
REQUEST_COOLDOWN_SECONDS = 6 * 60 * 60  # 같은 포스트에서 같은 작업자가 다시 요청하려면 6시간
_last_request: dict[tuple[int, int], float] = {}  # (포스트 ID, 작업자 ID) → 마지막 요청 시각 (time.monotonic)
HISTORY_SCAN_LIMIT = 3000  # 처리자 후보를 찾을 때 포스트에서 훑어볼 최대 메시지 수
CUR = activity.CURRENCY_NAME
EMOJI = activity.CURRENCY_EMOJI


# =====================================================================
# 데이터베이스 (activity 와 같은 data/chris.db)
# =====================================================================
def _conn():
    return activity.get_db().conn


def ensure_tables() -> None:
    _conn().executescript(
        """
        CREATE TABLE IF NOT EXISTS bounty_requests (
            thread_id INTEGER PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            requester_id INTEGER NOT NULL,
            amount INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'open',   -- open / done / cancelled
            solver_id INTEGER,
            created_at TEXT NOT NULL,
            closed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_bounty_guild_status ON bounty_requests (guild_id, status);
        """
    )
    # 기여 점수 기록이 생기기 전에 완료된 신청은 한 번만 옮겨 둠 (기간별 순위용)
    if _conn().execute("SELECT COUNT(*) FROM contribution_log").fetchone()[0] == 0:
        _conn().execute(
            "INSERT INTO contribution_log (guild_id, user_id, points, at) "
            "SELECT guild_id, solver_id, amount * ?, closed_at FROM bounty_requests "
            "WHERE status = 'done' AND solver_id IS NOT NULL AND amount > 0",
            (CONTRIBUTION_MULTIPLIER,),
        )
    _conn().commit()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def get_request(thread_id: int) -> Optional[dict]:
    row = _conn().execute(
        "SELECT thread_id, guild_id, requester_id, amount, status, solver_id FROM bounty_requests WHERE thread_id = ?",
        (thread_id,),
    ).fetchone()
    if not row:
        return None
    keys = ("thread_id", "guild_id", "requester_id", "amount", "status", "solver_id")
    return dict(zip(keys, row))


def get_or_create_request(thread: discord.Thread) -> dict:
    req = get_request(thread.id)
    if req is None:
        _conn().execute(
            "INSERT INTO bounty_requests (thread_id, guild_id, requester_id, created_at) VALUES (?, ?, ?, ?)",
            (thread.id, thread.guild.id, thread.owner_id, _now()),
        )
        _conn().commit()
        req = get_request(thread.id)
    return req


def add_bounty(thread: discord.Thread, requester_id: int, amount: int) -> tuple[bool, int, int]:
    """
    신청자 코인에서 amount 만큼 빼서 현상금에 더합니다.
    반환: (성공 여부, 현상금 총액, 신청자 남은 코인)
    """
    conn = _conn()
    req = get_or_create_request(thread)
    cur = conn.execute(
        "UPDATE members SET coins = coins - ? WHERE guild_id = ? AND user_id = ? AND coins >= ?",
        (amount, thread.guild.id, requester_id, amount),
    )
    if cur.rowcount != 1:
        conn.rollback()
        balance = activity.get_db().get_member(thread.guild.id, requester_id)["coins"]
        return False, req["amount"], balance
    # 취소했던 신청에 다시 걸면 다시 열림
    conn.execute(
        "UPDATE bounty_requests SET amount = amount + ?, status = 'open', solver_id = NULL, closed_at = NULL "
        "WHERE thread_id = ?",
        (amount, thread.id),
    )
    conn.commit()
    total = get_request(thread.id)["amount"]
    balance = activity.get_db().get_member(thread.guild.id, requester_id)["coins"]
    return True, total, balance


def complete_request(thread_id: int, guild_id: int, solver_id: int) -> Optional[tuple[int, int]]:
    """처리 완료: (기여 점수로 바뀐 코인, 처리자가 받은 기여 점수). 이미 끝난 신청이면 None.
    걸린 코인은 처리자에게 가지 않고 기여 점수로 바뀝니다."""
    conn = _conn()
    req = get_request(thread_id)
    if req is None or req["status"] != "open":
        return None
    amount = req["amount"]
    points = contribution_points(amount)
    conn.execute(
        "UPDATE bounty_requests SET status = 'done', solver_id = ?, closed_at = ? WHERE thread_id = ? AND status = 'open'",
        (solver_id, _now(), thread_id),
    )
    conn.execute(
        "INSERT INTO members (guild_id, user_id, contribution) VALUES (?, ?, ?) "
        "ON CONFLICT(guild_id, user_id) DO UPDATE SET contribution = contribution + excluded.contribution",
        (guild_id, solver_id, points),
    )
    if points:
        conn.execute(
            "INSERT INTO contribution_log (guild_id, user_id, points, at) VALUES (?, ?, ?, ?)",
            (guild_id, solver_id, points, _now()),
        )
    conn.commit()
    return amount, points


def refund_request(thread_id: int) -> Optional[tuple[int, int]]:
    """취소 · 환불: (신청자 ID, 돌려준 코인). 열린 신청이 아니면 None."""
    conn = _conn()
    req = get_request(thread_id)
    if req is None or req["status"] != "open":
        return None
    # 돌려준 만큼 현상금을 0으로 (다시 걸면 새로 쌓이도록 — 이중 환불 · 이중 지급 방지)
    conn.execute(
        "UPDATE bounty_requests SET status = 'cancelled', amount = 0, closed_at = ? WHERE thread_id = ? AND status = 'open'",
        (_now(), thread_id),
    )
    if req["amount"]:
        conn.execute(
            "INSERT INTO members (guild_id, user_id, coins) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET coins = coins + excluded.coins",
            (req["guild_id"], req["requester_id"], req["amount"]),
        )
    conn.commit()
    return req["requester_id"], req["amount"]


def open_bounties(guild_id: int) -> list[tuple[int, int, int]]:
    return _conn().execute(
        "SELECT thread_id, requester_id, amount FROM bounty_requests "
        "WHERE guild_id = ? AND status = 'open' AND amount > 0 ORDER BY amount DESC",
        (guild_id,),
    ).fetchall()


def solved_count(guild_id: int, user_id: int) -> int:
    return _conn().execute(
        "SELECT COUNT(*) FROM bounty_requests WHERE guild_id = ? AND solver_id = ? AND status = 'done'",
        (guild_id, user_id),
    ).fetchone()[0]


# =====================================================================
# 도우미
# =====================================================================
def _request_channels(guild_id: int) -> list[int]:
    return activity.get_db().get_settings(guild_id).get("request_channels", [])


def _request_thread(channel) -> Optional[discord.Thread]:
    """이 채널이 작품신청 포럼 안의 포스트면 그 스레드를 돌려줍니다."""
    if isinstance(channel, discord.Thread) and channel.parent_id in _request_channels(channel.guild.id):
        return channel
    return None


def _message_has_media(msg: discord.Message) -> bool:
    if any(activity._is_media(a) for a in msg.attachments):
        return True
    return any(e.type in ("image", "video", "gifv") for e in msg.embeds)


async def media_posters(thread: discord.Thread, exclude_ids: set[int]) -> list[discord.Member]:
    """포스트에 사진 · 영상을 올린 사람들 (최근에 올린 순, 최대 25명)"""
    last_seen: dict[int, datetime] = {}
    async for msg in thread.history(limit=HISTORY_SCAN_LIMIT):
        if msg.author.bot or msg.author.id in exclude_ids or msg.author.id in last_seen:
            continue
        if _message_has_media(msg):
            last_seen[msg.author.id] = msg.created_at
    members = []
    for uid in sorted(last_seen, key=last_seen.get, reverse=True):
        m = thread.guild.get_member(uid)
        if m is None:
            try:
                m = await thread.guild.fetch_member(uid)
            except discord.HTTPException:
                continue  # 서버를 나간 사람
        members.append(m)
        if len(members) >= 25:
            break
    return members


async def has_posted_media(thread: discord.Thread, user_id: int) -> bool:
    """이 사람이 포스트에 사진 · 영상을 올렸는지 (처리자 후보 조건과 같음)"""
    async for msg in thread.history(limit=HISTORY_SCAN_LIMIT):
        if msg.author.id == user_id and _message_has_media(msg):
            return True
    return False


def request_wait_seconds(thread_id: int, user_id: int, now: Optional[float] = None) -> int:
    """완료 요청을 다시 보내려면 남은 시간(초). 0이면 지금 보낼 수 있음."""
    last = _last_request.get((thread_id, user_id))
    if last is None:
        return 0
    left = REQUEST_COOLDOWN_SECONDS - ((now if now is not None else time.monotonic()) - last)
    return max(0, int(left))


def reward_text(amount: int) -> str:
    if amount:
        return f"현상금 **{amount} {CUR}**이 기여 점수로 바뀌어 기여 점수 **{contribution_points(amount)}점**을 받습니다."
    return f"걸린 {CUR}이 없어 기본 기여 점수 **{contribution_points(0)}점**을 받습니다."


async def _announce_completion(thread: discord.Thread, solver: discord.abc.User, coins: int, points: int, by_admin: bool):
    if coins:
        text = (
            f"✅ **작품 처리 완료!** 현상금 **{coins} {CUR}** {EMOJI}이 기여 점수로 바뀌어, "
            f"처리자 {solver.mention}님이 기여 점수 **{points}점**을 받았습니다."
        )
    else:
        text = f"✅ **작품 처리 완료!** 처리자 {solver.mention}님이 기여 점수 **{points}점**을 받았습니다. *(걸린 {CUR} 없음)*"
    if by_admin:
        text += " *(관리자 처리)*"
    try:
        await thread.send(text, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as e:
        logger.warning(f"완료 안내 전송 실패: {e}")


# =====================================================================
# 완료 선택 UI
# =====================================================================
class ConfirmSolverView(discord.ui.View):
    def __init__(self, thread: discord.Thread, solver: discord.Member):
        super().__init__(timeout=120)
        self.thread = thread
        self.solver = solver

    @discord.ui.button(label="확인", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        result = complete_request(self.thread.id, self.thread.guild.id, self.solver.id)
        if result is None:
            await interaction.response.edit_message(content="ℹ️ 이미 완료되었거나 취소된 신청입니다.", view=None)
            return
        coins, points = result
        await interaction.response.edit_message(
            content=f"✅ {self.solver.mention}님을 처리자로 확정했습니다.", view=None
        )
        await _announce_completion(self.thread, self.solver, coins, points, by_admin=False)

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="완료 처리를 취소했습니다.", view=None)


class SolverSelect(discord.ui.Select):
    def __init__(self, thread: discord.Thread, candidates: list[discord.Member], amount: int):
        self.thread = thread
        self.candidates = {m.id: m for m in candidates}
        self.amount = amount
        options = [
            discord.SelectOption(label=m.display_name[:100], value=str(m.id), description=f"@{m.name}"[:100])
            for m in candidates
        ]
        super().__init__(placeholder="작품을 준 사람(처리자)을 고르세요", options=options)

    async def callback(self, interaction: discord.Interaction):
        solver = self.candidates[int(self.values[0])]
        reward = reward_text(self.amount)
        await interaction.response.edit_message(
            content=f"**{solver.mention}님**을 처리자로 확정할까요? {reward}\n*(확정하면 되돌릴 수 없습니다)*",
            view=ConfirmSolverView(self.thread, solver),
        )


class CompletionRequestButton(discord.ui.DynamicItem[discord.ui.Button], template=r"bounty:done-request:(?P<solver>[0-9]+)"):
    """/작품완료요청 으로 올라가는 [완료 처리] 버튼. 신청자만 누를 수 있음.
    custom_id에 작업자 ID를 담아 두어, 봇을 다시 켜도 버튼이 계속 동작함 (register()에서 add_dynamic_items로 등록)."""

    def __init__(self, solver_id: int):
        super().__init__(discord.ui.Button(
            label="완료 처리", style=discord.ButtonStyle.success, emoji="✅",
            custom_id=f"bounty:done-request:{solver_id}",
        ))
        self.solver_id = solver_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match):
        return cls(int(match["solver"]))

    async def callback(self, interaction: discord.Interaction):
        thread = _request_thread(interaction.channel)
        if thread is None:
            await interaction.response.send_message("ℹ️ 더 이상 작품신청 포스트가 아닙니다.", ephemeral=True)
            return
        if interaction.user.id != thread.owner_id:
            await interaction.response.send_message(
                "❌ 이 버튼은 **신청자 본인**만 누를 수 있습니다.", ephemeral=True
            )
            return
        req = get_or_create_request(thread)
        if req["status"] != "open":
            await interaction.response.send_message(
                "ℹ️ 이미 처리가 끝났거나 취소된 신청입니다.", ephemeral=True
            )
            return
        solver = thread.guild.get_member(self.solver_id)
        if solver is None:
            try:
                solver = await thread.guild.fetch_member(self.solver_id)
            except discord.HTTPException:
                await interaction.response.send_message(
                    "❌ 요청한 작업자가 서버에 없습니다. 다른 사람이 처리했다면 `/작품완료`를 사용해 주세요.", ephemeral=True
                )
                return
        await interaction.response.send_message(
            f"**{solver.mention}님**을 처리자로 확정할까요? {reward_text(req['amount'])}\n"
            "*(확정하면 되돌릴 수 없습니다. 다른 사람이 작품을 줬다면 `취소`를 누르고 `/작품완료`로 골라 주세요)*",
            view=ConfirmSolverView(thread, solver),
            ephemeral=True,
        )


class CompletionRequestView(discord.ui.View):
    def __init__(self, solver_id: int):
        super().__init__(timeout=None)
        self.add_item(CompletionRequestButton(solver_id))


class SolverSelectView(discord.ui.View):
    def __init__(self, thread: discord.Thread, candidates: list[discord.Member], amount: int):
        super().__init__(timeout=180)
        self.add_item(SolverSelect(thread, candidates, amount))


# =====================================================================
# 이벤트
# =====================================================================
async def handle_thread_delete(payload: discord.RawThreadDeleteEvent) -> None:
    """포스트가 지워지면 걸린 코인을 신청자에게 돌려줍니다."""
    result = refund_request(payload.thread_id)
    if result and result[1]:
        logger.info(f"포스트 삭제로 현상금 환불: 신청자 {result[0]}, {result[1]} {CUR}")


# =====================================================================
# 명령어
# =====================================================================
def register(tree: app_commands.CommandTree) -> None:
    ensure_tables()
    tree.client.add_dynamic_items(CompletionRequestButton)  # 봇을 다시 켜도 [완료 처리] 버튼 동작

    async def _require_request_thread(interaction: discord.Interaction) -> Optional[discord.Thread]:
        thread = _request_thread(interaction.channel)
        if thread is None:
            await interaction.response.send_message(
                "❌ 이 명령어는 **작품신청 포럼의 포스트 안에서**만 사용할 수 있습니다.", ephemeral=True
            )
        return thread

    @tree.command(name="현상금", description=f"내 작품신청 포스트에 {CUR}을 겁니다. 처리해 준 사람의 기여 점수로 바뀝니다.")
    @app_commands.guild_only()
    @app_commands.describe(코인=f"걸 {CUR} 수 (내 {CUR}에서 바로 빠지고, 취소하면 돌려받습니다)")
    async def put_bounty(interaction: discord.Interaction, 코인: app_commands.Range[int, 1, 1_000_000]):
        thread = await _require_request_thread(interaction)
        if thread is None:
            return
        if thread.owner_id != interaction.user.id:
            await interaction.response.send_message("❌ **본인이 올린 신청 포스트**에만 걸 수 있습니다.", ephemeral=True)
            return
        req = get_or_create_request(thread)
        if req["status"] == "done":
            await interaction.response.send_message("❌ 이미 처리가 끝난 신청입니다.", ephemeral=True)
            return
        ok, total, balance = add_bounty(thread, interaction.user.id, 코인)
        if not ok:
            await interaction.response.send_message(
                f"❌ {CUR}이 부족합니다. *(보유: {balance} {EMOJI}, 걸려는 양: {코인})*", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"💰 {interaction.user.mention}님이 현상금 **{코인} {CUR}**을 걸었습니다! "
            f"*(현재 총 현상금: **{total} {CUR}** {EMOJI})*",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @tree.command(name="작품완료요청", description="[작업자용] 작품을 줬는데 신청자가 완료 처리를 안 했으면, 신청자에게 완료를 요청합니다.")
    @app_commands.guild_only()
    async def request_completion(interaction: discord.Interaction):
        thread = await _require_request_thread(interaction)
        if thread is None:
            return
        if thread.owner_id == interaction.user.id:
            await interaction.response.send_message(
                "ℹ️ 신청자 본인은 `/작품완료`로 바로 완료할 수 있습니다.", ephemeral=True
            )
            return
        req = get_or_create_request(thread)
        if req["status"] != "open":
            await interaction.response.send_message("ℹ️ 이미 처리가 끝났거나 취소된 신청입니다.", ephemeral=True)
            return
        wait = request_wait_seconds(thread.id, interaction.user.id)
        if wait:
            await interaction.response.send_message(
                f"⏳ 이 포스트에는 이미 완료 요청을 보냈습니다. **{wait // 3600}시간 {wait % 3600 // 60}분** 뒤에 다시 보낼 수 있어요.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not await has_posted_media(thread, interaction.user.id):
            await interaction.followup.send(
                "❌ 이 포스트에 **내가 올린 사진 · 영상**이 없습니다. 완성작을 먼저 올린 뒤 요청해 주세요.", ephemeral=True
            )
            return
        _last_request[(thread.id, interaction.user.id)] = time.monotonic()
        try:
            await thread.send(
                f"📮 <@{thread.owner_id}>님, {interaction.user.mention}님이 작품을 완성했다고 **완료 처리**를 요청했습니다.\n"
                f"받은 작품이 마음에 드시면 아래 **[완료 처리]** 버튼을 눌러 주세요. "
                f"완료하면 작업자가 기여 점수 **{contribution_points(req['amount'])}점**을 받습니다"
                + (f" *(걸린 현상금 {req['amount']} {CUR} 포함)*" if req['amount'] else "") + ".\n"
                "*(다른 사람이 작품을 줬다면 `/작품완료`로 직접 골라 주세요)*",
                view=CompletionRequestView(interaction.user.id),
                allowed_mentions=discord.AllowedMentions(users=[discord.Object(thread.owner_id)]),
            )
        except discord.HTTPException as e:
            _last_request.pop((thread.id, interaction.user.id), None)
            await interaction.followup.send(f"❌ 요청 메시지를 올리지 못했습니다: {e}", ephemeral=True)
            return
        await interaction.followup.send("✅ 신청자에게 완료 처리를 요청했습니다.", ephemeral=True)

    @tree.command(name="작품완료", description="작품을 받았으면 처리자를 골라 완료합니다. (포스트에 사진·영상을 올린 사람 중에서)")
    @app_commands.guild_only()
    async def complete(interaction: discord.Interaction):
        thread = await _require_request_thread(interaction)
        if thread is None:
            return
        if thread.owner_id != interaction.user.id:
            await interaction.response.send_message("❌ **신청자 본인**만 완료할 수 있습니다.", ephemeral=True)
            return
        req = get_or_create_request(thread)
        if req["status"] == "done":
            await interaction.response.send_message("ℹ️ 이미 처리가 끝난 신청입니다.", ephemeral=True)
            return
        if req["status"] == "cancelled":
            await interaction.response.send_message("ℹ️ 취소된 신청입니다.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        candidates = await media_posters(thread, exclude_ids={interaction.user.id})
        if not candidates:
            await interaction.followup.send(
                "❌ 이 포스트에 **사진·영상을 올린 사람**이 없습니다. 처리자는 작품(사진·영상)을 올린 사람 중에서만 고를 수 있습니다.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"작품을 준 사람(처리자)을 골라 주세요. *(현재 현상금: {req['amount']} {CUR})*",
            view=SolverSelectView(thread, candidates, req["amount"]),
            ephemeral=True,
        )

    @tree.command(name="작품취소", description=f"내 작품신청을 취소하고 걸었던 {CUR}을 돌려받습니다.")
    @app_commands.guild_only()
    async def cancel(interaction: discord.Interaction):
        thread = await _require_request_thread(interaction)
        if thread is None:
            return
        if thread.owner_id != interaction.user.id:
            await interaction.response.send_message("❌ **신청자 본인**만 취소할 수 있습니다.", ephemeral=True)
            return
        req = get_request(thread.id)
        if req is None or req["status"] != "open":
            await interaction.response.send_message("ℹ️ 진행 중인 현상금이 없습니다.", ephemeral=True)
            return
        _, refunded = refund_request(thread.id)
        await interaction.response.send_message(
            f"↩️ {interaction.user.mention}님이 신청을 취소했습니다. 걸었던 **{refunded} {CUR}**을 돌려받았습니다.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    admin = app_commands.Group(
        name="작품관리",
        description="[관리자용] 작품신청 채널과 현상금을 관리합니다.",
        default_permissions=discord.Permissions(manage_guild=True),
        guild_only=True,
    )

    @admin.command(name="신청채널추가", description="작품신청 포럼 채널을 등록합니다. 그 안의 포스트에서 현상금을 걸 수 있습니다.")
    async def add_channel(interaction: discord.Interaction, 채널: discord.ForumChannel):
        database = activity.get_db()
        s = database.get_settings(interaction.guild_id)
        chans = s.setdefault("request_channels", [])
        if 채널.id not in chans:
            chans.append(채널.id)
            database.save_settings(interaction.guild_id, s)
        await interaction.response.send_message(
            f"✅ {채널.mention}을 작품신청 채널로 등록했습니다. 이제 그 안의 포스트에서 `/현상금`, `/작품완료`, `/작품취소`를 쓸 수 있습니다.",
            ephemeral=True,
        )

    @admin.command(name="신청채널제거", description="작품신청 채널 목록에서 뺍니다. (이미 걸린 현상금은 그대로 남습니다)")
    async def remove_channel(interaction: discord.Interaction, 채널: discord.ForumChannel):
        database = activity.get_db()
        s = database.get_settings(interaction.guild_id)
        chans = s.setdefault("request_channels", [])
        if 채널.id in chans:
            chans.remove(채널.id)
            database.save_settings(interaction.guild_id, s)
        await interaction.response.send_message(f"🗑️ {채널.mention}을 작품신청 채널에서 뺐습니다.", ephemeral=True)

    @admin.command(name="강제완료", description="[포스트 안에서] 관리자가 처리자를 직접 지정해 완료합니다. (사진 조건 없이)")
    async def force_complete(interaction: discord.Interaction, 처리자: discord.Member):
        thread = await _require_request_thread(interaction)
        if thread is None:
            return
        if 처리자.bot or 처리자.id == thread.owner_id:
            await interaction.response.send_message("❌ 봇이나 신청자 본인은 처리자가 될 수 없습니다.", ephemeral=True)
            return
        get_or_create_request(thread)
        result = complete_request(thread.id, thread.guild.id, 처리자.id)
        if result is None:
            await interaction.response.send_message("ℹ️ 이미 완료되었거나 취소된 신청입니다.", ephemeral=True)
            return
        await interaction.response.send_message(f"✅ {처리자.mention}님을 처리자로 완료 처리했습니다.", ephemeral=True)
        await _announce_completion(thread, 처리자, *result, by_admin=True)

    @admin.command(name="강제환불", description="[포스트 안에서] 걸린 현상금을 신청자에게 돌려주고 신청을 취소합니다.")
    async def force_refund(interaction: discord.Interaction):
        thread = await _require_request_thread(interaction)
        if thread is None:
            return
        result = refund_request(thread.id)
        if result is None:
            await interaction.response.send_message("ℹ️ 진행 중인 현상금이 없습니다.", ephemeral=True)
            return
        requester_id, refunded = result
        await interaction.response.send_message(
            f"↩️ 관리자가 신청을 취소했습니다. 신청자 <@{requester_id}>님에게 **{refunded} {CUR}**을 돌려주었습니다.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @admin.command(name="현상금목록", description="현상금이 걸려 있는 진행 중인 신청 목록을 봅니다.")
    async def list_bounties(interaction: discord.Interaction):
        rows = open_bounties(interaction.guild_id)
        if not rows:
            await interaction.response.send_message("📋 현상금이 걸린 진행 중인 신청이 없습니다.", ephemeral=True)
            return
        lines = [f"- <#{tid}> — **{amt} {CUR}** (신청자 <@{rid}>)" for tid, rid, amt in rows]
        text = f"### 💰 진행 중인 현상금 ({len(rows)}건, 총 {sum(r[2] for r in rows)} {CUR})\n" + "\n".join(lines)
        chunks = activity._split_lines(text)
        await interaction.response.send_message(chunks[0], ephemeral=True)
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk, ephemeral=True)

    tree.add_command(admin)
