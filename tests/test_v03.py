import tempfile
import unittest
from pathlib import Path

from _load import load

E = load("pc_power_energy", "custom_components/pc_power/energy.py")
A = load("pc_power_agent", "agent/pc_power_agent.py")

P = dict(disks_w=0, controller_w=0, fans_w=0, fans_dynamic=False, rgb_w=0, board_w=10, efficiency=0.5,
         offline_w=2, monitors_off_w=1, grace_polls=3)


def r(ts, cpu_j=0.0, awake=None, session="s", monitors_w=0.0, fans_fraction=None):
    return E.Reading(session, ts, 0.0, cpu_j, 0.0, 0.0, monitors_w=monitors_w, awake_s=awake,
                     fans_fraction=fans_fraction)


class SuspendTests(unittest.TestCase):
    def test_suspend_does_not_bill_peripherals(self):
        """8 h suspended: same agent session on resume, but awake clock only moved 60 s."""
        a = E.Accountant(E.Profile(**P))
        a.on_reading(r(0, 0, awake=1000))
        t = 0
        for _ in range(3):
            t += 10
            a.on_failure(t)
        a.on_failure(8 * 3600)
        a.on_reading(r(8 * 3600 + 60, 0, awake=1000 + 90))
        dt = 8 * 3600 + 60
        expected = (10 * 90 / 0.5 + 2 * (dt - 90)) / 3600  # board while awake, standby while asleep
        self.assertAlmostEqual(a.state.energy_wh, expected, places=6)

    def test_without_awake_clock_falls_back_to_wall_time(self):
        a = E.Accountant(E.Profile(**P))
        a.on_reading(r(0)); a.on_reading(r(3600))
        self.assertAlmostEqual(a.state.energy_wh, 10 / 0.5)


class MonitorEnergyTests(unittest.TestCase):
    def test_monitors_accrue_separately_without_psu_loss(self):
        a = E.Accountant(E.Profile(**P))
        a.on_reading(r(0, monitors_w=50))
        s = a.on_reading(r(3600, monitors_w=50))
        self.assertAlmostEqual(s.monitors_kwh, 0.05)
        self.assertAlmostEqual(s.energy_kwh, 0.02)  # PC unaffected

    def test_monitors_standby_while_offline_no_double_count(self):
        a = E.Accountant(E.Profile(**P))
        a.on_reading(r(0, monitors_w=50))
        for t in (10, 20, 1800):
            a.on_failure(t)
        self.assertAlmostEqual(a.state.monitors_wh, 1 * 1800 / 3600)
        a.on_reading(r(3600, monitors_w=50))  # same session: was on all along (network blip)
        self.assertAlmostEqual(a.state.monitors_wh, 50.0)


class FanTests(unittest.TestCase):
    def test_fraction_learns_and_cubes(self):
        fans = [{"id": "a", "rpm": 900}, {"id": "b", "rpm": 0}]
        frac, mx = E.fan_fraction(fans, {})
        self.assertEqual(mx, {"a": 1200})  # seeded; b never spun
        self.assertAlmostEqual(frac, (900 / 1200) ** 3, places=4)
        frac, mx = E.fan_fraction([{"id": "a", "rpm": 1800}], mx)
        self.assertEqual((frac, mx["a"]), (1.0, 1800))
        frac, mx = E.fan_fraction([{"id": "a", "rpm": 300}, {"id": "b", "rpm": 0}], mx)
        self.assertAlmostEqual(frac, E.FAN_FLOOR)  # (1/6)^3 < floor

    def test_stopped_fan_counts_zero(self):
        frac, _ = E.fan_fraction([{"id": "a", "rpm": 0}, {"id": "b", "rpm": 1200}], {"a": 1500, "b": 1200})
        self.assertAlmostEqual(frac, 0.5)

    def test_no_fans(self):
        self.assertEqual(E.fan_fraction(None, {})[0], None)

    def test_dynamic_fans_feed_wall_power(self):
        prof = E.Profile(**{**P, "fans_w": 10, "fans_dynamic": True, "efficiency": 1.0})
        a = E.Accountant(prof)
        s = a.on_reading(r(0, fans_fraction=0.25))
        self.assertAlmostEqual(s.fans_w, 2.5)
        self.assertAlmostEqual(s.wall_w, 10 + 2.5)


EXAMPLE_MONITORS = [
    {"bus": 2, "mfg": "AOC", "model": "24G1WG4", "serial": None, "size_in": 23.4, "brightness": 60,
     "max_brightness": 100, "power_mode": "on", "responding": True, "dpms": "on"},
    {"bus": 3, "mfg": "WOO", "model": "WV27QFFIPG", "serial": None, "size_in": 27.2, "brightness": 44,
     "max_brightness": 100, "power_mode": "on", "responding": True, "dpms": "on"},
]


class MonitorEstimateTests(unittest.TestCase):
    def test_example_monitors(self):
        total, rows = E.estimate_monitors(EXAMPLE_MONITORS)
        self.assertEqual([r["key"] for r in rows], ["aoc_24g1wg4", "woo_wv27qffipg"])
        self.assertEqual(rows[0]["device"], 'AOC 24G1WG4 (23")')
        self.assertAlmostEqual(rows[0]["watts"], round(28.1 * (0.4 + 0.6 * 0.6), 1))
        self.assertAlmostEqual(total, rows[0]["watts"] + rows[1]["watts"])

    def test_dpms_off_is_standby_even_if_ddc_says_on(self):
        m = [{**EXAMPLE_MONITORS[0], "dpms": "off"}]
        self.assertEqual(E.estimate_monitors(m, standby_w=0.4)[0], 0.4)

    def test_unresponsive_after_answering_is_standby_never_answered_is_on(self):
        asleep = {"bus": 1, "mfg": "X", "model": "Y", "brightness": 50, "responding": False, "power_mode": "unknown"}
        no_ddc = {"bus": 2, "mfg": "X", "model": "Z", "responding": False, "power_mode": "unknown"}
        self.assertEqual(E.monitor_state(asleep), "standby")
        self.assertEqual(E.monitor_state(no_ddc), "on")

    def test_identical_twins_get_distinct_keys(self):
        twins = [{"bus": 2, "mfg": "DEL", "model": "U2720Q"}, {"bus": 5, "mfg": "DEL", "model": "U2720Q"}]
        self.assertEqual(E.monitor_keys(twins), ["del_u2720q_bus2", "del_u2720q_bus5"])

    def test_override(self):
        total, rows = E.estimate_monitors(EXAMPLE_MONITORS[:1], {"aoc_24g1wg4": 20})
        self.assertEqual(rows[0]["max_w"], 20)


DETECT = """Display 1
   I2C bus:          /dev/i2c-2
   DRM connector:    card1-HDMI-A-1
   drm_connector_id: 834
   Monitor:          AOC:24G1WG4:

Display 2
   I2C bus:          /dev/i2c-3
   DRM connector:    card1-DP-1
   drm_connector_id: 839
   Monitor:          WOO:WV27QFFIPG:0000000000000

Invalid display
   I2C bus:          /dev/i2c-5
"""


class AgentParserTests(unittest.TestCase):
    def test_ddc_detect(self):
        d = A.parse_ddc_detect(DETECT)
        self.assertEqual([(x["bus"], x["mfg"], x["model"], x["serial"], x["connector"]) for x in d],
                         [(2, "AOC", "24G1WG4", None, "card1-HDMI-A-1"), (3, "WOO", "WV27QFFIPG", None, "card1-DP-1")])

    def test_ddc_getvcp(self):
        self.assertEqual(A.parse_ddc_getvcp("VCP 10 C 60 100\nVCP D6 SNC x01\n"),
                         {"brightness": 60, "max_brightness": 100, "power_mode": "on"})
        self.assertEqual(A.parse_ddc_getvcp("VCP D6 SNC x04")["power_mode"], "off")

    def test_edid_size(self):
        edid = bytearray(128)
        edid[:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
        edid[21], edid[22] = 60, 34  # 27" 16:9
        self.assertAlmostEqual(A.edid_size_in(bytes(edid)), 27.2)
        self.assertIsNone(A.edid_size_in(b"junk"))

    def test_thermals(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)

            def chip(n, name, **files):
                d = root / "class/hwmon" / f"hwmon{n}"
                d.mkdir(parents=True)
                (d / "name").write_text(name + "\n")
                for k, v in files.items():
                    (d / k).write_text(v + "\n")

            chip(0, "nct6798", fan1_input="902", fan3_input="0", temp5_input="-61000", temp5_label="AUXTIN2")
            chip(1, "k10temp", temp1_input="51500", temp1_label="Tctl", temp3_input="43500", temp3_label="Tccd1")
            chip(2, "jc42", temp1_input="41750")
            chip(3, "amdgpu", fan1_input="1500", temp1_input="60000", temp1_label="edge")
            chip(4, "nvme", temp1_input="41850", temp1_label="Composite")
            th = A.discover_thermals(root)
            self.assertEqual(th["cpu_temp_c"], 51.5)
            self.assertEqual(th["ram_c"], [41.75])
            self.assertEqual([f["id"] for f in th["fans"]], ["nct6798_fan1", "nct6798_fan3"])  # no GPU fan
            self.assertEqual(th["nvme"][0]["temp_c"], 41.85)


class DdcCommandTests(unittest.TestCase):
    def test_poll_and_set_skip_ddc_checks(self):
        from unittest import mock
        poller = A.MonitorPoller.__new__(A.MonitorPoller)
        A.MonitorPoller.__init__(poller, Path("/nonexistent"), 30)
        poller.displays = [{"bus": 3}]
        with mock.patch.object(A, "run", return_value="VCP 10 C 54 100\nVCP D6 SNC x01\n") as run:
            poller._poll()
            poller.set_brightness(3, 50)
        for call in run.call_args_list:
            self.assertEqual(call.args[0][:2], ["ddcutil", "--skip-ddc-checks"])
        self.assertEqual(poller.state[3]["brightness"], 50)


if __name__ == "__main__":
    unittest.main()
