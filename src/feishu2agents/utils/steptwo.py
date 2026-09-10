"""Step-two utility: answer a request directly with OpenAI.

This is the "step two" direct-answer step from the original two-step message
routing. It uses the OpenAI Chat Completions API to produce an analysis answer
for a request.

Extracted as a standalone, reusable util so future integrations can call
:func:`answer_request` without coupling to any Feishu/agent flow.
"""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL = "gpt-4o-mini"

ANSWER_SYSTEM_PROMPT = "你是专业助手，请针对用户的问题给出清晰、完整的分析回答。"


def openai_chat(api_key: str, messages: list[dict]) -> str | None:
    """Call OpenAI Chat Completions and return the assistant text.

    Returns ``None`` on a missing key or any transport/API error (HTTP != 200).
    """
    if not api_key:
        return None
    try:
        with requests.post(
            OPENAI_CHAT_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"model": OPENAI_MODEL, "messages": messages},
            timeout=(5, 30),
            allow_redirects=False,
        ) as response:
            if response.status_code != 200:
                logger.error("OpenAI chat failed HTTP=%s", response.status_code)
                return None
            payload = response.json()
            return payload["choices"][0]["message"]["content"]
    except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as exc:
        logger.error("OpenAI chat failed error_type=%s", type(exc).__name__)
        return None


def answer_request(text: str, api_key: str) -> str | None:
    """Produce an OpenAI analysis answer for ``text``.

    Returns ``None`` when no key is configured or the API call fails.
    """
    if not api_key:
        return None
    return openai_chat(
        api_key,
        [
            {"role": "system", "content": ANSWER_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
    )