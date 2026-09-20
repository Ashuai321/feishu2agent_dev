from pathlib import Path
from types import SimpleNamespace

from feishu2agents.relay.store.relay_store import RelayStore
from feishu2agents.relay.trigger import build_trigger_input, generate_request_id

from feishu2agents.feishu_bridge import FeishuBridge
from feishu2agents.workspace_agent import WorkspaceAgentMessageHandler, WorkspaceAgentSettings

TRIGGER_URL = "https://api.chatgpt.com/v1/workspace_agents/agtch_test/trigger"


class RecordingDispatcher:
    def __init__(self):
        self.calls = []

    def dispatch(self, **kwargs):
        self.calls.append(kwargs)

    def shutdown(self):
        pass


def context(text="创建一个任务", message_id="om_1", reply_to_message_id=None):
    return SimpleNamespace(
        bot_app_id="app",
        message_id=message_id,
        text=text,
        chat_id="chat_1",
        chat_type="group",
        message_type="text",
        mentions_bot=True,
        sender_type="user",
        reply_to_message_id=reply_to_message_id,
    )


def make_store(tmp_path):
    store = RelayStore(Path(tmp_path) / "relay.sqlite")
    store.create_agent(name="default", trigger_url=TRIGGER_URL, access_token="agent-secret")
    return store


def make_handler(tmp_path):
    store = make_store(tmp_path)
    bridge = FeishuBridge(str(tmp_path / "bridge.sqlite"))
    dispatcher = RecordingDispatcher()
    settings = WorkspaceAgentSettings(
        store=store,
        bridge=bridge,
        dispatcher=dispatcher,
        relay_config=SimpleNamespace(agent_tokens={}, default_agent_token="agent-secret"),
    )
    return WorkspaceAgentMessageHandler(settings), store, bridge, dispatcher


def test_build_trigger_input_injects_protocol(tmp_path):
    request_id = generate_request_id("feishu")
    text = build_trigger_input(
        request_id=request_id,
        conversation_key="feishu:app:chat_1",
        user_input="查天气",
    )
    assert f"request_id: {request_id}" in text
    assert "conversation_key: feishu:app:chat_1" in text
    assert "record_result" in text
    assert "查天气" in text


def test_calendar_agent_trigger_requires_operation_specific_confirmation():
    text = build_trigger_input(
        request_id="req_calendar",
        conversation_key="feishu:app:chat_1",
        user_input="删除明天的日程",
        calendar_confirmation_required=True,
    )

    assert "The user's original request is never confirmation" in text
    assert "确认创建/确认修改/确认删除/确认取消" in text
    assert "Re-read after success" in text
    assert "never reject or withhold a successfully rendered image" in text


def test_non_calendar_agent_trigger_does_not_receive_calendar_gate():
    text = build_trigger_input(
        request_id="req_general",
        conversation_key="feishu:app:chat_1",
        user_input="创建一个群",
    )

    assert "The user's original request is never confirmation" not in text


def test_calendar_agent_name_match_is_narrow():
    from feishu2agents.relay.trigger import requires_calendar_confirmation

    assert requires_calendar_confirmation("Yuanbo Calendar Manager") is True
    assert requires_calendar_confirmation("You World 群聊管理工具") is False
    assert requires_calendar_confirmation("Calendar Manager") is False


def test_always_triggers_agent_starts_new_conversation(tmp_path):
    handler, store, bridge, dispatcher = make_handler(tmp_path)
    posted = []

    def fake_placeholder(mid, text):
        posted.append((mid, text))
        return "out_ph_1"

    handler.post_placeholder = fake_placeholder
    out = handler.handle(context())
    assert out is None  # placeholder already posted; bot posts nothing else
    assert posted == [("om_1", "正在处理，Agent 完成后会回复到这条消息。")]
    assert len(dispatcher.calls) == 1
    call = dispatcher.calls[0]
    # A fresh @ without a quote always creates a brand-new conversation.
    assert call["conversation_key"].startswith("feishu:app:chat_1:")
    assert call["idempotency_key"] == "app:om_1"
    assert bridge.resolve_outbound_message(call["request_id"]) == "out_ph_1"
    pending = bridge.pending()
    assert len(pending) == 1
    assert pending[0]["feishu_message_id"] == "om_1"


def test_unquoted_messages_create_separate_conversations(tmp_path):
    handler, store, bridge, dispatcher = make_handler(tmp_path)
    handler.handle(context("第一条", message_id="om_1"))
    handler.handle(context("第二条", message_id="om_2"))
    assert len(dispatcher.calls) == 2
    assert (
        dispatcher.calls[0]["conversation_key"]
        != dispatcher.calls[1]["conversation_key"]
    )
    # each conversation has exactly one run
    for call in dispatcher.calls:
        run = store.get_run_by_request_id(call["request_id"])
        assert run["conversation_key"] == call["conversation_key"]


def test_quote_continues_same_conversation(tmp_path):
    handler, store, bridge, dispatcher = make_handler(tmp_path)
    handler.handle(context("q1", message_id="om_1"))
    conv_key = dispatcher.calls[0]["conversation_key"]
    # The bot replied; Feishu reports the outbound reply id, which we map back.
    assert bridge.record_reply("msg_reply_1", conv_key)
    # User quotes that bot reply -> same conversation continues.
    handler.handle(context("q2", message_id="om_2", reply_to_message_id="msg_reply_1"))
    assert len(dispatcher.calls) == 2
    assert dispatcher.calls[1]["conversation_key"] == conv_key
    conversation = store.get_conversation_by_key(conv_key)
    runs = store.list_runs_for_conversation(int(conversation["id"]))
    assert len(runs) == 2


def test_quote_of_unknown_message_starts_new_conversation(tmp_path):
    handler, store, bridge, dispatcher = make_handler(tmp_path)
    handler.handle(context("q1", message_id="om_1"))
    conv_key = dispatcher.calls[0]["conversation_key"]
    # Quote a message we never replied to (unknown) -> new conversation.
    handler.handle(context("q2", message_id="om_2", reply_to_message_id="unknown_msg"))
    assert len(dispatcher.calls) == 2
    assert dispatcher.calls[1]["conversation_key"] != conv_key


def test_duplicate_message_is_ignored(tmp_path):
    handler, store, bridge, dispatcher = make_handler(tmp_path)
    assert handler.handle(context("文案")) is None  # first call accepted (None disables ack)
    assert handler.handle(context("文案")) is None  # same message, no duplicate run
    assert len(dispatcher.calls) == 1
    assert len(bridge.pending()) == 1
