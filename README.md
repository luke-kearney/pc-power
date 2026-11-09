<p align="center"><img src="custom_components/pc_power/brand/logo@2x.png" alt="PC Power" height="96"></p>

# PC Power for Home Assistant

Per-PC power, energy, monitors and control for Linux PCs. A tiny agent on each PC reports **measured** CPU and GPU power; Home Assistant adds well-reasoned estimates for everything else and turns it into an Energy-dashboard-ready kWh sensor that is correct even when the PC is off, asleep or unreachable.

- **Measured**: CPU package power (RAPL, Intel and AMD Zen), GPU board power (NVIDIA via NVML, AMD via amdgpu hwmon, including RDNA4)
- **Estimated**: disks, fans & pump (scaled by real fan RPM), RGB, motherboard, USB devices (known-device table + per-device overrides), PSU efficiency
- **Monitors** over DDC/CI: on/standby, brightness, power estimate from panel size, brightness sliders
- **Control**: Wake-on-LAN, lock screen, suspend / shut down / restart (opt-in)
- **Status**: online, screen locked, idle, CPU/GPU temperature & usage, memory, NVMe/RAM temperatures, fan speeds, IP addresses
- **Correct energy**: exact counter deltas (poll rate doesn't matter), no stale "last value" while the PC is off, suspend time billed at standby power, no double counting after network blips or HA restarts

Supported: Linux with systemd (developed on CachyOS/Arch; Ubuntu and Pop!_OS should work). Home Assistant 2026.11+.

## How it works

```
 each PC                                     Home Assistant
┌──────────────────────────────┐   GET /v1/metrics (token)   ┌───────────────────────────────┐
│ pc-power-agent (systemd)     │ ◀─────────────────────────── │ PC Power integration          │
│  RAPL / NVML / hwmon counters│    every 10 s (configurable) │  energy accounting, estimates │
│  USB, monitors, network, ... │ ──────────────────────────▶ │  sensors, buttons, sliders    │
└──────────────────────────────┘                              └───────────────────────────────┘
```

The agent turns every power source into a monotonically increasing energy counter, so Home Assistant computes energy from deltas: missed polls lose nothing. If the PC stops answering for a few polls it is treated as off: power drops to your configured standby figure and is billed at that rate until it's back.

## Install

### 1. The agent (on each PC)

```bash
git clone https://github.com/luke-kearney/pc-power
cd pc-power/agent
sudo ./install.sh
```

The installer copies the agent to `/usr/local/lib/pc-power`, installs and starts `pc-power-agent.service`, creates `/etc/pc-power/config.toml` with a random token, advertises the agent over mDNS (if avahi is installed) and opens the port in ufw/firewalld if active. It finishes with a summary like:

```
CPU:      rapl
GPU:      NVIDIA GeForce RTX 3060 Ti via nvml-energy
Monitors: AOC 24G1WG4, WOO WV27QFFIPG
Power control: disabled (allow_power_control = false)
Screen lock:   enabled
Network:  enp7s0 (ethernet, up) aa:bb:cc:dd:ee:ff 192.168.1.50/24
PC Power agent running on port 8765
Token: ...
```

Optional: `ddcutil` for monitors (`sudo pacman -S ddcutil` / `sudo apt install ddcutil`).

Upgrade by pulling and re-running `sudo ./install.sh` (your config and token are kept). Uninstall with `sudo ./install.sh --uninstall`.

### 2. The integration (in Home Assistant)

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/luke-kearney/pc-power`, type **Integration**.
2. Install **PC Power**, restart Home Assistant.
3. PCs are discovered automatically (Settings → Devices & services). Otherwise **Add integration → PC Power** and enter host, port `8765` and the token.

## Agent configuration

`/etc/pc-power/config.toml` (restart the service after changes: `sudo systemctl restart pc-power-agent`):

| Key | Default | |
|---|---|---|
| `listen`, `port` | `0.0.0.0`, `8765` | |
| `token` | generated | Every request needs `Authorization: Bearer <token>` |
| `sample_interval` | `1.0` | Seconds between power samples |
| `gpu_fallback_w` | unset | Watts to assume for an NVIDIA GPU that reports no power (some GTX 10xx) |
| `allow_power_control` | `false` | Adds Suspend / Shut down / Restart buttons in HA |
| `allow_session_lock` | `true` | Adds a Lock screen button |
| `allow_monitor_control` | `true` | Brightness sliders over DDC/CI |
| `monitor_interval` | `30` | Seconds between DDC/CI reads (~1 s per monitor) |
| `power_dry_run`, `lock_dry_run` | `false` | Log actions instead of performing them (testing) |

## Integration options

Settings → Devices & services → PC Power → **Configure**:

1. **Power estimate**: poll interval; disks, HBA/RAID controller, fans & AIO pump at full speed (scaled by measured fan speed), RGB on headers (GPU RGB is already in the GPU reading), motherboard; PSU efficiency; standby power when off; monitor standby; failed polls before "off".
2. **USB devices**: one field per connected device, pre-filled from a known-device table or a capped guess from the device's claimed current.
3. **Monitors**: power at 100 % brightness per monitor (defaults from the EDID panel size).

```
PC power   = (CPU + GPU + disks + controller + fans × speed³ + RGB + board + USB) ÷ PSU efficiency
Monitors   = max_W × (0.4 + 0.6 × brightness)    (wall-powered, separate sensors)
```

## Entities

| Entity | Notes |
|---|---|
| Energy, Monitors energy | `total_increasing` kWh, add to the Energy dashboard under *Individual devices* |
| Estimated power, CPU power, GPU power, USB devices power, Monitors power | W |
| Online, Screen locked, Idle, Idle since | Lock/idle come from logind hints set by the desktop |
| CPU/GPU temperature, CPU/GPU usage, Memory used, IP address | IP shows the last known address while off |
| RAM temperature, NVMe temperatures, Fan speeds | Disabled by default |
| Wake, Lock screen, Suspend, Shut down, Restart | Buttons (power ones only with `allow_power_control`) |
| *Monitor* brightness | Slider per monitor |

### Example: suspend an idle PC

```yaml
automation:
  - alias: Suspend idle desktop
    triggers:
      - trigger: state
        entity_id: binary_sensor.my_pc_screen_locked
        to: "on"
        for: "00:30:00"
    conditions:
      - condition: numeric_state
        entity_id: sensor.my_pc_gpu_usage
        below: 10
    actions:
      - action: button.press
        target:
          entity_id: button.my_pc_suspend
```

## Wake-on-LAN

Needs a wired connection, WoL enabled in the BIOS ("Power On By PCI-E") and in the OS. The installer summary tells you if it's supported but disabled, and prints the `nmcli` command to enable it.

## Security

- The agent runs as root (RAPL, I²C and power actions require it) under a hardened systemd unit.
- Every request needs the per-PC token; keep the port on your LAN.
- Power control is **off** by default. There is deliberately no remote unlock.

## Development

```bash
cd tests && python3 -m unittest            # no Home Assistant needed
docker compose -f dev/compose.yaml up -d   # throwaway HA on :8123 + containerised agent
```

The dev agent runs as root in a container (so RAPL is readable without sudo) with power actions in dry-run. Brand images are generated from `assets/*.svg` with `assets/build.sh`.

## License

MIT
