"""From-scratch discrete-event simulation primitives: an event queue and a clock.

This is the technically load-bearing piece of the package. `EventQueue` is a
real `heapq`-backed binary min-heap priority queue, not a fixed-timestep
polling loop -- the simulation only ever does work when something actually
happens (a departure or an arrival), and jumps the clock directly to the next
event's time no matter how far in the future that is.
"""

from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass
from enum import Enum, auto

from digitaltwin.network import NodeId


class EventKind(Enum):
    DEPART = auto()
    ARRIVE = auto()


@dataclass(frozen=True, slots=True)
class Event:
    """A discrete event: something happening to one agent at a point in time.

    The queue keys entries on (time, insertion order) rather than on `Event`
    itself, so `Event` need not define an ordering -- see `EventQueue`.
    """

    kind: EventKind
    agent_id: str
    from_node: NodeId | None = None
    to_node: NodeId | None = None


class SimClock:
    """The simulation's notion of "now". Monotonic: it only ever moves forward."""

    def __init__(self) -> None:
        self._now = 0.0

    @property
    def now(self) -> float:
        return self._now

    def advance_to(self, t: float) -> None:
        if t < self._now:
            raise ValueError(f"simulation clock cannot move backward: {t} < {self._now}")
        self._now = t


class EventQueue:
    """A real, from-scratch priority queue for discrete-event scheduling.

    Backed by `heapq` (a binary min-heap, O(log n) push/pop). Entries are
    keyed on `(time, insertion_sequence)`: the sequence counter is a
    tie-breaker so two events scheduled for the exact same simulated instant
    are processed in the deterministic order they were scheduled, without
    ever needing `Event` instances to be comparable to each other.
    """

    def __init__(self) -> None:
        self._heap: list[tuple[float, int, Event]] = []
        self._counter = itertools.count()

    def schedule(self, time: float, event: Event) -> None:
        heapq.heappush(self._heap, (time, next(self._counter), event))

    def pop(self) -> tuple[float, Event]:
        if not self._heap:
            raise IndexError("pop from an empty EventQueue")
        time, _, event = heapq.heappop(self._heap)
        return time, event

    def peek_time(self) -> float | None:
        return self._heap[0][0] if self._heap else None

    def __len__(self) -> int:
        return len(self._heap)

    def __bool__(self) -> bool:
        return bool(self._heap)
