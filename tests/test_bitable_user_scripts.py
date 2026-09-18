from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
from scripts import xiaoc_bitable_current_user as current_user


def test_choose_profile_requires_explicit_selection_without_tty(monkeypatch) -> None:
    profile_a = current_user.UserProfile(
        "feishu", "secret-a", "env:FEISHU_USER_ACCESS_TOKEN", "ou_a", "甲", "", ""
    )
    profile_b = current_user.UserProfile(
        "lark", "secret-b", "env:LARK_USER_ACCESS_TOKEN", "ou_b", "乙", "", ""
    )
    monkeypatch.setattr(current_user.sys.stdin, "isatty", lambda: False)
    try:
        current_user.choose_profile([profile_a, profile_b])
    except current_user.FeishuScriptError as exc:
        assert "多个已授权用户" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("multiple non-interactive profiles must require selection")
    assert current_user.choose_profile([profile_a, profile_b], selection="ou_b") is profile_b


def test_discover_profiles_uses_the_platform_api_for_each_token(
    monkeypatch, tmp_path: Path
) -> None:
    feishu_file = tmp_path / ".feishu-user-token-alice.json"
    feishu_file.write_text(json.dumps({"access_token": "f-token"}), encoding="utf-8")
    lark_file = tmp_path / ".lark-user-token-bob.json"
    lark_file.write_text(json.dumps({"access_token": "l-token"}), encoding="utf-8")
    seen: list[tuple[str, str]] = []

    def fake_user_info(token: str, *, base_url: str):
        seen.append((token, base_url))
        return {"open_id": "ou_f" if token.startswith("f") else "ou_l", "name": token}

    monkeypatch.setattr(current_user, "get_user_info", fake_user_info)
    monkeypatch.setattr(current_user, "_project_root", lambda: tmp_path)
    profiles, failures = current_user.discover_profiles()
    assert failures == []
    assert [(item.platform, item.open_id) for item in profiles] == [
        ("feishu", "ou_f"),
        ("lark", "ou_l"),
    ]
    assert seen == [
        ("f-token", "https://open.feishu.cn"),
        ("l-token", "https://open.larksuite.com"),
    ]
