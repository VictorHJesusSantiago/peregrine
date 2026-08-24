"""Visualization data export: trajectories and network topology, in serializable form.

This module produces structured, JSON-serializable data (and a tidy
`pandas.DataFrame`) suitable for feeding a downstream 3D visualization tool.
Building the 3D renderer itself is out of scope for a Python backend package
-- that belongs in a JS/WebGL frontend. What this module guarantees is that
the *data* is genuinely correct and complete: every agent's position is
recoverable at any sampled time across the full simulated run.

A `matplotlib`-based 2D static plot is included as a clearly optional
convenience for local debugging -- not a substitute for the structured
export, which is the actual required deliverable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pandas as pd

from digitaltwin.network import RoadNetwork
from digitaltwin.scenario import SimulationResult
from digitaltwin.simulation import TrajectorySegment

if TYPE_CHECKING:
    from matplotlib.axes import Axes


@dataclass(frozen=True, slots=True)
class TrajectoryPoint:
    """One sampled (agent, time, position) row."""

    agent_id: str
    t: float
    x: float
    y: float


def sample_trajectories(result: SimulationResult, dt: float) -> list[TrajectoryPoint]:
    """Sample every agent's continuous position at a fixed time step `dt`.

    Produces dense `(agent_id, t, x, y)` rows -- suitable for a frame-by-frame
    replay in a 3D visualization frontend. Positions between simulation
    events are recovered analytically from each `TrajectorySegment`'s
    kinematic profile (`digitaltwin.simulation.TrajectorySegment.position_at`);
    this fixed-`dt` sampling is purely a presentation choice for downstream
    consumers, not how the simulation itself advanced (that was event-driven).
    Each agent's final position is always included exactly, even if it falls
    between two `dt` samples.
    """
    if dt <= 0:
        raise ValueError("dt must be > 0")

    segments_by_agent: dict[str, list[TrajectorySegment]] = {}
    for segment in result.trajectory_log:
        segments_by_agent.setdefault(segment.agent_id, []).append(segment)

    points: list[TrajectoryPoint] = []
    for agent_id, unordered in segments_by_agent.items():
        segments = sorted(unordered, key=lambda s: s.depart_time)
        start_time = segments[0].depart_time
        end_time = segments[-1].arrival_time

        t = start_time
        seg_idx = 0
        while t <= end_time:
            while seg_idx < len(segments) - 1 and t > segments[seg_idx].arrival_time:
                seg_idx += 1
            x, y = segments[seg_idx].position_at(t)
            points.append(TrajectoryPoint(agent_id=agent_id, t=t, x=x, y=y))
            t += dt

        x, y = segments[-1].position_at(end_time)
        points.append(TrajectoryPoint(agent_id=agent_id, t=end_time, x=x, y=y))

    return points


def trajectories_to_dataframe(points: list[TrajectoryPoint]) -> pd.DataFrame:
    """Flatten trajectory points into a tidy `(agent_id, t, x, y)` DataFrame."""
    return pd.DataFrame(
        {
            "agent_id": [p.agent_id for p in points],
            "t": [p.t for p in points],
            "x": [p.x for p in points],
            "y": [p.y for p in points],
        }
    )


def export_network(network: RoadNetwork) -> dict[str, Any]:
    """A JSON-serializable description of the road network's nodes and edges."""
    return {
        "nodes": [{"id": n.id, "x": n.x, "y": n.y} for n in network.nodes()],
        "edges": [
            {
                "u": e.u,
                "v": e.v,
                "length": e.length,
                "speed_limit": e.speed_limit,
                "congestion_factor": e.congestion_factor,
            }
            for e in network.edges()
        ],
    }


def export_trajectories(points: list[TrajectoryPoint]) -> list[dict[str, Any]]:
    """A JSON-serializable list of trajectory point records."""
    return [{"agent_id": p.agent_id, "t": p.t, "x": p.x, "y": p.y} for p in points]


def plot_network_and_trajectories(
    network: RoadNetwork, points: list[TrajectoryPoint], *, ax: Axes | None = None
) -> Axes:
    """Optional 2D convenience plot of the road network plus sampled agent trajectories.

    A bonus, not a substitute for `export_network` / `export_trajectories` --
    those are the actual required visualization-data deliverable. Real 3D
    rendering belongs in a downstream frontend, out of scope here.
    """
    import matplotlib.pyplot as plt

    if ax is None:
        _, ax = plt.subplots()

    for e in network.edges():
        u, v = network.node(e.u), network.node(e.v)
        ax.plot([u.x, v.x], [u.y, v.y], color="lightgray", zorder=1)

    by_agent: dict[str, list[TrajectoryPoint]] = {}
    for p in points:
        by_agent.setdefault(p.agent_id, []).append(p)
    for agent_id, pts in by_agent.items():
        ax.plot(
            [p.x for p in pts],
            [p.y for p in pts],
            marker=".",
            markersize=2,
            label=agent_id,
            zorder=2,
        )

    ax.set_aspect("equal")
    ax.set_xlabel("x (m)")
    ax.set_ylabel("y (m)")
    ax.legend(fontsize="small")
    return ax
