"""Open-loop demo: what happens to the motor with no derating controller in the loop."""

import matplotlib.pyplot as plt

from plant import MotorPlant, PlantInputs

DT = 0.01
plant = MotorPlant(seed=1)

# Scenario: warm-up, sustained heavy load, then cool-down
profile = [(60, 100.0), (840, 220.0), (600, 40.0)]  # (seconds, torque command)

t, speed, torque, tw, th, ts, ploss = [], [], [], [], [], [], []
for seconds, tq in profile:
    for _ in range(round(seconds / DT)):
        o = plant.step(PlantInputs(torque_cmd=tq), DT)
        if round(o.time / DT) % 50 == 0:  # log every 0.5 s
            t.append(o.time); speed.append(o.omega * 60 / (2 * 3.14159265))
            torque.append(o.torque_actual); tw.append(o.theta_winding)
            th.append(o.theta_housing); ts.append(o.temp_meas); ploss.append(o.p_loss / 1000)

fig, ax = plt.subplots(3, 1, figsize=(10, 9), sharex=True)
ax[0].plot(t, torque, label="Actual torque [N·m]")
ax[0].plot(t, [s / 30 for s in speed], label="Speed [rpm / 30]")
ax[0].legend(); ax[0].set_title("Mechanical")
ax[1].plot(t, ploss, color="tab:red", label="Total loss [kW]")
ax[1].legend(); ax[1].set_title("Losses")
ax[2].plot(t, tw, label="Winding (true)")
ax[2].plot(t, ts, label="Winding (sensor)", alpha=0.7)
ax[2].plot(t, th, label="Housing")
ax[2].axhline(120, ls="--", color="gray", label="Derating threshold")
ax[2].axhline(150, ls=":", color="red", label="Winding limit")
ax[2].legend(); ax[2].set_title("Thermal (open loop, no derating)")
ax[2].set_xlabel("Time [s]"); ax[2].set_ylabel("°C")
fig.tight_layout()
fig.savefig("plant_open_loop_demo.png", dpi=120)
print(f"Peak winding {max(tw):.1f} °C, peak housing {max(th):.1f} °C, top speed {max(speed):.0f} rpm")
