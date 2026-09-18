"""Inspect the 测试 table or create one user-authorized record in it."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
from datetime import date
from pathlib import Path
from typing import Any

try:
    from scripts.feishu_api import (
        FeishuScriptError,
        create_bitable_record,
        get_bitable_fields,
        get_bitable_records,
        get_user_info,
        load_dotenv,
        parse_date_to_millis,
        platform_endpoints,
        resolve_wiki_bitable_app_token,
    )
except ModuleNotFoundError:  # Allows ``python scripts/xiaoc_bitable_create_row.py``.
    from feishu_api import (  # type: ignore[no-redef]
        FeishuScriptError,
        create_bitable_record,
        get_bitable_fields,
        get_bitable_records,
        get_user_info,
        load_dotenv,
        parse_date_to_millis,
        platform_endpoints,
        resolve_wiki_bitable_app_token,
    )

WIKI_TOKEN = "AYNDwkmOUiZtbgkZ3BAcLchUnnb"
TABLE_ID = "tblVyvH3RGHqwQBC"
VIEW_ID = "vewXxBNTOK"
FIELD_NAMES = (
    "任务描述",
    "任务情况总结",
    "任务执行人",
    "进展",
    "开始日期",
    "预计完成日期",
    "是否延期",
    "实际完成日期",
    "最新进展记录",
    "重要紧急程度",
)


def _token_from_args(path: Path | None, platform: str) -> str:
    endpoints = platform_endpoints(platform)
    value = os.getenv(endpoints.user_token_env, "").strip()
    if value:
        return value
    if path and path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        value = str(data.get("access_token") or "").strip()
        if value:
            return value
    raise FeishuScriptError(
        f"缺少{platform}用户 token：先运行 xiaoc_bitable_oauth.py --platform {platform}，"
        f"或设置 {endpoints.user_token_env}"
    )


def _field_map(fields: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(item.get("field_name") or ""): item for item in fields}


def _print_fields(fields: list[dict[str, Any]]) -> None:
    print("测试表字段：")
    for item in fields:
        print(f"- {item.get('field_name')} (type={item.get('type')}, id={item.get('field_id')})")


def _build_fields(
    args: argparse.Namespace,
    available: dict[str, dict[str, Any]],
    user_open_id: str,
) -> dict[str, Any]:
    values: dict[str, Any] = {}

    def set_text(name: str, value: str | None) -> None:
        if value:
            if name not in available:
                raise FeishuScriptError(f"测试表缺少字段：{name}")
            values[name] = value

    set_text("任务描述", args.task_description)
    set_text("任务情况总结", args.summary)
    set_text("进展", args.progress)
    set_text("最新进展记录", args.latest_progress)
    set_text("重要紧急程度", args.importance)
    if args.assignee_open_id or user_open_id:
        if "任务执行人" not in available:
            raise FeishuScriptError("测试表缺少字段：任务执行人")
        values["任务执行人"] = [{"id": args.assignee_open_id or user_open_id}]
    for name, value in (
        ("开始日期", args.start_date),
        ("预计完成日期", args.due_date),
        ("实际完成日期", args.completed_date),
    ):
        if value:
            if name not in available:
                raise FeishuScriptError(f"测试表缺少字段：{name}")
            values[name] = parse_date_to_millis(value)
    if args.delayed is not None:
        if "是否延期" not in available:
            raise FeishuScriptError("测试表缺少字段：是否延期")
        values["是否延期"] = args.delayed
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=("feishu", "lark"), default="feishu")
    parser.add_argument("--token-file", type=Path, default=None)
    parser.add_argument("--wiki-token", default=WIKI_TOKEN)
    parser.add_argument("--table-id", default=TABLE_ID)
    parser.add_argument("--app-token", help="已知 app_token；不传则用 wiki token 解析")
    parser.add_argument("--inspect", action="store_true", help="只读取并打印字段和现有记录，不写入")
    parser.add_argument("--task-description", default="小C脚本测试")
    parser.add_argument("--summary", default="由小C脚本创建的测试记录")
    parser.add_argument("--progress", default="待开始")
    parser.add_argument("--latest-progress", default="")
    parser.add_argument("--importance", default="")
    parser.add_argument("--assignee-open-id", default="")
    parser.add_argument("--start-date", default=date.today().isoformat())
    parser.add_argument("--due-date", default="")
    parser.add_argument("--completed-date", default="")
    parser.add_argument("--delayed", action=argparse.BooleanOptionalAction, default=None)
    args = parser.parse_args()
    try:
        load_dotenv()
        endpoints = platform_endpoints(args.platform)
        token_file = args.token_file or Path(endpoints.user_token_file)
        user_token = _token_from_args(token_file, args.platform)
        app_token = args.app_token or resolve_wiki_bitable_app_token(
            user_token, args.wiki_token, base_url=endpoints.api_base
        )
        fields = get_bitable_fields(
            user_token, app_token, args.table_id, base_url=endpoints.api_base
        )
        available = _field_map(fields)
        _print_fields(fields)
        if args.inspect:
            records = get_bitable_records(
                user_token, app_token, args.table_id, base_url=endpoints.api_base
            )
            print(f"现有记录数（当前页最多 100 条）：{len(records)}")
            for record in records[:10]:
                print(json.dumps(record, ensure_ascii=False))
            return 0
        user_open_id = ""
        with contextlib.suppress(FeishuScriptError):
            user_open_id = str(
                get_user_info(user_token, base_url=endpoints.api_base).get("open_id") or ""
            )
        values = _build_fields(args, available, user_open_id)
        record = create_bitable_record(
            user_token,
            app_token=app_token,
            table_id=args.table_id,
            fields=values,
            base_url=endpoints.api_base,
        )
    except (FeishuScriptError, OSError, json.JSONDecodeError) as exc:
        print(f"多维表格操作失败：{exc}")
        return 1
    print(f"创建成功，record_id={record.get('record_id')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
