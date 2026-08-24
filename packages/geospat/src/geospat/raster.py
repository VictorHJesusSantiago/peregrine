"""Raster grids: a `numpy` array plus enough georeferencing metadata to place it in space.

`Raster` is a plain mutable class, not a frozen dataclass — the whole point of a raster is that
its backing array is worked on in place (resampled, filtered), so pretending it is an immutable
value type would be dishonest about what it actually is.

The on-disk format is `numpy.savez` (a zip of `.npy` arrays) with the georeferencing packed in
alongside the pixel data — a real, working, round-trippable serialization, but a "custom format
built on numpy's own serializer," explicitly not a GeoTIFF or any other standard raster format.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from geospat.errors import RasterFormatError


class Raster:
    def __init__(
        self,
        data: NDArray[Any],
        origin_x: float,
        origin_y: float,
        cell_size_x: float,
        cell_size_y: float,
        crs: str = "EPSG:4326",
    ) -> None:
        if data.ndim not in (2, 3):
            raise RasterFormatError(f"raster data must be 2D (single-band) or 3D (multi-band), got {data.ndim}D")
        if cell_size_x <= 0 or cell_size_y <= 0:
            raise RasterFormatError("cell sizes must be positive")
        self.data = data
        self.origin_x = origin_x
        self.origin_y = origin_y  # the northwest corner's y coordinate; rows increase southward
        self.cell_size_x = cell_size_x
        self.cell_size_y = cell_size_y
        self.crs = crs

    @property
    def shape(self) -> tuple[int, ...]:
        return self.data.shape

    @property
    def n_rows(self) -> int:
        return int(self.data.shape[-2])

    @property
    def n_cols(self) -> int:
        return int(self.data.shape[-1])

    def band(self, index: int) -> Raster:
        """Returns a single 2D band of a multi-band raster as its own `Raster`."""
        if self.data.ndim != 3:
            raise RasterFormatError("band() only applies to a 3D multi-band raster")
        return Raster(self.data[index], self.origin_x, self.origin_y, self.cell_size_x, self.cell_size_y, self.crs)

    # -- I/O ----------------------------------------------------------------------------------

    def save(self, path: str | Path) -> None:
        np.savez(
            path,
            data=self.data,
            origin=np.array([self.origin_x, self.origin_y]),
            cell_size=np.array([self.cell_size_x, self.cell_size_y]),
            crs=np.array(self.crs),
        )

    @classmethod
    def load(cls, path: str | Path) -> Raster:
        # np.savez appends .npz if the given path doesn't already end with it; np.load needs the
        # real on-disk name to match.
        actual_path = Path(path)
        if not actual_path.exists() and not str(actual_path).endswith(".npz"):
            actual_path = Path(f"{path}.npz")
        try:
            with np.load(actual_path) as npz:
                return cls(
                    data=npz["data"],
                    origin_x=float(npz["origin"][0]),
                    origin_y=float(npz["origin"][1]),
                    cell_size_x=float(npz["cell_size"][0]),
                    cell_size_y=float(npz["cell_size"][1]),
                    crs=str(npz["crs"]),
                )
        except (OSError, KeyError) as exc:
            raise RasterFormatError(f"could not load raster from {path}: {exc}") from exc

    # -- resampling -----------------------------------------------------------------------------

    def resample(self, out_rows: int, out_cols: int, method: str = "nearest") -> Raster:
        """Resamples a 2D single-band raster to a new `(out_rows, out_cols)` shape, keeping the
        same ground extent (so cell size changes instead)."""
        if self.data.ndim != 2:
            raise RasterFormatError("resample() only supports a 2D single-band raster")
        if out_rows < 1 or out_cols < 1:
            raise RasterFormatError("output shape must be at least 1x1")

        extent_x = self.n_cols * self.cell_size_x
        extent_y = self.n_rows * self.cell_size_y
        new_cell_x = extent_x / out_cols
        new_cell_y = extent_y / out_rows

        # Sample each output cell at its center, mapped back into input pixel-space coordinates.
        out_x = (np.arange(out_cols) + 0.5) * new_cell_x / self.cell_size_x - 0.5
        out_y = (np.arange(out_rows) + 0.5) * new_cell_y / self.cell_size_y - 0.5

        match method:
            case "nearest":
                src_x = np.clip(np.round(out_x).astype(int), 0, self.n_cols - 1)
                src_y = np.clip(np.round(out_y).astype(int), 0, self.n_rows - 1)
                new_data = self.data[np.ix_(src_y, src_x)]
            case "bilinear":
                new_data = self._bilinear_sample(out_x, out_y)
            case _:
                raise RasterFormatError(f"unknown resampling method {method!r}")

        return Raster(new_data, self.origin_x, self.origin_y, new_cell_x, new_cell_y, self.crs)

    def _bilinear_sample(self, out_x: NDArray[Any], out_y: NDArray[Any]) -> NDArray[np.float64]:
        x0 = np.clip(np.floor(out_x).astype(int), 0, self.n_cols - 1)
        x1 = np.clip(x0 + 1, 0, self.n_cols - 1)
        y0 = np.clip(np.floor(out_y).astype(int), 0, self.n_rows - 1)
        y1 = np.clip(y0 + 1, 0, self.n_rows - 1)
        wx = np.clip(out_x - x0, 0.0, 1.0)
        wy = np.clip(out_y - y0, 0.0, 1.0)

        data = self.data.astype(float)
        top = data[np.ix_(y0, x0)] * (1 - wx)[None, :] + data[np.ix_(y0, x1)] * wx[None, :]
        bottom = data[np.ix_(y1, x0)] * (1 - wx)[None, :] + data[np.ix_(y1, x1)] * wx[None, :]
        result: NDArray[np.float64] = top * (1 - wy)[:, None] + bottom * wy[:, None]
        return result

    # -- analytical operations ------------------------------------------------------------------

    def slope(self) -> NDArray[np.float64]:
        """Slope in degrees at every cell of a 2D elevation raster, via central-difference
        gradients (`numpy.gradient`) scaled to real ground units by cell size. Edge cells fall
        back to a one-sided difference, which is `numpy.gradient`'s own documented edge behavior."""
        if self.data.ndim != 2:
            raise RasterFormatError("slope() only supports a 2D single-band elevation raster")
        elevation = self.data.astype(float)
        dz_dy, dz_dx = np.gradient(elevation, self.cell_size_y, self.cell_size_x)
        slope_deg: NDArray[np.float64] = np.degrees(np.arctan(np.hypot(dz_dx, dz_dy)))
        return slope_deg

    def aspect(self) -> NDArray[np.float64]:
        """Downslope-facing direction in degrees clockwise from north (0 = north, 90 = east), the
        standard GIS aspect convention. Flat cells (zero gradient in both directions) are reported
        as `-1`, also standard-convention, since "direction of a slope that doesn't exist" has no
        real answer."""
        if self.data.ndim != 2:
            raise RasterFormatError("aspect() only supports a 2D single-band elevation raster")
        elevation = self.data.astype(float)
        dz_dy, dz_dx = np.gradient(elevation, self.cell_size_y, self.cell_size_x)
        aspect_rad = np.arctan2(dz_dy, -dz_dx)
        aspect_deg = (90.0 - np.degrees(aspect_rad)) % 360.0
        flat = (dz_dx == 0) & (dz_dy == 0)
        return np.where(flat, -1.0, aspect_deg)

    def focal_mean(self, window: int = 3) -> NDArray[np.float64]:
        """A `window x window` mean filter (edge cells replicate the border pixel outward, i.e.
        'edge' padding) computed by summing `window^2` shifted views of the padded array rather
        than looping per-pixel — the same total arithmetic, done as vectorized numpy ops."""
        if self.data.ndim != 2:
            raise RasterFormatError("focal_mean() only supports a 2D single-band raster")
        if window < 1 or window % 2 == 0:
            raise RasterFormatError("window must be a positive odd integer")
        radius = window // 2
        padded = np.pad(self.data.astype(float), radius, mode="edge")
        total = np.zeros_like(self.data, dtype=float)
        for dy in range(window):
            for dx in range(window):
                total += padded[dy : dy + self.n_rows, dx : dx + self.n_cols]
        return total / (window * window)


def elevation_grid(values: list[list[float]], cell_size: float = 1.0, origin: tuple[float, float] = (0.0, 0.0)) -> Raster:
    """Convenience constructor for a small synthetic elevation raster from a nested list, mainly
    useful for tests and examples where hand-computing expected slope/aspect values is easiest
    against literal numbers."""
    array = np.array(values, dtype=float)
    return Raster(array, origin[0], origin[1], cell_size, cell_size)
