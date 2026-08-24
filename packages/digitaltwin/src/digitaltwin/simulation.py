"""The discrete-event simulation engine tying network, agents, and events together.

Agents' departures and arrivals are modeled as discrete events processed
strictly in time order by `EventQueue` -- there is no fixed-timestep polling
loop in the scheduling core itself. Between events, an agent's exact
position is recoverable analytically from the `SegmentProfile` recorded for
that leg of the journey (see `TrajectorySegment.position_at`), so dense,
continuous trajectories for visualization fall out of the event log without
the engine ever needing to step at a fixed dt internally.
"""

from __future__ import annotations

from dataclasses import dataclass

from digitaltwin.agents import AgentStatus, DeliveryAgent
from digitaltwin.errors import SimulationError
from digitaltwin.events import Event, EventKind, EventQueue, SimClock
from digitaltwin.kinematics import SegmentProfile
from digitaltwin.network import NodeId, RoadNetwork


@dataclass(frozen=True, slots=True)
class TrajectorySegment:
    """One leg of an agent's journey: travel along a road segment, or a stationary dwell.

    `profile` is `None` for a dwell (the agent is parked at `from_node ==
    to_node` making a delivery) and a real `SegmentProfile` for road travel.
    """

    agent_id: str
    from_node: NodeId
    to_node: NodeId
    from_xy: tuple[float, float]
    to_xy: tuple[float, float]
    depart_time: float
    arrival_time: float
    profile: SegmentProfile | None

    def position_at(self, t: float) -> tuple[float, float]:
        """Interpolated (x, y) position at absolute simulation time `t`."""
        if self.profile is None or self.profile.length == 0:
            return self.from_xy
        elapsed = t - self.depart_time
        frac = self.profile.distance_at(elapsed) / self.profile.length
        x0, y0 = self.from_xy
        x1, y1 = self.to_xy
        return (x0 + frac * (x1 - x0), y0 + frac * (y1 - y0))


class Simulation:
    """Event-driven discrete-event simulation engine for a fleet of delivery agents."""

    def __init__(self, network: RoadNetwork, agents: dict[str, DeliveryAgent]) -> None:
        self.network = network
        self.agents = agents
        self.clock = SimClock()
        self.queue = EventQueue()
        self.trajectory_log: list[TrajectorySegment] = []
        self._ran = False

    def run(self) -> None:
        """Run the simulation to completion (until the event queue drains)."""
        if self._ran:
            raise SimulationError("Simulation.run() may only be called once per instance")
        self._ran = True

        for agent in self.agents.values():
            if agent.is_finished:
                # No stops assigned: nothing to simulate, agent is trivially done.
                agent.status = AgentStatus.COMPLETED
                agent.start_time = 0.0
                agent.completion_time = 0.0
                continue
            self.queue.schedule(0.0, Event(kind=EventKind.DEPART, agent_id=agent.agent_id))

        while self.queue:
            time, event = self.queue.pop()
            self.clock.advance_to(time)
            self._handle(event)

    def _handle(self, event: Event) -> None:
        match event.kind:
            case EventKind.DEPART:
                self._handle_depart(event)
            case EventKind.ARRIVE:
                self._handle_arrive(event)

    def _handle_depart(self, event: Event) -> None:
        agent = self.agents[event.agent_id]
        if agent.is_finished:
            # Reached here after dwelling at the final stop (see _handle_arrive):
            # the delivery is done, there is nowhere left to depart to.
            agent.status = AgentStatus.COMPLETED
            agent.completion_time = self.clock.now
            return

        if agent.start_time is None:
            agent.start_time = self.clock.now
        agent.status = AgentStatus.IN_TRANSIT

        from_node = agent.current_node
        to_node = agent.next_node
        edge = self.network.get_edge(from_node, to_node)
        segment_speed = min(edge.effective_speed_limit, agent.max_speed)
        profile = SegmentProfile(length=edge.length, max_speed=segment_speed, acceleration=agent.acceleration)
        duration = profile.duration()
        arrival_time = self.clock.now + duration

        from_pt = self.network.node(from_node)
        to_pt = self.network.node(to_node)
        self.trajectory_log.append(
            TrajectorySegment(
                agent_id=agent.agent_id,
                from_node=from_node,
                to_node=to_node,
                from_xy=(from_pt.x, from_pt.y),
                to_xy=(to_pt.x, to_pt.y),
                depart_time=self.clock.now,
                arrival_time=arrival_time,
                profile=profile,
            )
        )
        agent.distance_traveled += edge.length
        agent.moving_time += duration

        self.queue.schedule(
            arrival_time,
            Event(kind=EventKind.ARRIVE, agent_id=agent.agent_id, from_node=from_node, to_node=to_node),
        )

    def _handle_arrive(self, event: Event) -> None:
        agent = self.agents[event.agent_id]
        agent.route_index += 1

        if agent.is_stop(agent.route_index):
            # Dwell to make the delivery -- including at the *final* stop: completion
            # happens only once the dwell is over (see the is_finished check in
            # _handle_depart, which the post-dwell DEPART event below routes into).
            agent.status = AgentStatus.DWELLING
            node = self.network.node(agent.current_node)
            dwell_end = self.clock.now + agent.dwell_time
            self.trajectory_log.append(
                TrajectorySegment(
                    agent_id=agent.agent_id,
                    from_node=agent.current_node,
                    to_node=agent.current_node,
                    from_xy=(node.x, node.y),
                    to_xy=(node.x, node.y),
                    depart_time=self.clock.now,
                    arrival_time=dwell_end,
                    profile=None,
                )
            )
            self.queue.schedule(dwell_end, Event(kind=EventKind.DEPART, agent_id=agent.agent_id))
        elif agent.is_finished:
            # Route ended on a node that wasn't flagged as a stop -- shouldn't happen via
            # build_route_plan (routes always end at a requested stop), but handled for
            # robustness against a directly hand-built RoutePlan.
            agent.status = AgentStatus.COMPLETED
            agent.completion_time = self.clock.now
        else:
            # Pass-through intersection, no delivery here: proceed immediately.
            # Still a discrete event (scheduled at the current time), not a poll.
            self.queue.schedule(self.clock.now, Event(kind=EventKind.DEPART, agent_id=agent.agent_id))
