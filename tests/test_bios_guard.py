import ast
import json
import os
import tempfile
import unittest
import urllib.error

import bios_guard as bg
from tests import ROOT

CFG = json.load(open(os.path.join(ROOT, "bios-guard", "targets.example.json")))


def attrs(**over):
    base = {k: v for k, v in bg.targets_for(CFG, "pve1").items()}
    base.update(over)
    return {k: {"value": v, "writable": True} for k, v in base.items()}


class FakeNode:
    def __init__(self, before, module="/lib/modules/6.17/updates/dkms/hp-bioscfg.ko", fail=()):
        self.state, self.module, self.fail, self.scripts = dict(before), module, set(fail), []

    def __call__(self, host, script):
        self.scripts.append(script)
        writes = ast.literal_eval(script.split("WRITES = ", 1)[1].split("\n", 1)[0])  # the dict the script would write
        res = {"module": self.module, "uptime_s": 100, "before": json.loads(json.dumps(self.state)), "writes": {}}
        for n, v in writes.items():
            if n in self.fail:
                res["writes"][n] = "error: [Errno 5] Input/output error"
            else:
                self.state[n] = {"value": v, "writable": True}
                res["writes"][n] = "ok"
        if writes:
            res["after"] = self.state
        return res


class NodeTest(unittest.TestCase):
    def test_clean(self):
        r = bg.check_node(CFG, "pve1", True, remote=FakeNode(attrs()))
        self.assertEqual(r["drift"], {})
        self.assertTrue(r["patched"])
        self.assertEqual(bg.summary({"nodes": {"pve1": r}, "amt": {}}), "pve1: BIOS OK")

    def test_fix_drift(self):
        node = FakeNode(attrs(**{"After Power Loss": "Power Off"}))
        r = bg.check_node(CFG, "pve1", True, remote=node)
        self.assertEqual(r["fixed"], {"After Power Loss": "Power On"})
        self.assertTrue(r["reboot_pending"])
        self.assertIn("FIXED After Power Loss=Power On (reboot pending)", bg.summary({"nodes": {"pve1": r}, "amt": {}}))

    def test_never_write_and_read_only(self):
        a = attrs(**{"Unconfigure AMT on next boot": "Apply"})
        a["SATA Power Management"] = {"value": "Enable", "writable": False}
        node = FakeNode(a)
        r = bg.check_node(CFG, "pve1", True, remote=node)
        self.assertEqual(r["fixed"], {})
        self.assertEqual(len(node.scripts), 1)  # nothing written
        self.assertEqual(set(r["drift"]), {"Unconfigure AMT on next boot", "SATA Power Management"})

    def test_stock_module_refuses_fix(self):
        node = FakeNode(attrs(**{"After Power Loss": "Power Off"}), module="/lib/modules/6.17/kernel/hp-bioscfg.ko")
        r = bg.check_node(CFG, "pve1", True, remote=node)
        self.assertIn("After Power Loss", r["fix_failed"])
        self.assertIn("PATCHED MODULE NOT LOADED", bg.summary({"nodes": {"pve1": r}, "amt": {}}))

    def test_write_error(self):
        node = FakeNode(attrs(**{"After Power Loss": "Power Off"}), fail={"After Power Loss"})
        r = bg.check_node(CFG, "pve1", True, remote=node)
        self.assertIn("Input/output error", r["fix_failed"]["After Power Loss"])

    def test_override(self):
        self.assertEqual(bg.targets_for(CFG, "pve3")["Increase Idle Fan Speed(%)"], "40")
        self.assertNotIn("Increase Idle Fan Speed(%)", bg.targets_for(CFG, "pve1"))


def amt_reply(values, status=200):
    def get(ip, user, password, uri, timeout):
        field = next(f for u, f, _, _ in bg.AMT_CHECKS if u == uri)
        return status, f"<s:Envelope><g:Body><h:X><h:{field}>{values[field]}</h:{field}></h:X></g:Body></s:Envelope>"
    return get


GOOD = {"CurrentControlMode": "2", "OptInRequired": "0", "EnabledState": "32771"}


class AmtTest(unittest.TestCase):
    def test_ok(self):
        vals = dict(GOOD)

        def get(ip, u, p, uri, t):
            v = dict(vals, EnabledState="2" if "KVM" in uri else "32771")
            return amt_reply(v)(ip, u, p, uri, t)
        r = bg.check_amt("192.0.2.1", "u", "p", get=get)
        self.assertEqual(r["bad"], {})
        self.assertEqual(len(r["ok"]), 4)

    def test_kvm_disabled(self):
        r = bg.check_amt("192.0.2.1", "u", "p", get=amt_reply(dict(GOOD, EnabledState="3")))
        self.assertIn("KVM on", r["bad"])
        self.assertIn("SOL + IDE-R on", r["bad"])

    def test_401(self):
        r = bg.check_amt("192.0.2.1", "u", "p", get=amt_reply(GOOD, status=401))
        s = bg.summary({"nodes": {}, "amt": {"pve1": r}})
        self.assertIn("login rejected (401)", s)

    def test_unreachable(self):
        def get(*a):
            raise urllib.error.URLError(TimeoutError("timed out"))
        r = bg.check_amt("192.0.2.1", "u", "p", get=get)
        self.assertTrue(r["unreachable"])
        self.assertEqual(bg.summary({"nodes": {}, "amt": {"x": r}}), "x AMT: UNREACHABLE")


class OutputTest(unittest.TestCase):
    def test_prom(self):
        ok = bg.check_node(CFG, "pve1", True, remote=FakeNode(attrs()))
        bad = bg.check_node(CFG, "pve2", False, remote=FakeNode(attrs(**{"After Power Loss": "Off"})))
        out = bg.prom({"nodes": {"pve1": ok, "pve2": bad}, "amt": {"pve1": {"ok": ["x"], "bad": {}},
                                                                   "pve2": {"ok": [], "bad": {"x": "HTTP 401"}}}},
                      now=7)
        self.assertIn('bios_guard_bios_ok{node="pve1"} 1', out)
        self.assertIn('bios_guard_bios_ok{node="pve2"} 0', out)
        self.assertIn('bios_guard_drift_settings{node="pve2"} 1', out)
        self.assertIn('bios_guard_amt_ok{node="pve2"} 0', out)
        self.assertIn("bios_guard_last_run_timestamp_seconds 7", out)

    def test_load_env_missing_file(self):
        self.assertEqual(bg.load_env("/nonexistent/x.env"), {})
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            f.write("# c\nAMT_USER='mon'\nAMT_PASS=\"a=b\"\n")
        try:
            self.assertEqual(bg.load_env(f.name), {"AMT_USER": "mon", "AMT_PASS": "a=b"})
        finally:
            os.unlink(f.name)

    def test_notify_nothing_configured(self):
        self.assertIn("nothing configured", bg.notify("x", {})[0])

    def test_remote_script_compiles(self):
        compile(bg.REMOTE % {"attr": bg.ATTR, "writes": repr({"A": "1"})}, "remote", "exec")


if __name__ == "__main__":
    unittest.main()
