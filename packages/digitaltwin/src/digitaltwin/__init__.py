"""digitaltwin: an agent-based / discrete-event simulation engine for urban logistics — a fleet
of delivery agents moving through a road network, with physics, automatic parameter calibration
against observed data, a scenario API, and exportable visualization data."""

from __future__ import annotations

from digitaltwin.agents import AgentStatus, DeliveryAgent, RoutePlan
from digitaltwin.calibration import CalibrationResult, calibrate_parameter
from digitaltwin.errors import (
    CalibrationError,
    DigitalTwinError,
    NetworkGenerationError,
    RoutingError,
    SimulationError,
)
from digitaltwin.events import Event, EventKind, EventQueue, SimClock
from digitaltwin.kinematics import SegmentProfile
from digitaltwin.network import (
    Node,
    NodeId,
    RoadEdge,
    RoadNetwork,
    generate_grid_network,
    generate_random_network,
)
from digitaltwin.scenario import AgentResult, Scenario, SimulationResult, run_scenario
from digitaltwin.simulation import Simulation, TrajectorySegment
from digitaltwin.viz import (
    TrajectoryPoint,
    export_network,
    export_trajectories,
    sample_trajectories,
    trajectories_to_dataframe,
)

__version__ = "0.1.0"

__all__ = [
    "AgentResult",
    "AgentStatus",
    "CalibrationError",
    "CalibrationResult",
    "DeliveryAgent",
    "DigitalTwinError",
    "Event",
    "EventKind",
    "EventQueue",
    "NetworkGenerationError",
    "Node",
    "NodeId",
    "RoadEdge",
    "RoadNetwork",
    "RoutePlan",
    "RoutingError",
    "Scenario",
    "SegmentProfile",
    "SimClock",
    "Simulation",
    "SimulationError",
    "SimulationResult",
    "TrajectoryPoint",
    "TrajectorySegment",
    "calibrate_parameter",
    "export_network",
    "export_trajectories",
    "generate_grid_network",
    "generate_random_network",
    "run_scenario",
    "sample_trajectories",
    "trajectories_to_dataframe",
]
