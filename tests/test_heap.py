from hypothesis import given, settings
from hypothesis import strategies as st

from leasejobs.heap import IndexedMinHeap


def test_pops_in_priority_then_insertion_order():
    h = IndexedMinHeap()
    for k, p in [("a", 5), ("b", 1), ("c", 5), ("d", 0), ("e", 1)]:
        h.push(k, p)
    assert [h.pop()[0] for _ in range(5)] == ["d", "b", "e", "a", "c"]


def test_update_and_remove():
    h = IndexedMinHeap()
    for k, p in [("a", 1), ("b", 2), ("c", 3)]:
        h.push(k, p)
    assert h.push("c", 0) is False  # existing key moves
    assert h.remove("a") and not h.remove("a")
    assert h.peek() == ("c", 0) and len(h) == 2 and "a" not in h
    assert list(h.pop_due(2)) == [("c", 0), ("b", 2)] and len(h) == 0


ops = st.lists(st.one_of(
    st.tuples(st.just("push"), st.integers(0, 15), st.integers(-50, 50)),
    st.tuples(st.just("remove"), st.integers(0, 15), st.just(0)),
    st.tuples(st.just("pop"), st.just(0), st.just(0)),
), max_size=200)


@settings(max_examples=300, deadline=None)
@given(ops)
def test_matches_a_dict_model(seq):
    """Differential test: the heap and a plain dict + sort must always agree."""
    h, model, order, n = IndexedMinHeap(), {}, {}, 0
    for op, k, p in seq:
        if op == "push":
            if k not in model:
                n += 1
                order[k] = n
            model[k] = p
            h.push(k, p)
        elif op == "remove":
            assert h.remove(k) == (k in model)
            model.pop(k, None)
            order.pop(k, None)
        elif model:
            want = min(model, key=lambda key: (model[key], order[key]))
            assert h.pop() == (want, model.pop(want))
            order.pop(want)
        assert len(h) == len(model)
        assert h.smallest(3) == sorted(model.items(), key=lambda kv: (kv[1], order[kv[0]]))[:3]
