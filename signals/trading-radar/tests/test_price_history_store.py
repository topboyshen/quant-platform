"""价格历史存储（内存实现）。"""


from radar.data.price_history_store import InMemoryPriceStore


def test_in_memory_append_and_tail():
    s = InMemoryPriceStore(max_history=5)
    for i in range(10):
        s.append("000001", float(i), float(i * 100))
    hist = s.get_history("000001", n=100)
    assert len(hist) == 5
    assert hist[-1][1] == 9.0
    series = s.get_price_series("000001", n=3)
    assert series == [7.0, 8.0, 9.0]


def test_clear_one_code():
    s = InMemoryPriceStore()
    s.append("A", 1.0, 0)
    s.append("B", 2.0, 0)
    s.clear("A")
    assert s.get_history("A") == []
    assert len(s.get_history("B")) == 1
