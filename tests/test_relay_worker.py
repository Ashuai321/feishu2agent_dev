from pathlib import Path

from feishu2agents.relay.store.relay_store import RelayStore

from feishu2agents.feishu_bridge import FeishuBridge
from feishu2agents.relay_worker import RelayWorker, format_result_for_feishu

TRIGGER_URL = "https://api.chatgpt.com/v1/workspace_agents/agtch_test/trigger"


def make_store(tmp_path):
    store = RelayStore(Path(tmp_path) / "relay.sqlite")
    store.create_agent(name="default", trigger_url=TRIGGER_URL, access_token="agent-secret")
    return store


def make_run(store, tmp_path, *, message_id="om_1", request_id="req_1", input_text="task"):
    agent_id = store.resolve_default_agent_id()
    conversation = store.create_conversation(
        agent_id=agent_id,
        name="飞书消息",
        conversation_key="feishu:app:chat_1",
    )
    store.create_run(
        agent_id=agent_id,
        conversation_id=conversation["id"],
        conversation_key="feishu:app:chat_1",
        input_markdown=input_text,
        idempotency_key="app:om_1",
        request_id=request_id,
    )
    store.mark_run_trigger_sent(request_id)
    bridge = FeishuBridge(str(tmp_path / "bridge.sqlite"))
    assert bridge.claim(feishu_message_id=message_id, request_id=request_id)
    return bridge


def test_deliver_once_posts_result_to_reply(tmp_path):
    store = make_store(tmp_path)
    bridge = make_run(store, tmp_path)
    store.record_result(
        request_id="req_1",
        conversation_key="feishu:app:chat_1",
        status="done",
        title="完成",
        markdown="明天晴天",
    )
    replies = []

    def fake_reply(mid, text):
        replies.append((mid, text))
        return "msg_out_1"  # outbound reply id reported by Feishu

    worker = RelayWorker(store, bridge, fake_reply)
    assert worker.deliver_once() == 1
    assert replies == [("om_1", "完成\n明天晴天")]
    # The delivered bot reply is mapped back to its conversation for quoting.
    assert bridge.resolve_reply_conversation("msg_out_1") == "feishu:app:chat_1"
    assert worker.deliver_once() == 0


def test_deliver_once_edits_placeholder_in_place(tmp_path):
    store = make_store(tmp_path)
    bridge = make_run(store, tmp_path)
    store.record_result(
        request_id="req_1",
        conversation_key="feishu:app:chat_1",
        status="done",
        title="完成",
        markdown="明天晴天",
    )
    # The handler already posted a "processing" placeholder for this run.
    bridge.record_outbound_message("req_1", "ph_1")
    updated = []
    replies = []

    def fake_update(mid, text):
        updated.append((mid, text))

    def fake_reply(mid, text):
        replies.append((mid, text))
        return "out_reply_1"

    worker = RelayWorker(store, bridge, fake_reply, update_message=fake_update)
    assert worker.deliver_once() == 1
    # The placeholder message is overwritten in place; no extra reply is posted.
    assert updated == [("ph_1", "完成\n明天晴天")]
    assert replies == []
    # The edited placeholder becomes quotable for conversation continuity.
    assert bridge.resolve_reply_conversation("ph_1") == "feishu:app:chat_1"


def test_deliver_once_skips_non_terminal_runs(tmp_path):
    store = make_store(tmp_path)
    # Run dispatched but the agent has not called record_result yet.
    bridge = make_run(store, tmp_path)
    replies = []
    worker = RelayWorker(store, bridge, lambda mid, text: replies.append((mid, text)))
    assert worker.deliver_once() == 0
    assert replies == []


def test_format_result_for_feishu_styles_statuses(tmp_path):
    store = make_store(tmp_path)
    agent_id = store.resolve_default_agent_id()
    conversation = store.create_conversation(
        agent_id=agent_id, name="t", conversation_key="feishu:app:chat_1"
    )
    for index, status in enumerate(("done", "blocked", "failed")):
        request_id = f"req_{index}"
        store.create_run(
            agent_id=agent_id,
            conversation_id=conversation["id"],
            conversation_key="feishu:app:chat_1",
            input_markdown="task",
            idempotency_key=f"app:om_{index}",
            request_id=request_id,
        )
        store.mark_run_trigger_sent(request_id)
        store.record_result(
            request_id=request_id,
            conversation_key="feishu:app:chat_1",
            status=status,
            title="标题",
            markdown="正文",
        )
        run = store.get_run_by_request_id(request_id)
        text = format_result_for_feishu(store, run)
        if status == "done":
            assert "正文" in text
        else:
            assert {"blocked": "阻塞", "failed": "失败"}[status] in text