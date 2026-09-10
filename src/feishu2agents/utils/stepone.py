"""Step-one utility: classify a user request with DeepSeek.

This is the "step one" intent classifier from the original two-step message
routing. It uses the DeepSeek Chat Completions API to decide whether a request
is a simple analysis/chat (``analysis``) or a concrete action task (``action``).

It is extracted as a standalone, reusable util so future integrations can call
:func:`classify_intent` without coupling to any Feishu/agent flow.
"""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

DEEPSEEK_CHAT_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"

CLASSIFY_SYSTEM_PROMPT = (
    "你是意图分类器。判断用户请求属于哪一种：\n"
    "1. 若是文本分析/内容分析/查询/聊天（例如“帮我分析股市”“解释这个概念”“写一段话”），"
    "回复仅一个词：analysis\n"
    "2. 若是要求执行具体操作任务（例如“帮我创建一个飞书日程”“发消息给某人”“创建任务”），"
    "回复仅一个词：action\n"
    "只输出 analysis 或 action，不要输出其他内容。"
)


def deepseek_chat(api_key: str, messages: list[dict]) -> str | None:
    """Call DeepSeek Chat Completions and return the assistant text.

    Returns ``None`` on a missing key or any transport/API error (HTTP != 200).
    """
    if not api_key:
        return None
    try:
        with requests.post(
            DEEPSEEK_CHAT_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"model": DEEPSEEK_MODEL, "messages": messages},
            timeout=(5, 30),
            allow_redirects=False,
        ) as response:
            if response.status_code != 200:
                logger.error("DeepSeek chat failed HTTP=%s", response.status_code)
                return None
            payload = response.json()
            return payload["choices"][0]["message"]["content"]
    except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as exc:
        logger.error("DeepSeek chat failed error_type=%s", type(exc).__name__)
        return None


def classify_intent(text: str, api_key: str) -> str:
    """Classify a user request as ``analysis`` or ``action``.

    - No key configured  -> ``action`` (caller falls through to the action path).
    - Key configured but call failed -> ``error``.
    - Otherwise ``analysis`` for analysis/chat, ``action`` for concrete tasks.
    """
    if not api_key:
        return "action"
    reply = deepseek_chat(
        api_key,
        [
            {"role": "system", "content": CLASSIFY_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
    )
    if not reply:
        return "error"
    normalized = reply.strip().lower()
    return "analysis" if normalized in {"analysis", "analyze", "analyse"} else "action"