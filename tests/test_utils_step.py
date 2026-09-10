"""Tests for the reusable step-one/step-two utilities."""

from unittest.mock import patch

from feishu2agents.utils.stepone import classify_intent, deepseek_chat
from feishu2agents.utils.steptwo import answer_request, openai_chat


def _fake_reply(text="analysis"):
    class Resp:
        status = 200
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": text}}]}

    return Resp()


def test_classify_intent_returns_action_without_key():
    assert classify_intent("任意内容", "") == "action"


def test_classify_intent_returns_error_on_api_failure():
    with patch("feishu2agents.utils.stepone.requests.post") as post:
        post.return_value.__enter__.return_value.status_code = 500
        assert classify_intent("分析一下行情", "sk-test") == "error"


def test_classify_intent_returns_analysis():
    with patch("feishu2agents.utils.stepone.requests.post") as post:
        post.return_value.__enter__.return_value = _fake_reply("analysis")
        assert classify_intent("帮我分析股市", "sk-test") == "analysis"


def test_classify_intent_returns_action():
    with patch("feishu2agents.utils.stepone.requests.post") as post:
        post.return_value.__enter__.return_value = _fake_reply("action")
        assert classify_intent("帮我创建一个任务", "sk-test") == "action"


def test_deepseek_chat_returns_none_without_key():
    assert deepseek_chat("", [{"role": "user", "content": "hi"}]) is None


def test_answer_request_returns_none_without_key():
    assert answer_request("hi", "") is None


def test_answer_request_returns_reply():
    with patch("feishu2agents.utils.steptwo.requests.post") as post:
        post.return_value.__enter__.return_value = _fake_reply("这是回答")
        assert answer_request("你好", "sk-test") == "这是回答"


def test_openai_chat_returns_none_on_http_error():
    with patch("feishu2agents.utils.steptwo.requests.post") as post:
        post.return_value.__enter__.return_value.status_code = 429
        assert openai_chat("sk-test", [{"role": "user", "content": "hi"}]) is None