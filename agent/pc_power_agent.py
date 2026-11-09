#!/usr/bin/env python3
"""PC Power agent: exposes CPU/GPU power and cumulative energy over HTTP for Home Assistant.

Stdlib only. Runs as root (RAPL energy_uj is root-readable only).

Every power source is sampled every `sample_interval` seconds by a background thread and
turned into a monotonically increasing energy counter (joules since agent start, wrap/reset
safe). Home Assistant computes exact energy from counter deltas, so its poll rate does not
affect accuracy. `session_id` changes whenever the agent restarts, telling HA to re-baseline.

Also reports: USB devices, monitors (DDC/CI via ddcutil), temperatures, fans, CPU/GPU
utilisation, memory, network interfaces (IPs, MAC, Wake-on-LAN state) and the logind
session (locked / idle). Optional control: suspend / poweroff / reboot (off by default)
and monitor brightness.
"""
from __future__ import annotations

import argparse
import ctypes
import fcntl
import hmac
import ipaddress
import json
import logging
import math
import os
import re
import shutil
import socket
import struct
import subprocess
import threading
import time
import tomllib
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "0.3.2"
API_VERSION = 1
LOG = logging.getLogger("pc-power-agent")

DEFAULTS = {
    "listen": "0.0.0.0",
    "port": 8765,
    "token": "",
    "sample_interval": 1.0,
    "gpu_fallback_w": None,  # used for an NVIDIA GPU that reports no power reading
    "allow_power_control": False,  # let HA suspend / power off / reboot this PC
    "power_dry_run": False,  # log suspend/poweroff/reboot instead of running them (testing)
    "allow_session_lock": True,  # let HA lock the screen (loginctl lock-session)
    "lock_dry_run": False,  # log screen locks instead of locking (testing)
    "allow_monitor_control": True,  # let HA set monitor brightness over DDC/CI
    "monitor_interval": 30.0,  # seconds between DDC/CI reads (each read takes ~1 s per monitor)
}

POWER_ACTIONS = {
    "suspend": ["systemctl", "suspend"],
    "poweroff": ["systemctl", "poweroff"],
    "reboot": ["systemctl", "reboot"],
}


def read_text(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except (OSError, UnicodeDecodeError):
        return None


def read_int(path: Path) -> int | None:
    text = read_text(path)
    try:
        return int(text) if text is not None else None
    except ValueError:
        return None


def run(cmd: list[str], timeout: float = 10.0) -> str | None:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return res.stdout if res.returncode == 0 else None


# --------------------------------------------------------------------------- power sources


class Source:
    """A power source. sample() returns (power_w, energy_j_increment) for the last interval."""

    kind = "cpu"
    vendor = "unknown"
    name = "unknown"
    method = "unknown"

    def sample(self, dt: float) -> tuple[float, float] | None:
        raise NotImplementedError

    def extras(self) -> dict:
        """Temperature / utilisation etc. Read on demand, cheap."""
        return {}


class RaplSource(Source):
    """Sum of top-level powercap RAPL package domains (works on Intel and AMD Zen)."""

    kind = "cpu"
    method = "rapl"

    def __init__(self, domains: list[Path]):
        self.domains = domains
        self.vendor = "rapl"
        self.name = "+".join(read_text(d / "name") or d.name for d in domains)
        self.last: dict[Path, int] = {}
        self.max_range = {d: read_int(d / "max_energy_range_uj") or 0 for d in domains}

    @classmethod
    def discover(cls, sysfs: Path) -> "RaplSource | None":
        domains = []
        for d in sorted((sysfs / "class/powercap").glob("intel-rapl:*")):
            if d.name.count(":") != 1:  # skip subdomains like intel-rapl:0:0 (core)
                continue
            if (read_text(d / "name") or "").startswith("package") and read_int(d / "energy_uj") is not None:
                domains.append(d)
        if not domains:
            return None
        return cls(domains)

    def sample(self, dt):
        total_uj = 0
        for d in self.domains:
            now = read_int(d / "energy_uj")
            if now is None:
                return None
            prev = self.last.get(d)
            self.last[d] = now
            if prev is None:
                continue
            delta = now - prev
            if delta < 0:
                delta += self.max_range[d]
            if delta < 0:  # bogus range; drop this interval rather than inventing energy
                delta = 0
            total_uj += delta
        joules = total_uj / 1e6
        return (joules / dt if dt > 0 else 0.0), joules


class AmdHwmonSource(Source):
    """amdgpu hwmon power (power1_input on RDNA3+, power1_average on older cards), in µW."""

    kind = "gpu"
    vendor = "amd"
    method = "hwmon"

    def __init__(self, hwmon: Path, card: str):
        self.hwmon = hwmon
        self.device = hwmon.parent.parent
        self.name = read_text(self.device / "product_name") or f"AMD GPU ({card})"
        self.files = [hwmon / "power1_input", hwmon / "power1_average"]

    @classmethod
    def discover(cls, sysfs: Path) -> list["AmdHwmonSource"]:
        found = []
        for hw in sorted(sysfs.glob("class/drm/card[0-9]*/device/hwmon/hwmon*")):
            if read_text(hw / "name") != "amdgpu":
                continue
            card = hw.parts[-4]
            if any(read_int(hw / f) is not None for f in ("power1_input", "power1_average")):
                found.append(cls(hw, card))
        return found

    def sample(self, dt):
        for f in self.files:
            uw = read_int(f)
            if uw is not None:
                watts = uw / 1e6
                return watts, watts * dt
        return None

    def extras(self):
        out = {}
        temps = {}
        for t in self.hwmon.glob("temp*_input"):
            label = read_text(t.with_name(t.name.replace("_input", "_label"))) or t.name
            val = read_int(t)
            if val is not None:
                temps[label] = val / 1000
        if temps:
            out["temp_c"] = temps.get("edge") or next(iter(temps.values()))
            if "junction" in temps:
                out["hotspot_c"] = temps["junction"]
        busy = read_int(self.device / "gpu_busy_percent")
        if busy is not None:
            out["util_pct"] = busy
        return out


class _Nvml:
    """Minimal ctypes binding to libnvidia-ml."""

    class _Util(ctypes.Structure):
        _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

    def __init__(self, lib: ctypes.CDLL):
        self.lib = lib
        if lib.nvmlInit_v2() != 0:
            raise OSError("nvmlInit failed")

    @classmethod
    def load(cls) -> "_Nvml | None":
        for name in ("libnvidia-ml.so.1", "libnvidia-ml.so"):
            try:
                return cls(ctypes.CDLL(name))
            except OSError:
                continue
        return None

    def count(self) -> int:
        n = ctypes.c_uint()
        return n.value if self.lib.nvmlDeviceGetCount_v2(ctypes.byref(n)) == 0 else 0

    def handle(self, index: int):
        h = ctypes.c_void_p()
        return h if self.lib.nvmlDeviceGetHandleByIndex_v2(index, ctypes.byref(h)) == 0 else None

    def name(self, h) -> str:
        buf = ctypes.create_string_buffer(96)
        return buf.value.decode() if self.lib.nvmlDeviceGetName(h, buf, 96) == 0 else "NVIDIA GPU"

    def _uint(self, fn, h, *args) -> int | None:
        v = ctypes.c_uint()
        return v.value if fn(h, *args, ctypes.byref(v)) == 0 else None

    def power_mw(self, h) -> int | None:
        return self._uint(self.lib.nvmlDeviceGetPowerUsage, h)

    def temp_c(self, h) -> int | None:
        return self._uint(self.lib.nvmlDeviceGetTemperature, h, 0)  # NVML_TEMPERATURE_GPU

    def util_pct(self, h) -> int | None:
        u = self._Util()
        return u.gpu if self.lib.nvmlDeviceGetUtilizationRates(h, ctypes.byref(u)) == 0 else None

    def power_limit_w(self, h) -> float | None:
        mw = self._uint(self.lib.nvmlDeviceGetEnforcedPowerLimit, h)
        return mw / 1000 if mw is not None else None

    def energy_mj(self, h) -> int | None:
        v = ctypes.c_ulonglong()
        return v.value if self.lib.nvmlDeviceGetTotalEnergyConsumption(h, ctypes.byref(v)) == 0 else None


class NvidiaSource(Source):
    """NVML: total energy counter on Volta+, else integrated power, else configured fallback."""

    kind = "gpu"
    vendor = "nvidia"

    def __init__(self, nvml: _Nvml, handle, fallback_w: float | None):
        self.nvml = nvml
        self.handle = handle
        self.name = nvml.name(handle)
        self.fallback_w = fallback_w
        self.last_mj = nvml.energy_mj(handle)
        if self.last_mj is not None:
            self.method = "nvml-energy"
        elif nvml.power_mw(handle) is not None:
            self.method = "nvml-power"
        else:
            self.method = "fallback"

    @classmethod
    def discover(cls, fallback_w: float | None) -> list["NvidiaSource"]:
        nvml = _Nvml.load()
        if nvml is None:
            return []
        out = []
        for i in range(nvml.count()):
            h = nvml.handle(i)
            if h is not None:
                out.append(cls(nvml, h, fallback_w))
        return out

    def sample(self, dt):
        mw = self.nvml.power_mw(self.handle)
        watts = mw / 1000 if mw is not None else self.fallback_w
        if self.method == "nvml-energy":
            now = self.nvml.energy_mj(self.handle)
            if now is not None:
                prev, self.last_mj = self.last_mj, now
                if prev is not None and now >= prev:  # counter resets on driver reload
                    joules = (now - prev) / 1000
                    return (watts if watts is not None else joules / dt), joules
                return (watts or 0.0), 0.0
        if watts is None:
            return None
        return watts, watts * dt

    def extras(self):
        out = {}
        for key, fn in (("temp_c", self.nvml.temp_c), ("util_pct", self.nvml.util_pct),
                        ("power_limit_w", self.nvml.power_limit_w)):
            val = fn(self.handle)
            if val is not None:
                out[key] = val
        return out


class FakeSource(Source):
    """Synthetic load for testing without hardware or root."""

    method = "fake"

    def __init__(self, kind: str, base: float, swing: float, period: float):
        self.kind, self.vendor, self.name = kind, "fake", f"Fake {kind.upper()}"
        self.base, self.swing, self.period = base, swing, period

    def sample(self, dt):
        watts = self.base + self.swing * (1 + math.sin(2 * math.pi * time.time() / self.period)) / 2
        return watts, watts * dt

    def extras(self):
        return {"temp_c": 40 + self.swing / 10, "util_pct": 25}


# --------------------------------------------------------------------------- inventory


def discover_disks(sysfs: Path) -> dict[str, int]:
    counts = {"hdd": 0, "ssd": 0, "nvme": 0}
    for dev in (sysfs / "block").glob("*"):
        name = dev.name
        if name.startswith(("loop", "ram", "zram", "dm-", "md", "sr", "nbd")):
            continue
        if read_int(dev / "removable") == 1 or not (dev / "device").exists():
            continue
        if name.startswith("nvme"):
            counts["nvme"] += 1
        elif read_int(dev / "queue/rotational") == 1:
            counts["hdd"] += 1
        else:
            counts["ssd"] += 1
    return counts


def discover_usb(sysfs: Path) -> list[dict]:
    """USB devices (not hubs) with the facts HA needs to estimate their draw.

    Re-read on every request so plugging/unplugging is reflected immediately.
    """
    out = []
    for dev in sorted((sysfs / "bus/usb/devices").glob("*")):
        if ":" in dev.name or not (dev / "idVendor").exists():
            continue  # interfaces, not devices
        if read_text(dev / "bDeviceClass") == "09":
            continue  # hubs (including root hubs)
        attrs = read_text(dev / "bmAttributes") or "0"
        try:
            self_powered = bool(int(attrs, 16) & 0x40)
        except ValueError:
            self_powered = False
        max_power = read_text(dev / "bMaxPower") or "0mA"
        try:
            max_ma = int(max_power.rstrip("mA") or 0)
        except ValueError:
            max_ma = 0
        out.append({
            "port": dev.name,
            "vid": read_text(dev / "idVendor"),
            "pid": read_text(dev / "idProduct"),
            "manufacturer": read_text(dev / "manufacturer"),
            "product": read_text(dev / "product"),
            "max_power_ma": max_ma,
            "self_powered": self_powered,
            "removable": read_text(dev / "removable") or "unknown",  # fixed = internal port
            "suspended": read_text(dev / "power/runtime_status") == "suspended",
        })
    return out


CPU_TEMP_SENSORS = (("k10temp", "Tctl"), ("k10temp", "Tdie"), ("zenpower", "Tdie"), ("coretemp", "Package id 0"))
GPU_HWMON = {"amdgpu", "nouveau", "nvidia", "radeon", "i915", "xe"}


def discover_thermals(sysfs: Path) -> dict:
    """Curated temperatures and every motherboard fan header.

    Motherboard Super-I/O temperature channels are mostly unlabelled or garbage, so only
    well-known sensors are reported. GPU fans are excluded: their power is in the GPU reading.
    """
    cpu_temp = None
    cpu_rank = len(CPU_TEMP_SENSORS)
    nvme, ram, fans = [], [], []
    for hw in sorted((sysfs / "class/hwmon").glob("hwmon*"), key=lambda p: int(p.name[5:] or 0)):
        chip = read_text(hw / "name") or ""
        if chip == "nvme":
            t = read_int(hw / "temp1_input")
            dev = hw / "device"
            try:
                devname = dev.resolve().name if dev.exists() else hw.name
            except OSError:
                devname = hw.name
            if t is not None:
                nvme.append({"device": devname, "temp_c": t / 1000})
            continue
        if chip == "jc42":  # DIMM thermal sensors
            t = read_int(hw / "temp1_input")
            if t is not None:
                ram.append(t / 1000)
            continue
        for t in hw.glob("temp*_input"):
            label = read_text(t.with_name(t.name.replace("_input", "_label"))) or ""
            for rank, (c, lbl) in enumerate(CPU_TEMP_SENSORS):
                if chip == c and label == lbl and rank < cpu_rank:
                    val = read_int(t)
                    if val is not None:
                        cpu_temp, cpu_rank = val / 1000, rank
        if chip in GPU_HWMON:
            continue
        for f in sorted(hw.glob("fan*_input"), key=lambda p: int(re.sub(r"\D", "", p.name) or 0)):
            rpm = read_int(f)
            if rpm is None:
                continue
            label = read_text(f.with_name(f.name.replace("_input", "_label")))
            fans.append({"id": f"{chip}_{f.name.removesuffix('_input')}", "label": label, "rpm": rpm})
    return {"cpu_temp_c": cpu_temp, "nvme": nvme, "ram_c": ram, "fans": fans}


def read_meminfo() -> dict:
    vals = {}
    text = read_text(Path("/proc/meminfo")) or ""
    for line in text.splitlines():
        k, _, rest = line.partition(":")
        if k in ("MemTotal", "MemAvailable"):
            vals[k] = int(rest.split()[0]) * 1024
    total, avail = vals.get("MemTotal"), vals.get("MemAvailable")
    if not total or avail is None:
        return {}
    return {"total_b": total, "used_b": total - avail, "used_pct": round(100 * (total - avail) / total, 1)}


# --------------------------------------------------------------------------- network

SIOCGIFADDR = 0x8915
SIOCGIFNETMASK = 0x891B
SIOCETHTOOL = 0x8946
ETHTOOL_GWOL = 0x00000005
WAKE_MAGIC = 1 << 5


def _ifreq_ipv4(sock: socket.socket, name: str, req: int) -> str | None:
    try:
        res = fcntl.ioctl(sock.fileno(), req, struct.pack("256s", name.encode()[:15]))
    except OSError:
        return None
    return socket.inet_ntoa(res[20:24])


def _wol(sock: socket.socket, name: str) -> dict | None:
    """ETHTOOL_GWOL (needs CAP_NET_ADMIN, i.e. root). None if unknown."""
    wolinfo = ctypes.create_string_buffer(struct.pack("III6s", ETHTOOL_GWOL, 0, 0, b""), 20)
    ifreq = struct.pack("16sP", name.encode()[:15], ctypes.addressof(wolinfo)).ljust(40, b"\0")
    try:
        fcntl.ioctl(sock.fileno(), SIOCETHTOOL, ifreq)
    except OSError:
        return None
    _, supported, enabled = struct.unpack("III", wolinfo.raw[:12])
    return {"supported": bool(supported & WAKE_MAGIC), "enabled": bool(enabled & WAKE_MAGIC)}


def default_route_iface() -> str | None:
    text = read_text(Path("/proc/net/route")) or ""
    best = None
    for line in text.splitlines()[1:]:
        f = line.split()
        if len(f) > 7 and f[1] == "00000000" and f[7] == "00000000":
            metric = int(f[6])
            if best is None or metric < best[0]:
                best = (metric, f[0])
    return best[1] if best else None


def discover_network(sysfs: Path) -> dict:
    """Physical interfaces only (bridges/veth/docker have no backing device)."""
    v6: dict[str, list[str]] = {}
    for line in (read_text(Path("/proc/net/if_inet6")) or "").splitlines():
        f = line.split()
        if len(f) < 6:
            continue
        addr = ipaddress.IPv6Address(int(f[0], 16))
        scope, flags = int(f[3], 16), int(f[4], 16)
        if scope == 0 and not flags & 0x01:  # global, not a temporary privacy address
            v6.setdefault(f[5], []).append(f"{addr.compressed}/{int(f[2], 16)}")
    primary = default_route_iface()
    ifaces = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for nd in sorted((sysfs / "class/net").glob("*")):
            name = nd.name
            if name == "lo" or not (nd / "device").exists():
                continue
            ip4 = _ifreq_ipv4(sock, name, SIOCGIFADDR)
            mask = _ifreq_ipv4(sock, name, SIOCGIFNETMASK)
            ipv4 = []
            if ip4:
                prefix = ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen if mask else 32
                ipv4.append(f"{ip4}/{prefix}")
            ifaces.append({
                "name": name,
                "type": "wifi" if (nd / "wireless").exists() or (nd / "phy80211").exists() else "ethernet",
                "mac": read_text(nd / "address"),
                "up": read_text(nd / "operstate") == "up",
                "ipv4": ipv4,
                "ipv6": v6.get(name, []),
                "wol": _wol(sock, name),
            })
    return {"primary": primary, "interfaces": ifaces}


# --------------------------------------------------------------------------- session


def discover_session() -> dict | None:
    """The active graphical logind session (locked / idle hints set by the desktop)."""
    if not shutil.which("loginctl"):
        return None
    listing = run(["loginctl", "list-sessions", "--no-legend"], timeout=5)
    if listing is None:
        return None
    props = ("Name", "Type", "Class", "Desktop", "Active", "IdleHint", "IdleSinceHint", "LockedHint", "Seat")
    prop_args = [a for prop in props for a in ("-p", prop)]  # comma lists are ignored by newer systemd
    best = None
    for line in listing.splitlines():
        sid = line.split()[0] if line.split() else None
        if not sid:
            continue
        out = run(["loginctl", "show-session", sid, *prop_args], timeout=5)
        if not out:
            continue
        p = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
        if p.get("Class") != "user" or p.get("Type") not in ("wayland", "x11", "mir"):
            continue
        score = (p.get("Active") == "yes", p.get("Seat") == "seat0")
        if best is None or score > best[0]:
            best = (score, {**p, "Id": sid})
    if best is None:
        return {"user": None, "active": False}
    p = best[1]
    since_us = int(p.get("IdleSinceHint") or 0)
    idle = p.get("IdleHint") == "yes"
    return {
        "id": p.get("Id"),
        "user": p.get("Name"),
        "type": p.get("Type"),
        "desktop": p.get("Desktop") or None,
        "active": p.get("Active") == "yes",
        "locked": p.get("LockedHint") == "yes",
        "idle": idle,
        "idle_since": since_us / 1e6 if idle and since_us else None,
    }


# --------------------------------------------------------------------------- monitors


def edid_size_in(edid: bytes) -> float | None:
    if len(edid) < 23 or edid[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        return None
    w_cm, h_cm = edid[21], edid[22]
    if not w_cm or not h_cm:
        return None
    return round(math.hypot(w_cm, h_cm) / 2.54, 1)


def parse_ddc_detect(text: str) -> list[dict]:
    """Parse `ddcutil detect --terse`."""
    out = []
    for block in re.split(r"\n\s*\n", text.strip()):
        if not block.startswith("Display"):
            continue  # "Invalid display" blocks etc.
        bus = re.search(r"I2C bus:\s*/dev/i2c-(\d+)", block)
        mon = re.search(r"Monitor:\s*(.*)", block)
        conn = re.search(r"DRM connector:\s*(\S+)", block)
        if not bus:
            continue
        mfg, model, serial = ((mon.group(1).strip() if mon else "") + "::").split(":")[:3]
        out.append({
            "bus": int(bus.group(1)),
            "connector": conn.group(1) if conn else None,
            "mfg": mfg or None,
            "model": model or None,
            "serial": serial.strip("0") or None,
        })
    return out


def parse_ddc_getvcp(text: str) -> dict:
    """Parse `ddcutil getvcp 10 D6 --terse`."""
    out = {}
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 5 and f[0] == "VCP" and f[1] == "10" and f[2] == "C":
            out["brightness"], out["max_brightness"] = int(f[3]), int(f[4])
        elif len(f) >= 4 and f[0] == "VCP" and f[1].upper() == "D6":
            code = int(f[3].lstrip("x"), 16)
            out["power_mode"] = {1: "on", 2: "standby", 3: "suspend", 4: "off", 5: "off"}.get(code, "unknown")
    return out


# For the frequent reads/writes. Without --skip-ddc-checks, ddcutil re-probes how each monitor
# reports unsupported features on every invocation and some monitors make it log 4 warnings to
# syslog each time (--syslog=NEVER does not silence them). Skipping is also ~5-10x faster.
# `detect` (every 10 min) keeps the checks.
DDC_FAST = ["ddcutil", "--skip-ddc-checks"]


class MonitorPoller(threading.Thread):
    """Reads monitors over DDC/CI in the background (slow: ~1 s per monitor)."""

    REDETECT_S = 600

    def __init__(self, sysfs: Path, interval: float):
        super().__init__(daemon=True, name="monitors")
        self.sysfs = sysfs
        self.interval = interval
        self.available = shutil.which("ddcutil") is not None
        self.lock = threading.Lock()
        self.ddc_lock = threading.Lock()  # serialise ddcutil calls (setvcp vs polling)
        self.displays: list[dict] = []
        self.state: dict[int, dict] = {}
        self._detected_at = 0.0
        self._wake = threading.Event()

    def _detect(self):
        with self.ddc_lock:
            text = run(["ddcutil", "detect", "--terse"], timeout=30)
        displays = parse_ddc_detect(text or "")
        for d in displays:
            d["size_in"] = None
            if d["connector"]:
                try:
                    d["size_in"] = edid_size_in((self.sysfs / "class/drm" / d["connector"] / "edid").read_bytes())
                except OSError:
                    pass
        with self.lock:
            self.displays = displays
            self.state = {k: v for k, v in self.state.items() if any(d["bus"] == k for d in displays)}
        self._detected_at = time.monotonic()
        LOG.info("monitors: %s", ", ".join(f"{d['mfg']} {d['model']} (bus {d['bus']})" for d in displays) or "none")

    def _poll(self):
        with self.lock:
            buses = [d["bus"] for d in self.displays]
        missing = False
        for bus in buses:
            with self.ddc_lock:
                text = run([*DDC_FAST, "--bus", str(bus), "getvcp", "10", "D6", "--terse"], timeout=15)
            if text is None:  # asleep monitors often stop answering DDC
                vals = {"power_mode": "unknown", "responding": False}
                missing = True
            else:
                vals = {**parse_ddc_getvcp(text), "responding": True}
            vals["read_at"] = time.time()
            with self.lock:
                prev = self.state.get(bus, {})
                self.state[bus] = {**prev, **vals}
        return missing

    def run(self):
        if not self.available:
            return
        while True:
            try:
                if not self.displays or time.monotonic() - self._detected_at > self.REDETECT_S:
                    self._detect()
                self._poll()
            except Exception:
                LOG.exception("monitor poll failed")
            self._wake.wait(self.interval)
            self._wake.clear()

    def set_brightness(self, bus: int, value: int) -> bool:
        with self.lock:
            if not any(d["bus"] == bus for d in self.displays):
                return False
        with self.ddc_lock:
            ok = run([*DDC_FAST, "--bus", str(bus), "setvcp", "10", str(value)], timeout=15) is not None
        if ok:
            with self.lock:
                self.state.setdefault(bus, {})["brightness"] = value
        return ok

    def snapshot(self) -> list[dict] | None:
        if not self.available:
            return None
        with self.lock:
            out = [{**d, **self.state.get(d["bus"], {})} for d in self.displays]
        for m in out:
            # The kernel's view is instant and reliable; DDC/CI is slow and sleeping monitors
            # often stop answering it. "dpms" is None when the connector is unknown.
            m["dpms"] = None
            if m.get("connector"):
                conn = self.sysfs / "class/drm" / m["connector"]
                dpms, enabled = read_text(conn / "dpms"), read_text(conn / "enabled")
                if dpms is not None:
                    m["dpms"] = "on" if dpms == "On" and enabled != "disabled" else "off"
        return out


# --------------------------------------------------------------------------- sampler


class Channel:
    def __init__(self, source: Source, index: int):
        self.source = source
        self.index = index
        self.power_w: float | None = None
        self.energy_j = 0.0
        self.ok = False

    def as_dict(self) -> dict:
        s = self.source
        try:
            extras = s.extras()
        except Exception:
            LOG.debug("extras for %s failed", s.name, exc_info=True)
            extras = {}
        return {
            "index": self.index,
            "name": s.name,
            "vendor": s.vendor,
            "method": s.method,
            "available": self.ok,
            "power_w": None if self.power_w is None else round(self.power_w, 2),
            "energy_j": round(self.energy_j, 3),
            **extras,
        }


def read_cpu_times() -> tuple[int, int] | None:
    line = (read_text(Path("/proc/stat")) or "").split("\n", 1)[0]
    f = line.split()
    if not f or f[0] != "cpu":
        return None
    vals = [int(x) for x in f[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return sum(vals[:8]), idle


class Sampler(threading.Thread):
    def __init__(self, cpu: Source | None, gpus: list[Source], interval: float):
        super().__init__(daemon=True, name="sampler")
        self.cpu = Channel(cpu, 0) if cpu else None
        self.gpus = [Channel(g, i) for i, g in enumerate(gpus)]
        self.interval = interval
        self.lock = threading.Lock()
        self.last_sample = 0.0
        self.cpu_util: float | None = None
        self._cpu_times = read_cpu_times()

    def _tick(self, dt: float):
        for ch in ([self.cpu] if self.cpu else []) + self.gpus:
            try:
                res = ch.source.sample(dt)
            except Exception:  # never let one bad source kill the sampler
                LOG.exception("sampling %s failed", ch.source.name)
                res = None
            if res is None:
                ch.ok = False
                continue
            watts, joules = res
            ch.ok = True
            ch.power_w = watts
            ch.energy_j += max(joules, 0.0)
        now = read_cpu_times()
        if now and self._cpu_times:
            total, idle = now[0] - self._cpu_times[0], now[1] - self._cpu_times[1]
            if total > 0:
                self.cpu_util = round(100 * (total - idle) / total, 1)
        self._cpu_times = now

    def run(self):
        last = time.monotonic()
        self._tick(0.0)  # prime counters
        while True:
            time.sleep(self.interval)
            now = time.monotonic()
            with self.lock:
                self._tick(now - last)
                self.last_sample = time.time()
            last = now

    def snapshot(self) -> dict:
        with self.lock:
            cpu = self.cpu.as_dict() if self.cpu else None
            if cpu is not None:
                cpu["util_pct"] = self.cpu_util
            return {
                "cpu": cpu,
                "gpus": [g.as_dict() for g in self.gpus],
                "cpu_util_pct": self.cpu_util,
                "sampled_at": self.last_sample,
            }


# --------------------------------------------------------------------------- http


def machine_id() -> str:
    for p in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
        mid = read_text(p)
        if mid:
            return mid
    return uuid.uuid5(uuid.NAMESPACE_DNS, socket.gethostname()).hex


class Agent:
    def __init__(self, cfg: dict, sampler: Sampler, monitors: MonitorPoller, disks: dict, sysfs: Path):
        self.cfg = cfg
        self.sysfs = sysfs
        self.sampler = sampler
        self.monitors = monitors
        self.disks = disks
        self.session_id = uuid.uuid4().hex
        self.hostname = socket.gethostname()
        self.machine_id = machine_id()
        self.boot_id = read_text(Path("/proc/sys/kernel/random/boot_id")) or ""
        self._session_cache: tuple[float, dict | None] = (0.0, None)

    def session(self) -> dict | None:
        ts, val = self._session_cache
        if time.monotonic() - ts > 5:
            val = discover_session()
            self._session_cache = (time.monotonic(), val)
        return val

    def capabilities(self) -> dict:
        return {
            "power": sorted(POWER_ACTIONS) if self.cfg["allow_power_control"] else [],
            "monitor_brightness": bool(self.cfg["allow_monitor_control"] and self.monitors.available),
            "lock": bool(self.cfg["allow_session_lock"] and shutil.which("loginctl")),
        }

    def metrics(self) -> dict:
        uptime = read_text(Path("/proc/uptime"))
        return {
            "api": API_VERSION,
            "agent_version": VERSION,
            "hostname": self.hostname,
            "machine_id": self.machine_id,
            "boot_id": self.boot_id,
            "session_id": self.session_id,
            "uptime_s": float(uptime.split()[0]) if uptime else None,
            "timestamp": time.time(),
            "awake_s": time.monotonic(),  # CLOCK_MONOTONIC: does not advance while suspended
            "capabilities": self.capabilities(),
            "disks": self.disks,
            "usb": discover_usb(self.sysfs),
            "thermals": discover_thermals(self.sysfs),
            "memory": read_meminfo(),
            "network": discover_network(self.sysfs),
            "session": self.session(),
            "monitors": self.monitors.snapshot(),
            **self.sampler.snapshot(),
        }

    def power(self, action: str) -> tuple[int, dict]:
        if not self.cfg["allow_power_control"]:
            return 403, {"error": "power control disabled (allow_power_control = false)"}
        cmd = POWER_ACTIONS.get(action)
        if cmd is None:
            return 404, {"error": f"unknown action {action}"}
        if self.cfg["power_dry_run"]:
            LOG.warning("DRY RUN: would run %s", " ".join(cmd))
            return 202, {"ok": True, "action": action, "dry_run": True}

        def later():
            time.sleep(1.0)  # let the HTTP response go out first
            LOG.warning("power action: %s", " ".join(cmd))
            subprocess.run(cmd, check=False)

        threading.Thread(target=later, daemon=True).start()
        return 202, {"ok": True, "action": action}

    def lock(self) -> tuple[int, dict]:
        if not self.capabilities()["lock"]:
            return 403, {"error": "screen locking disabled (allow_session_lock = false) or loginctl missing"}
        session = discover_session()  # fresh, not cached: the active session may have changed
        sid = (session or {}).get("id")
        if not sid:
            return 409, {"error": "no active graphical session to lock"}
        if (session or {}).get("locked"):
            return 200, {"ok": True, "already_locked": True}
        cmd = ["loginctl", "lock-session", sid]
        if self.cfg["lock_dry_run"]:
            LOG.warning("DRY RUN: would run %s", " ".join(cmd))
            return 202, {"ok": True, "dry_run": True, "session": sid}
        if run(cmd, timeout=10) is None:
            return 502, {"error": "loginctl lock-session failed"}
        self._session_cache = (0.0, None)  # next poll re-reads LockedHint
        LOG.info("locked session %s (%s)", sid, session.get("user"))
        return 200, {"ok": True, "session": sid}

    def brightness(self, bus: int, value) -> tuple[int, dict]:
        if not self.capabilities()["monitor_brightness"]:
            return 403, {"error": "monitor control disabled or ddcutil missing"}
        try:
            value = int(value)
        except (TypeError, ValueError):
            return 400, {"error": "value must be an integer"}
        if not 0 <= value <= 100:
            return 400, {"error": "value must be 0-100"}
        if not self.monitors.set_brightness(bus, value):
            return 502, {"error": f"setting brightness on bus {bus} failed"}
        return 200, {"ok": True, "bus": bus, "brightness": value}


def make_handler(agent: Agent):
    token = agent.cfg["token"]

    class Handler(BaseHTTPRequestHandler):
        server_version = f"pc-power-agent/{VERSION}"

        def log_message(self, fmt, *args):
            LOG.debug("%s %s", self.address_string(), fmt % args)

        def _send(self, code: int, body: dict):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorised(self) -> bool:
            auth = self.headers.get("Authorization", "")
            if token and hmac.compare_digest(auth.encode(), f"Bearer {token}".encode()):
                return True
            self._send(401, {"error": "unauthorized"})
            return False

        def do_GET(self):
            if self.path == "/v1/health":
                return self._send(200, {"ok": True, "api": API_VERSION, "machine_id": agent.machine_id})
            if self.path != "/v1/metrics":
                return self._send(404, {"error": "not found"})
            if self._authorised():
                self._send(200, agent.metrics())

        def do_POST(self):
            if not self._authorised():
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > 4096:
                return self._send(413, {"error": "body too large"})
            try:
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            except ValueError:
                return self._send(400, {"error": "invalid JSON"})
            if self.path == "/v1/session/lock":
                return self._send(*agent.lock())
            if m := re.fullmatch(r"/v1/power/(\w+)", self.path):
                return self._send(*agent.power(m.group(1)))
            if m := re.fullmatch(r"/v1/monitors/(\d+)/brightness", self.path):
                return self._send(*agent.brightness(int(m.group(1)), body.get("value")))
            self._send(404, {"error": "not found"})

    return Handler


# --------------------------------------------------------------------------- main


def summarise(d: dict) -> str:
    lines = [f"CPU:      {(d.get('cpu') or {}).get('method') or 'not available (RAPL unreadable)'}"]
    for g in d.get("gpus") or []:
        lines.append(f"GPU:      {g['name']} via {g['method']}")
    mons = d.get("monitors")
    lines.append("Monitors: " + ("ddcutil not installed" if mons is None else
                                 ", ".join(f"{m.get('mfg')} {m.get('model')}" for m in mons) or "none found"))
    caps = d.get("capabilities") or {}
    lines.append(f"Power control: {'enabled' if caps.get('power') else 'disabled (allow_power_control = false)'}")
    lines.append(f"Screen lock:   {'enabled' if caps.get('lock') else 'disabled'}")
    net = d.get("network") or {}
    for i in net.get("interfaces") or []:
        ip = ", ".join(i.get("ipv4") or []) or "no IPv4"
        lines.append(f"Network:  {i['name']} ({i['type']}, {'up' if i.get('up') else 'down'}) {i.get('mac')} {ip}")
        wol = i.get("wol") or {}
        if i.get("type") == "ethernet" and wol.get("supported") and not wol.get("enabled"):
            lines.append(f"  note: Wake-on-LAN is supported but disabled on {i['name']}. Enable it with:")
            lines.append(f"    nmcli connection modify \"$(nmcli -g GENERAL.CONNECTION device show {i['name']})\" "
                         "802-3-ethernet.wake-on-lan magic")
            lines.append("    and enable Wake-on-LAN / 'Power On By PCI-E' in the BIOS.")
    if net.get("primary") and all(i["name"] != net["primary"] or i["type"] == "wifi" for i in net.get("interfaces") or []):
        lines.append("  note: this PC is on Wi-Fi; Wake-on-LAN generally needs a wired connection.")
    return "\n".join(lines)


def load_config(path: str | None) -> dict:
    cfg = dict(DEFAULTS)
    if path:
        with open(path, "rb") as f:
            cfg.update(tomllib.load(f))
    if env := os.environ.get("PC_POWER_TOKEN"):
        cfg["token"] = env
    return cfg


def build_sources(args, cfg) -> tuple[Source | None, list[Source]]:
    if args.fake:
        return FakeSource("cpu", 15, 60, 120), [FakeSource("gpu", 20, 150, 300)]
    sysfs = Path(args.sysfs_root)
    cpu = RaplSource.discover(sysfs)
    gpus: list[Source] = []
    if not args.no_nvml:
        gpus += NvidiaSource.discover(cfg.get("gpu_fallback_w"))
    gpus += AmdHwmonSource.discover(sysfs)
    return cpu, gpus


def main(argv=None):
    p = argparse.ArgumentParser(description="PC Power agent for Home Assistant")
    p.add_argument("--config", default="/etc/pc-power/config.toml" if os.path.exists("/etc/pc-power/config.toml") else None)
    p.add_argument("--sysfs-root", default="/sys", help="read sysfs from here (for testing with captured trees)")
    p.add_argument("--fake", action="store_true", help="synthetic CPU/GPU data, no hardware or root needed")
    p.add_argument("--no-nvml", action="store_true", help="skip NVIDIA detection")
    p.add_argument("--once", action="store_true", help="sample for a few seconds, print metrics JSON, exit")
    p.add_argument("--summary", action="store_true", help="like --once, but a human-readable summary with hints")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")

    try:
        cfg = load_config(args.config)
    except PermissionError:
        p.error(f"cannot read {args.config}: run with sudo (the config holds the token and is root-only)")
    except (OSError, tomllib.TOMLDecodeError) as err:
        p.error(f"bad config {args.config}: {err}")
    cpu, gpus = build_sources(args, cfg)
    if cpu is None and not args.fake:
        LOG.warning("no readable RAPL package domain (not root, or unsupported CPU)")
    for s in ([cpu] if cpu else []) + gpus:
        LOG.info("source: %s %s %s via %s", s.kind, s.vendor, s.name, s.method)
    if cfg["allow_power_control"]:
        LOG.warning("power control ENABLED%s", " (dry run)" if cfg["power_dry_run"] else "")

    sysfs = Path(args.sysfs_root)
    sampler = Sampler(cpu, gpus, float(cfg["sample_interval"]))
    sampler.start()
    monitors = MonitorPoller(sysfs, float(cfg["monitor_interval"]))
    if not monitors.available:
        LOG.info("ddcutil not found: monitor reporting disabled")
    monitors.start()
    agent = Agent(cfg, sampler, monitors, discover_disks(sysfs), sysfs)

    if args.once or args.summary:
        deadline = time.monotonic() + 15
        time.sleep(max(2.0, 2 * sampler.interval) + 0.1)
        while monitors.available and time.monotonic() < deadline and (
            not monitors.displays or len(monitors.state) < len(monitors.displays)
        ):
            time.sleep(0.25)  # give DDC/CI a chance so --once shows monitors
        data = agent.metrics()
        print(summarise(data) if args.summary else json.dumps(data, indent=2))
        return 0

    if not cfg["token"]:
        p.error("no token configured (set token in config.toml or PC_POWER_TOKEN)")
    server = ThreadingHTTPServer((cfg["listen"], int(cfg["port"])), make_handler(agent))
    LOG.info("listening on %s:%s", cfg["listen"], cfg["port"])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
