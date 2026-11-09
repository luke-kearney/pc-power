import unittest

from _load import load

E = load("pc_power_energy", "custom_components/pc_power/energy.py")

# peripherals = 10 W, efficiency 0.5 -> easy maths
PROFILE = dict(disks_w=0, controller_w=0, fans_w=0, board_w=10, efficiency=0.5, offline_w=2, grace_polls=3)


def acct():
    return E.Accountant(E.Profile(**PROFILE))


def r(ts, cpu_j, gpu_j=0.0, session="s1", cpu_w=40.0, gpu_w=0.0):
    return E.Reading(session, ts, cpu_w, cpu_j, gpu_w, gpu_j)


class EnergyTests(unittest.TestCase):
    def test_first_reading_only_baselines(self):
        a = acct()
        s = a.on_reading(r(1000, 5000))
        self.assertTrue(s.online)
        self.assertEqual(s.energy_kwh, 0)
        self.assertAlmostEqual(s.wall_w, (40 + 10) / 0.5)

    def test_delta_accounting(self):
        a = acct()
        a.on_reading(r(0, 0))
        # 3600 s, CPU 36000 J (10 W) + GPU 0, peripherals 10 W -> (36000+36000)/0.5 J = 40 Wh
        s = a.on_reading(r(3600, 36000))
        self.assertAlmostEqual(s.energy_kwh, 0.04)

    def test_poll_rate_does_not_matter(self):
        a, b = acct(), acct()
        a.on_reading(r(0, 0)); a.on_reading(r(3600, 36000))
        for i in range(0, 3601, 10):
            b.on_reading(r(i, i * 10))
        self.assertAlmostEqual(a.state.energy_wh, b.state.energy_wh, places=6)

    def test_short_blip_within_grace_is_counted_from_counters(self):
        a = acct()
        a.on_reading(r(0, 0))
        a.on_failure(10); a.on_failure(20)  # 2 < grace 3
        self.assertTrue(a.snapshot().online)
        s = a.on_reading(r(30, 300))
        self.assertAlmostEqual(s.energy_kwh * 1000, (300 + 10 * 30) / 0.5 / 3600)

    def test_offline_reports_offline_w_and_accrues_it(self):
        a = acct()
        a.on_reading(r(0, 0))
        for t in (10, 20, 30):
            s = a.on_failure(t)
        self.assertFalse(s.online)
        self.assertEqual(s.wall_w, 2)
        self.assertEqual(s.cpu_w, 0)
        a.on_failure(3630)  # another hour
        self.assertAlmostEqual(a.state.energy_wh, 2 * 3630 / 3600)

    def test_offline_then_new_session_keeps_offline_estimate(self):
        a = acct()
        a.on_reading(r(0, 0))
        for t in (10, 20, 3600):
            a.on_failure(t)
        before = a.state.energy_wh
        a.on_reading(r(3610, 0, session="s2"))  # rebooted: counters restarted
        self.assertAlmostEqual(a.state.energy_wh, before)
        s = a.on_reading(r(3620, 100, session="s2"))
        self.assertAlmostEqual(s.energy_kwh * 1000 - before, (100 + 100) / 0.5 / 3600)

    def test_offline_then_same_session_no_double_count(self):
        """Network outage while PC kept running: real delta wins, offline credit is replaced."""
        a = acct()
        a.on_reading(r(0, 0))
        for t in (10, 20, 3600):
            a.on_failure(t)
        a.on_reading(r(3600, 36000))
        self.assertAlmostEqual(a.state.energy_wh, 40.0)

    def test_counter_going_backwards_never_decreases_total(self):
        a = acct()
        a.on_reading(r(0, 1000)); a.on_reading(r(10, 2000))
        before = a.state.energy_wh
        a.on_reading(r(20, 500))  # same session but lower (shouldn't happen) -> only peripherals
        self.assertGreaterEqual(a.state.energy_wh, before)

    def test_state_roundtrip(self):
        a = acct()
        a.on_reading(r(0, 0)); a.on_reading(r(60, 600))
        st = E.AccountState.from_dict(a.state.to_dict())
        self.assertEqual(st.energy_wh, a.state.energy_wh)
        self.assertEqual(st.failures, 0)
        b = E.Accountant(E.Profile(**PROFILE), st)
        b.on_reading(r(120, 1200))  # HA restarted, same agent session: gap credited
        self.assertAlmostEqual(b.state.energy_wh, 2 * a.state.energy_wh)

    def test_default_disks(self):
        self.assertEqual(E.default_disks_w({"hdd": 6, "ssd": 1, "nvme": 1}), 34.5)


if __name__ == "__main__":
    unittest.main()
