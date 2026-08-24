import pytest

from digitaltwin.events import Event, EventKind, EventQueue, SimClock


def _depart(agent_id: str) -> Event:
    return Event(kind=EventKind.DEPART, agent_id=agent_id)


class TestEventQueueOrdering:
    def test_pops_in_ascending_time_order_regardless_of_insertion_order(self) -> None:
        q = EventQueue()
        q.schedule(5.0, _depart("c"))
        q.schedule(1.0, _depart("a"))
        q.schedule(3.0, _depart("b"))

        times_and_ids = []
        while q:
            t, event = q.pop()
            times_and_ids.append((t, event.agent_id))

        assert times_and_ids == [(1.0, "a"), (3.0, "b"), (5.0, "c")]

    def test_ties_broken_by_insertion_order_fifo(self) -> None:
        q = EventQueue()
        q.schedule(2.0, _depart("first"))
        q.schedule(2.0, _depart("second"))
        q.schedule(2.0, _depart("third"))

        order = [q.pop()[1].agent_id for _ in range(3)]
        assert order == ["first", "second", "third"]

    def test_peek_time_does_not_remove(self) -> None:
        q = EventQueue()
        q.schedule(4.0, _depart("a"))
        assert q.peek_time() == pytest.approx(4.0)
        assert len(q) == 1

    def test_peek_time_empty_is_none(self) -> None:
        assert EventQueue().peek_time() is None

    def test_len_and_bool(self) -> None:
        q = EventQueue()
        assert len(q) == 0
        assert not q
        q.schedule(1.0, _depart("a"))
        assert len(q) == 1
        assert q

    def test_pop_empty_raises(self) -> None:
        with pytest.raises(IndexError):
            EventQueue().pop()

    def test_many_events_stay_correctly_ordered(self) -> None:
        # A denser check that the heap invariant holds under many pushes.
        q = EventQueue()
        times = [37.0, 2.0, 19.5, 0.1, 100.0, 4.0, 4.0, 55.5, 3.0]
        for i, t in enumerate(times):
            q.schedule(t, _depart(f"agent-{i}"))

        popped_times = [q.pop()[0] for _ in range(len(times))]
        assert popped_times == sorted(times)


class TestSimClock:
    def test_starts_at_zero(self) -> None:
        assert SimClock().now == 0.0

    def test_advances_forward(self) -> None:
        clock = SimClock()
        clock.advance_to(5.0)
        assert clock.now == 5.0
        clock.advance_to(5.0)  # staying still is fine
        assert clock.now == 5.0
        clock.advance_to(9.5)
        assert clock.now == 9.5

    def test_rejects_moving_backward(self) -> None:
        clock = SimClock()
        clock.advance_to(10.0)
        with pytest.raises(ValueError):
            clock.advance_to(5.0)
