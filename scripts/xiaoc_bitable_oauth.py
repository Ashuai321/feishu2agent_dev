"""Obtain the current Feishu user's Bitable OAuth token through a browser."""

from __future__ import annotations

import argparse
import contextlib
import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

try:
    from scripts.feishu_api import (
        FeishuScriptError,
        exchange_user_access_token,
        load_dotenv,
        required_env,
    )
except ModuleNotFoundError:  # Allows ``python scripts/xiaoc_bitable_oauth.py``.
    from feishu_api import (  # type: ignore[no-redef]
        FeishuScriptError,
        exchange_user_access_token,
        load_dotenv,
        required_env,
    )

DEFAULT_AUTHORIZE_URL = "https://bot.boooe.com/feishu/oauth/authorize"
DEFAULT_CALLBACK_URI = "https://bot.boooe.com/feishu/oauth/callback"


class _CallbackHandler(BaseHTTPRequestHandler):
    result: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802
        query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
        _CallbackHandler.result = {
            "code": (query.get("code") or [""])[0],
            "state": (query.get("state") or [""])[0],
            "error": (query.get("error") or [""])[0],
        }
        body = (
            "授权完成，可以关闭此页面。"
            if _CallbackHandler.result["code"]
            else "授权失败，请查看终端。"
        )
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        return


def _save_token(path: Path, token: dict[str, object]) -> None:
    path.write_text(
        json.dumps(token, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with contextlib.suppress(OSError):
        path.chmod(0o600)

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authorize-url", default=DEFAULT_AUTHORIZE_URL)
    parser.add_argument("--callback-uri", default=DEFAULT_CALLBACK_URI)
    parser.add_argument("--token-file", type=Path, default=Path(".feishu-user-token.json"))
    parser.add_argument("--no-open", action="store_true", help="只打印授权地址，不自动打开浏览器")
    args = parser.parse_args()
    parsed_callback = urlparse(args.callback_uri)
    is_local_callback = parsed_callback.scheme == "http" and parsed_callback.hostname in {
        "127.0.0.1",
        "localhost",
    }
    is_public_callback = (
        parsed_callback.scheme == "https"
        and parsed_callback.netloc == "bot.boooe.com"
        and parsed_callback.path == "/feishu/oauth/callback"
    )
    if not is_local_callback and not is_public_callback:
        print(
            "callback-uri 必须使用公开回调地址："
            "https://bot.boooe.com/feishu/oauth/callback；"
            "如需本地自动保存 token，可显式传入本机回调地址。"
        )
        return 1
    if is_local_callback and not parsed_callback.port:
        print("本地 callback-uri 必须包含端口，例如 http://127.0.0.1:8765/callback")
        return 1
    try:
        if is_local_callback:
            load_dotenv()
            app_id = required_env("FEISHU_APP_ID")
            app_secret = required_env("FEISHU_APP_SECRET")
            server = ThreadingHTTPServer(
                (parsed_callback.hostname, parsed_callback.port), _CallbackHandler
            )
            server_thread = threading.Thread(target=server.handle_request, daemon=True)
            server_thread.start()
        authorize_url = args.authorize_url.rstrip("/") + "?" + urlencode(
            {"callback_uri": args.callback_uri}
        )
        print("请在浏览器中完成飞书授权：")
        print(authorize_url)
        if not args.no_open:
            webbrowser.open(authorize_url)
        if is_local_callback:
            server_thread.join(timeout=300)
            server.server_close()
            result = dict(_CallbackHandler.result)
            if not result.get("code"):
                raise FeishuScriptError(result.get("error") or "等待授权回调超时")
            token = exchange_user_access_token(
                app_id,
                app_secret,
                result["code"],
                redirect_uri=args.callback_uri,
            )
            _save_token(args.token_file, token)
            print(f"OAuth 成功，token 已保存到 {args.token_file}（权限由飞书授权页决定）")
            if token.get("open_id"):
                print(f"当前用户 open_id={token['open_id']}")
            return 0
        print(
            "授权完成后，公开回调页会显示 token；"
            "请将回调页返回的 JSON 保存后再运行写入脚本。"
        )
        return 0
    except (OSError, FeishuScriptError) as exc:
        print(f"OAuth 失败：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
