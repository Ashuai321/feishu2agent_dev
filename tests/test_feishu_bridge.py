from feishu2agents.feishu_bridge import FeishuBridge


def make_bridge(tmp_path):
    return FeishuBridge(tmp_path / "bridge.sqlite")


def test_claim_first_returns_true_and_pending(tmp_path):
    bridge = make_bridge(tmp_path)
    assert bridge.claim(feishu_message_id="om_1", request_id="req_1") is True
    assert bridge.claim(feishu_message_id="om_2", request_id="req_2") is True
    pending = bridge.pending()
    assert len(pending) == 2
    assert {row["feishu_message_id"] for row in pending} == {"om_1", "om_2"}


def test_claim_duplicate_message_or_request_is_rejected(tmp_path):
    bridge = make_bridge(tmp_path)
    assert bridge.claim(feishu_message_id="om_1", request_id="req_1") is True
    # Same message, different run: second instance must lose.
    assert bridge.claim(feishu_message_id="om_1", request_id="req_2") is False
    # Same run request_id must stay unique.
    assert bridge.claim(feishu_message_id="om_9", request_id="req_1") is False


def test_outbound_placeholder_message_roundtrip(tmp_path):
    bridge = make_bridge(tmp_path)
    bridge.claim(feishu_message_id="om_1", request_id="req_1")
    assert bridge.resolve_outbound_message("req_1") is None
    bridge.record_outbound_message("req_1", "ph_1")
    assert bridge.resolve_outbound_message("req_1") == "ph_1"
    pending = bridge.pending()
    assert pending[0]["outbound_message_id"] == "ph_1"


def test_reply_conversation_mapping(tmp_path):
    bridge = make_bridge(tmp_path)
    assert bridge.resolve_reply_conversation("no_such_msg") is None
    assert bridge.record_reply("out_1", "feishu:app:chat_1:x") is True
    assert bridge.resolve_reply_conversation("out_1") == "feishu:app:chat_1:x"
    # duplicate mapping is ignored, first wins
    assert bridge.record_reply("out_1", "feishu:app:chat_1:y") is False
    assert bridge.resolve_reply_conversation("out_1") == "feishu:app:chat_1:x"


def test_mark_delivered_is_atomic(tmp_path):
    bridge = make_bridge(tmp_path)
    bridge.claim(feishu_message_id="om_1", request_id="req_1")
    # Only the first 0->1 transition wins.
    assert bridge.mark_delivered("req_1") is True
    assert bridge.pending() == []
    assert bridge.mark_delivered("req_1") is False  # already delivered
    assert bridge.mark_delivered("not-there") is False