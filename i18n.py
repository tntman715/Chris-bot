"""
명령어 영어 표시 (주인 요청 2026-11-14 — 서버에 외국인 멤버도 있음)

디스코드는 명령어 이름 · 설명 · 옵션을 언어별로 따로 보여 줄 수 있습니다 (localization).
- 디스코드 앱 언어가 **한국어**인 사람: 지금처럼 한국어 (`/랭크`)
- **그 밖의 언어**인 사람: 영어 (`/rank`)
명령어 자체는 하나라서, 어느 쪽으로 실행해도 같은 명령어가 실행됩니다. 코드는 계속 한국어 이름을 씁니다.

봇의 답장 문구는 아직 한국어입니다 (명령어 이름 · 설명 · 옵션 · 선택지만 영어).

새 명령어 · 옵션 · 선택지를 추가하면 아래 표에도 영어를 추가합니다.
빠진 것은 그대로 한국어로 보입니다.
"""

from typing import Optional

import discord
from discord import app_commands
from discord.app_commands import TranslationContextLocation as Loc

# 명령어 · 그룹 · 옵션 이름: 소문자 · 숫자 · 하이픈만, 32자 이하
NAMES = {
    # 레벨 · 활동량
    "활동량": "activity",
    "순위": "top",
    "랭크": "rank",
    "리더보드": "leaderboard",
    "레벨관리": "level-admin",
    "레벨기능": "toggle",
    "알림채널": "announce-channel",
    "제외채널추가": "exclude-channel-add",
    "제외채널제거": "exclude-channel-remove",
    "작업물채널추가": "work-channel-add",
    "작업물채널제거": "work-channel-remove",
    "레벨역할추가": "level-role-add",
    "레벨역할제거": "level-role-remove",
    "역할동기화": "sync-roles",
    "레벨역할자동등록": "level-role-auto",
    "뱃지추가": "badge-add",
    "뱃지제거": "badge-remove",
    "설정목록": "settings",
    "아케인가져오기": "import-arcane",
    "레벨설정": "set-level",
    "연동서버추가": "link-server-add",
    "연동서버제거": "link-server-remove",
    "코인조정": "adjust-coins",
    # 현상금
    "현상금": "bounty",
    "작품완료요청": "request-complete",
    "작품완료": "complete",
    "작품취소": "cancel-request",
    "작품관리": "request-admin",
    "신청채널추가": "request-channel-add",
    "신청채널제거": "request-channel-remove",
    "강제완료": "force-complete",
    "강제환불": "force-refund",
    "현상금목록": "bounties",
    # 스타보드
    "스타순위": "star-top",
    "스타정보": "star-info",
    "스타작품순위": "star-works",
    "스타관리": "star-admin",
    "가져오기": "import",
    "준사람가져오기": "import-givers",
    "칼봇맞추기": "sync-carl",
    "스타보드기능": "board-toggle",
    "자동별": "auto-star",
    "보정": "adjust",
    # 닉네임색
    "닉네임색": "name-color",
    "설정": "set",
    "해제": "clear",
    "미리보기": "preview",
    # 역할 유지
    "역할유지": "keep-roles",
    "기능": "toggle",
    "제외추가": "exclude-add",
    "제외제거": "exclude-remove",
    # 포스트 삭제
    "포스트삭제": "delete-post",
    "포스트삭제채널": "delete-post-channel",
    "추가": "add",
    "제거": "remove",
    "목록": "list",
    # 등급
    "등급평가": "grade-vote",
    "등급박탈": "grade-drop",
    "등급관리": "grade-admin",
    "등급등록": "grade-add",
    "등급심사연결": "grade-judge-link",
    "등급삭제": "grade-delete",
    "아카이브채널": "archive-channel",
    "심사역할지정": "judge-role-set",
    "심사역할해제": "judge-role-remove",
    "시즌시작": "season-start",
    "시즌종료": "season-end",
    "취합현황": "tally",
    "평가삭제": "vote-delete",
    "등급수정": "grade-override",
    "최종승인": "finalize",
    # 옵션 이름
    "유저": "user",
    "기준": "by",
    "페이지": "page",
    "상태": "state",
    "채널": "channel",
    "레벨": "level",
    "역할": "role",
    "추가경험치": "extra-xp",
    "서버": "server",
    "양": "amount",
    "코인": "coins",
    "처리자": "solver",
    "스타보드채널": "board-channel",
    "개수": "count",
    "별수": "stars",
    "색": "color",
    "그라데이션색": "gradient-color",
    "대상": "target",
    "등급": "grade",
    "등급기호": "grade-symbol",
    "점수": "score",
    "동시부여심사역할": "bonus-judge-role",
    "심사역할": "judge-role",
    "최대등급": "max-grade",
    "플러스허용": "allow-plus",
    "기록초기화": "reset-votes",
    "심사위원": "judge",
    "변경할등급": "new-grade",
}

# 같은 한국어라도 자리마다 영어가 다른 것 (명령어 전체 경로 기준)
NAME_OVERRIDES = {
    "스타관리 기준": "threshold",  # 옵션 '기준'(by)과 다름
    "역할유지 설정": "settings",  # 닉네임색 설정(set)과 다름
}

# 설명 · 선택지 (100자 이하)
TEXT = {
    # 레벨 · 활동량
    "레벨, 경험치, 보유 코인, 채팅 활동량을 확인합니다.": "Shows level, XP, coins and chat activity.",
    "확인할 유저 (비워두면 본인)": "User to check (leave empty for yourself)",
    "서버 활동 순위(상위 10명)를 확인합니다.": "Shows the server top 10.",
    "어떤 기준으로 순위를 볼지 선택하세요": "What to rank by",
    "레벨": "Level",
    "보유 코인": "Coins held",
    "기여 점수 (누적)": "Contribution (all time)",
    "기여 점수 (최근 30일)": "Contribution (last 30 days)",
    "기여 점수 (최근 90일)": "Contribution (last 90 days)",
    "최근 7일 채팅 수": "Chats (last 7 days)",
    "최근 30일 채팅 수": "Chats (last 30 days)",
    "레벨 카드(이미지)를 보여줍니다. 서버 등급과 뱃지도 함께 표시됩니다.": "Shows your level card (image) with server grade and badges.",
    "레벨 순위표(이미지)를 보여줍니다. 한 페이지에 10명씩 표시됩니다.": "Shows the level leaderboard (image), 10 per page.",
    "볼 페이지 (1페이지 = 1~10위)": "Page (page 1 = ranks 1-10)",
    "[관리자용] 레벨 · 활동량 · 코인 시스템을 설정합니다.": "[Admin] Configure the level, activity and coin system.",
    "레벨업 알림과 레벨 역할 지급을 켜거나 끕니다. (Arcane에서 옮긴 뒤에 켜세요)": "Turn level-up messages and level roles on or off.",
    "켜기": "On",
    "끄기": "Off",
    "레벨업 알림을 보낼 채널을 정합니다. (비워두면 채팅한 채널에 알림)": "Channel for level-up messages (empty = the channel where they chatted).",
    "활동량(경험치·코인)을 세지 않을 채널을 추가합니다.": "Add a channel where activity (XP and coins) is not counted.",
    "활동량 제외 목록에서 채널을 뺍니다.": "Remove a channel from the activity exclusion list.",
    "작업물 채널을 추가합니다. 여기에 사진·영상을 올리면 경험치 보너스를 줍니다.": "Add a work channel. Posting images or videos there gives bonus XP.",
    "작업물 채널 목록에서 채널을 뺍니다.": "Remove a channel from the work channel list.",
    "특정 레벨에 도달하면 줄 역할을 정합니다.": "Set a role to give when a level is reached.",
    "역할을 줄 레벨 (예: 10)": "Level that gives the role (e.g. 10)",
    "그 레벨에 줄 역할": "Role to give at that level",
    "레벨 기록이 있는 모든 사람의 레벨 역할을 현재 레벨에 맞게 한꺼번에 정리합니다.": "Fix everyone's level role to match their current level.",
    "이름이 '10 Level', 'Lv.10', '레벨 10' 같은 역할을 찾아 레벨 역할로 한 번에 등록합니다.": "Register roles named like '10 Level' or 'Lv.10' as level roles.",
    "레벨 역할 설정을 지웁니다.": "Remove a level role setting.",
    "역할을 뱃지로 등록합니다. 그 역할을 가진 사람의 /랭크 카드에 표시됩니다.": "Register a role as a badge shown on the /rank card.",
    "뱃지로 보여 줄 역할 (역할 아이콘이 있으면 아이콘이, 없으면 역할 색이 표시됨)": "Role to show as a badge (its icon, or its color if no icon)",
    "뱃지로 등록한 역할을 뺍니다.": "Remove a badge role.",
    "레벨 · 활동량 시스템의 현재 설정을 확인합니다.": "Show the current level and activity settings.",
    "Arcane 순위표 1~100위 기록(경험치)을 크리스봇 경험치에 더합니다. (한 번만 실행)": "Add the Arcane top 100 XP snapshot to Chris Bot XP (run once).",
    "유저의 레벨(경험치)을 직접 지정합니다. (Arcane 기록 보정 등)": "Set a user's level (XP) directly.",
    "레벨을 지정할 유저": "User to set the level for",
    "지정할 레벨": "Level to set",
    "그 레벨 안에서 이미 쌓은 경험치 (모르면 비워두세요)": "XP already earned within that level (leave empty if unsure)",
    "다른 서버의 채팅 · 반응을 이 서버의 레벨 · 코인 · 순위에 합산합니다.": "Count another server's chats and reactions toward this server's levels.",
    "연동할 서버 (크리스봇이 들어가 있는 서버 목록에서 고르기)": "Server to link (pick from servers Chris Bot is in)",
    "연동한 서버를 뺍니다. (이미 합산된 기록은 그대로 남습니다)": "Unlink a server (records already counted stay).",
    "뺄 연동 서버": "Linked server to remove",
    "유저의 코인을 더하거나 뺍니다. (빼려면 음수 입력)": "Add or remove a user's coins (negative to remove).",
    "대상 유저": "Target user",
    "더할 양 (빼려면 -10 처럼 음수)": "Amount to add (use a negative number like -10 to remove)",
    # 현상금
    "내 작품신청 포스트에 코인을 겁니다. 처리해 준 사람의 기여 점수로 바뀝니다.": "Put coins on your request post. They become the solver's contribution points.",
    "걸 코인 수 (내 코인에서 바로 빠지고, 취소하면 돌려받습니다)": "Coins to put up (taken now, refunded if you cancel)",
    "[작업자용] 작품을 줬는데 신청자가 완료 처리를 안 했으면, 신청자에게 완료를 요청합니다.": "[Artist] Ask the requester to mark your delivered work as complete.",
    "작품을 받았으면 처리자를 골라 완료합니다. (포스트에 사진·영상을 올린 사람 중에서)": "Got your work? Pick who made it and complete the request.",
    "내 작품신청을 취소하고 걸었던 코인을 돌려받습니다.": "Cancel your request and get your coins back.",
    "[관리자용] 작품신청 채널과 현상금을 관리합니다.": "[Admin] Manage request channels and bounties.",
    "작품신청 포럼 채널을 등록합니다. 그 안의 포스트에서 현상금을 걸 수 있습니다.": "Register a request forum. Bounties can be put on its posts.",
    "작품신청 채널 목록에서 뺍니다. (이미 걸린 현상금은 그대로 남습니다)": "Remove a request channel (existing bounties stay).",
    "[포스트 안에서] 관리자가 처리자를 직접 지정해 완료합니다. (사진 조건 없이)": "[In a post] Complete the request with a solver chosen by an admin.",
    "[포스트 안에서] 걸린 현상금을 신청자에게 돌려주고 신청을 취소합니다.": "[In a post] Refund the bounty and cancel the request.",
    "현상금이 걸려 있는 진행 중인 신청 목록을 봅니다.": "List open requests with bounties.",
    # 스타보드
    "별(⭐)을 많이 받은(또는 준) 사람 순위(이미지)를 봅니다. 한 페이지에 10명씩.": "Star ranking (image) by stars received or given, 10 per page.",
    "받은 별 순위 / 준 별 순위": "Stars received or stars given",
    "받은 별": "Stars received",
    "준 별": "Stars given",
    "받은 별 · 준 별, 별 많이 받은 작품, 나에게 별을 많이 준 사람 등을 봅니다.": "Stars received and given, top works, and who stars you most.",
    "별(⭐)을 많이 받은 작품 순위를 봅니다. 작품 바로가기 링크 포함, 10개씩.": "Most-starred works with links, 10 per page.",
    "[관리자용] 스타보드와 별 기록을 관리합니다.": "[Admin] Manage the starboard and star records.",
    "칼봇 스타보드와 원래 채널을 훑어 지금까지 받은 별을 모읍니다. (몇 분~수십 분)": "Import past stars from the Carl-bot starboard and channels (takes a while).",
    "스타보드 채널 (칼봇이 쓰던 채널 — 크리스봇도 여기에 올립니다)": "Starboard channel (Chris Bot posts here too)",
    "별 받은 메시지마다 누가 별을 줬는지 읽어 옵니다. (/스타정보 · 준 별 순위용, 약 1시간)": "Import who starred each message (about 1 hour).",
    "채널의 최근 칼봇 starboard stats · config 답변을 읽어 별 수 · 기준 · 제외 채널을 맞춥니다.": "Sync star counts and settings from recent Carl-bot starboard replies.",
    "칼봇 답변이 있는 채널 (비워두면 현재 채널)": "Channel with Carl-bot replies (empty = this channel)",
    "크리스봇이 스타보드에 직접 올리는 기능을 켜거나 끕니다. (칼봇 스타보드를 먼저 끄세요)": "Turn Chris Bot's own starboard posting on or off.",
    "작업물 채널에 사진·영상이 올라오면 크리스봇이 ⭐을 미리 달아 둡니다. (봇의 별은 안 셈)": "Auto-add a ⭐ to images and videos in work channels (not counted).",
    "스타보드에 올라갈 별 개수 기준을 정합니다. (칼봇 기본 10)": "Set how many stars a message needs for the starboard.",
    "별을 세지 않고 스타보드에도 올리지 않을 채널을 추가합니다.": "Add a channel where stars are not counted or posted.",
    "별 제외 채널 목록에서 뺍니다.": "Remove a channel from the star exclusion list.",
    "유저가 받은 별 합계를 직접 정합니다. (칼봇 숫자와 맞출 때)": "Set a user's total stars received directly.",
    "받은 별 합계": "Total stars received",
    "스타보드 설정과 별 기록 현황을 봅니다.": "Show starboard settings and star record status.",
    # 닉네임색
    "서버 부스터 전용: 내 닉네임 색을 정합니다. (두 색을 넣으면 그라데이션)": "Server boosters: set your name color (two colors = gradient).",
    "내 닉네임 색을 정합니다. 그라데이션색까지 넣으면 두 색 그라데이션이 됩니다.": "Set your name color. Add a second color for a gradient.",
    "색 이름을 고르거나 헥스 코드 입력 (예: #FF8800)": "Pick a color or type a hex code (e.g. #FF8800)",
    "(선택) 두 번째 색 — 넣으면 왼쪽 → 오른쪽 그라데이션": "(Optional) Second color for a left-to-right gradient",
    "내 닉네임 색을 없앱니다.": "Remove your name color.",
    "색을 정하기 전에 내 닉네임이 어떻게 보일지 봅니다. (누구나)": "Preview how your name would look (anyone can use).",
    "(선택) 두 번째 색": "(Optional) Second color",
    # 역할 유지
    "[관리자용] 서버를 나갔다 다시 들어온 사람에게 예전 역할을 돌려줍니다.": "[Admin] Give members their old roles back when they rejoin.",
    "역할 유지를 켜거나 끕니다.": "Turn role keeping on or off.",
    "다시 들어와도 돌려주지 않을 역할을 추가합니다.": "Add a role that is not given back on rejoin.",
    "돌려주지 않을 역할 목록에서 뺍니다.": "Remove a role from the not-given-back list.",
    "역할 유지 설정과 저장 상태를 봅니다.": "Show role keeping settings and status.",
    # 포스트 삭제
    "내가 작성한 포스트(또는 스레드)를 통째로 삭제합니다. (허용된 채널에서만 가능)": "Delete your own post or thread (allowed channels only).",
    "[관리자용] 작성자가 자신의 포스트를 삭제할 수 있는 채널을 관리합니다.": "[Admin] Manage channels where authors can delete their own posts.",
    "작성자가 포스트를 스스로 삭제할 수 있도록 허용할 채널(포럼/일반채널)을 추가합니다.": "Allow authors to delete their own posts in a channel.",
    "허용할 포럼 채널 또는 텍스트 채널": "Forum or text channel to allow",
    "포스트 삭제 허용 목록에서 채널을 제외합니다.": "Remove a channel from the post delete list.",
    "제외할 포럼 채널 또는 텍스트 채널": "Forum or text channel to remove",
    "현재 포스트 직접 삭제가 허용된 채널 목록을 확인합니다.": "List channels where authors can delete their posts.",
    # 등급
    "등급 조정 시즌 동안 특정 유저의 등급을 평가합니다. (예: C, C+, C++, B-, B-- 입력 가능)": "Vote on a user's grade during a grading season (e.g. C, C+, B-).",
    "등급을 평가할 대상 유저": "User to grade",
    "부여할 등급 (예: D, C+, C++, B-, B-- 등 + / - 세부 등급 지원)": "Grade to give (e.g. D, C+, C++, B-, B--)",
    "본인이 현재 보유하고 있는 등급 역할을 스스로 박탈(반납)합니다. (받은 심사위원 역할은 유지됨)": "Give up your current grade role (judge roles are kept).",
    "[관리자용] 등급 등록, 심사위원 자격, 아카이브 채널, 시즌 시작/종료, 취합 및 최종 승인을 관리합니다.": "[Admin] Manage grades, judges, archive, seasons and final approval.",
    "기본 등급 기호와 점수, 연결할 역할(및 달성 시 동시 부여할 심사역할)을 등록합니다.": "Register a grade with its score and role (and optional judge role).",
    "기본 등급 기호 (예: N, D, C, B, A, S)": "Grade symbol (e.g. N, D, C, B, A, S)",
    "산술평균 계산에 사용할 기본 숫자 점수 (예: 1, 2, 3...)": "Score used for the average (e.g. 1, 2, 3)",
    "해당 등급에 부여할 디스코드 역할(Role)": "Discord role for this grade",
    "(선택) 이 등급을 받게 될 때 동시에 자동으로 부여할 심사위원 역할": "(Optional) Judge role also given with this grade",
    "특정 등급을 달성했을 때 동시에 부여할 심사위원 역할을 설정(또는 해제)합니다.": "Set or clear the judge role given together with a grade.",
    "설정할 기본 등급 기호 (예: B, A)": "Grade symbol (e.g. B, A)",
    "이 등급 달성 시 동시에 부여할 심사위원 역할 (비워두면 동시 부여 해제)": "Judge role given with this grade (empty = none)",
    "등록되어 있는 기본 등급 기호를 삭제합니다.": "Delete a registered grade.",
    "삭제할 기본 등급 기호 (예: D)": "Grade symbol to delete (e.g. D)",
    "누가 어떻게 투표했는지 상세 내역을 기록해 둘 아카이브(보관용) 채널을 지정합니다.": "Set the archive channel for detailed vote records.",
    "투표 실명 내역과 최종 취합 기록을 보관할 텍스트 채널 (비워두면 아카이브 해제)": "Text channel for vote records (empty = no archive)",
    "심사위원 역할을 등록하고, 그 역할이 투표할 수 있는 최대 등급 상한선을 설정합니다.": "Register a judge role and the highest grade it can give.",
    "심사위원으로 지정할 디스코드 역할": "Discord role to make a judge role",
    "이 역할이 투표할 수 있는 최대 등급 (예: B 입력 시 B까지만 투표 가능 / 비워두면 모든 등급 가능)": "Highest grade this role can give (empty = any grade)",
    "최대등급이 B일 때 B+, B++ 까지 허용할지 여부 (기본값: 허용)": "If the max is B, allow B+ and B++ too (default: yes)",
    "허용 (예: 최대 B 지정 시 B+, B++ 까지 투표 가능)": "Allow (max B also allows B+ and B++)",
    "비허용 (예: 최대 B 지정 시 정확히 B 이하만 투표 가능)": "Don't allow (max B means B or lower only)",
    "등록된 심사위원 역할을 심사위원 목록에서 제외합니다.": "Remove a judge role.",
    "심사위원에서 해제할 디스코드 역할": "Judge role to remove",
    "현재 등록된 등급 목록, 심사위원 역할별 투표 상한선, 아카이브 채널, 시즌 상태를 확인합니다.": "Show grades, judge limits, archive channel and season status.",
    "지정한 채널에서 등급 조정 시즌을 활성화하여 /등급평가 제출을 받습니다.": "Start a grading season in a channel.",
    "등급 평가를 제출받을 채널": "Channel for grade votes",
    "새 시즌을 위해 남아있는 평가 기록을 비울지 여부 (기본값: 예)": "Clear old votes for a new season (default: yes)",
    "예 (새로운 시즌 시작 - 기존 평가 기록 비우기)": "Yes (new season, clear old votes)",
    "아니오 (잠시 멈췄던 현 시즌 이어서 진행)": "No (continue the paused season)",
    "등급 평가 제출을 마감합니다. (취합된 데이터는 최종승인 전까지 유지됩니다)": "Close grade voting (votes are kept until final approval).",
    "현재까지 제출된 등급 평가의 산술평균 취합 결과와 예상 공지 문구를 확인합니다.": "Show the averaged results and the expected announcement.",
    "특정 유저의 상세 평가 내역만 보려면 선택하세요 (비워두면 전체 조회)": "Pick a user to see only their votes (empty = everyone)",
    "변수 상황 발생 시 특정 심사위원의 평가를 지우거나, 특정 대상의 평가 전체를 삭제합니다.": "Delete one judge's vote, or all votes for a user.",
    "평가를 삭제할 대상 유저": "User whose votes to delete",
    "특정 심사위원이 남긴 평가만 지우려면 선택하세요 (비워두면 해당 대상의 모든 평가 삭제)": "Judge whose vote to delete (empty = all votes for the user)",
    "산술평균과 무관하게 특정 유저의 최종 취합 등급을 관리자가 직접 지정(또는 초기화)합니다.": "Set (or reset) a user's final grade manually.",
    "최종 등급을 수정할 대상 유저": "User whose final grade to change",
    "지정할 기본 등급 기호 (수동 지정을 해제하려면 'RESET' 또는 '초기화' 선택)": "Grade symbol to set (choose 'RESET' to undo)",
    "취합된 등급을 최종 승인하여 역할을 변경하고, 아카이브 기록 후 시즌 데이터를 초기화합니다.": "Approve the results: update roles, archive, and reset the season.",
}

_NAME_PLACES = {Loc.command_name, Loc.group_name, Loc.parameter_name}


def _path(context: app_commands.TranslationContext) -> Optional[str]:
    data = context.data
    return getattr(data, "qualified_name", None)


class EnglishTranslator(app_commands.Translator):
    """한국어 앱이 아닌 사람에게는 명령어를 영어로 보여 줍니다."""

    async def translate(self, string: app_commands.locale_str, locale: discord.Locale,
                        context: app_commands.TranslationContext) -> Optional[str]:
        if locale == discord.Locale.korean:
            return None
        if context.location in _NAME_PLACES:
            if context.location != Loc.parameter_name:
                override = NAME_OVERRIDES.get(_path(context) or "")
                if override:
                    return override
            return NAMES.get(string.message)
        return TEXT.get(string.message)
