import contextlib
import io
import json
import unittest

from tests import load

pm = load("maintenance/pve-maint", "pve_maint")


class FakeCluster:
    """Answers pvesh/ha-manager like a 3-node cluster; services leave a node one poll after maintenance starts."""

    def __init__(self):
        self.nodes = {"pve1": "online", "pve2": "online", "pve3": "online"}
        self.services = [{"type": "service", "sid": "vm:100", "node": "pve1", "state": "started"},
                         {"type": "service", "sid": "ct:200", "node": "pve1", "state": "started"},
                         {"type": "service", "sid": "vm:300", "node": "pve1", "state": "stopped"},
                         {"type": "service", "sid": "vm:101", "node": "pve2", "state": "started"}]
        self.guests = {"qemu": [{"vmid": 100, "status": "running"}, {"vmid": 150, "name": "scratch",
                                                                      "status": "running"},
                                {"vmid": 300, "status": "stopped"}],
                       "lxc": [{"vmid": 200, "status": "running"}]}
        self.calls, self.polls = [], 0

    def run(self, args):
        self.calls.append(args)
        if args[0] == "ha-manager":
            node, on = args[-1], args[-2] == "enable"
            self.nodes[node] = "maintenance" if on else "online"
            return ""
        path = args[2]
        if path == "/cluster/ha/status/current":
            self.polls += 1
            if self.nodes["pve1"] == "maintenance" and self.polls > 2:
                for s in self.services:
                    if s["node"] == "pve1" and s["state"] != "stopped":
                        s["node"] = "pve2"
            return json.dumps([{"type": "quorum", "quorate": 1}] + self.services)
        if path == "/cluster/ha/status/manager_status":
            return json.dumps({"manager_status": {"node_status": self.nodes}})
        if path.startswith("/nodes/pve1/"):
            g = self.guests[path.rsplit("/", 1)[1]]
            if self.nodes["pve1"] == "maintenance" and self.polls > 2:
                g = [x for x in g if x["vmid"] not in (100, 200)]
            return json.dumps(g)
        raise AssertionError(path)


def quiet(fn, *a, **kw):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = fn(*a, **kw)
    return rc, out.getvalue(), err.getvalue()


class MaintTest(unittest.TestCase):
    def test_services_on(self):
        c = FakeCluster()
        self.assertEqual(pm.services_on("pve1", c.services), ["vm:100", "ct:200"])

    def test_enable_waits_and_lists_unmanaged(self):
        c = FakeCluster()
        rc, out, _ = quiet(pm.enable, "pve1", 60, run=c.run, sleep=lambda s: None)
        self.assertEqual(rc, 0)
        self.assertIn(["ha-manager", "crm-command", "node-maintenance", "enable", "pve1"], c.calls)
        self.assertIn("waiting: still on pve1: vm:100, ct:200", out)
        self.assertIn("NOT HA-managed and still running", out)
        self.assertIn("vm:150 scratch", out)
        self.assertNotIn("ct:200 ", out.split("NOT HA-managed")[1])

    def test_enable_times_out(self):
        c = FakeCluster()
        c.run_orig = c.run

        def stuck(args):  # services never leave
            c.polls = 0
            return c.run_orig(args)

        t = iter(range(0, 1000, 10))
        rc, _, err = quiet(pm.enable, "pve1", 25, run=stuck, sleep=lambda s: None, clock=lambda: next(t))
        self.assertEqual(rc, 1)
        self.assertIn("timed out: still on pve1", err)

    def test_unknown_node(self):
        rc, _, err = quiet(pm.enable, "nope", 5, run=FakeCluster().run)
        self.assertEqual(rc, 2)
        self.assertIn("unknown node", err)

    def test_disable(self):
        c = FakeCluster()
        c.nodes["pve1"] = "maintenance"
        rc, out, _ = quiet(pm.disable, "pve1", 5, run=c.run, sleep=lambda s: None)
        self.assertEqual(rc, 0)
        self.assertIn("pve1: online", out)

    def test_status(self):
        rc, out, _ = quiet(pm.status, run=FakeCluster().run)
        self.assertEqual(rc, 0)
        self.assertIn("pve1: online  vm:100 (started), ct:200 (started), vm:300 (stopped)", out)


if __name__ == "__main__":
    unittest.main()
