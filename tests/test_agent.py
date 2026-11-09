import tempfile
import unittest
from pathlib import Path

from _load import load

A = load("pc_power_agent", "agent/pc_power_agent.py")


def write(root: Path, rel: str, text: str):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


class AgentSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_rapl_packages_and_wrap(self):
        write(self.root, "class/powercap/intel-rapl:0/name", "package-0")
        write(self.root, "class/powercap/intel-rapl:0/energy_uj", "999000000")
        write(self.root, "class/powercap/intel-rapl:0/max_energy_range_uj", "1000000000")
        write(self.root, "class/powercap/intel-rapl:0:0/name", "core")
        write(self.root, "class/powercap/intel-rapl:0:0/energy_uj", "1")
        write(self.root, "class/powercap/intel-rapl:1/name", "psys")
        write(self.root, "class/powercap/intel-rapl:1/energy_uj", "1")
        src = A.RaplSource.discover(self.root)
        self.assertEqual(src.name, "package-0")
        src.sample(0)
        write(self.root, "class/powercap/intel-rapl:0/energy_uj", "4000000")  # wrapped: +5 J
        watts, joules = src.sample(1.0)
        self.assertAlmostEqual(joules, 5.0)
        self.assertAlmostEqual(watts, 5.0)

    def test_no_rapl(self):
        self.assertIsNone(A.RaplSource.discover(self.root))

    def test_amd_rdna4_power1_input(self):
        hw = "class/drm/card1/device/hwmon/hwmon5"
        write(self.root, f"{hw}/name", "amdgpu")
        write(self.root, f"{hw}/power1_input", "250000000")  # 250 W in µW
        write(self.root, f"{hw}/power1_cap", "304000000")
        write(self.root, "class/drm/card0/device/hwmon/hwmon2/name", "nouveau")
        srcs = A.AmdHwmonSource.discover(self.root)
        self.assertEqual(len(srcs), 1)
        self.assertEqual(srcs[0].sample(2.0), (250.0, 500.0))

    def test_amd_older_power1_average(self):
        hw = "class/drm/card0/device/hwmon/hwmon3"
        write(self.root, f"{hw}/name", "amdgpu")
        write(self.root, f"{hw}/power1_average", "30000000")
        self.assertEqual(A.AmdHwmonSource.discover(self.root)[0].sample(1.0), (30.0, 30.0))

    def test_sampler_accumulates_and_survives_bad_source(self):
        class Boom(A.Source):
            kind, name = "gpu", "boom"
            def sample(self, dt):
                raise RuntimeError("x")
        s = A.Sampler(A.FakeSource("cpu", 10, 0, 60), [Boom()], 1.0)
        s._tick(0); s._tick(1.0); s._tick(1.0)
        snap = s.snapshot()
        self.assertAlmostEqual(snap["cpu"]["energy_j"], 20.0)
        self.assertFalse(snap["gpus"][0]["available"])

    def test_disks(self):
        for name, rot in (("sda", "1"), ("sdb", "0"), ("nvme0n1", "0"), ("loop0", "0")):
            write(self.root, f"block/{name}/queue/rotational", rot)
            write(self.root, f"block/{name}/removable", "0")
            (self.root / f"block/{name}/device").mkdir()
        self.assertEqual(A.discover_disks(self.root), {"hdd": 1, "ssd": 1, "nvme": 1})


if __name__ == "__main__":
    unittest.main()
