import unittest

from tests import load

ha = load("textfile/pve-ha-metrics.py", "pve_ha_metrics")
coro = load("textfile/pve-corosync-metrics.py", "pve_corosync_metrics")
repl = load("textfile/pve-replication-metrics.py", "pve_replication_metrics")

CURRENT = [
    {"id": "quorum", "type": "quorum", "quorate": 1, "status": "OK"},
    {"id": "service:vm:100", "type": "service", "sid": "vm:100", "node": "pve3", "state": "started",
     "request_state": "started"},
    {"id": "service:vm:101", "type": "service", "sid": "vm:101", "node": "pve2", "state": "migrate",
     "request_state": "started"},
    {"id": "lrm:pve1", "type": "lrm", "node": "pve1", "status": "idle"},
]
MANAGER = {"node_status": {"pve1": "maintenance", "pve2": "online", "pve3": "online", "pve4": "unknown"}}
RULES = [
    {"rule": "r1", "type": "node-affinity", "nodes": "pve3:2,pve1:1", "resources": "vm:100"},
    {"rule": "r2", "type": "node-affinity", "nodes": "pve1,pve2", "resources": "vm:101"},
    {"rule": "r3", "type": "node-affinity", "nodes": "pve2", "resources": "vm:102", "disable": 1},
    {"rule": "r4", "type": "resource-affinity", "resources": "vm:100,vm:101", "affinity": "negative"},
]

CFGTOOL = """Local node ID 1, transport knet
LINK ID 0 udp
\taddr\t= 192.0.2.11
\tstatus:
\t\tnodeid:          1:\tlocalhost
\t\tnodeid:          2:\tconnected
\t\tnodeid:          3:\tconnected
LINK ID 1 udp
\taddr\t= 198.51.100.11
\tstatus:
\t\tnodeid:          1:\tlocalhost
\t\tnodeid:          2:\tconnected
\t\tnodeid:          3:\tdisconnected
"""


class HaTest(unittest.TestCase):
    def setUp(self):
        self.out = ha.render(CURRENT, MANAGER, RULES, now=1700000000)

    def test_preferred(self):
        self.assertEqual(ha.preferred_nodes(RULES), {"vm:100": {"pve3"}, "vm:101": {"pve1", "pve2"}})

    def test_services(self):
        self.assertIn('pve_ha_service_ok{sid="vm:100"} 1', self.out)
        self.assertIn('pve_ha_service_ok{sid="vm:101"} 0', self.out)
        self.assertIn('pve_ha_service_on_preferred{sid="vm:100",node="pve3"} 1', self.out)
        self.assertIn('pve_ha_service_on_preferred{sid="vm:101",node="pve2"} 1', self.out)
        self.assertIn('pve_ha_service_info{sid="vm:101",node="pve2",state="migrate",request_state="started"} 1',
                      self.out)

    def test_nodes_and_quorum(self):
        self.assertIn('pve_ha_node_maintenance{node="pve1"} 1', self.out)
        self.assertIn('pve_ha_node_online{node="pve1"} 1', self.out)
        self.assertIn('pve_ha_node_online{node="pve4"} 0', self.out)
        self.assertIn("pve_ha_quorate 1", self.out)
        self.assertTrue(self.out.endswith("pve_ha_metrics_timestamp_seconds 1700000000\n"))

    def test_empty(self):
        out = ha.render([], {}, [], now=1)
        self.assertIn("pve_ha_quorate 0", out)
        self.assertNotIn("pve_ha_service_ok{", out)


class CorosyncTest(unittest.TestCase):
    def test_links(self):
        out = coro.render(CFGTOOL, True, now=5)
        self.assertIn('pve_corosync_link_connected{link="0",peer="2"} 1', out)
        self.assertIn('pve_corosync_link_connected{link="1",peer="3"} 0', out)
        self.assertNotIn('peer="1"', out)
        self.assertIn("pve_corosync_cfgtool_ok 1", out)

    def test_cfgtool_failed(self):
        out = coro.render("", False, now=5)
        self.assertIn("pve_corosync_cfgtool_ok 0", out)
        self.assertNotIn("pve_corosync_link_connected{", out)


class ReplicationTest(unittest.TestCase):
    def test_jobs(self):
        jobs = [{"id": "100-0", "guest": 100, "target": "pve1", "last_sync": 1699999000, "fail_count": 0,
                 "duration": 4.2},
                {"id": "101-0", "guest": 101, "source": "pve2", "target": "pve1", "fail_count": 3}]
        out = repl.render(jobs, "pve2", now=9)
        self.assertIn('pve_replication_last_sync_timestamp_seconds{repl_id="100-0",guest="100",source="pve2",'
                      'target="pve1"} 1699999000', out)
        self.assertIn('pve_replication_failures{repl_id="101-0",guest="101",source="pve2",target="pve1"} 3', out)
        self.assertIn('pve_replication_last_sync_timestamp_seconds{repl_id="101-0",guest="101",source="pve2",'
                      'target="pve1"} 0', out)

    def test_no_jobs_still_writes_timestamp(self):
        out = repl.render([], "pve1", now=9)
        self.assertIn("pve_replication_metrics_timestamp_seconds 9", out)
        self.assertNotIn("repl_id", out)


class WriteTest(unittest.TestCase):
    def test_atomic_write(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.prom")
            ha.write(p, "a 1\n")
            with open(p) as f:
                self.assertEqual(f.read(), "a 1\n")
            self.assertEqual(os.stat(p).st_mode & 0o777, 0o644)
            self.assertEqual(os.listdir(d), ["x.prom"])


if __name__ == "__main__":
    unittest.main()
