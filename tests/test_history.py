"""链路延迟记录：探测时写入、汇总、异常时段、内核切换、proxyctl history 与 GUI 延迟页；GUI“关于”窗口。"""

import json
import os
import socket
import time
import unittest

from tests.helpers import FakeEnv, P, fixture
from tests.test_system import _has_gui

INIT = fixture("config_basic_init.dae")


def rec(t, s=0.3, ok=True, core="sing-box"):
    return {"t": t, "ok": 1 if ok else 0, "s": s, "core": core}


class TestHistoryPure(unittest.TestCase):
    def test_stats(self):
        recs = [rec(0, 0.2), rec(120, 0.4), rec(240, 5.0), rec(360, 15, ok=False)]
        st = P.history_stats(recs, slow=3)
        self.assertEqual((st["count"], st["fails"], st["slow"]), (4, 1, 1))
        self.assertEqual(st["median"], 0.4)
        self.assertEqual(st["max"], 5.0)
        self.assertIsNone(P.history_stats([], slow=3)["median"])

    def test_episodes_merge_and_ignore_single_blip(self):
        recs = [rec(0), rec(120, 4.0),                       # 只有 1 次偏慢：不算
                rec(240), rec(360, 4.0), rec(375, 6.0), rec(390, ok=False), rec(405, 5.0),
                rec(420, 0.3)]
        eps = P.history_episodes(recs, slow=3)
        self.assertEqual(len(eps), 1)
        e = eps[0]
        self.assertEqual((e["start"], e["end"], e["fails"], e["slow"]), (360, 405, 1, 3))
        self.assertEqual(P.describe_episode(e, slow=3)[0], "不稳定")

    def test_episode_split_by_gap(self):
        # 两次偏慢之间隔了 1 小时（休眠），不能连成一个时段
        recs = [rec(0, 4.0), rec(3600, 4.0)]
        self.assertEqual(P.history_episodes(recs, slow=3), [])
        recs = [rec(0, ok=False), rec(15, ok=False)]
        self.assertEqual(P.describe_episode(P.history_episodes(recs, slow=3)[0], slow=3)[0], "不通")

    def test_core_switches(self):
        recs = [rec(0, core="sing-box"), rec(120, core=""), rec(240, core="xray"), rec(360, core="xray"),
                rec(480, core="sing-box")]
        self.assertEqual(P.history_core_switches(recs), [(240, "sing-box", "xray"), (480, "xray", "sing-box")])

    def test_core_color_follows_name(self):
        self.assertEqual(P.core_color("xray", False), "#eb6834")
        self.assertEqual(P.core_color("sing-box", True), "#199e70")
        self.assertEqual(P.core_color("verge-mihomo", False), P.core_color("mihomo", False))  # Clash Verge 改名的 mihomo
        self.assertIsNone(P.core_color("some-core", False))
        self.assertIsNone(P.core_color("", True))

    def test_buckets(self):
        recs = [rec(10, 0.2), rec(20, 0.4, core="xray"), rec(3700, 1.0)]
        b = P.history_buckets(recs, 0, 7200, 3600)
        self.assertEqual([k for k, _, _ in b], [0, 3600])
        self.assertEqual(b[0][1]["count"], 2)
        self.assertEqual(b[0][2], ["sing-box", "xray"])


class TestHistoryFile(FakeEnv):
    def setUp(self):
        super().setUp()
        os.environ["PROXYCTL_HISTORY"] = self.env["PROXYCTL_HISTORY"]
        self.addCleanup(os.environ.pop, "PROXYCTL_HISTORY", None)
        self.path = self.env["PROXYCTL_HISTORY"]

    def test_record_load_and_skip_bad_lines(self):
        P.record_chain_sample(1000, True, 0.25, "xray")
        with open(self.path, "a") as f:
            f.write("not json\n{\"x\": 1}\n")
        P.record_chain_sample(1100, False, 15.0, None)
        recs = P.load_history()
        self.assertEqual(recs, [rec(1000, 0.25, core="xray"), rec(1100, 15.0, ok=False, core="")])
        self.assertEqual(P.load_history(1050), [rec(1100, 15.0, ok=False, core="")])

    def test_trim_keeps_recent_days(self):
        now = time.time()
        old = now - (P.HISTORY_DAYS + 1) * 86400
        with open(self.path, "w") as f:
            for i in range(4000):  # 约 300 KB，超过重写门槛
                f.write(json.dumps(rec(int(old + i), core="sing-box-padding-padding")) + "\n")
            f.write(json.dumps(rec(int(now - 60))) + "\n")
        P.record_chain_sample(now, True, 0.2, "xray")
        recs = P.load_history()
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[-1]["core"], "xray")


class TestHistoryCli(FakeEnv):
    def setUp(self):
        super().setUp()
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(5)
        self.addCleanup(s.close)
        self.env["PROXYCTL_SOCKS"] = "127.0.0.1:%d" % s.getsockname()[1]
        self.add_proc(66511, "xray")
        self.path = self.env["PROXYCTL_HISTORY"]

    def records(self):
        return P.load_history(path=self.path)

    def test_probe_writes_history_only_when_it_really_probes(self):
        self.run_cli("check", "--basic")
        recs = self.records()
        self.assertEqual(len(recs), 1)
        self.assertEqual((recs[0]["ok"], recs[0]["core"]), (1, "xray"))
        self.run_cli("check", "--basic")  # 间隔内复用结果：不记录
        self.assertEqual(len(self.records()), 1)
        self.flag("rc_proxy", "28")
        self.run_cli("check", "--basic", extra_env={"PROXYCTL_CHAIN_INTERVAL": "0"})
        self.assertEqual([r["ok"] for r in self.records()], [1, 0])

    def test_history_empty(self):
        rc, out, err = self.run_cli("history")
        self.assertEqual(rc, 0, err)
        self.assertIn("没有记录", out)
        self.assertIn("proxyctl-check.timer", out)

    def test_history_table_episode_and_switch(self):
        now = int(time.time())
        recs = [rec(now - 7200 + i * 120, 0.3) for i in range(20)]
        recs += [rec(now - 3000 + i * 15, 5.0) for i in range(4)]
        recs += [rec(now - 2000 + i * 120, 0.6, core="xray") for i in range(10)]
        with open(self.path, "w") as f:
            f.write("".join(json.dumps(r) + "\n" for r in recs))
        rc, out, err = self.run_cli("history", "--hours", "3")
        self.assertEqual(rc, 0, err)
        self.assertIn("共 34 次探测", out)
        self.assertIn("偏慢（超过 3 秒）4 次", out)
        self.assertIn("当前内核：xray", out)
        self.assertIn("很慢：4 次超过 3 秒", out)
        self.assertIn("内核切换：sing-box → xray", out)
        self.assertIn("█", out)


class TestHistoryGui(FakeEnv):
    @unittest.skipUnless(_has_gui(), "没有图形环境或 PyGObject")
    def test_chain_page_renders(self):
        self.write_config(INIT)
        now = int(time.time())
        with open(self.env["PROXYCTL_HISTORY"], "w") as f:
            for i in range(30):
                f.write(json.dumps(rec(now - 3600 + i * 120, 0.3 if i % 7 else 4.0, ok=i != 20)) + "\n")
        env = {"PROXYCTL_GUI_AUTOQUIT": "3", "PROXYCTL_GUI_PAGE": "chain"}
        if os.environ.get("XDG_RUNTIME_DIR"):
            env["XDG_RUNTIME_DIR"] = os.environ["XDG_RUNTIME_DIR"]
        rc, out, err = self.run_cli("gui", extra_env=env, timeout=60)
        if "无法连接图形显示" in err:
            self.skipTest("图形显示不可用")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("Traceback", err)
        self.assertNotIn("Error", err)


class TestAboutGui(FakeEnv):
    @unittest.skipUnless(_has_gui(), "没有图形环境或 PyGObject")
    def test_about_opens(self):
        self.write_config(INIT)
        env = {"PROXYCTL_GUI_AUTOQUIT": "2", "PROXYCTL_GUI_ABOUT": "1"}
        if os.environ.get("XDG_RUNTIME_DIR"):
            env["XDG_RUNTIME_DIR"] = os.environ["XDG_RUNTIME_DIR"]
        rc, out, err = self.run_cli("gui", extra_env=env, timeout=60)
        if "无法连接图形显示" in err:
            self.skipTest("图形显示不可用")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(P.HOMEPAGE, "https://github.com/atmago/dae-proxyctl")
        self.assertIn("本程序遵循 MIT 许可证", P.LICENSE_MARKUP)
        self.assertNotIn("担保", P.LICENSE_MARKUP)


if __name__ == "__main__":
    unittest.main()
