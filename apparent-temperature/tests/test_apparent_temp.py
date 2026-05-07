"""Tests for apparent temperature calculations."""

import numpy as np
import pytest
from src.apparent_temp import apparent_temperature, heat_index, wind_chill


class TestWindChill:
    def test_cold_moderate_wind(self):
        assert wind_chill(10, 10) == pytest.approx(-4, abs=1)

    def test_zero_with_high_wind(self):
        assert wind_chill(0, 20) == pytest.approx(-22, abs=1)

    def test_freezing_calm_wind(self):
        assert wind_chill(32, 5) == pytest.approx(27, abs=1)

    def test_array_input(self):
        result = wind_chill(np.array([10, 0]), np.array([10, 20]))
        assert result.shape == (2,)
        assert result[0] == pytest.approx(-4, abs=1)
        assert result[1] == pytest.approx(-22, abs=1)


class TestHeatIndex:
    def test_hot_humid(self):
        assert heat_index(90, 70) == pytest.approx(105, abs=1)

    def test_very_hot_moderate_humidity(self):
        assert heat_index(100, 50) == pytest.approx(119, abs=1)

    def test_low_humidity_adjustment(self):
        result = heat_index(100, 10)
        assert result == pytest.approx(95, abs=2)

    def test_below_threshold(self):
        result = heat_index(75, 50)
        assert 70 < result < 80


class TestApparentTemperature:
    def test_cold_windy_uses_wind_chill(self):
        assert apparent_temperature(10, 0, 10) == pytest.approx(-4, abs=1)

    def test_hot_humid_uses_heat_index(self):
        result = apparent_temperature(90, 80, 5)
        assert result > 100

    def test_moderate_returns_temperature(self):
        assert apparent_temperature(60, 50, 5) == pytest.approx(60, abs=1)

    def test_cold_calm_returns_temperature(self):
        assert apparent_temperature(30, 20, 2) == pytest.approx(30, abs=1)

    def test_array_inputs(self):
        T = np.array([10, 90, 60])
        Td = np.array([0, 80, 50])
        V = np.array([10, 5, 5])
        result = apparent_temperature(T, Td, V)
        assert result.shape == (3,)
        assert result[0] == pytest.approx(-4, abs=1)
        assert result[1] > 100
        assert result[2] == pytest.approx(60, abs=1)
