"""geospat: a geospatial analysis engine — spatial indexes (R-tree, H3), on-the-fly vector tiles,
raster processing, contracted-graph shortest-path routing, and a spatial query DSL.

See each submodule's docstring for what is a faithful from-scratch implementation versus an
honestly-documented simplification (most notably `geospat.hexgrid`, a simplified quadtree analog
to H3, and `geospat.routing`'s contraction hierarchy, which simplifies witness search but not node
ordering, shortcut correctness, or query correctness).
"""

from __future__ import annotations

from geospat.geometry import BBox, Geometry, LineString, Point, Polygon
from geospat.hexgrid import Cell
from geospat.index import Feature, FeatureIndex
from geospat.query import Query
from geospat.raster import Raster
from geospat.routing import ContractionHierarchy, Graph
from geospat.rtree import RTree
from geospat.tiles import TileCoord, VectorTile, generate_tile

__version__ = "0.1.0"

__all__ = [
    "BBox",
    "Cell",
    "ContractionHierarchy",
    "Feature",
    "FeatureIndex",
    "Geometry",
    "Graph",
    "LineString",
    "Point",
    "Polygon",
    "Query",
    "RTree",
    "Raster",
    "TileCoord",
    "VectorTile",
    "__version__",
    "generate_tile",
]
