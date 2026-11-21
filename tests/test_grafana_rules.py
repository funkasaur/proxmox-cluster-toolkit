import http.server
import json
import threading
import unittest
from urllib.parse import unquote

import grafana_rules as gr


class FakeGrafana:
    def __init__(self):
        self.rules, self.groups, self.writes = {}, {}, []
        self.folders = {"f1"}
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, code, value=None):
                data = json.dumps(value).encode() if value is not None else b""
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def body(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                return json.loads(raw) if raw else None

            def do_GET(self):
                p = unquote(self.path)
                if p.startswith("/api/folders/"):
                    return self.reply(200 if p.rsplit("/", 1)[1] in fake.folders else 404, {})
                if p.startswith("/api/v1/provisioning/alert-rules/"):
                    uid = p.rsplit("/", 1)[1]
                    return self.reply(200, fake.rules[uid]) if uid in fake.rules else self.reply(404, {})
                if "/rule-groups/" in p:
                    group = p.rsplit("/", 1)[1]
                    members = [r for r in fake.rules.values() if r["ruleGroup"] == group]
                    return self.reply(200, {"title": group, "rules": members}) if members else self.reply(404, {})
                return self.reply(404, {})

            def do_POST(self):
                b = self.body()
                fake.writes.append(("POST", unquote(self.path), self.headers.get("X-Disable-Provenance")))
                fake.rules[b["uid"]] = dict(b, id=len(fake.rules) + 1)
                return self.reply(201, b)

            def do_PUT(self):
                b = self.body()
                p = unquote(self.path)
                fake.writes.append(("PUT", p, self.headers.get("X-Disable-Provenance")))
                if "/rule-groups/" in p:
                    group = p.rsplit("/", 1)[1]
                    keep = {r["uid"] for r in b["rules"]}
                    for uid in [u for u, r in fake.rules.items() if r["ruleGroup"] == group and u not in keep]:
                        del fake.rules[uid]  # Grafana replaces the whole group
                    fake.groups[group] = b["interval"]
                    return self.reply(200, b)
                fake.rules[p.rsplit("/", 1)[1]] = b
                return self.reply(200, b)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def make(uid, title="T", group="g"):
    return gr.rule(uid, title, "up == 0", folder_uid="f1", group=group, datasource_uid="ds")


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGrafana()
        self.addCleanup(self.fake.close)
        self.g = gr.Grafana(self.fake.url, token="t", timeout=2)
        self.log = []

    def test_create_then_update_idempotently(self):
        gr.apply(self.g, [make("a"), make("b")], folder_uid="f1", group="g", interval_s=120, log=self.log.append)
        self.assertEqual(set(self.fake.rules), {"a", "b"})
        self.assertEqual(self.fake.groups["g"], 120)
        self.assertTrue(all(w[2] == "true" for w in self.fake.writes))
        gr.apply(self.g, [make("a", "New title"), make("b")], folder_uid="f1", group="g", log=self.log.append)
        self.assertEqual(self.fake.rules["a"]["title"], "New title")
        posts = [w for w in self.fake.writes if w[0] == "POST"]
        self.assertEqual(len(posts), 2)  # second run updated, didn't create

    def test_refuses_to_drop_unlisted_rules_without_prune(self):
        gr.apply(self.g, [make("a"), make("handmade")], folder_uid="f1", group="g", log=self.log.append)
        with self.assertRaises(gr.GrafanaError) as e:
            gr.apply(self.g, [make("a")], folder_uid="f1", group="g", log=self.log.append)
        self.assertIn("handmade", str(e.exception))
        self.assertIn("handmade", self.fake.rules)
        removed = gr.apply(self.g, [make("a")], folder_uid="f1", group="g", prune=True, log=self.log.append)
        self.assertEqual(removed, ["handmade"])
        self.assertNotIn("handmade", self.fake.rules)

    def test_other_groups_untouched(self):
        gr.apply(self.g, [make("x", group="other")], folder_uid="f1", group="other", log=self.log.append)
        gr.apply(self.g, [make("a")], folder_uid="f1", group="g", log=self.log.append)
        self.assertIn("x", self.fake.rules)

    def test_validation(self):
        with self.assertRaises(gr.GrafanaError):
            gr.apply(self.g, [make("a", group="h")], folder_uid="f1", group="g", log=self.log.append)
        with self.assertRaises(gr.GrafanaError):
            gr.apply(self.g, [make("a"), make("a")], folder_uid="f1", group="g", log=self.log.append)
        with self.assertRaises(gr.GrafanaError):
            gr.apply(self.g, [gr.rule("a", "T", "up", folder_uid="nope", group="g", datasource_uid="d")],
                     folder_uid="nope", group="g", log=self.log.append)

    def test_dry_run_writes_nothing(self):
        g = gr.Grafana(self.fake.url, token="t", timeout=2, dry_run=True)
        gr.apply(g, [make("a")], folder_uid="f1", group="g", log=self.log.append)
        self.assertEqual(self.fake.writes, [])

    def test_rule_shape(self):
        r = gr.rule("u", "Title", "x > 1", folder_uid="f", group="g", datasource_uid="ds", severity="critical",
                    labels={"team": "ops"}, threshold=5, comparison="lt")
        self.assertEqual(r["labels"], {"severity": "critical", "team": "ops"})
        self.assertEqual(r["data"][0]["datasourceUid"], "ds")
        self.assertEqual(r["data"][1]["model"]["conditions"][0]["evaluator"], {"type": "lt", "params": [5]})


if __name__ == "__main__":
    unittest.main()
