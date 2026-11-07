"""Physics sanity tests for the plant: does the simulation agree with theory?"""

import math

import pytest

from plant import (MotorPlant, PlantInputs, PlantParams, SensorFault,
                   steady_state_speed, steady_state_temps)

DT = 0.01  # controller tick


def run(plant, inputs, seconds):
    out = None
    for _ in range(round(seconds / DT)):
        out = plant.step(inputs, DT)
    return out


def test_rest_state_is_stable():
    plant = MotorPlant()
    out = run(plant, PlantInputs(), 5.0)
    assert abs(out.omega) < 1e-6
    assert out.theta_winding == pytest.approx(25.0, abs=1e-6)


def test_actuator_reaches_63_percent_after_one_time_constant():
    p = PlantParams(J=1e6)  # huge inertia so speed effects are negligible
    plant = MotorPlant(p)
    plant.step(PlantInputs(torque_cmd=100.0), p.tau_e)
    assert plant.t_m == pytest.approx(100 * (1 - math.exp(-1)), rel=1e-6)


def test_torque_saturates_at_drive_limit():
    plant = MotorPlant()
    out = run(plant, PlantInputs(torque_cmd=1000.0), 0.2)
    assert out.torque_actual == pytest.approx(plant.params.t_max, rel=1e-3)


def test_steady_state_speed_matches_analytic():
    plant = MotorPlant()
    out = run(plant, PlantInputs(torque_cmd=100.0), 60.0)
    expected = steady_state_speed(plant.params, 100.0)
    assert out.omega == pytest.approx(expected, rel=1e-3)


def test_coasts_down_to_rest_without_torque():
    plant = MotorPlant()
    run(plant, PlantInputs(torque_cmd=100.0), 30.0)
    out = run(plant, PlantInputs(torque_cmd=0.0), 120.0)
    assert abs(out.omega) < 0.05


def test_thermal_steady_state_matches_analytic():
    p = PlantParams(c_w=30.0, c_h=150.0)  # shrink capacities 100x so the test is fast
    plant = MotorPlant(p)
    out = run(plant, PlantInputs(torque_cmd=150.0), 120.0)
    exp_w, exp_h = steady_state_temps(p, out.torque_actual, out.omega)
    assert out.theta_winding == pytest.approx(exp_w, abs=0.1)
    assert out.theta_housing == pytest.approx(exp_h, abs=0.1)


def test_hot_winding_increases_copper_loss():
    plant = MotorPlant()
    cold, _ = plant.losses(200.0, 0.0, 25.0)
    hot, _ = plant.losses(200.0, 0.0, 150.0)
    assert hot / cold == pytest.approx(
        (1 + 0.00393 * 130) / (1 + 0.00393 * 5), rel=1e-6)


def test_temperature_sensor_lags_winding():
    plant = MotorPlant(PlantParams(temp_noise_std=0.0))
    out = run(plant, PlantInputs(torque_cmd=250.0), 20.0)
    assert out.temp_meas < out.theta_winding


def test_same_seed_gives_identical_runs():
    a, b = MotorPlant(seed=42), MotorPlant(seed=42)
    u = PlantInputs(torque_cmd=120.0)
    assert run(a, u, 5.0) == run(b, u, 5.0)


@pytest.mark.parametrize("mode,value,check", [
    ("stuck", 0.0, lambda o1, o2: o1.temp_meas == o2.temp_meas),
    ("offset", 15.0, lambda o1, o2: o2.temp_meas - o2.theta_winding > 10.0),
    ("dropout", 0.0, lambda o1, o2: not o2.temp_valid),
])
def test_temperature_sensor_faults(mode, value, check):
    plant = MotorPlant(PlantParams(tau_temp_sensor=0.01, temp_noise_std=0.0))
    u = PlantInputs(torque_cmd=250.0)
    run(plant, u, 2.0)
    plant.set_fault("temp", SensorFault(mode, value))
    o1 = plant.step(u, DT)
    o2 = run(plant, u, 10.0)
    assert check(o1, o2)
