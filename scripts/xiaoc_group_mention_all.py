"""Send one message mentioning each visible member of the test group."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Any

try:
    from scripts.feishu_api import (
        FeishuScriptError,
        get_bot_info,
        get_tenant_access_token,
        list_chat_members,
        load_dotenv,
        platform_endpoints,
        required_env,
        send_text_mention,
    )
except ModuleNotFoundError:  # Allows ``python scripts/xiaoc_group_mention_all.py``.
    from feishu_api import (  # type: ignore[no-redef]
        FeishuScriptError,
        get_bot_info,
        get_tenant_access_token,
        list_chat_members,
        load_dotenv,
        platform_endpoints,
        required_env,
        send_text_mention,
    )


DEFAULT_CHAT_ID = "oc_5e9132f3638772d53d92d6fc5e953abc"


@dataclass(frozen=True)
class PlatformGroup:
    platform: str
    tenant_token: str
    bot_open_id: str
    members: tuple[dict[str, Any], ...]


def _try_platform(platform: str, chat_id: str) -> PlatformGroup:
    endpoints = platform_endpoints(platform)
    app_id = required_env(endpoints.app_id_env)
    app_secret = required_env(endpoints.app_secret_env)
    tenant_token = get_tenant_access_token(
        app_id, app_secret, base_url=endpoints.api_base
    )
    members = list_chat_members(
        tenant_token, chat_id, base_url=endpoints.api_base
    )
    bot = get_bot_info(app_id, app_secret, base_url=endpoints.api_base)
    bot_open_id = str(bot.get("open_id") or "")
    if not bot_open_id:
        raise FeishuScriptError(f"{platform} 机器人身份没有 open_id")
    return PlatformGroup(platform, tenant_token, bot_open_id, tuple(members))


def _select_group(
    platform: str,
    chat_id: str,
) -> PlatformGroup:
    if platform != "auto":
        return _try_platform(platform, chat_id)
    found: list[PlatformGroup] = []
    failures: list[str] = []
    for candidate in ("feishu", "lark"):
        try:
            found.append(_try_platform(candidate, chat_id))
        except FeishuScriptError as exc:
            failures.append(f"{candidate}: {exc}")
    if not found:
        details = "；".join(failures)
        raise FeishuScriptError(f"Feishu/Lark 都无法读取群 {chat_id}：{details}")
    if len(found) == 1:
        return found[0]
    if not sys.stdin.isatty():
        raise FeishuScriptError("该 chat_id 同时能被 Feishu 和 Lark 读取，请显式传 --platform")
    print("该群在两个平台都可见，请选择发送平台：")
    for index, item in enumerate(found, 1):
        print(f"{index}. {item.platform}（成员 {len(item.members)} 人）")
    while True:
        answer = input("请输入序号：").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(found):
            return found[int(answer) - 1]
        print("序号无效，请重新输入。")


def _member_open_id(member: dict[str, Any]) -> str:
    return str(
        member.get("member_id")
        or member.get("open_id")
        or (member.get("member_id_info") or {}).get("open_id")
        or ""
    ).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=("auto", "feishu", "lark"), default="auto")
    parser.add_argument("--chat-id", default=DEFAULT_CHAT_ID)
    parser.add_argument("--text", default="小C测试，请确认收到。")
    parser.add_argument("--dry-run", action="store_true", help="只列出 open_id，不发送消息")
    args = parser.parse_args()
    try:
        load_dotenv()
        group = _select_group(args.platform, args.chat_id)
        members: list[tuple[str, str]] = []
        for member in group.members:
            open_id = _member_open_id(member)
            if not open_id or open_id == group.bot_open_id:
                continue
            name = str(member.get("name") or member.get("display_name") or open_id).strip()
            members.append((open_id, name))
        print(f"平台={group.platform}，群={args.chat_id}，可 @ 的成员数={len(members)}")
        for index, (open_id, name) in enumerate(members, 1):
            print(f"{index}. {name}: {open_id}")
        if args.dry_run:
            return 0
        endpoints = platform_endpoints(group.platform)
        sent = 0
        for open_id, name in members:
            send_text_mention(
                group.tenant_token,
                chat_id=args.chat_id,
                user_open_id=open_id,
                user_name=name,
                text=args.text,
                base_url=endpoints.api_base,
            )
            sent += 1
        print(f"已发送 {sent} 条消息；每条消息只 @ 一个成员。")
        return 0
    except (FeishuScriptError, OSError) as exc:
        print(f"群成员 @ 操作失败：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
