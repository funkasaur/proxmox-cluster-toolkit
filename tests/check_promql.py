#!/usr/bin/env python3
"""Syntax-check every alert and dashboard query with promtool (needs Docker; not part of the unit tests).

  python3 tests/check_promql.py [--image prom/prometheus:<tag>]
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "grafana"))
import cluster_dashboard  # noqa: E402
import cluster_rules  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--image", default="prom/prometheus:latest")
ap.add_argument("--config", default=os.path.join(ROOT, "config.example.json"))
a = ap.parse_args()
cfg = json.load(open(a.config))
rules = []
for _, group in cluster_rules.Builder(cfg).groups().values():
    rules += [{"alert": r["title"], "expr": r["data"][0]["model"]["expr"]} for r in group]
for p in cluster_dashboard.build(cfg)["panels"]:
    rules += [{"record": f"dash:{p['id']}:{t['refId']}", "expr": t["expr"].replace("$__rate_interval", "5m")}
              for t in p["targets"]]
with tempfile.TemporaryDirectory() as d:
    with open(os.path.join(d, "rules.yml"), "w") as f:
        json.dump({"groups": [{"name": "check", "rules": rules}]}, f)  # JSON is valid YAML
    os.chmod(d, 0o755)  # the image runs promtool as nobody
    os.chmod(os.path.join(d, "rules.yml"), 0o644)
    r = subprocess.run(["docker", "run", "--rm", "-v", f"{d}:/r:ro", "--entrypoint", "promtool", a.image,
                        "check", "rules", "/r/rules.yml"], capture_output=True, text=True)
print(r.stdout + r.stderr, end="")
print(f"{len(rules)} queries")
sys.exit(r.returncode)
