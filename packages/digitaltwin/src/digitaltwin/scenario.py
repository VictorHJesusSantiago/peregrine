"""The public scenario API: define a simulation and run it.

This is the package's main entry point. Build a `Scenario` (road network +
fleet size + demand pattern + physical/calibration parameters), pass it to
`run_scenario`, and get back a `SimulationResult` with aggregate metrics,
per-agent detail, and the full trajectory log for downstream visualization.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from digitaltwin.agents import DeliveryAgent, build_route_plan, clarke_wright_routes, nearest_neighbor_order
from digitaltwin.errors import SimulationError
from digitaltwin.network import NodeId, RoadNetwork
from digitaltwin.simulation import Simulation, TrajectorySegment

RoutingStrategy = Literal["nearest_neighbor", "clarke_wright"]


@dataclass(frozen=True, slots=True)
class Scenario:
    """A complete, immutable definition of a simulation run.

    Attributes
    ----------
    network:
        The road network agents travel over.
    depot:
        The node id every agent starts from.
    demand_points:
        Node ids that need a delivery. How they are split across the fleet
        and ordered within each agent's route is controlled by
        `routing_strategy`.
    fleet_size:
        Number of delivery agents.
    routing_strategy:
        How `demand_points` are grouped into routes and ordered within each
        route:

        - `"nearest_neighbor"` (the default, and the only behavior before
          this field existed): demand points are split across the fleet
          round robin, then ordered within each agent via the
          nearest-neighbor heuristic -- see
          `digitaltwin.agents.nearest_neighbor_order`. Assignment to
          vehicles is not distance-aware at all.
        - `"clarke_wright"`: demand points are grouped *and* ordered in one
          step by the Clarke-Wright savings heuristic -- see
          `digitaltwin.agents.clarke_wright_routes` -- a genuine (if still
          heuristic) multi-vehicle VRP solver, unlike the round-robin split
          above. Produces at most `fleet_size` routes; if the savings merges
          settle on fewer routes than `fleet_size`, the extra agents are
          simply idle (same as when `nearest_neighbor` gets more agents than
          demand points). If the merges need *more* routes than
          `fleet_size` provides (only possible when `vehicle_capacity`
          caps how much a single route can carry), `run_scenario` raises
          `SimulationError`.
    vehicle_capacity:
        Only used when `routing_strategy == "clarke_wright"`. Caps the
        number of stops (not a weighted demand quantity -- this package has
        no per-point delivery weight) a single Clarke-Wright route may
        carry. `None` (the default) means uncapacitated: merge as
        aggressively as the savings heuristic allows.
    base_speed:
        Each agent's own top speed in m/s, before any road speed limit or
        congestion factor caps it further.
    speed_factor:
        A multiplier on `base_speed` -- the primary knob automatic
        calibration (see `digitaltwin.calibration`) is expected to tune.
    acceleration:
        Constant acceleration/deceleration magnitude, in m/s^2.
    dwell_time:
        Seconds an agent spends stationary at each delivery stop.
    seed:
        Reserved for scenario-level randomness (e.g. if demand points are
        drawn randomly by the caller before constructing the `Scenario`).
    """

    network: RoadNetwork
    depot: NodeId
    demand_points: tuple[NodeId, ...]
    fleet_size: int = 1
    routing_strategy: RoutingStrategy = "nearest_neighbor"
    vehicle_capacity: int | None = None
    base_speed: float = 13.4  # m/s, roughly 30 mph
    speed_factor: float = 1.0
    acceleration: float = 2.0  # m/s^2, a comfortable car acceleration
    dwell_time: float = 60.0  # seconds
    seed: int = 0

    def __post_init__(self) -> None:
        if self.fleet_size < 1:
            raise SimulationError("fleet_size must be >= 1")
        if not self.demand_points:
            raise SimulationError("scenario needs at least one demand point")
        if self.base_speed <= 0 or self.speed_factor <= 0:
            raise SimulationError("base_speed and speed_factor must be > 0")
        if self.routing_strategy not in ("nearest_neighbor", "clarke_wright"):
            raise SimulationError(f"unknown routing_strategy: {self.routing_strategy!r}")
        if self.vehicle_capacity is not None and self.vehicle_capacity < 1:
            raise SimulationError("vehicle_capacity must be >= 1 when set")

    @property
    def max_speed(self) -> float:
        """Effective top speed each agent's vehicle can reach: base_speed * speed_factor."""
        return self.base_speed * self.speed_factor


@dataclass(frozen=True, slots=True)
class AgentResult:
    """Per-agent summary metrics from a completed scenario run."""

    agent_id: str
    stops: tuple[NodeId, ...]
    distance_traveled: float
    delivery_time: float
    moving_time: float


@dataclass(frozen=True, slots=True)
class SimulationResult:
    """Structured output of a scenario run.

    `trajectory_log` holds every road-segment traversal and delivery dwell
    for every agent, in the order the simulation produced them -- feed it to
    `digitaltwin.viz.sample_trajectories` to get dense (agent, t, x, y) rows
    for a downstream visualization tool.
    """

    total_distance: float
    average_delivery_time: float
    makespan: float
    utilization: float
    agent_results: tuple[AgentResult, ...]
    trajectory_log: tuple[TrajectorySegment, ...]
    network: RoadNetwork


def _assign_demand_points(demand_points: Sequence[NodeId], fleet_size: int) -> list[list[NodeId]]:
    """Round-robin split of demand points across the fleet.

    A dispatch-optimal assignment (balancing load by distance/time, or a
    proper VRP) is out of scope; this keeps assignment simple and
    deterministic. Routing *within* an agent's assigned stops still uses the
    nearest-neighbor heuristic in `digitaltwin.agents`.
    """
    buckets: list[list[NodeId]] = [[] for _ in range(fleet_size)]
    for i, point in enumerate(demand_points):
        buckets[i % fleet_size].append(point)
    return buckets


def _clarke_wright_buckets(scenario: Scenario) -> list[list[NodeId]]:
    """Group and order demand points into routes via Clarke-Wright savings, padded to `fleet_size`.

    Each resulting route is already ordered (the merge order the savings
    heuristic settled on), unlike the round-robin buckets from
    `_assign_demand_points` which still need `nearest_neighbor_order` to be
    put in a sensible visiting order. If the merges need fewer routes than
    `fleet_size`, the remainder are padded with empty routes -- those agents
    are simply idle, same as `nearest_neighbor` with a fleet larger than the
    demand. If they need *more* routes than `fleet_size` (only possible with
    a `vehicle_capacity` cap), that is a genuine fleet-sizing problem: raise
    rather than silently dropping stops or violating the capacity cap by
    forcing a merge.
    """
    routes = clarke_wright_routes(
        scenario.network, scenario.depot, scenario.demand_points, vehicle_capacity=scenario.vehicle_capacity
    )
    if len(routes) > scenario.fleet_size:
        raise SimulationError(
            f"clarke_wright routing needs {len(routes)} vehicles to respect vehicle_capacity="
            f"{scenario.vehicle_capacity}, but fleet_size is only {scenario.fleet_size}; "
            "increase fleet_size or vehicle_capacity"
        )
    return [*routes, *([[]] * (scenario.fleet_size - len(routes)))]


def run_scenario(scenario: Scenario) -> SimulationResult:
    """Build agents from a `Scenario` and run the discrete-event simulation to completion.

    Returns a `SimulationResult` with aggregate fleet metrics (total
    distance, average delivery time, makespan, utilization), per-agent
    detail, and the raw trajectory log.
    """
    if scenario.routing_strategy == "clarke_wright":
        stop_orders = _clarke_wright_buckets(scenario)
    else:
        buckets = _assign_demand_points(scenario.demand_points, scenario.fleet_size)
        stop_orders = [nearest_neighbor_order(scenario.network, scenario.depot, stops) for stops in buckets]

    agents: dict[str, DeliveryAgent] = {}
    for i, ordered_stops in enumerate(stop_orders):
        agent_id = f"agent-{i}"
        route = build_route_plan(scenario.network, scenario.depot, ordered_stops)
        agents[agent_id] = DeliveryAgent(
            agent_id=agent_id,
            route=route,
            max_speed=scenario.max_speed,
            acceleration=scenario.acceleration,
            dwell_time=scenario.dwell_time,
        )

    sim = Simulation(scenario.network, agents)
    sim.run()

    agent_results = []
    for agent in agents.values():
        if agent.start_time is None or agent.completion_time is None:
            raise SimulationError(f"agent {agent.agent_id} never completed")
        agent_results.append(
            AgentResult(
                agent_id=agent.agent_id,
                stops=tuple(agent.route.nodes[i] for i in sorted(agent.route.stops)),
                distance_traveled=agent.distance_traveled,
                delivery_time=agent.completion_time - agent.start_time,
                moving_time=agent.moving_time,
            )
        )

    total_distance = sum(r.distance_traveled for r in agent_results)
    average_delivery_time = sum(r.delivery_time for r in agent_results) / len(agent_results)
    makespan = max((r.delivery_time for r in agent_results), default=0.0)
    total_moving_time = sum(r.moving_time for r in agent_results)
    utilization = total_moving_time / (scenario.fleet_size * makespan) if makespan > 0 else 0.0

    return SimulationResult(
        total_distance=total_distance,
        average_delivery_time=average_delivery_time,
        makespan=makespan,
        utilization=utilization,
        agent_results=tuple(agent_results),
        trajectory_log=tuple(sim.trajectory_log),
        network=scenario.network,
    )
