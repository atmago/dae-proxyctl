"""名单导入 / 导出：文件解析、合并与替换规则、完整 CLI 流程。"""

import json
import os

from tests.helpers import BASIC, FakeEnv, P, fixture

INIT = fixture("config_basic_init.dae")  # direct: NetworkManager；proxy: curl, signal-desktop


def data(proxy=(), direct=()):
    return {"proxy": list(proxy), "direct": list(direct)}


class TestParse(FakeEnv):
    def test_roundtrip(self):
        text = P.export_text({"proxy": ["firefox"], "direct": ["NetworkManager"]})
        d = json.loads(text)
        self.assertEqual(d["format"], P.EXPORT_FORMAT)
        self.assertEqual(P.parse_import(text.encode()), data(["firefox"], ["NetworkManager"]))

    def test_minimal_handwritten(self):
        self.assertEqual(P.parse_import('{"proxy": ["a", "a"]}'), data(["a"], []))

    def test_rejects(self):
        for raw in (b"not json", b"[]", b"{}", b'{"proxy": "firefox"}',
                    b'{"proxy": ["bad name"]}', b'{"proxy": ["x) -> direct\\n"]}',
                    b'{"format": "other", "proxy": []}', b'{"version": 99, "proxy": []}',
                    b'{"proxy": ["a"], "direct": ["a"]}', b"\xff\xfe",
                    b" " * (P.IMPORT_MAX_BYTES + 1)):
            with self.assertRaises(P.ProxyctlError, msg=raw[:40]):
                P.parse_import(raw)


class TestPlan(FakeEnv):
    def test_merge_adds_only(self):
        p, d, msgs = P.import_plan(["curl"], ["NetworkManager"], data(["firefox", "curl"], ["sshd"]))
        self.assertEqual(p, ["curl", "firefox"])
        self.assertEqual(d, ["NetworkManager", "sshd"])

    def test_merge_keeps_conflicts_unchanged(self):
        p, d, msgs = P.import_plan(["firefox"], ["chrome"], data(["chrome"], ["firefox"]))
        self.assertEqual((p, d), (["firefox"], ["chrome"]))
        self.assertEqual(sum("跳过" in m for m in msgs), 2)

    def test_replace(self):
        p, d, msgs = P.import_plan(["curl", "old"], ["NetworkManager"], data(["firefox"], []), replace=True)
        self.assertEqual((p, d), (["firefox"], []))
        self.assertTrue(any("移除 2 个" in m for m in msgs))

    def test_replace_can_swap_sides(self):
        p, d, _ = P.import_plan(["a"], ["b"], data(["b"], ["a"]), replace=True)
        self.assertEqual((p, d), (["b"], ["a"]))

    def test_safety_and_blacklist_skipped(self):
        p, d, msgs = P.import_plan([], [], data(["xray", "python3", "proxyctl-probe", "ok"],
                                                ["proxyctl-probe", "sing-box"]))
        self.assertEqual((p, d), (["ok"], []))
        self.assertTrue(any("xray" in m for m in msgs))
        self.assertTrue(any("python3" in m for m in msgs))


class TestCli(FakeEnv):
    def export(self):
        path = os.path.join(self.tmp, "lists.json")
        rc, out, err = self.run_cli("export", path)
        self.assertEqual(rc, 0, err)
        return path

    def test_export_stdout_and_file(self):
        self.write_config(INIT)
        rc, out, err = self.run_cli("export")
        self.assertEqual(rc, 0, err)
        d = json.loads(out)
        self.assertEqual((d["proxy"], d["direct"]), (["curl", "signal-desktop"], ["NetworkManager"]))
        with open(self.export(), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["proxy"], ["curl", "signal-desktop"])

    def test_export_uninitialized(self):
        self.write_config(BASIC)
        rc, out, err = self.run_cli("export")
        self.assertEqual(rc, 1)
        self.assertIn("尚未初始化", err)

    def test_import_merge(self):
        self.write_config(INIT)
        path = os.path.join(self.tmp, "in.json")
        with open(path, "w") as f:
            json.dump(data(["firefox", "curl"], ["sshd"]), f)
        rc, out, err = self.run_cli("import", path)
        self.assertEqual(rc, 0, err)
        conf = self.read_config()
        for line in ("pname(curl) -> proxy", "pname(signal-desktop) -> proxy", "pname(firefox) -> proxy",
                     "pname(NetworkManager) -> must_direct", "pname(sshd) -> must_direct"):
            self.assertIn(line, conf)
        self.assertEqual(self.reload_count(), 1)
        self.assertEqual(len(self.backups()), 1)
        self.assertTrue(self.backups()[0].endswith("-import.dae"))

    def test_import_replace_and_roundtrip(self):
        self.write_config(INIT)
        path = self.export()
        rc, out, err = self.run_cli("add", "firefox")
        self.assertEqual(rc, 0, err)
        rc, out, err = self.run_cli("import", path, "--replace")
        self.assertEqual(rc, 0, err)
        # curl 是黑名单中的通用工具名：导入时跳过，所以替换后不再代理
        self.assertIn("跳过：“curl”", out)
        self.assertEqual(self.read_config(), INIT.replace("    pname(curl) -> proxy\n", ""))

    def test_import_no_change(self):
        self.write_config(INIT)
        path = self.export()
        rc, out, err = self.run_cli("import", path)
        self.assertEqual(rc, 0, err)
        self.assertIn("无变化", out)
        self.assertEqual(self.reload_count(), 0)

    def test_bad_file_changes_nothing(self):
        self.write_config(INIT)
        path = os.path.join(self.tmp, "bad.json")
        with open(path, "w") as f:
            f.write('{"proxy": ["ok", "evil) -> proxy"]}')
        rc, out, err = self.run_cli("import", path)
        self.assertEqual(rc, 1)
        self.assertIn("不合法", err)
        self.assertEqual(self.read_config(), INIT)
        self.assertEqual(self.backups(), [])

    def test_missing_file(self):
        self.write_config(INIT)
        rc, out, err = self.run_cli("import", os.path.join(self.tmp, "nope.json"))
        self.assertEqual(rc, 1)
        self.assertIn("无法读取", err)
