"""Pure power/energy accounting for PC Power (no Home Assistant imports, unit-testable).

Model
-----
The agent exposes monotonically increasing CPU/GPU energy counters (joules) that reset only
when the agent restarts (new ``session_id``), plus its CLOCK_MONOTONIC (``awake_s``), which
does not advance while the PC is suspended. Each successful poll credits, for the interval
since the previous good reading (dt wall-clock, of which dt_awake the PC was running):

    pc_J = (ΔcpuJ + ΔgpuJ + other_W·dt_awake) / psu_efficiency + offline_W·(dt − dt_awake)

where other_W = disks + controller + RGB + board + fans·fan_fraction + USB devices.
Monitors are wall-powered (no PSU loss) and accounted separately the same way.

When the PC stops answering for ``grace_polls`` consecutive polls it is considered offline:
reported power drops to ``offline_w`` and that rate is credited for the offline time. If the
same agent session comes back (network blip, HA restart, resume from suspend) the real
figures for the gap are credited minus what was already credited as offline, so nothing is
double counted. A new session (reboot / agent restart) re-baselines.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field


@dataclass
class Profile:
    disks_w: float = 0.0
    controller_w: float = 0.0
    fans_w: float = 4.5  # fan motors + AIO pump; at full speed when fans_dynamic
    fans_dynamic: bool = True
    rgb_w: float = 0.0  # header/RAM RGB (GPU RGB is already in the GPU reading)
    board_w: float = 12.0
    efficiency: float = 0.85
    offline_w: float = 0.0  # PC (not monitors) when off / asleep, at the wall
    monitors_off_w: float = 0.0  # all monitors in standby, at the wall
    grace_polls: int = 3

    @property
    def static_w(self) -> float:
        return self.disks_w + self.controller_w + self.rgb_w + self.board_w

    def fans_effective_w(self, fraction: float | None) -> float:
        if not self.fans_dynamic or fraction is None:
            return self.fans_w
        return self.fans_w * fraction


@dataclass
class Reading:
    """What one successful agent poll tells us."""

    session_id: str
    timestamp: float
    cpu_w: float | None
    cpu_j: float | None
    gpu_w: float | None
    gpu_j: float | None
    usb_w: float = 0.0
    fans_fraction: float | None = None
    monitors_w: float = 0.0
    awake_s: float | None = None  # agent CLOCK_MONOTONIC; stops during suspend


@dataclass
class Snapshot:
    online: bool
    cpu_w: float | None
    gpu_w: float | None
    wall_w: float
    energy_kwh: float
    usb_w: float = 0.0
    fans_w: float = 0.0
    monitors_w: float = 0.0
    monitors_kwh: float = 0.0


@dataclass
class AccountState:
    """Persisted between HA restarts."""

    energy_wh: float = 0.0
    monitors_wh: float = 0.0
    session_id: str | None = None
    base_ts: float | None = None  # time of the last good reading
    base_awake_s: float | None = None
    base_cpu_j: float = 0.0
    base_gpu_j: float = 0.0
    base_dynamic_w: float = 0.0  # fans + USB at the last reading (applies to the next interval)
    base_monitors_w: float = 0.0
    accounted_ts: float | None = None  # energy credited up to this time
    offline_credit_wh: float = 0.0  # offline estimates credited since base_ts
    offline_monitors_credit_wh: float = 0.0
    fan_max_rpm: dict = field(default_factory=dict)  # learned per header
    failures: int = 0
    online: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> "AccountState":
        if not data:
            return cls()
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        st = cls(**known)
        st.failures = 0  # be optimistic after a restart; the first poll decides
        return st


@dataclass
class Accountant:
    profile: Profile
    state: AccountState = field(default_factory=AccountState)
    last_cpu_w: float | None = None
    last_gpu_w: float | None = None
    last_usb_w: float = 0.0
    last_fans_w: float = 0.0
    last_monitors_w: float = 0.0

    def wall_w(self, cpu_w: float | None, gpu_w: float | None, usb_w: float = 0.0, fans_w: float | None = None) -> float:
        p = self.profile
        fans = p.fans_w if fans_w is None else fans_w
        return ((cpu_w or 0.0) + (gpu_w or 0.0) + p.static_w + fans + usb_w) / p.efficiency

    def on_reading(self, r: Reading) -> Snapshot:
        st, p = self.state, self.profile
        cpu_j = r.cpu_j or 0.0
        gpu_j = r.gpu_j or 0.0
        fans_w = p.fans_effective_w(r.fans_fraction)
        if st.session_id == r.session_id and st.base_ts is not None and r.timestamp > st.base_ts:
            dt = r.timestamp - st.base_ts
            dt_awake = dt
            if r.awake_s is not None and st.base_awake_s is not None:
                dt_awake = min(max(r.awake_s - st.base_awake_s, 0.0), dt)
            dt_asleep = dt - dt_awake
            d_cpu = max(cpu_j - st.base_cpu_j, 0.0)
            d_gpu = max(gpu_j - st.base_gpu_j, 0.0)
            other_w = p.static_w + st.base_dynamic_w
            pc_wh = ((d_cpu + d_gpu + other_w * dt_awake) / p.efficiency + p.offline_w * dt_asleep) / 3600.0
            st.energy_wh += max(pc_wh - st.offline_credit_wh, 0.0)
            mon_wh = (st.base_monitors_w * dt_awake + p.monitors_off_w * dt_asleep) / 3600.0
            st.monitors_wh += max(mon_wh - st.offline_monitors_credit_wh, 0.0)
        # else: first reading, new agent session, or clock went backwards -> re-baseline only

        st.session_id = r.session_id
        st.base_ts = r.timestamp
        st.base_awake_s = r.awake_s
        st.base_cpu_j = cpu_j
        st.base_gpu_j = gpu_j
        st.base_dynamic_w = fans_w + r.usb_w
        st.base_monitors_w = r.monitors_w
        st.accounted_ts = r.timestamp
        st.offline_credit_wh = 0.0
        st.offline_monitors_credit_wh = 0.0
        st.failures = 0
        st.online = True
        self.last_cpu_w, self.last_gpu_w, self.last_usb_w = r.cpu_w, r.gpu_w, r.usb_w
        self.last_fans_w, self.last_monitors_w = fans_w, r.monitors_w
        return self.snapshot()

    def on_failure(self, now: float) -> Snapshot:
        st, p = self.state, self.profile
        st.failures += 1
        if st.failures >= p.grace_polls:
            st.online = False
            if st.accounted_ts is not None and now > st.accounted_ts:
                gap_h = (now - st.accounted_ts) / 3600.0
                st.energy_wh += p.offline_w * gap_h
                st.offline_credit_wh += p.offline_w * gap_h
                st.monitors_wh += p.monitors_off_w * gap_h
                st.offline_monitors_credit_wh += p.monitors_off_w * gap_h
            st.accounted_ts = now
        return self.snapshot()

    def snapshot(self) -> Snapshot:
        st, p = self.state, self.profile
        if st.online:
            return Snapshot(
                True, self.last_cpu_w, self.last_gpu_w,
                self.wall_w(self.last_cpu_w, self.last_gpu_w, self.last_usb_w, self.last_fans_w),
                st.energy_wh / 1000, self.last_usb_w, self.last_fans_w,
                self.last_monitors_w, st.monitors_wh / 1000,
            )
        return Snapshot(False, 0.0, 0.0, p.offline_w, st.energy_wh / 1000, 0.0, 0.0,
                        p.monitors_off_w, st.monitors_wh / 1000)


# --------------------------------------------------------------------------- fans
#
# Fan power scales roughly with speed cubed. We don't know each header's top speed, so we
# learn it (the highest RPM ever seen, starting from FAN_SEED_RPM so a fan idling at 900 rpm
# on day one isn't assumed to be flat out). Run a stress test once to teach it real maxima.

FAN_SEED_RPM = 1200
FAN_FLOOR = 0.1  # motor electronics + bearings at very low speed


def fan_fraction(fans: list[dict] | None, fan_max: dict) -> tuple[float | None, dict]:
    """Average fraction of full-speed power across headers that have ever spun.

    Returns (fraction or None if no fan data, updated max-rpm dict).
    """
    new_max = dict(fan_max)
    fracs = []
    for f in fans or []:
        fid, rpm = f.get("id"), f.get("rpm") or 0
        if not fid:
            continue
        if rpm > 0:
            new_max[fid] = max(new_max.get(fid, FAN_SEED_RPM), rpm)
        if fid not in new_max:
            continue  # never spun: empty header
        if rpm <= 0:
            fracs.append(0.0)  # stopped (zero-RPM mode)
        else:
            fracs.append(max(FAN_FLOOR, (rpm / new_max[fid]) ** 3))
    if not fracs:
        return None, new_max
    return round(sum(fracs) / len(fracs), 4), new_max


# --------------------------------------------------------------------------- monitors
#
# Monitors are wall-powered. Estimate: max_W · (0.4 + 0.6 · brightness), where max_W
# defaults from the panel size in the EDID (~1.2 W per inch of diagonal for typical
# LED-backlit LCDs) and can be overridden per monitor.

MONITOR_W_PER_INCH = 1.2
MONITOR_DEFAULT_W = 30.0
MONITOR_BASE_FRACTION = 0.4
MONITOR_ASSUMED_BRIGHTNESS = 0.7
MONITOR_STANDBY_W = 0.5


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def monitor_keys(monitors: list[dict] | None) -> list[str]:
    """Stable per-monitor keys: make_model[_serial], with the I2C bus added for identical twins."""
    base = [_slug(f"{m.get('mfg') or 'monitor'}_{m.get('model') or ''}_{m.get('serial') or ''}") for m in monitors or []]
    return [k if base.count(k) == 1 else f"{k}_bus{m.get('bus')}" for k, m in zip(base, monitors or [])]


def monitor_label(m: dict) -> str:
    name = " ".join(x for x in (m.get("mfg"), m.get("model")) if x) or f"Monitor (bus {m.get('bus')})"
    return f"{name} ({m['size_in']:.0f}\")" if m.get("size_in") else name


def monitor_default_w(m: dict) -> float:
    size = m.get("size_in")
    return round(size * MONITOR_W_PER_INCH, 1) if size else MONITOR_DEFAULT_W


def monitor_state(m: dict) -> str:
    """on / standby, preferring the kernel's DPMS view over DDC/CI."""
    if m.get("dpms") in ("on", "off"):
        return "on" if m["dpms"] == "on" else "standby"
    mode = m.get("power_mode")
    if mode == "on":
        return "on"
    if mode in ("standby", "suspend", "off"):
        return "standby"
    # unknown: a monitor that answered before but not now is asleep; one that never
    # answered probably doesn't support DDC/CI, so assume it's on
    return "standby" if m.get("brightness") is not None and m.get("responding") is False else "on"


def estimate_monitors(
    monitors: list[dict] | None, overrides: dict[str, float] | None = None, standby_w: float = MONITOR_STANDBY_W
) -> tuple[float, list[dict]]:
    overrides = overrides or {}
    total, rows = 0.0, []
    for key, m in zip(monitor_keys(monitors), monitors or []):
        max_w = float(overrides.get(key, monitor_default_w(m)))
        state = monitor_state(m)
        b, bmax = m.get("brightness"), m.get("max_brightness") or 100
        frac = (b / bmax) if b is not None and bmax else MONITOR_ASSUMED_BRIGHTNESS
        watts = max_w * (MONITOR_BASE_FRACTION + (1 - MONITOR_BASE_FRACTION) * frac) if state == "on" else standby_w
        watts = round(watts, 1)
        total += watts
        rows.append({
            "key": key, "device": monitor_label(m), "bus": m.get("bus"), "state": state,
            "brightness": b, "max_w": max_w, "watts": watts,
        })
    return round(total, 1), rows


# --------------------------------------------------------------------------- disks


def default_disks_w(disks: dict | None) -> float:
    """Rough per-device draw: 3.5" HDD ~5 W, SATA SSD ~1.5 W, NVMe ~3 W (idle-ish averages)."""
    disks = disks or {}
    return round(5.0 * disks.get("hdd", 0) + 1.5 * disks.get("ssd", 0) + 3.0 * disks.get("nvme", 0), 1)


# --------------------------------------------------------------------------- USB devices
#
# bMaxPower is what a device *claims* and is unreliable in both directions (an Ajazz
# stream deck claims 0.5 W but has 15 LCD keys; a LAMZU mouse dongle claims 2.5 W and
# draws ~0.2 W). So: known table first, then a capped fraction of the claim, and the user
# can override any device in the options flow.

# (vid, pid, product substring or None) -> (label, watts or None to skip)
KNOWN_USB: dict[tuple[str, str, str | None], tuple[str, float | None]] = {
    ("17a0", "0001", None): ("Samson C01U microphone", 0.4),
    ("320f", "50b7", None): ("MONTECH MK087 keyboard (RGB)", 1.5),
    ("0300", "3010", "HOTSPOTEK"): ("Ajazz stream deck", 2.0),
    ("37b0", "002e", None): ("LAMZU wireless dongle", 0.2),
    ("0fd9", "0080", None): ("Elgato Stream Deck MK.2", 2.5),
    ("0fd9", "006d", None): ("Elgato Stream Deck", 2.5),
    ("0b05", "1939", None): ("ASUS AURA LED controller", None),  # LEDs powered from headers -> RGB field
    ("8087", "0029", None): ("Intel AX200 Bluetooth", None),  # part of the Wi-Fi card
    ("8087", "0032", None): ("Intel AX210 Bluetooth", None),
    ("8087", "0033", None): ("Intel AX211 Bluetooth", None),
}
UNKNOWN_FRACTION = 0.4
UNKNOWN_CAP_W = 2.5


def usb_key(dev: dict) -> str:
    return f"{dev.get('vid') or '0000'}_{dev.get('pid') or '0000'}"


def _known(dev: dict) -> tuple[str, float | None] | None:
    vid, pid, product = dev.get("vid"), dev.get("pid"), (dev.get("product") or "") + (dev.get("manufacturer") or "")
    for (kv, kp, ksub), val in KNOWN_USB.items():
        if kv == vid and kp == pid and (ksub is None or ksub.lower() in product.lower()):
            return val
    return None


def usb_label(dev: dict) -> str:
    known = _known(dev)
    if known:
        return known[0]
    name = " ".join(x for x in (dev.get("manufacturer"), dev.get("product")) if x)
    return name or f"USB {dev.get('vid')}:{dev.get('pid')}"


def usb_default_w(dev: dict) -> tuple[float, str]:
    """Estimate for one device, before user overrides. Returns (watts, basis)."""
    known = _known(dev)
    if known is not None:
        return (0.0, "skipped (built in)") if known[1] is None else (known[1], "known device")
    if dev.get("removable") == "fixed":
        return 0.0, "skipped (internal port)"
    if dev.get("self_powered"):
        return 0.0, "skipped (self-powered)"
    claimed = (dev.get("max_power_ma") or 0) * 5 / 1000
    return round(min(claimed * UNKNOWN_FRACTION, UNKNOWN_CAP_W), 2), f"guess from {claimed:.1f} W claim"


def estimate_usb(devices: list[dict] | None, overrides: dict[str, float] | None = None) -> tuple[float, list[dict]]:
    """Total USB draw (W at the PSU) and a per-device breakdown."""
    overrides = overrides or {}
    total, rows = 0.0, []
    for dev in devices or []:
        key = usb_key(dev)
        watts, basis = usb_default_w(dev)
        if key in overrides:
            watts, basis = float(overrides[key]), "your setting"
        if dev.get("suspended") and watts:
            watts, basis = 0.0, basis + ", suspended"
        total += watts
        rows.append({"device": usb_label(dev), "key": key, "watts": watts, "basis": basis})
    return round(total, 2), rows
