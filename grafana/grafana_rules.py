#!/usr/bin/env python3
"""grafana_rules: keep Grafana-managed alert rules in code.

Write a small Python file that builds a list of rules with `rule(...)` and calls `apply(...)`. Each run is
idempotent: a rule that exists (same uid) is updated with PUT, a new one is created with POST, then the rule
group's evaluation interval is set.

WARNING: setting the group goes through Grafana's rule-group endpoint
(PUT /api/v1/provisioning/folder/<folder>/rule-groups/<group>), and that call REPLACES the whole group:
any rule in that group that is not in your list is deleted. This module therefore reads the group first and
refuses to go on if it holds rules you didn't list, unless you pass prune=True (CLI: --prune). Keep
hand-made rules in a different group than code-managed ones.

Connection settings come from the environment:
  GRAFANA_URL         e.g. http://localhost:3000
  GRAFANA_TOKEN       service-account token with alert-rule write access, or
  GRAFANA_TOKEN_FILE  a file holding that token
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


class GrafanaError(Exception):
    pass


class Grafana:
    def __init__(self, url=None, token=None, timeout=30, dry_run=False):
        self.url = (url or os.environ.get("GRAFANA_URL", "http://localhost:3000")).rstrip("/")
        if token is None:
            token = os.environ.get("GRAFANA_TOKEN")
            path = os.environ.get("GRAFANA_TOKEN_FILE")
            if not token and path:
                with open(os.path.expanduser(path), encoding="utf-8") as f:
                    token = f.read().strip()
        if not token:
            raise GrafanaError("set GRAFANA_TOKEN or GRAFANA_TOKEN_FILE")
        self.token, self.timeout, self.dry_run = token, timeout, dry_run

    def call(self, method, path, body=None):
        if self.dry_run and method != "GET":
            print(f"DRY RUN: {method} {path}")
            return 200, None
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json"}
        if body is not None:
            # Without this header, rules created through the API are locked in the UI ("provisioned").
            headers.update({"Content-Type": "application/json", "X-Disable-Provenance": "true"})
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError) as e:
            raise GrafanaError(f"{method} {path}: {e}") from None


def rule(uid, title, expr, *, folder_uid, group, datasource_uid, for_="5m", description="", summary=None,
         severity="warning", labels=None, no_data="OK", exec_err="Error", lookback_s=900, threshold=0,
         comparison="gt", paused=False):
    """One Prometheus-backed rule that fires per series while `expr` is `comparison` `threshold` (default > 0)."""
    return {
        "uid": uid, "title": title, "folderUID": folder_uid, "ruleGroup": group,
        "condition": "B", "noDataState": no_data, "execErrState": exec_err, "for": for_, "isPaused": paused,
        "labels": dict({"severity": severity}, **(labels or {})),
        "annotations": {"summary": summary or title, "description": description},
        "data": [
            {"refId": "A", "queryType": "", "relativeTimeRange": {"from": lookback_s, "to": 0},
             "datasourceUid": datasource_uid,
             "model": {"refId": "A", "expr": expr, "instant": True, "range": False,
                       "intervalMs": 1000, "maxDataPoints": 43200}},
            {"refId": "B", "queryType": "", "relativeTimeRange": {"from": 0, "to": 0},
             "datasourceUid": "__expr__",
             "model": {"refId": "B", "type": "threshold", "expression": "A",
                       "conditions": [{"evaluator": {"type": comparison, "params": [threshold]}}]}},
        ],
    }


def _group_path(folder_uid, group):
    return (f"/api/v1/provisioning/folder/{urllib.parse.quote(folder_uid, safe='')}"
            f"/rule-groups/{urllib.parse.quote(group, safe='')}")


def apply(grafana, rules, *, folder_uid, group, interval_s=60, prune=False, log=print):
    """Create or update `rules` (all in one folder + group), then set the group interval.

    Returns the uids that the group update removed (only possible with prune=True)."""
    if not rules:
        raise GrafanaError("no rules given")
    bad = [r["uid"] for r in rules if r["folderUID"] != folder_uid or r["ruleGroup"] != group]
    if bad:
        raise GrafanaError(f"rules not in folder {folder_uid!r} / group {group!r}: {', '.join(bad)}")
    uids = [r["uid"] for r in rules]
    if len(set(uids)) != len(uids):
        raise GrafanaError("duplicate rule uids")
    status, folder = grafana.call("GET", f"/api/folders/{urllib.parse.quote(folder_uid, safe='')}")
    if status != 200:
        raise GrafanaError(f"folder {folder_uid!r} not found (HTTP {status}); create it first")

    status, existing = grafana.call("GET", _group_path(folder_uid, group))
    current = {r["uid"] for r in existing.get("rules", [])} if status == 200 and isinstance(existing, dict) else set()
    extra = sorted(current - set(uids))
    if extra and not prune:
        raise GrafanaError(f"group {group!r} also holds rules not in this file: {', '.join(extra)}. "
                           "The group update would delete them. Move them to another group, add them here, "
                           "or rerun with prune to delete them.")

    for r in rules:
        path = f"/api/v1/provisioning/alert-rules/{urllib.parse.quote(r['uid'], safe='')}"
        status, _ = grafana.call("GET", path)
        status, body = grafana.call("PUT", path, r) if status == 200 else \
            grafana.call("POST", "/api/v1/provisioning/alert-rules", r)
        log(f"rule {r['title']}: HTTP {status}" + ("" if status in (200, 201) else f" {body}"))
        if status not in (200, 201):
            raise GrafanaError(f"rule {r['uid']} failed")

    # The group body must carry every rule as Grafana stores it, so read them back.
    stored = []
    for uid in uids:
        status, body = grafana.call("GET", f"/api/v1/provisioning/alert-rules/{urllib.parse.quote(uid, safe='')}")
        stored.append(body if status == 200 and body else next(r for r in rules if r["uid"] == uid))
    status, body = grafana.call("PUT", _group_path(folder_uid, group),
                                {"title": group, "folderUid": folder_uid, "interval": int(interval_s),
                                 "rules": stored})
    log(f"group {group} (interval {interval_s}s): HTTP {status}" + ("" if status == 200 else f" {body}"))
    if status != 200:
        raise GrafanaError("group update failed")
    if extra:
        log("removed: " + ", ".join(extra))
    return extra


def cli(rules, *, folder_uid, group, interval_s=60, argv=None):
    """Command line for a rules file: --dry-run, --prune, --print (dump the JSON, no API calls)."""
    ap = argparse.ArgumentParser(description=f"Apply {len(rules)} Grafana alert rules to {folder_uid}/{group}")
    ap.add_argument("--dry-run", action="store_true", help="read from Grafana, print writes instead of sending them")
    ap.add_argument("--prune", action="store_true", help="allow the group update to delete rules not listed here")
    ap.add_argument("--print", action="store_true", help="print the rules as JSON and exit")
    a = ap.parse_args(argv)
    if a.print:
        print(json.dumps(rules, indent=2))
        return 0
    try:
        apply(Grafana(dry_run=a.dry_run), rules, folder_uid=folder_uid, group=group,
              interval_s=interval_s, prune=a.prune)
    except (GrafanaError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0
