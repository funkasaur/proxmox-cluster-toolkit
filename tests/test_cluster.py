import contextlib
import io
import json
import os
import unittest
from unittest import mock

import cluster_dashboard
import cluster_rules
import grafana_rules as gr
from tests import ROOT
from tests.test_grafana_rules import FakeGrafana

CFG = json.load(open(os.path.join(ROOT, "config.example.json")))


def titles(rules):
    return [r["title"] for r in rules]


def exprs(rules):
    return [r["data"][0]["model"]["expr"] for r in rules]


class RulesBuildTest(unittest.TestCase):
    def test_example_config(self):
        groups = cluster_rules.Builder(CFG).groups()
        smart, health = groups["smart"][1], groups["health"][1]
        self.assertEqual(len(smart), 13)
        self.assertEqual(len(health), 9)
        uids = [r["uid"] for r in smart + health]
        self.assertEqual(len(uids), len(set(uids)))
        self.assertTrue(all(r["ruleGroup"] == "ClusterSMART" for r in smart))
        self.assertTrue(all(r["ruleGroup"] == "ClusterHealth" for r in health))
        self.assertTrue(all(r["data"][0]["datasourceUid"] == "prometheus" for r in smart + health))
        for e in exprs(smart + health):
            self.assertNotIn("HOST", e)
            self.assertEqual(e.count("("), e.count(")"), e)
            self.assertEqual(e.count("{"), e.count("}"), e)

    def test_absent_per_host(self):
        down = next(r for r in cluster_rules.Builder(CFG).smart() if r["title"] == "PveSmartExporterDown")
        e = down["data"][0]["model"]["expr"]
        for h in ("pve1", "pve2", "pve3", "pve4", "pbs"):
            self.assertIn(f'absent(up{{job="smartctl-pve",host="{h}"}})', e)
        self.assertIn('host!~"pve4"', e)
        self.assertEqual(down["noDataState"], "Alerting")

    def test_writes_limits(self):
        e = next(x for x in exprs(cluster_rules.Builder(CFG).smart()) if 'attribute_id="241"' in x)
        self.assertIn('host!~"pbs|pve4"', e)
        self.assertIn('host="pbs",attribute_id="241",attribute_value_type="raw"}) > 75000', e)

    def test_minimal_config(self):
        cfg = dict(CFG, standalone_nodes=[], pbs_hosts=[], smart={}, offsite_max_age_s=0)
        b = cluster_rules.Builder(cfg)
        t = titles(b.smart() + b.health())
        for gone in ("PbsDown", "PbsDatastoreLow", "PveDiskWritesHigh", "StandaloneNodeDown", "PbsOffsiteStale"):
            self.assertNotIn(gone, t)
        self.assertIn("PveNodeDown", t)
        e = exprs(b.smart())[0]
        self.assertTrue(e.startswith('max by (host) (1 - up{job="smartctl-pve"}) or '), e)
        self.assertNotIn('!~""', " ".join(exprs(b.smart() + b.health())))


class RulesApplyTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGrafana()
        self.fake.folders.add("proxmox")
        env = {"GRAFANA_URL": self.fake.url, "GRAFANA_TOKEN": "t"}
        self.patch = mock.patch.dict(os.environ, env)
        self.patch.start()
        self.cfgfile = os.path.join(ROOT, "config.example.json")

    def tearDown(self):
        self.patch.stop()
        self.fake.close()

    def run_main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cluster_rules.main(["--config", self.cfgfile, *args])
        return rc, out.getvalue(), err.getvalue()

    def test_apply_both_groups_twice(self):
        self.assertEqual(self.run_main()[0], 0)
        self.assertEqual(len(self.fake.rules), 22)
        self.assertEqual(self.fake.groups, {"ClusterSMART": 60, "ClusterHealth": 60})
        self.assertEqual(self.run_main()[0], 0)
        self.assertEqual(len(self.fake.rules), 22)

    def test_hand_made_rule_blocks_without_prune(self):
        self.run_main()
        mine = gr.rule("mine", "Mine", "up == 0", folder_uid="proxmox", group="ClusterHealth", datasource_uid="x")
        self.fake.rules["mine"] = mine
        rc, _, err = self.run_main("--group", "health")
        self.assertEqual(rc, 1)
        self.assertIn("mine", err)
        self.assertIn("mine", self.fake.rules)
        rc, _, _ = self.run_main("--group", "health", "--prune")
        self.assertEqual(rc, 0)
        self.assertNotIn("mine", self.fake.rules)

    def test_print_makes_no_calls(self):
        rc, out, _ = self.run_main("--print", "--group", "smart")
        self.assertEqual(rc, 0)
        self.assertEqual(len(json.loads(out)), 13)
        self.assertEqual(self.fake.writes, [])


class DashboardTest(unittest.TestCase):
    def test_build(self):
        d = cluster_dashboard.build(CFG)
        ids = [p["id"] for p in d["panels"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(d["templating"]["list"][0]["type"], "datasource")
        for p in d["panels"]:
            self.assertEqual(p["datasource"]["uid"], "${datasource}")
            for t in p["targets"]:
                self.assertEqual(t["datasource"]["uid"], "${datasource}")
        self.assertEqual([x["url"] for x in d["links"]], list(CFG["links"].values()))
        q = " ".join(t["expr"] for p in d["panels"] for t in p["targets"])
        self.assertIn('job="node-pve"', q)
        self.assertIn('job="smartctl-pve"', q)

    def test_no_overlap(self):
        cells = set()
        for p in cluster_dashboard.build(CFG)["panels"]:
            g = p["gridPos"]
            for x in range(g["x"], g["x"] + g["w"]):
                for y in range(g["y"], g["y"] + g["h"]):
                    self.assertNotIn((x, y), cells, p["title"])
                    cells.add((x, y))
            self.assertLessEqual(g["x"] + g["w"], 24)


if __name__ == "__main__":
    unittest.main()
