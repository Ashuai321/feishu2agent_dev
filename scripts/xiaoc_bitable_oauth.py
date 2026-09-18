"""Obtain the current Feishu user's Bitable OAuth token through a browser."""

from __future__ import annotations

import argparse
import webbrowser
from urllib.parse import urlencode, urlparse

DEFAULT_AUTHORIZE_URL = "https://bot.boooe.com/feishu/oauth/authorize"
DEFAULT_CALLBACK_URI = "https://bot.boooe.com/feishu/oauth/callback"

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authorize-url", default=DEFAULT_AUTHORIZE_URL)
    parser.add_argument("--callback-uri", default=DEFAULT_CALLBACK_URI)
    parser.add_argument("--no-open", action="store_true", help="只打印授权地址，不自动打开浏览器")
    args = parser.parse_args()
    parsed_callback = urlparse(args.callback_uri)
    if parsed_callback.scheme != "https" or parsed_callback.netloc != "bot.boooe.com":
        print(
            "callback-uri 必须使用公开回调地址："
            "https://bot.boooe.com/feishu/oauth/callback"
        )
        return 1
    try:
        authorize_url = args.authorize_url.rstrip("/") + "?" + urlencode(
            {"callback_uri": args.callback_uri}
        )
        print("请在浏览器中完成飞书授权：")
        print(authorize_url)
        if not args.no_open:
            webbrowser.open(authorize_url)
        print(
            "授权完成后，公开回调页会显示 token；"
            "请将回调页返回的 JSON 保存后再运行写入脚本。"
        )
        return 0
    except OSError as exc:
        print(f"OAuth 失败：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
