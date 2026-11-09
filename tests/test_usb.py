import tempfile
import unittest
from pathlib import Path

from _load import load

E = load("pc_power_energy", "custom_components/pc_power/energy.py")
A = load("pc_power_agent", "agent/pc_power_agent.py")


def dev(vid, pid, product, ma, removable="removable", self_powered=False, suspended=False, manufacturer=None):
    return {"vid": vid, "pid": pid, "product": product, "manufacturer": manufacturer, "max_power_ma": ma,
            "removable": removable, "self_powered": self_powered, "suspended": suspended}


# A real desktop: Bluetooth + AURA (built in), mic, keyboard, mouse dongle, Ajazz stream deck
EXAMPLE_PC = [
    dev("8087", "0029", None, 100, removable="fixed", self_powered=True, suspended=True),
    dev("0b05", "1939", "AURA LED Controller", 16, removable="fixed"),
    dev("17a0", "0001", "Samson C01U", 90),
    dev("320f", "50b7", "MONTECH MK087", 400),
    dev("37b0", "002e", "LAMZU 8K Dongle V2 Dongle", 500),
    dev("0300", "3010", "HOTSPOTEKUSB HID DEMO", 100, manufacturer="HOTSPOTEKUSB"),
]


class UsbEstimateTests(unittest.TestCase):
    def test_example_pc(self):
        total, rows = E.estimate_usb(EXAMPLE_PC)
        by = {r["key"]: r for r in rows}
        self.assertEqual(by["0b05_1939"]["watts"], 0.0)  # AURA: LEDs are on the RGB field
        self.assertEqual(by["8087_0029"]["watts"], 0.0)  # built-in bluetooth
        self.assertEqual(by["0300_3010"]["device"], "Ajazz stream deck")
        self.assertEqual(by["37b0_002e"]["watts"], 0.2)  # not its 2.5 W claim
        self.assertAlmostEqual(total, 0.4 + 1.5 + 0.2 + 2.0)

    def test_generic_0300_vid_not_mistaken_for_stream_deck(self):
        _, rows = E.estimate_usb([dev("0300", "3010", "Some Gamepad", 100, manufacturer="Generic")])
        self.assertNotEqual(rows[0]["device"], "Ajazz stream deck")
        self.assertEqual(rows[0]["watts"], 0.2)  # 0.5 W claim * 0.4

    def test_unknown_guess_is_capped(self):
        _, rows = E.estimate_usb([dev("dead", "beef", "Lamp", 3000)])
        self.assertEqual(rows[0]["watts"], E.UNKNOWN_CAP_W)

    def test_self_powered_and_internal_skipped(self):
        total, _ = E.estimate_usb([dev("dead", "0001", "Hub", 100, self_powered=True),
                                   dev("dead", "0002", "Thing", 100, removable="fixed")])
        self.assertEqual(total, 0)

    def test_override_and_suspend(self):
        total, rows = E.estimate_usb(EXAMPLE_PC, {"0300_3010": 3.1})
        self.assertAlmostEqual(total, 0.4 + 1.5 + 0.2 + 3.1)
        self.assertEqual(rows[-1]["basis"], "your setting")
        total, _ = E.estimate_usb([dev("320f", "50b7", "MONTECH MK087", 400, suspended=True)])
        self.assertEqual(total, 0)

    def test_usb_and_rgb_feed_energy(self):
        a = E.Accountant(E.Profile(disks_w=0, controller_w=0, fans_w=0, rgb_w=6, board_w=0, efficiency=1.0))
        a.on_reading(E.Reading("s", 0, 0, 0, 0, 0, usb_w=4))
        s = a.on_reading(E.Reading("s", 3600, 0, 0, 0, 0, usb_w=4))
        self.assertAlmostEqual(s.energy_kwh, 0.010)
        self.assertAlmostEqual(s.wall_w, 10)
        self.assertEqual(s.usb_w, 4)


class AgentUsbDiscoveryTests(unittest.TestCase):
    def test_discover(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)

            def mk(name, **files):
                d = root / "bus/usb/devices" / name
                d.mkdir(parents=True)
                for k, v in files.items():
                    (d / k.replace("__", "/")).parent.mkdir(parents=True, exist_ok=True)
                    (d / k.replace("__", "/")).write_text(v + "\n")

            mk("usb1", idVendor="1d6b", idProduct="0002", bDeviceClass="09", bmAttributes="e0", bMaxPower="0mA")
            mk("1-9", idVendor="17a0", idProduct="0001", bDeviceClass="00", bmAttributes="80", bMaxPower="90mA",
               product="Samson C01U", removable="removable", power__runtime_status="active")
            mk("1-9:1.0", bInterfaceClass="01")
            mk("1-5", idVendor="8087", idProduct="0029", bDeviceClass="e0", bmAttributes="e0", bMaxPower="100mA",
               removable="fixed", power__runtime_status="suspended")
            found = {d["port"]: d for d in A.discover_usb(root)}
            self.assertEqual(set(found), {"1-9", "1-5"})
            self.assertEqual(found["1-9"]["max_power_ma"], 90)
            self.assertFalse(found["1-9"]["self_powered"])
            self.assertTrue(found["1-5"]["self_powered"])
            self.assertTrue(found["1-5"]["suspended"])
            self.assertEqual(found["1-5"]["removable"], "fixed")


if __name__ == "__main__":
    unittest.main()
