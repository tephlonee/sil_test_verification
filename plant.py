"""
plant.py - Electric motor plant model for the SIL verification rig.

Physics
-------
Torque actuator (inverter + current loop) as a first-order lag with saturation:
    tau_e * dT_m/dt = sat(T_cmd, +-T_max) - T_m

Rotor dynamics:
    J * dw/dt = T_m - T_load - b*w - T_c*tanh(w/w_eps) - k_drag*w*|w|

Losses:
    R(theta_w) = R_20 * (1 + alpha_cu * (theta_w - 20))
    P_cu = (T_m / k_t)^2 * R(theta_w)          copper loss, I = T / k_t
    P_fe = k_fe * w^2                          iron + windage loss

Two-node thermal network (winding -> housing -> ambient):
    C_w * dtheta_w/dt = P_cu + P_fe - (theta_w - theta_h) / R_wh
    C_h * dtheta_h/dt = (theta_w - theta_h) / R_wh - (theta_h - theta_amb) / R_ha

Temperature sensor (thermal contact lag) on the winding:
    tau_s * dtheta_s/dt = theta_w - theta_s

Interface
---------
Mirrors FMI co-simulation (reset / step), so a Simulink or Modelica FMU can
replace this class later without touching the harness or the tests.

The plant integrates internally at <= 1 ms sub-steps while the controller runs
at 10 ms - a continuous plant sampled by a discrete controller, as in reality.
All randomness comes from a seeded RNG, so every run is reproducible.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
# Parameters, inputs, outputs
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PlantParams:
    # Torque actuator (inverter + current loop)
    tau_e: float = 0.005        # s, actuator time constant
    t_max: float = 250.0        # N*m, torque limit of the drive
    # Rotor
    J: float = 0.5              # kg*m^2, rotor + reflected load inertia
    b: float = 0.01             # N*m*s/rad, viscous friction
    t_coulomb: float = 1.0      # N*m, Coulomb friction
    omega_eps: float = 0.5      # rad/s, smoothing width for friction sign
    k_drag: float = 5e-4        # N*m/(rad/s)^2, speed-squared load (fan/aero-like)
    # Losses
    k_t: float = 1.0            # N*m/A, torque constant
    r_20: float = 0.02          # ohm, winding resistance at 20 degC
    alpha_cu: float = 0.00393   # 1/K, copper temperature coefficient
    k_fe: float = 0.003         # W/(rad/s)^2, iron + windage loss coefficient
    # Thermal network
    c_w: float = 3000.0         # J/K, winding heat capacity
    c_h: float = 15000.0        # J/K, housing heat capacity
    r_wh: float = 0.02          # K/W, winding -> housing
    r_ha: float = 0.04          # K/W, housing -> ambient / coolant
    # Sensors
    tau_temp_sensor: float = 2.0  # s
    temp_noise_std: float = 0.2   # K
    speed_noise_std: float = 0.5  # rad/s
    # Numerics
    max_substep: float = 0.001    # s


@dataclass
class PlantInputs:
    torque_cmd: float = 0.0     # N*m, from the controller
    load_torque: float = 0.0    # N*m, external load from the test scenario
    ambient_temp: float = 25.0  # degC


@dataclass
class PlantOutputs:
    time: float
    # What the ECU sees (via CAN)
    speed_meas: float           # rad/s
    speed_valid: bool
    temp_meas: float            # degC
    temp_valid: bool
    # Ground truth, recorded for evidence and analysis only
    omega: float                # rad/s
    torque_actual: float        # N*m
    theta_winding: float        # degC
    theta_housing: float        # degC
    p_loss: float               # W


# --------------------------------------------------------------------------- #
# Fault injection
# --------------------------------------------------------------------------- #

FAULT_MODES = ("none", "stuck", "offset", "dropout", "noisy")


@dataclass
class SensorFault:
    """mode: none | stuck (hold value) | offset (+value) | dropout (invalid) | noisy (x value noise)"""
    mode: str = "none"
    value: float = 0.0
    _held: float | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.mode not in FAULT_MODES:
            raise ValueError(f"Unknown fault mode {self.mode!r}, expected one of {FAULT_MODES}")


# --------------------------------------------------------------------------- #
# Plant
# --------------------------------------------------------------------------- #


class MotorPlant:
    def __init__(self, params: PlantParams | None = None, seed: int = 0) -> None:
        self.params = params or PlantParams()
        self._seed = seed
        self.reset()

    # ---- FMI-style interface ------------------------------------------- #

    def reset(self, params: PlantParams | None = None, ambient_temp: float = 25.0,
              seed: int | None = None) -> PlantOutputs:
        if params is not None:
            self.params = params
        if seed is not None:
            self._seed = seed
        self._rng = random.Random(self._seed)
        self.time = 0.0
        self.t_m = 0.0
        self.omega = 0.0
        self.theta_w = ambient_temp
        self.theta_h = ambient_temp
        self.theta_s = ambient_temp
        self._ambient = ambient_temp
        self._p_loss = 0.0
        self.faults = {"speed": SensorFault(), "temp": SensorFault()}
        return self._outputs()

    def step(self, inputs: PlantInputs, dt: float) -> PlantOutputs:
        """Advance the plant by dt seconds, holding inputs constant (zero-order hold)."""
        if dt <= 0:
            raise ValueError("dt must be positive")
        self._ambient = inputs.ambient_temp
        n = max(1, math.ceil(dt / self.params.max_substep - 1e-9))
        h = dt / n
        for _ in range(n):
            self._substep(inputs, h)
        self.time += dt
        return self._outputs()

    def set_fault(self, sensor: str, fault: SensorFault) -> None:
        if sensor not in self.faults:
            raise KeyError(f"Unknown sensor {sensor!r}, expected 'speed' or 'temp'")
        self.faults[sensor] = fault

    def clear_faults(self) -> None:
        self.faults = {"speed": SensorFault(), "temp": SensorFault()}

    # ---- Physics -------------------------------------------------------- #

    def winding_resistance(self, theta_w: float) -> float:
        p = self.params
        return p.r_20 * (1.0 + p.alpha_cu * (theta_w - 20.0))

    def losses(self, t_m: float, omega: float, theta_w: float) -> tuple[float, float]:
        p = self.params
        p_cu = (t_m / p.k_t) ** 2 * self.winding_resistance(theta_w)
        p_fe = p.k_fe * omega ** 2
        return p_cu, p_fe

    def _substep(self, u: PlantInputs, h: float) -> None:
        p = self.params

        # Actuator: exact discretisation of the first-order lag (unconditionally stable)
        t_target = max(-p.t_max, min(p.t_max, u.torque_cmd))
        self.t_m += (t_target - self.t_m) * (1.0 - math.exp(-h / p.tau_e))

        # Rotor: semi-implicit Euler using the updated torque
        friction = p.b * self.omega + p.t_coulomb * math.tanh(self.omega / p.omega_eps)
        drag = p.k_drag * self.omega * abs(self.omega)
        self.omega += h * (self.t_m - u.load_torque - friction - drag) / p.J

        # Thermal network (time constants are seconds to minutes, Euler at 1 ms is ample)
        p_cu, p_fe = self.losses(self.t_m, self.omega, self.theta_w)
        self._p_loss = p_cu + p_fe
        q_wh = (self.theta_w - self.theta_h) / p.r_wh
        q_ha = (self.theta_h - u.ambient_temp) / p.r_ha
        self.theta_w += h * (self._p_loss - q_wh) / p.c_w
        self.theta_h += h * (q_wh - q_ha) / p.c_h

        # Sensor contact lag
        self.theta_s += h * (self.theta_w - self.theta_s) / p.tau_temp_sensor

    # ---- Sensors -------------------------------------------------------- #

    def _apply_sensor(self, name: str, true_value: float, noise_std: float) -> tuple[float, bool]:
        fault = self.faults[name]
        noise_scale = fault.value if fault.mode == "noisy" else 1.0
        value = true_value + self._rng.gauss(0.0, noise_std * noise_scale)

        if fault.mode == "stuck":
            if fault._held is None:
                fault._held = value
            return fault._held, True
        if fault.mode == "offset":
            return value + fault.value, True
        if fault.mode == "dropout":
            return 0.0, False
        return value, True

    def _outputs(self) -> PlantOutputs:
        p = self.params
        speed_meas, speed_valid = self._apply_sensor("speed", self.omega, p.speed_noise_std)
        temp_meas, temp_valid = self._apply_sensor("temp", self.theta_s, p.temp_noise_std)
        return PlantOutputs(
            time=self.time,
            speed_meas=speed_meas,
            speed_valid=speed_valid,
            temp_meas=temp_meas,
            temp_valid=temp_valid,
            omega=self.omega,
            torque_actual=self.t_m,
            theta_winding=self.theta_w,
            theta_housing=self.theta_h,
            p_loss=self._p_loss,
        )


# --------------------------------------------------------------------------- #
# Analytic helpers - used by tests to check the simulation against theory
# --------------------------------------------------------------------------- #


def steady_state_speed(params: PlantParams, torque: float, load: float = 0.0) -> float:
    """Solve k_drag*w^2 + b*w + T_c - (T - load) = 0 for w > 0 (tanh ~ 1 at speed)."""
    net = torque - load - params.t_coulomb
    if net <= 0:
        return 0.0
    a, b, c = params.k_drag, params.b, -net
    return (-b + math.sqrt(b * b - 4 * a * c)) / (2 * a)


def steady_state_temps(params: PlantParams, torque: float, omega: float,
                       ambient: float = 25.0) -> tuple[float, float]:
    """Fixed point of the thermal network, including the resistance-temperature coupling."""
    theta_w = ambient
    for _ in range(200):
        r = params.r_20 * (1 + params.alpha_cu * (theta_w - 20.0))
        p_loss = (torque / params.k_t) ** 2 * r + params.k_fe * omega ** 2
        theta_h = ambient + p_loss * params.r_ha
        theta_w_new = theta_h + p_loss * params.r_wh
        if abs(theta_w_new - theta_w) < 1e-9:
            break
        theta_w = theta_w_new
    return theta_w, theta_h
