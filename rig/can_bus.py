"""
can_bus.py - CAN layer of the SIL rig.

Every signal between the ECU and the rest of the vehicle travels as a CAN frame
defined in can/motor.dbc. This module provides:

  CanCodec      encode/decode frames from the DBC, with alive counter and checksum
  BusFault      a fault to inject on the bus (drop, corrupt, freeze_counter)
  CanNode       one bus participant: sends with fault injection, receives and validates
  EcuNode       the ECU side: CAN frames in -> MC_Step() -> CAN frames out
  InverterNode  the plant side: publishes INV_Status, applies ECU_TorqueCmd

Note on SIL scope: in this rig the ECU's frame decoding runs in Python, so SIL
verifies the control logic at the CAN interface, not the C decoding code. A
processor-in-the-loop stage (C decoding on the target) closes that gap.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

import can
import cantools

DEFAULT_DBC = Path(__file__).resolve().parent.parent / "can" / "motor.dbc"

# How many 10 ms ticks a receiver tolerates without a fresh frame
RX_TIMEOUT_TICKS = 5


# --------------------------------------------------------------------------- #
# Codec
# --------------------------------------------------------------------------- #


def checksum(data: bytes) -> int:
    """(sum of bytes 0-6) XOR 0xFF - simple, but catches corrupted payloads."""
    return (sum(data[:7]) & 0xFF) ^ 0xFF


class CanCodec:
    def __init__(self, dbc_path: Path | str = DEFAULT_DBC) -> None:
        self.db = cantools.database.load_file(str(dbc_path))
        self._by_name = {m.name: m for m in self.db.messages}
        self._by_id = {m.frame_id: m for m in self.db.messages}

    def frame_id(self, name: str) -> int:
        return self._by_name[name].frame_id

    def encode(self, name: str, signals: dict, counter: int) -> bytes:
        msg = self._by_name[name]
        values = {}
        for sig in msg.signals:
            if sig.name == "AliveCounter":
                values[sig.name] = counter & 0x0F
            elif sig.name == "Checksum":
                values[sig.name] = 0
            else:
                v = float(signals[sig.name])
                # A real sender saturates at the signal range instead of overflowing
                values[sig.name] = min(max(v, sig.minimum), sig.maximum)
        data = bytearray(msg.encode(values, strict=False))
        if "Checksum" in {s.name for s in msg.signals}:
            data[7] = checksum(data)
        return bytes(data)

    def decode(self, frame_id: int, data: bytes) -> tuple[str, dict, bool]:
        """Returns (message name, signals, checksum_ok)."""
        msg = self._by_id[frame_id]
        signals = msg.decode(data, decode_choices=False)
        ok = True
        if "Checksum" in signals:
            ok = data[7] == checksum(data)
        return msg.name, signals, ok


# --------------------------------------------------------------------------- #
# Fault injection
# --------------------------------------------------------------------------- #

FAULT_KINDS = ("drop", "corrupt", "freeze_counter")


@dataclass(frozen=True)
class BusFault:
    """Applies to frames of `message` sent while start <= t < end."""
    message: str
    start: float
    end: float
    kind: str = "drop"

    def __post_init__(self) -> None:
        if self.kind not in FAULT_KINDS:
            raise ValueError(f"Unknown bus fault kind {self.kind!r}, expected one of {FAULT_KINDS}")

    def active(self, name: str, t: float) -> bool:
        return name == self.message and self.start <= t < self.end


# --------------------------------------------------------------------------- #
# Nodes
# --------------------------------------------------------------------------- #


def open_virtual_bus() -> str:
    """A unique virtual channel per run, so parallel or consecutive runs never cross-talk."""
    return f"sil-{uuid.uuid4().hex[:8]}"


class CanNode:
    def __init__(self, channel: str, codec: CanCodec, faults: list[BusFault] | None = None) -> None:
        self.bus = can.Bus(interface="virtual", channel=channel, receive_own_messages=False)
        self.codec = codec
        self.faults = list(faults or [])
        self._tx_counter: dict[str, int] = {}
        self._last_rx_counter: dict[str, int] = {}
        self.last_rx_tick: dict[str, int] = {}
        self.latest: dict[str, dict] = {}
        self.rx_errors = 0      # frames discarded for a bad checksum
        self.rx_stale = 0       # frames discarded for a frozen alive counter

    def send(self, name: str, signals: dict, t: float) -> None:
        active = [f for f in self.faults if f.active(name, t)]
        counter = self._tx_counter.get(name, 0)
        if not any(f.kind == "freeze_counter" for f in active):
            counter = (counter + 1) & 0x0F
            self._tx_counter[name] = counter
        if any(f.kind == "drop" for f in active):
            return
        data = bytearray(self.codec.encode(name, signals, counter))
        if any(f.kind == "corrupt" for f in active):
            data[0] ^= 0x01     # single bit flip in the payload
        self.bus.send(can.Message(arbitration_id=self.codec.frame_id(name),
                                  data=bytes(data), is_extended_id=False))

    def receive_all(self, tick: int) -> None:
        """Drain the receive queue, keeping only frames that pass validation."""
        while (msg := self.bus.recv(timeout=0)) is not None:
            name, signals, ok = self.codec.decode(msg.arbitration_id, msg.data)
            if not ok:
                self.rx_errors += 1
                continue
            counter = signals.get("AliveCounter")
            if counter is not None and self._last_rx_counter.get(name) == counter:
                self.rx_stale += 1
                continue
            if counter is not None:
                self._last_rx_counter[name] = counter
            self.latest[name] = signals
            self.last_rx_tick[name] = tick

    def fresh(self, name: str, tick: int) -> bool:
        last = self.last_rx_tick.get(name)
        return last is not None and (tick - last) < RX_TIMEOUT_TICKS

    def close(self) -> None:
        self.bus.shutdown()


class EcuNode(CanNode):
    """The motor controller ECU: decodes CAN, runs the C controller, encodes the result."""

    STATUS_EVERY_TICKS = 10

    def __init__(self, channel, codec, controller, faults=None) -> None:
        super().__init__(channel, codec, faults)
        self.ctrl = controller
        self.inputs_used: dict = {}

    def step(self, tick: int, t: float):
        self.receive_all(tick)

        req_ok = self.fresh("VCU_Request", tick)
        inv_ok = self.fresh("INV_Status", tick)
        req = self.latest.get("VCU_Request", {})
        inv = self.latest.get("INV_Status", {})

        # A missing request means "request zero"; a missing inverter frame means
        # both sensor values are invalid - the controller's own logic does the rest.
        speed_req = req.get("SpeedRequest", 0.0) if req_ok else 0.0
        speed = inv.get("MotorSpeed", 0.0)
        temp = inv.get("MotorTemp", 0.0)
        speed_valid = bool(inv_ok and inv.get("SpeedValid", 0))
        temp_valid = bool(inv_ok and inv.get("TempValid", 0))

        out = self.ctrl.step(speed_req, speed, speed_valid, temp, temp_valid)
        self.inputs_used = dict(speed_req=speed_req, speed=speed, temp=temp,
                                speed_valid=speed_valid, temp_valid=temp_valid)

        self.send("ECU_TorqueCmd", {"TorqueRequest": out.torque_cmd,
                                    "TorqueLimit": out.torque_limit}, t)
        if tick % self.STATUS_EVERY_TICKS == 0:
            self.send("ECU_Status", {"Mode": out.mode, "DerateActive": out.derate_active,
                                     "FaultBits": out.faults}, t)
        return out


class InverterNode(CanNode):
    """Plant side: publishes measurements, applies the torque command (zero on timeout)."""

    def publish(self, meas, t: float) -> None:
        self.send("INV_Status", {"MotorSpeed": meas.speed_meas, "MotorTemp": meas.temp_meas,
                                 "SpeedValid": int(meas.speed_valid),
                                 "TempValid": int(meas.temp_valid)}, t)

    def torque_command(self, tick: int) -> float:
        self.receive_all(tick)
        if not self.fresh("ECU_TorqueCmd", tick):
            return 0.0      # inverter safety reaction: no fresh command -> no torque
        return self.latest["ECU_TorqueCmd"]["TorqueRequest"]
