"""Use the currently authorized Feishu/Lark user to inspect or write a Bitable row.

The platform is selected from the token profile and is never inferred from the
shape of an ``open_id``.  A profile is a user OAuth token created by
``xiaoc_bitable_oauth.py``; an App ID/Secret alone identifies the bot and cannot
act as the human requester.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from scripts.feishu_api import (
        FeishuScriptError,
        create_bitable_record,
        get_bitable_fields,
        get_bitable_records,
        get_bot_info,
        get_user_info,
        load_dotenv,
        platform_endpoints,
        required_env,
        resolve_wiki_bitable_app_token,
    )
except ModuleNotFoundError:  # Allows ``python scripts/xiaoc_bitable_current_user.py``.
    from feishu_api import (  # type: ignore[no-redef]
        FeishuScriptError,
        create_bitable_record,
        get_bitable_fields,
        get_bitable_records,
        get_bot_info,
        get_user_info,
        load_dotenv,
        platform_endpoints,
        required_env,
        resolve_wiki_bitable_app_token,
    )


WIKI_TOKEN = "AYNDwkmOUiZtbgkZ3BAcLchUnnb"
TABLE_ID = "tblVyvH3RGHqwQBC"
VIEW_ID = "vewPjVWZHm"
DEFAULT_TEXT = "小C脚本测试"


@dataclass(frozen=True)
class UserProfile:
    platform: str
    token: str
    source: str
    open_id: str
    name: str
    email: str
    user_id: str

    @property
    def label(self) -> str:
        who = self.name or self.email or self.open_id
        return f"{self.platform} / {who} / {self.open_id}"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _platform_for_filename(path: Path) -> str:
    name = path.name.lower()
    return "lark" if "lark" in name else "feishu"


def _token_from_file(path: Path) -> tuple[str, str, str, str, str] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FeishuScriptError(f"无法读取 token 文件 {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise FeishuScriptError(f"token 文件 {path} 不是 JSON 对象")
    token = str(payload.get("access_token") or payload.get("user_access_token") or "").strip()
    if not token:
        return None
    platform = str(payload.get("platform") or _platform_for_filename(path)).strip().lower()
    if platform not in {"feishu", "lark"}:
        raise FeishuScriptError(f"token 文件 {path} 的 platform 必须是 feishu 或 lark")
    return (
        platform,
        token,
        str(payload.get("open_id") or "").strip(),
        str(payload.get("name") or "").strip(),
        str(payload.get("email") or "").strip(),
    )


def _candidate_files(explicit: Iterable[Path]) -> list[Path]:
    roots = [_project_root(), Path.cwd()]
    result: list[Path] = []
    for path in explicit:
        resolved = path.expanduser().resolve()
        if resolved not in result:
            result.append(resolved)
    for root in roots:
        for pattern in (
            ".feishu-user-token.json",
            ".feishu-user-token-*.json",
            ".lark-user-token.json",
            ".lark-user-token-*.json",
        ):
            for path in sorted(root.glob(pattern)):
                resolved = path.resolve()
                if resolved not in result:
                    result.append(resolved)
    return result


def _raw_candidates(token_files: Iterable[Path]) -> list[tuple[str, str, str]]:
    """Return platform, token, source triples without exposing token values."""
    candidates: list[tuple[str, str, str]] = []
    for platform in ("feishu", "lark"):
        endpoints = platform_endpoints(platform)
        token = os.getenv(endpoints.user_token_env, "").strip()
        if token:
            candidates.append((platform, token, f"env:{endpoints.user_token_env}"))
    for path in _candidate_files(token_files):
        value = _token_from_file(path)
        if value is None:
            continue
        platform, token, _open_id, _name, _email = value
        candidates.append((platform, token, str(path)))
    # Avoid calling the API twice when an env token and its saved token file are
    # the same profile.  Different tokens are intentionally retained.
    unique: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for platform, token, source in candidates:
        key = (platform, token)
        if key in seen:
            continue
        seen.add(key)
        unique.append((platform, token, source))
    return unique


def discover_profiles(
    *,
    platform: str = "auto",
    token_files: Iterable[Path] = (),
    user_open_id: str = "",
) -> tuple[list[UserProfile], list[str]]:
    """Validate every saved OAuth profile and return usable identities.

    Invalid/expired profiles are reported separately so one stale Feishu token
    cannot hide a valid Lark profile (or the reverse).
    """
    normalized = str(platform or "auto").strip().lower()
    if normalized not in {"auto", "feishu", "lark"}:
        raise FeishuScriptError("--platform 只能是 auto、feishu 或 lark")
    profiles: list[UserProfile] = []
    failures: list[str] = []
    for candidate_platform, token, source in _raw_candidates(token_files):
        if normalized != "auto" and candidate_platform != normalized:
            continue
        endpoints = platform_endpoints(candidate_platform)
        try:
            info = get_user_info(token, base_url=endpoints.api_base)
        except FeishuScriptError as exc:
            failures.append(f"{candidate_platform} ({source}): {exc}")
            continue
        open_id = str(info.get("open_id") or "").strip()
        if not open_id:
            failures.append(f"{candidate_platform} ({source}): 用户信息没有 open_id")
            continue
        if user_open_id and open_id != user_open_id:
            continue
        profiles.append(
            UserProfile(
                platform=candidate_platform,
                token=token,
                source=source,
                open_id=open_id,
                name=str(info.get("name") or "").strip(),
                email=str(info.get("email") or "").strip(),
                user_id=str(info.get("user_id") or "").strip(),
            )
        )
    return profiles, failures


def choose_profile(profiles: list[UserProfile], *, selection: str = "") -> UserProfile:
    if not profiles:
        raise FeishuScriptError(
            "没有找到可用的 Feishu/Lark 用户 OAuth token；先运行 "
            "xiaoc_bitable_oauth.py（对应平台）完成授权。"
        )
    if selection:
        value = selection.strip().lower()
        for index, profile in enumerate(profiles, 1):
            if value in {str(index), profile.open_id.lower(), profile.source.lower()}:
                return profile
        raise FeishuScriptError("--profile 不匹配任何已授权用户，请用 --list-users 查看")
    if len(profiles) == 1:
        return profiles[0]
    if not sys.stdin.isatty():
        choices = "; ".join(
            f"{index}: {profile.label}" for index, profile in enumerate(profiles, 1)
        )
        raise FeishuScriptError(
            "检测到多个已授权用户，请在交互终端使用 --profile；"
            f"可选项：{choices}"
        )
    print("检测到多个已登录用户，请选择本次操作的账号：")
    for index, profile in enumerate(profiles, 1):
        print(f"{index}. {profile.label}（来源：{profile.source}）")
    while True:
        answer = input("请输入序号：").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(profiles):
            return profiles[int(answer) - 1]
        print("序号无效，请重新输入。")


def _print_fields(fields: list[dict[str, Any]]) -> None:
    print("测试表字段：")
    for item in fields:
        print(f"- {item.get('field_name')} (type={item.get('type')}, id={item.get('field_id')})")


def _create_row(profile: UserProfile, args: argparse.Namespace) -> dict[str, Any]:
    endpoints = platform_endpoints(profile.platform)
    app_token = args.app_token or resolve_wiki_bitable_app_token(
        profile.token, args.wiki_token, base_url=endpoints.api_base
    )
    fields = get_bitable_fields(
        profile.token, app_token, args.table_id, base_url=endpoints.api_base
    )
    available = {str(item.get("field_name") or ""): item for item in fields}
    _print_fields(fields)
    if args.inspect:
        records = get_bitable_records(
            profile.token, app_token, args.table_id, base_url=endpoints.api_base
        )
        print(
            f"平台={profile.platform}，用户={profile.open_id}，"
            f"现有记录数（当前页最多 100 条）：{len(records)}"
        )
        for record in records[:10]:
            print(json.dumps(record, ensure_ascii=False))
        return {"inspect": True, "platform": profile.platform, "open_id": profile.open_id}
    if "任务描述" not in available or "任务执行人" not in available:
        raise FeishuScriptError("测试表必须包含字段：任务描述、任务执行人")
    values = {
        "任务描述": args.text,
        "任务执行人": [{"id": profile.open_id}],
    }
    return create_bitable_record(
        profile.token,
        app_token=app_token,
        table_id=args.table_id,
        fields=values,
        base_url=endpoints.api_base,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=("auto", "feishu", "lark"), default="auto")
    parser.add_argument("--profile", default="", help="用户序号、open_id 或 token 文件路径")
    parser.add_argument("--user-open-id", default="", help="只选择指定 open_id 的授权用户")
    parser.add_argument("--token-file", action="append", type=Path, default=[])
    parser.add_argument("--list-users", action="store_true", help="只列出当前可用用户，不写入")
    parser.add_argument("--skip-bot-check", action="store_true", help="跳过对应平台机器人凭证检查")
    parser.add_argument("--inspect", action="store_true", help="只读取字段和记录，不写入")
    parser.add_argument("--text", default=DEFAULT_TEXT, help="写入字段[任务描述]的文字")
    parser.add_argument("--wiki-token", default=WIKI_TOKEN)
    parser.add_argument("--table-id", default=TABLE_ID)
    parser.add_argument("--app-token", default="")
    args = parser.parse_args()
    try:
        load_dotenv()
        profiles, failures = discover_profiles(
            platform=args.platform,
            token_files=args.token_file,
            user_open_id=args.user_open_id,
        )
        if failures:
            for failure in failures:
                print(f"跳过不可用授权：{failure}", file=sys.stderr)
        if args.list_users:
            if not profiles:
                raise FeishuScriptError("没有可用的已授权用户")
            for index, profile in enumerate(profiles, 1):
                print(f"{index}. {profile.label}（来源：{profile.source}）")
            return 0
        profile = choose_profile(profiles, selection=args.profile)
        endpoints = platform_endpoints(profile.platform)
        bot_id = ""
        if not args.skip_bot_check:
            app_id = required_env(endpoints.app_id_env)
            app_secret = required_env(endpoints.app_secret_env)
            bot = get_bot_info(app_id, app_secret, base_url=endpoints.api_base)
            bot_id = str(bot.get("open_id") or "")
        print(f"已选择平台={profile.platform}，用户 open_id={profile.open_id}")
        if bot_id:
            print(f"对应机器人 open_id={bot_id}")
        result = _create_row(profile, args)
        if result.get("inspect"):
            return 0
        print(
            f"创建成功，平台={profile.platform}，用户 open_id={profile.open_id}，"
            f"record_id={result.get('record_id')}"
        )
        return 0
    except (FeishuScriptError, OSError, json.JSONDecodeError) as exc:
        print(f"多维表格操作失败：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
