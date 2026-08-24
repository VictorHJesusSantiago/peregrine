from pathlib import Path

import numpy as np
import pytest

from geospat.errors import RasterFormatError
from geospat.raster import Raster, elevation_grid


class TestConstruction:
    def test_rejects_1d_array(self) -> None:
        with pytest.raises(RasterFormatError):
            Raster(np.array([1, 2, 3]), 0, 0, 1, 1)

    def test_rejects_non_positive_cell_size(self) -> None:
        with pytest.raises(RasterFormatError):
            Raster(np.zeros((2, 2)), 0, 0, 0, 1)

    def test_shape_and_dims(self) -> None:
        r = Raster(np.zeros((3, 4)), 0, 0, 1, 1)
        assert r.n_rows == 3
        assert r.n_cols == 4

    def test_band_extracts_a_2d_slice_from_multiband(self) -> None:
        data = np.stack([np.zeros((2, 2)), np.ones((2, 2))])
        r = Raster(data, 0, 0, 1, 1)
        band0 = r.band(0)
        band1 = r.band(1)
        assert np.all(band0.data == 0)
        assert np.all(band1.data == 1)

    def test_band_on_2d_raster_raises(self) -> None:
        r = Raster(np.zeros((2, 2)), 0, 0, 1, 1)
        with pytest.raises(RasterFormatError):
            r.band(0)


class TestRoundTrip:
    def test_save_and_load_preserves_data_and_metadata(self, tmp_path: Path) -> None:
        data = np.arange(12, dtype=float).reshape(3, 4)
        r = Raster(data, origin_x=10.0, origin_y=20.0, cell_size_x=2.5, cell_size_y=3.5, crs="EPSG:3857")
        path = tmp_path / "test_raster"
        r.save(path)
        loaded = Raster.load(path)
        assert np.array_equal(loaded.data, data)
        assert loaded.origin_x == pytest.approx(10.0)
        assert loaded.origin_y == pytest.approx(20.0)
        assert loaded.cell_size_x == pytest.approx(2.5)
        assert loaded.cell_size_y == pytest.approx(3.5)
        assert loaded.crs == "EPSG:3857"

    def test_load_missing_file_raises_raster_format_error(self) -> None:
        with pytest.raises(RasterFormatError):
            Raster.load("does_not_exist_at_all")


class TestResample:
    def test_nearest_neighbor_downsample_2x_averages_correctly_shaped_output(self) -> None:
        # 4x4 raster of 4 distinct 2x2 blocks; downsampling to 2x2 with nearest-neighbor should
        # pick one representative value per block, and the result must be one of that block's
        # values (since nearest-neighbor never blends).
        data = np.array(
            [
                [1, 1, 2, 2],
                [1, 1, 2, 2],
                [3, 3, 4, 4],
                [3, 3, 4, 4],
            ],
            dtype=float,
        )
        r = Raster(data, 0, 0, 1, 1)
        result = r.resample(2, 2, method="nearest")
        assert result.data.shape == (2, 2)
        assert result.data[0, 0] == 1
        assert result.data[0, 1] == 2
        assert result.data[1, 0] == 3
        assert result.data[1, 1] == 4

    def test_upsample_preserves_extent(self) -> None:
        data = np.array([[1.0, 2.0], [3.0, 4.0]])
        r = Raster(data, 0, 0, cell_size_x=5.0, cell_size_y=5.0)
        result = r.resample(4, 4, method="nearest")
        assert result.n_rows == 4
        assert result.n_cols == 4
        assert result.cell_size_x == pytest.approx(2.5)
        assert result.cell_size_y == pytest.approx(2.5)

    def test_bilinear_interpolates_between_known_values(self) -> None:
        # A perfectly linear ramp (column 0 = 0, column 1 = 10): bilinear resampling to 4 output
        # columns samples at pixel-space positions -0.25, 0.25, 0.75, 1.25 (each output column's
        # center, mapped back into input-pixel coordinates where column 0's center is x=0 and
        # column 1's is x=1). The two out-of-range positions clamp to the nearest edge value (0
        # and 10); the two in-range ones interpolate exactly linearly: 0 + 10*0.25 = 2.5 and
        # 0 + 10*0.75 = 7.5.
        data = np.array([[0.0, 10.0], [0.0, 10.0]])
        r = Raster(data, 0, 0, 1, 1)
        result = r.resample(2, 4, method="bilinear")
        expected_row = np.array([0.0, 2.5, 7.5, 10.0])
        assert result.data[0] == pytest.approx(expected_row, abs=1e-9)
        assert result.data[1] == pytest.approx(expected_row, abs=1e-9)

    def test_unknown_method_raises(self) -> None:
        r = Raster(np.zeros((2, 2)), 0, 0, 1, 1)
        with pytest.raises(RasterFormatError):
            r.resample(1, 1, method="bicubic")

    def test_resample_requires_2d(self) -> None:
        r = Raster(np.zeros((2, 2, 2)), 0, 0, 1, 1)
        with pytest.raises(RasterFormatError):
            r.resample(1, 1)


class TestSlopeAndAspect:
    def test_flat_raster_has_zero_slope_everywhere(self) -> None:
        r = elevation_grid([[5.0] * 5 for _ in range(5)], cell_size=1.0)
        assert np.allclose(r.slope(), 0.0)

    def test_uniform_east_facing_ramp_has_hand_computed_slope(self) -> None:
        # Elevation increases by 1 unit per column, cell size 1: dz/dx = 1 everywhere (central
        # difference in the interior), dz/dy = 0. slope = atan(sqrt(1^2 + 0^2)) = 45 degrees.
        data = [[float(col) for col in range(5)] for _ in range(5)]
        r = elevation_grid(data, cell_size=1.0)
        slope = r.slope()
        assert np.allclose(slope[1:-1, 1:-1], 45.0)

    def test_steeper_ramp_has_larger_slope_than_gentler_ramp(self) -> None:
        gentle = elevation_grid([[float(col) for col in range(5)] for _ in range(5)], cell_size=1.0)
        steep = elevation_grid([[float(col) * 4 for col in range(5)] for _ in range(5)], cell_size=1.0)
        assert steep.slope()[2, 2] > gentle.slope()[2, 2]

    def test_aspect_of_flat_cell_is_sentinel(self) -> None:
        r = elevation_grid([[1.0] * 5 for _ in range(5)], cell_size=1.0)
        assert np.all(r.aspect() == -1.0)

    def test_aspect_points_downhill_east_for_a_west_high_east_low_ramp(self) -> None:
        # Elevation decreases eastward -> downhill direction is east -> aspect should be 90.
        data = [[float(4 - col) for col in range(5)] for _ in range(5)]
        r = elevation_grid(data, cell_size=1.0)
        aspect = r.aspect()
        assert aspect[2, 2] == pytest.approx(90.0, abs=1e-6)

    def test_slope_requires_2d(self) -> None:
        r = Raster(np.zeros((2, 2, 2)), 0, 0, 1, 1)
        with pytest.raises(RasterFormatError):
            r.slope()


class TestFocalMean:
    def test_uniform_raster_mean_filter_is_unchanged(self) -> None:
        r = elevation_grid([[3.0] * 6 for _ in range(6)])
        assert np.allclose(r.focal_mean(3), 3.0)

    def test_hand_computed_center_value_3x3(self) -> None:
        data = [
            [1, 2, 3],
            [4, 5, 6],
            [7, 8, 9],
        ]
        r = elevation_grid([[float(v) for v in row] for row in data])
        result = r.focal_mean(3)
        # Center cell's 3x3 neighborhood is the entire grid: mean of 1..9 = 5.
        assert result[1, 1] == pytest.approx(5.0)

    def test_hand_computed_corner_value_uses_edge_replication(self) -> None:
        data = [
            [1, 2],
            [3, 4],
        ]
        r = elevation_grid([[float(v) for v in row] for row in data])
        result = r.focal_mean(3)
        # Top-left corner's 3x3 neighborhood with 'edge' padding replicates row/col 0 outward:
        #   1 1 2
        #   1 1 2
        #   3 3 4
        # mean = (1+1+2+1+1+2+3+3+4)/9 = 18/9 = 2.0
        assert result[0, 0] == pytest.approx(2.0)

    def test_rejects_even_window(self) -> None:
        r = elevation_grid([[1.0] * 4 for _ in range(4)])
        with pytest.raises(RasterFormatError):
            r.focal_mean(4)
