"""Send one test message from Xiao C and mention 文帅 in the fixed test group."""

from __future__ import annotations

import argparse

try:
    from scripts.feishu_api import (
        FeishuScriptError,
        get_tenant_access_token,
        load_dotenv,
        required_env,
        send_text_mention,
    )
except ModuleNotFoundError:  # Allows ``python scripts/xiaoc_send_mention.py``.
    from feishu_api import (  # type: ignore[no-redef]
        FeishuScriptError,
        get_tenant_access_token,
        load_dotenv,
        required_env,
        send_text_mention,
    )

CHAT_ID = "oc_5e9132f3638772d53d92d6fc5e953abc"
USER_OPEN_ID = "ou_a00b35e763c13d60eb411ca9a344e777"
USER_NAME = "文帅"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--text",
        default="小C测试消息，请查收。",
        help="@文帅后发送的文本内容",
    )
    parser.add_argument("--chat-id", default=CHAT_ID, help="目标群 chat_id")
    parser.add_argument("--user-open-id", default=USER_OPEN_ID, help="被@用户的 open_id")
    args = parser.parse_args()
    try:
        load_dotenv()
        app_id = required_env("FEISHU_APP_ID")
        app_secret = required_env("FEISHU_APP_SECRET")
        tenant_token = get_tenant_access_token(app_id, app_secret)
        message_id = send_text_mention(
            tenant_token,
            chat_id=args.chat_id,
            user_open_id=args.user_open_id,
            user_name=USER_NAME,
            text=args.text,
        )
    except FeishuScriptError as exc:
        print(f"发送失败：{exc}")
        return 1
    print(f"发送成功，message_id={message_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
