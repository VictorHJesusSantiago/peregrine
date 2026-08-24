"""Exception hierarchy for the digitaltwin package.

All exceptions inherit from :class:`DigitalTwinError`, so callers who want to
catch "anything this package can raise" have a single type to reach for.
"""

from __future__ import annotations


class DigitalTwinError(Exception):
    """Base class for all errors raised by digitaltwin."""


class NetworkGenerationError(DigitalTwinError):
    """Raised when a synthetic road network cannot be generated as requested."""


class RoutingError(DigitalTwinError):
    """Raised when no route exists between two nodes, or a route is malformed."""


class SimulationError(DigitalTwinError):
    """Raised when the discrete-event simulation reaches an inconsistent state."""


class CalibrationError(DigitalTwinError):
    """Raised when parameter calibration fails to converge or is misconfigured."""
