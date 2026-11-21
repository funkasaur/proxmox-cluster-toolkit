#!/usr/bin/env python3
"""bios-guard: daily BIOS + Intel AMT drift check for HP business desktops used as Proxmox nodes.

Per node, over ssh: read every hp-bioscfg attribute, compare with the targets file, and (with --fix) write
drifted writable settings back. Writes need the patched hp-bioscfg in dkms/ (the stock driver reserves too small a
password buffer and the BIOS answers 0x4). After writing, the module is reloaded so the value is re-read from the
BIOS, not the driver cache. Changes apply at the next boot: it reports "reboot pending" and never reboots.

Per AMT address, over WS-Man (HTTP digest auth on :16992, a read-only AMT account): control mode (2 = ACM),
user consent (0 = none), SOL + IDE-R (32771) and KVM (2 or 6) enabled. A failure here usually means AMT lost its
setup: CCM re-activation from the OS needs on-screen user consent for KVM, so fix it in MEBx (Ctrl+P) or with an
AMT admin tool (e.g. MeshCentral).

Output: a JSON state file, a stdout summary, optionally a node-exporter textfile (--prom) and, with --notify, a
message (webhook and/or Telegram) only when something is not OK. Exit 0 even with drift; 2 on a bad config.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ATTR = "/sys/class/firmware-attributes/hp-bioscfg/attributes"

# Runs on the node: dumps attributes, optionally writes, reloads the module and re-reads.
REMOTE = r'''
import json, os, subprocess
A = "%(attr)s"
WRITES = %(writes)s
def dump():
    out = {}
    for n in sorted(os.listdir(A)):
        p = os.path.join(A, n, "current_value")
        if os.path.isfile(p):
            try:
                out[n] = {"value": open(p).read().strip(), "writable": os.stat(p).st_mode & 0o222 != 0}
            except OSError as e:
                out[n] = {"value": None, "error": str(e)}
    return out
def module():
    r = subprocess.run(["modinfo", "-n", "hp_bioscfg"], capture_output=True, text=True)
    return r.stdout.strip()
res = {"module": module(), "uptime_s": int(float(open("/proc/uptime").read().split()[0]))}
if not os.path.isdir(A):
    res["error"] = "hp-bioscfg not loaded"
else:
    res["before"] = dump()
    results = {}
    for name, value in WRITES.items():
        try:
            with open(os.path.join(A, name, "current_value"), "w") as f:
                f.write(value)
            results[name] = "ok"
        except OSError as e:
            results[name] = "error: " + str(e)
    if WRITES:
        subprocess.run(["modprobe", "-r", "hp_bioscfg"], check=False)
        subprocess.run(["modprobe", "hp_bioscfg"], check=False)
        res["after"] = dump()
    res["writes"] = results
    pr = os.path.join(A, "pending_reboot")
    res["pending_reboot"] = open(pr).read().strip() if os.path.isfile(pr) else None
print(json.dumps(res))
'''

AMT_CHECKS = [  # (resource URI, field, ok values, label)
    ("http://intel.com/wbem/wscim/1/ips-schema/1/IPS_HostBasedSetupService", "CurrentControlMode", {"2"}, "control mode ACM"),
    ("http://intel.com/wbem/wscim/1/ips-schema/1/IPS_OptInService", "OptInRequired", {"0"}, "no user consent"),
    ("http://intel.com/wbem/wscim/1/amt-schema/1/AMT_RedirectionService", "EnabledState", {"32771"}, "SOL + IDE-R on"),
    ("http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_KVMRedirectionSAP", "EnabledState", {"2", "6"}, "KVM on"),
]

ENVELOPE = ('<?xml version="1.0" encoding="UTF-8"?><Envelope xmlns="http://www.w3.org/2003/05/soap-envelope" '
            'xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
            'xmlns:w="http://schemas.dmtf.org/wbem/wsman/1/wsman.xsd"><Header>'
            '<a:Action>http://schemas.xmlsoap.org/ws/2004/09/transfer/Get</a:Action><a:To>/wsman</a:To>'
            '<w:ResourceURI>{uri}</w:ResourceURI><a:MessageID>1</a:MessageID><a:ReplyTo><a:Address>'
            'http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous</a:Address></a:ReplyTo>'
            '<w:OperationTimeout>PT30S</w:OperationTimeout></Header><Body/></Envelope>')


def load_env(path):
    env = {}
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        return env
    with open(path, encoding="utf-8") as f:
        for line in f:
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.strip().split("=", 1)
                env[k] = v.strip().strip('"').strip("'")
    return env


def targets_for(cfg, node):
    t = dict(cfg["common"])
    t.update(cfg["nodes"][node].get("override", {}))
    return t


def ssh_python(host, script, timeout=60):
    """Run `script` with python3 on `host` and parse its JSON output."""
    r = subprocess.run(["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes", host, "python3", "-"],
                       input=script, capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError(f"ssh {host}: rc {r.returncode}: {(r.stderr or r.stdout).strip()[:200]}")
    return json.loads(r.stdout)


def check_node(cfg, node, fix, remote=ssh_python):
    host = cfg["nodes"][node]["ssh"]
    want = targets_for(cfg, node)

    def run(writes):
        return remote(host, REMOTE % {"attr": ATTR, "writes": repr(writes)})

    res = run({})
    out = {"ssh": host, "module": res.get("module"), "patched": "/updates/" in (res.get("module") or ""),
           "uptime_s": res.get("uptime_s"), "drift": {}, "fixed": {}, "fix_failed": {}, "missing": []}
    if res.get("error"):
        out["error"] = res["error"]
        return out
    cur = res["before"]
    for name, value in want.items():
        if name not in cur:
            out["missing"].append(name)
        elif cur[name]["value"] != value:
            out["drift"][name] = {"is": cur[name]["value"], "want": value, "writable": cur[name]["writable"]}
    writes = {n: d["want"] for n, d in out["drift"].items()
              if d["writable"] and n not in cfg.get("never_write", [])}
    if fix and writes:
        if not out["patched"]:
            out["fix_failed"] = {n: "stock hp-bioscfg loaded (writes fail 0x4); run install_module.sh" for n in writes}
        else:
            res2 = run(writes)
            after = res2.get("after", {})
            for n, v in writes.items():
                got = after.get(n, {}).get("value")
                if res2["writes"].get(n) == "ok" and got == v:
                    out["fixed"][n] = v
                else:
                    out["fix_failed"][n] = f"{res2['writes'].get(n)}; reads back {got!r}"
            out["reboot_pending"] = bool(out["fixed"])
    return out


def amt_get(ip, user, password, uri, timeout=10):
    """One WS-Man Get; returns (HTTP status, body)."""
    url = f"http://{ip}:16992/wsman"
    mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    mgr.add_password(None, url, user, password)
    opener = urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(mgr))
    req = urllib.request.Request(url, data=ENVELOPE.format(uri=uri).encode(), method="POST",
                                 headers={"Content-Type": "application/soap+xml;charset=UTF-8"})
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""


def check_amt(ip, user, password, timeout=10, get=amt_get):
    out = {"ok": [], "bad": {}}
    for uri, field, ok, label in AMT_CHECKS:
        try:
            status, text = get(ip, user, password, uri, timeout)
        except (urllib.error.URLError, OSError) as e:  # refused, no route, timeout
            out["bad"][label] = type(getattr(e, "reason", e)).__name__
            out["unreachable"] = True
            break
        if status != 200:
            out["bad"][label] = f"HTTP {status}"
            continue
        m = re.search(rf"<(?:\w+:)?{field}>([^<]*)<", text)
        val = m.group(1) if m else None
        if val in ok:
            out["ok"].append(label)
        else:
            out["bad"][label] = f"{field}={val}"
    return out


def summary(data):
    lines = []
    for node, r in data["nodes"].items():
        if r.get("error"):
            lines.append(f"{node}: ERROR {r['error']}")
            continue
        bits = []
        if r.get("fixed"):
            bits.append("FIXED " + ", ".join(f"{n}={v}" for n, v in r["fixed"].items()) + " (reboot pending)")
        rest = {n: d for n, d in r.get("drift", {}).items() if n not in r.get("fixed", {})}
        if rest:
            bits.append("DRIFT " + ", ".join(f"{n}: {d['is']} (want {d['want']})" for n, d in rest.items()))
        if r.get("fix_failed"):
            bits.append("FIX FAILED " + "; ".join(f"{n}: {e}" for n, e in r["fix_failed"].items()))
        if r.get("missing"):
            bits.append("missing " + ", ".join(r["missing"]))
        if not r.get("patched"):  # loud on purpose: a kernel update without a DKMS rebuild silently loads the stock module
            bits.append(f"PATCHED MODULE NOT LOADED, BIOS fixes are off ({r.get('module') or 'no module'}) "
                        f"-> ./install_module.sh {r.get('ssh') or 'root@<node>'}")
        lines.append(f"{node}: " + ("; ".join(bits) if bits else "BIOS OK"))
    for name, r in data["amt"].items():
        if r.get("error"):
            lines.append(f"{name} AMT: ERROR {r['error']}")
        elif r.get("unreachable"):
            lines.append(f"{name} AMT: UNREACHABLE")
        elif r["bad"] and all(v == "HTTP 401" for v in r["bad"].values()):
            lines.append(f"{name} AMT: login rejected (401): AMT was reset or the account removed -> MEBx or your AMT admin tool")
        elif r["bad"]:
            lines.append(f"{name} AMT: " + ", ".join(f"{k} ({v})" for k, v in r["bad"].items()) + " -> fix in MEBx or your AMT admin tool")
        else:
            lines.append(f"{name} AMT: OK")
    return "\n".join(lines)


def prom(data, now=None):
    """node-exporter textfile metrics for the run."""
    L = ["# HELP bios_guard_bios_ok 1 if the node's BIOS matches its targets (fixed-but-pending counts as OK).",
         "# TYPE bios_guard_bios_ok gauge",
         "# HELP bios_guard_drift_settings Settings that differed from the targets at the start of the run.",
         "# TYPE bios_guard_drift_settings gauge",
         "# HELP bios_guard_patched_module 1 if the patched hp-bioscfg (DKMS) is the loaded module.",
         "# TYPE bios_guard_patched_module gauge",
         "# HELP bios_guard_reboot_pending 1 if settings were written and wait for the next boot.",
         "# TYPE bios_guard_reboot_pending gauge",
         "# HELP bios_guard_amt_ok 1 if every AMT check passed.",
         "# TYPE bios_guard_amt_ok gauge",
         "# HELP bios_guard_last_run_timestamp_seconds When bios-guard last ran.",
         "# TYPE bios_guard_last_run_timestamp_seconds gauge"]
    for node, r in data["nodes"].items():
        bad = bool(r.get("error") or r.get("fix_failed") or r.get("missing")
                   or set(r.get("drift", {})) - set(r.get("fixed", {})))
        L.append(f'bios_guard_bios_ok{{node="{node}"}} {int(not bad)}')
        L.append(f'bios_guard_drift_settings{{node="{node}"}} {len(r.get("drift", {}))}')
        L.append(f'bios_guard_patched_module{{node="{node}"}} {int(bool(r.get("patched")))}')
        L.append(f'bios_guard_reboot_pending{{node="{node}"}} {int(bool(r.get("reboot_pending")))}')
    for name, r in data["amt"].items():
        L.append(f'bios_guard_amt_ok{{node="{name}"}} {int(not r.get("error") and not r.get("bad"))}')
    L.append(f"bios_guard_last_run_timestamp_seconds {int(now if now is not None else time.time())}")
    return "\n".join(L) + "\n"


def notify(text, env):
    """Send to NOTIFY_WEBHOOK_URL (JSON {NOTIFY_WEBHOOK_FIELD: text}) and/or Telegram, whichever is configured."""
    sent = []
    if env.get("NOTIFY_WEBHOOK_URL"):
        body = json.dumps({env.get("NOTIFY_WEBHOOK_FIELD", "text"): text}).encode()
        req = urllib.request.Request(env["NOTIFY_WEBHOOK_URL"], data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            sent.append(f"webhook HTTP {r.status}")
    if env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID"):
        body = urllib.parse.urlencode({"chat_id": env["TELEGRAM_CHAT_ID"], "text": text[:4000]}).encode()
        url = f"https://api.telegram.org/bot{env['TELEGRAM_BOT_TOKEN']}/sendMessage"
        with urllib.request.urlopen(url, data=body, timeout=30) as r:
            sent.append(f"telegram ok={json.loads(r.read()).get('ok')}")
    return sent or ["nothing configured (NOTIFY_WEBHOOK_URL or TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID)"]


def write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=os.environ.get("BIOS_GUARD_CONFIG", os.path.join(HERE, "targets.json")),
                    help="targets file (default: targets.json next to this script)")
    ap.add_argument("--fix", action="store_true", help="write drifted writable settings back")
    ap.add_argument("--node", action="append", help="limit to these nodes (BIOS and AMT)")
    ap.add_argument("--no-amt", action="store_true")
    ap.add_argument("--amt-env", default="~/.config/bios-guard/amt.env",
                    help="file with AMT_USER= / AMT_PASS= (environment variables win)")
    ap.add_argument("--dump", metavar="NODE", help="print every attribute on one node and exit")
    ap.add_argument("--state", default=os.path.join(HERE, "last.json"), help="JSON state file")
    ap.add_argument("--no-save", action="store_true", help="don't write the state file")
    ap.add_argument("--prom", metavar="FILE", help="also write node-exporter textfile metrics here")
    ap.add_argument("--notify", action="store_true", help="send problems to the webhook / Telegram set in the env")
    ap.add_argument("--notify-env", default="~/.config/bios-guard/notify.env",
                    help="file with NOTIFY_WEBHOOK_URL / TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (environment wins)")
    args = ap.parse_args(argv)
    try:
        with open(args.config, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        print(f"config {args.config}: {e}", file=sys.stderr)
        sys.exit(2)
    if args.dump:
        for n, v in ssh_python(cfg["nodes"][args.dump]["ssh"], REMOTE % {"attr": ATTR, "writes": "{}"})["before"].items():
            print(f"[{'RW' if v.get('writable') else 'RO'}] {n} = {v.get('value')}")
        return
    nodes = args.node or list(cfg["nodes"])
    bad = [n for n in nodes if n not in cfg["nodes"] and n not in cfg.get("amt_only", {})]
    if bad:
        print(f"unknown node(s): {bad}", file=sys.stderr)
        sys.exit(2)
    data = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "fix": args.fix, "nodes": {}, "amt": {}}
    for node in nodes:
        if node not in cfg["nodes"]:
            continue
        try:
            data["nodes"][node] = check_node(cfg, node, args.fix)
        except Exception as e:  # one dead node must not stop the rest
            data["nodes"][node] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    if not args.no_amt:
        env = {**load_env(args.amt_env), **{k: os.environ[k] for k in ("AMT_USER", "AMT_PASS") if k in os.environ}}
        amt = {n: c["amt"] for n, c in cfg["nodes"].items() if c.get("amt")}
        amt.update(cfg.get("amt_only", {}))
        for name, ip in amt.items():
            if args.node and name not in args.node:
                continue
            try:
                data["amt"][name] = check_amt(ip, env["AMT_USER"], env["AMT_PASS"])
            except Exception as e:
                data["amt"][name] = {"error": f"{type(e).__name__}: {str(e)[:200]}", "bad": {}}
    data["summary"] = summary(data)
    if not args.no_save:
        write_atomic(args.state, json.dumps(data, indent=1))
    if args.prom:
        write_atomic(args.prom, prom(data))
    print(f"{data['time']} bios-guard{' --fix' if args.fix else ''}\n{data['summary']}")
    problems = [line for line in data["summary"].splitlines() if not line.endswith((": OK", ": BIOS OK"))]
    if args.notify and problems:
        env = {**load_env(args.notify_env), **os.environ}
        print("notify:", ", ".join(notify("BIOS/AMT check\n" + "\n".join(problems), env)))


if __name__ == "__main__":
    main()
