"""
Closed-loop demo: C controller (via ctypes) driving the Python plant.

Scenario
  0-20 s      request 300 rad/s           warm-up
  20-1300 s   request 650 rad/s           sustained heavy load -> derating expected
  1300 s      temperature sensor dropout  -> LIMP_HOME expected
  1450 s      speed sensor dropout        -> SAFE expected
"""

import matplotlib.pyplot as plt

from ctrl_sil import MODE_NAMES, MotorController
from plant import MotorPlant, PlantInputs, SensorFault

DT, T_END = 0.01, 1500.0

ctrl = MotorController()
assert ctrl.init() == 0, "default calibration rejected"
plant = MotorPlant(seed=7)
meas = plant.reset()
torque_cmd = 0.0

log = {k: [] for k in ("t", "req", "speed", "tcmd", "tlim", "tw", "tmeas", "mode")}
first_derate = None

for k in range(round(T_END / DT)):
    t = k * DT
    req = 300.0 if t < 20 else 650.0
    if abs(t - 1300.0) < DT / 2:
        plant.set_fault("temp", SensorFault("dropout"))
    if abs(t - 1450.0) < DT / 2:
        plant.set_fault("speed", SensorFault("dropout"))

    # Controller sees last tick's measurements (one-sample delay, as on a real ECU)
    out = ctrl.step(req, meas.speed_meas, meas.speed_valid, meas.temp_meas, meas.temp_valid)
    meas = plant.step(PlantInputs(torque_cmd=out.torque_cmd), DT)

    if out.derate_active and first_derate is None:
        first_derate = t
    if k % 50 == 0:
        for key, val in (("t", t), ("req", req), ("speed", meas.omega), ("tcmd", out.torque_cmd),
                         ("tlim", out.torque_limit), ("tw", meas.theta_winding),
                         ("tmeas", meas.temp_meas if meas.temp_valid else float("nan")),
                         ("mode", out.mode)):
            log[key].append(val)

fig, ax = plt.subplots(4, 1, figsize=(10, 11), sharex=True)
ax[0].plot(log["t"], log["req"], "--", label="Request"); ax[0].plot(log["t"], log["speed"], label="Actual")
ax[0].set_ylabel("rad/s"); ax[0].legend(); ax[0].set_title("Speed")
ax[1].plot(log["t"], log["tcmd"], label="Torque command"); ax[1].plot(log["t"], log["tlim"], label="Torque limit")
ax[1].set_ylabel("N·m"); ax[1].legend(); ax[1].set_title("Torque")
ax[2].plot(log["t"], log["tw"], label="Winding (true)"); ax[2].plot(log["t"], log["tmeas"], label="Winding (sensor)", alpha=0.7)
ax[2].axhline(120, ls="--", color="gray", label="Derate start"); ax[2].axhline(150, ls=":", color="red", label="Derate end")
ax[2].set_ylabel("°C"); ax[2].legend(); ax[2].set_title("Thermal")
ax[3].step(log["t"], log["mode"], where="post")
ax[3].set_yticks(list(MODE_NAMES)); ax[3].set_yticklabels(MODE_NAMES.values()); ax[3].set_title("Controller mode")
ax[3].set_xlabel("Time [s]")
for a in ax:
    a.axvline(1300, color="k", lw=0.5); a.axvline(1450, color="k", lw=0.5)
fig.tight_layout()
fig.savefig("closed_loop_demo.png", dpi=120)

print(f"Controller v{ctrl.version}")
print(f"Derating first active at {first_derate:.1f} s")
print(f"Peak winding temperature {max(log['tw']):.1f} °C")
print(f"Final mode {MODE_NAMES[out.mode]}, faults 0x{out.faults:04X}, torque {out.torque_cmd:.1f} N·m")
