from gpt2feishu.dedupe import DedupeCache


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_duplicate_is_rejected_after_begin_and_complete() -> None:
    cache = DedupeCache()
    assert cache.begin("om_1") is True
    assert cache.begin("om_1") is False
    cache.complete("om_1")
    assert cache.begin("om_1") is False


def test_failure_allows_retry() -> None:
    cache = DedupeCache()
    assert cache.begin("om_1") is True
    cache.fail("om_1")
    assert cache.begin("om_1") is True


def test_expired_entry_allows_retry() -> None:
    clock = Clock()
    cache = DedupeCache(ttl_seconds=10, clock=clock)
    assert cache.begin("om_1") is True
    clock.now = 11
    assert cache.begin("om_1") is True


def test_capacity_is_bounded() -> None:
    cache = DedupeCache(max_entries=2)
    cache.begin("om_1")
    cache.begin("om_2")
    cache.begin("om_3")
    assert len(cache) == 2
    assert cache.begin("om_1") is True
