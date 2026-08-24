"""Every subsystem in this package raises its own narrow error type rather than a single generic
`GeospatError(message)`, so a caller can catch exactly the failure mode it knows how to handle
(e.g. `except RasterFormatError` around a file load) without swallowing unrelated bugs.
"""

from __future__ import annotations


class GeospatError(Exception):
    """Base for every error raised by this package. Never raised directly."""


class InvalidGeometryError(GeospatError):
    """A geometry value failed a structural invariant (e.g. a bounding box with min > max, or a
    ring with fewer than three points)."""


class SpatialIndexError(GeospatError):
    """Not named `IndexError` — that shadows the builtin, and shadowing it would make an
    unrelated `except IndexError` (e.g. around plain list indexing) accidentally catch this too."""


class TileError(GeospatError):
    """A vector tile could not be generated (e.g. an invalid z/x/y coordinate)."""


class RasterFormatError(GeospatError):
    """A raster could not be read, written, or operated on (bad shape, corrupt file, ...)."""


class RoutingError(GeospatError):
    """A routing graph query failed (e.g. unknown node, or no path between source and target)."""


class QueryError(GeospatError):
    """A spatial query DSL expression was malformed or could not be executed."""
