import unittest
from unittest import mock

from _load import load

A = load("pc_power_agent", "agent/pc_power_agent.py")


def agent(**cfg):
    a = A.Agent.__new__(A.Agent)
    a.cfg = {**A.DEFAULTS, **cfg}
    a.monitors = mock.Mock(available=False)
    a._session_cache = (0.0, None)
    return a


SESSION = {"id": "4", "user": "luke", "locked": False}


class LockTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(A.shutil, "which", return_value="/usr/bin/loginctl")
        p.start(); self.addCleanup(p.stop)

    def test_disabled(self):
        self.assertEqual(agent(allow_session_lock=False).lock()[0], 403)

    def test_no_session(self):
        with mock.patch.object(A, "discover_session", return_value={"user": None, "active": False}):
            self.assertEqual(agent().lock()[0], 409)

    def test_dry_run_does_not_call_loginctl(self):
        with mock.patch.object(A, "discover_session", return_value=SESSION), mock.patch.object(A, "run") as run:
            code, body = agent(lock_dry_run=True).lock()
        self.assertEqual((code, body["dry_run"]), (202, True))
        run.assert_not_called()

    def test_locks_the_active_session(self):
        with mock.patch.object(A, "discover_session", return_value=SESSION), \
             mock.patch.object(A, "run", return_value="") as run:
            code, _ = agent().lock()
        self.assertEqual(code, 200)
        run.assert_called_once_with(["loginctl", "lock-session", "4"], timeout=10)

    def test_already_locked_is_a_noop(self):
        with mock.patch.object(A, "discover_session", return_value={**SESSION, "locked": True}), \
             mock.patch.object(A, "run") as run:
            self.assertTrue(agent().lock()[1]["already_locked"])
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
