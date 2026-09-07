from dataclasses import replace
from datetime import datetime, timezone

from gpt2feishu.bot_handler import EchoMessageHandler, should_process
from gpt2feishu.message_context import Mention, MessageContext, SenderIdentifiers


def context() -> MessageContext:
    return MessageContext(
        message_id="om_1",
        event_id="evt_1",
        chat_id="oc_1",
        chat_type="group",
        sender_id="ou_user",
        sender_ids=SenderIdentifiers(open_id="ou_user"),
        sender_type="user",
        sender_tenant_key="tenant-external",
        event_tenant_key="tenant-app",
        message_type="text",
        text="hello",
        mentions=(
            Mention(
                key="@_user_1",
                name="Bot",
                ids=SenderIdentifiers(open_id="ou_bot"),
                tenant_key="tenant-app",
                is_bot=True,
            ),
        ),
        create_time=datetime.now(timezone.utc),  # noqa: UP017
        bot_app_id="cli_test",
    )


def test_echo_handler() -> None:
    assert EchoMessageHandler().handle(context()) == "收到：hello"


def test_ignores_non_group_message() -> None:
    assert not should_process(replace(context(), chat_type="p2p"))


def test_ignores_non_text_message() -> None:
    assert not should_process(replace(context(), message_type="image"))


def test_ignores_message_without_bot_mention() -> None:
    assert not should_process(replace(context(), mentions=()))


def test_ignores_empty_message() -> None:
    assert not should_process(replace(context(), text=""))


def test_ignores_bot_and_app_senders() -> None:
    assert not should_process(replace(context(), sender_type="bot"))
    assert not should_process(replace(context(), sender_type="app"))
