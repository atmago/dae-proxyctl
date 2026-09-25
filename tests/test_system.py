"""ss 解析、check、test、run、guard-desktop、真实 dae validate、脚本语法与 GUI 冒烟测试。"""

import os
import py_compile
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest

from tests.helpers import BASIC, FAKES, PROXYCTL, ROOT, FakeEnv, P, fixture

INIT = fixture("config_basic_init.dae")


class TestSs(unittest.TestCase):
    def setUp(self):
        self.rows = {a["name"]: a for a in P.aggregate(P.parse_ss(fixture("ss_sample.txt")))}

    def test_excludes_local(self):
        for n in ("loop6", "sshd", "ula", "lanapp"):
            self.assertNotIn(n, self.rows)

    def test_aggregation(self):
        self.assertEqual(self.rows["xray"]["conns"], 2)
        self.assertEqual(self.rows["xray"]["pids"], {66511})
        self.assertEqual(self.rows["chrome"]["conns"], 3)
        self.assertEqual(self.rows["chrome"]["pids"], {5725, 5726})
        self.assertIn("[2001:4860:4840:400::]:443", self.rows["chrome"]["samples"])
        self.assertEqual(self.rows["signal-desktop"]["samples"], ["22.33.44.55:443"])

    def test_mapped_and_multi_owner(self):
        self.assertEqual(self.rows["java"]["pids"], {900, 901})
        self.assertEqual(self.rows["java"]["samples"], ["8.8.4.4:443"])
        self.assertIn("Web Content", self.rows)
        self.assertIn("firefox", self.rows)

    def test_unknown_process_and_udp(self):
        self.assertEqual(self.rows["(未知进程)"]["conns"], 1)
        self.assertIn("chronyd", self.rows)

    def test_real_recorded_sample(self):
        rows = P.aggregate(P.parse_ss(fixture("ss_real_sample.txt")))
        names = {a["name"] for a in rows}
        self.assertIn("xray", names)
        self.assertIn("signal-desktop", names)
        for a in rows:
            for s in a["samples"]:
                self.assertFalse(s.startswith("127.") or s.startswith("192.168."), s)

    def test_status(self):
        view = P.rules_view_from_parsed(P.Parsed(INIT))
        self.assertEqual(P.status_of("signal-desktop", view), P.STATUS_PROXY)
        self.assertEqual(P.status_of("proxyctl-probe", view), P.STATUS_PROXY)
        self.assertEqual(P.status_of("xray", view), P.STATUS_DIRECT)
        self.assertEqual(P.status_of("NetworkManager", view), P.STATUS_DIRECT)
        self.assertEqual(P.status_of("chrome", view), P.STATUS_DEFAULT)
        self.assertEqual(P.status_of("chrome", None), P.STATUS_UNKNOWN)

    def test_split_addr(self):
        self.assertEqual(str(P.split_addr("192.168.1.10%eth0:68")[0]), "192.168.1.10")
        self.assertEqual(P.split_addr("[::1]:53")[1], "53")
        self.assertIsNone(P.split_addr("*:*")[0])
        self.assertEqual(str(P.split_addr("[::ffff:1.2.3.4]:80")[0]), "1.2.3.4")


class TestCliReadOnly(FakeEnv):
    def listen(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(5)
        self.addCleanup(s.close)
        self.env["PROXYCTL_SOCKS"] = "127.0.0.1:%d" % s.getsockname()[1]
        return s

    def test_list(self):
        self.write_config(INIT)
        rc, out, err = self.run_cli("list")
        self.assertEqual(rc, 0, err)
        self.assertIn("非 root", out)
        line = [l for l in out.splitlines() if l.startswith("signal-desktop")][0]
        self.assertIn("代理", line)
        line = [l for l in out.splitlines() if l.startswith("chrome")][0]
        self.assertIn("默认直连", line)
        self.assertNotIn("lanapp", out)

    def test_list_uses_cache_when_unreadable(self):
        self.write_config(BASIC)
        self.assertEqual(self.run_cli("init")[0], 0)
        os.chmod(self.config, 0o000)
        try:
            if os.access(self.config, os.R_OK):
                self.skipTest("以 root 运行，无法模拟不可读")
            rc, out, err = self.run_cli("list")
            rc2, out2, _ = self.run_cli("rules")
        finally:
            os.chmod(self.config, 0o600)
        self.assertEqual(rc, 0, err)
        self.assertIn("缓存", out)
        self.assertIn("代理", [l for l in out.splitlines() if l.startswith("signal-desktop")][0])
        self.assertEqual(rc2, 0)
        self.assertIn("signal-desktop", out2)

    def test_pick(self):
        self.write_config(INIT)
        rc, out, err = self.run_cli("pick", input="\n")
        self.assertEqual(rc, 0, err)
        self.assertIn("已取消", out)
        # 选中 firefox（按连接数排序后找到编号）
        rows = P.aggregate(P.parse_ss(fixture("ss_sample.txt")))
        rows = [r for r in rows if r["name"] != "(未知进程)"]
        idx = [r["name"] for r in rows].index("firefox") + 1
        rc, out, err = self.run_cli("pick", input="%d\ny\n" % idx)
        self.assertEqual(rc, 0, err)
        self.assertIn("pname(firefox) -> proxy", self.read_config())
        # 黑名单进程（java）被拒绝
        idx = [r["name"] for r in rows].index("java") + 1
        rc, out, err = self.run_cli("pick", input="%d\ny\n" % idx)
        self.assertEqual(rc, 1)
        self.assertIn("通用运行时", err)

    def test_check_failures(self):
        self.write_config(INIT)
        self.flag("state", "inactive")
        self.flag("restart", "on-abnormal")
        rc, out, err = self.run_cli("check", "--notify")
        self.assertEqual(rc, 2, out + err)
        self.assertIn("[FAIL] dae 服务未运行", out)
        self.assertIn("故障即放行", out)
        self.assertIn("没有在监听", out)
        self.assertIn("权限不符合", out)  # 测试文件不是 root 所有
        self.assertIn("Restart=on-abnormal", out)
        self.assertIn("通用运行时名称“curl”", out)
        self.assertIn("dial_mode：未设置（默认 domain）", out)
        self.assertIn("安全区块完整", out)
        self.assertIn("proxyctl：代理故障", self.fake_file("notify.log"))
        # 同一故障 10 分钟内不重复弹通知
        os.unlink(os.path.join(self.fake, "notify.log"))
        self.run_cli("check", "--notify", "--quiet")
        self.assertIsNone(self.fake_file("notify.log"))
        # 恢复正常：弹一条“已恢复”通知并清除状态；之后不再重复
        self.flag("state", "active")
        self.listen()
        self.add_proc(66511, "xray")
        rc, out, err = self.run_cli("check", "--notify", "--quiet", "--basic")
        self.assertEqual(rc, 0, out + err)
        log = self.fake_file("notify.log")
        self.assertIn("代理已恢复正常", log)
        self.assertIn("{'urgency': <byte 1>}", log)  # “已恢复”为普通级别
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "proxyctl-notify.json")))
        os.unlink(os.path.join(self.fake, "notify.log"))
        self.run_cli("check", "--notify", "--quiet", "--basic")
        self.assertIsNone(self.fake_file("notify.log"))
        # 再次故障会立即提醒
        self.flag("state", "failed")
        self.run_cli("check", "--notify", "--quiet", "--basic")
        self.assertIn("代理故障", self.fake_file("notify.log"))

    def test_check_quiet_ok(self):
        self.write_config(INIT)
        self.listen()
        self.add_proc(66511, "xray")
        rc, out, err = self.run_cli("check", "--quiet", "--basic")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(out.strip(), "")
        rc, out, err = self.run_cli("check", "--basic")
        self.assertIn("[ OK ] dae 服务正在运行", out)
        self.assertIn("xray(pid 66511)", out)

    def test_check_safety_broken(self):
        self.write_config(INIT.replace("    pname(mihomo) -> must_direct\n", ""))
        rc, out, err = self.run_cli("check")
        self.assertIn("安全区块不完整", out)
        self.write_config(BASIC.replace("log_level: info", "log_level: info\n    dial_mode: domain++"))
        rc, out, err = self.run_cli("check")
        self.assertIn("没有 proxyctl 管理区块", out)
        self.assertIn("dial_mode：domain++", out)
        self.assertIn("systemd-resolve 的 must_direct 规则：不存在", out)

    def test_check_socks_mismatch_and_conf_file(self):
        self.write_config(INIT)  # dae 节点是 socks5://127.0.0.1:10808，而测试环境的 SOCKS 是 127.0.0.1:1
        rc, out, err = self.run_cli("check")
        self.assertIn("SOCKS5 节点是 127.0.0.1:10808，而 proxyctl 检查的是 127.0.0.1:1", out)
        # 不设环境变量时读取配置文件
        del self.env["PROXYCTL_SOCKS"]
        conf = self.env["PROXYCTL_CONF"]
        with open(conf, "w") as f:
            f.write("# 注释\nsocks = 127.0.0.1:10808  # 行尾注释\nunknown = 1\n")
        os.chmod(conf, 0o644)
        rc, out, err = self.run_cli("check")
        self.assertNotIn("SOCKS5 节点是", out)
        self.assertIn("127.0.0.1:10808", out)
        # 其他用户可写的配置文件被忽略
        os.chmod(conf, 0o666)
        rc, out, err = self.run_cli("check")
        self.assertIn("已忽略", err)

    def test_load_conf_and_extra_ifaces(self):
        conf = self.env["PROXYCTL_CONF"]
        with open(conf, "w") as f:
            f.write("SOCKS=127.0.0.1:7891\niface = eth1, wlan0\nstate_dir = /tmp/x\n")
        os.chmod(conf, 0o600)
        self.assertEqual(P.load_conf(conf), {"socks": "127.0.0.1:7891", "iface": "eth1, wlan0"})
        self.assertEqual(P.config_socks_addrs("a: 'socks5://u:p@localhost:7891' b: 'socks5://[::1]:1080#x'"),
                         {"127.0.0.1:7891", "[::1]:1080"})
        old = os.environ.get("PROXYCTL_CONF")
        os.environ["PROXYCTL_CONF"] = conf
        try:
            self.assertEqual(P.extra_ifaces(), {"eth1", "wlan0"})
        finally:
            os.environ["PROXYCTL_CONF"] = old

    def _libdir(self):
        lib = os.path.join(self.tmp, "lib")
        os.mkdir(lib)
        for n in ("proxyctl-probe", "proxyctl-direct"):
            os.symlink(os.path.join(FAKES, "curl"), os.path.join(lib, n))
        self.env["PROXYCTL_LIBDIR"] = lib
        self.env["PROXYCTL_CURL"] = os.path.join(FAKES, "curl")
        self.env["PROXYCTL_SPEED_SAMPLES"] = "3"
        self.env["PROXYCTL_SYSFS_NET"] = os.path.join(self.tmp, "net")
        os.makedirs(os.path.join(self.tmp, "net", "eth0"))

    def test_selftest_pass(self):
        self._libdir()
        self.flag("ip_proxy", "203.0.113.7\n")
        self.flag("ip_proxyctl-probe", "203.0.113.7\n")
        self.flag("ip_proxyctl-direct", "198.51.100.1\n")
        rc, out, err = self.run_cli("test")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(out.count("[PASS]"), 4)
        self.assertIn("新建代理连接速度正常", out)
        self.assertNotIn("[WARN]", out)
        self.assertIn("全部通过", out)

    def test_selftest_warns_slow_new_connections_and_tun(self):
        self._libdir()
        self.flag("ip_proxy", "203.0.113.7\n")
        self.flag("ip_proxyctl-probe", "203.0.113.7\n")
        self.flag("ip_proxyctl-direct", "198.51.100.1\n")
        self.flag("sleep_proxy", "0.3")
        os.makedirs(os.path.join(self.tmp, "net", "singbox_tun"))
        open(os.path.join(self.tmp, "net", "singbox_tun", "tun_flags"), "w").close()
        rc, out, err = self.run_cli("test", extra_env={"PROXYCTL_CONNECT_SLOW": "0.1"})
        self.assertEqual(rc, 0, out + err)  # 警告不影响退出码：分流本身是好的
        self.assertEqual(out.count("[PASS]"), 3)
        self.assertIn("[WARN] 4. 新建代理连接偏慢", out)
        self.assertIn("Mux", out)
        self.assertIn("[WARN] 5. 检测到 TUN 网卡：singbox_tun", out)
        self.assertIn("有 2 项警告", out)
        self.assertNotIn("eth0", out)

    def test_selftest_retries_transient_timeout(self):
        self._libdir()
        self.flag("ip_proxy", "203.0.113.7\n")
        self.flag("flaky_proxy", "2")  # 前两次超时，第三次成功
        self.flag("ip_proxyctl-probe", "203.0.113.7\n")
        self.flag("ip_proxyctl-direct", "198.51.100.1\n")
        rc, out, err = self.run_cli("test")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(out.count("[PASS]"), 4)
        self.assertIn("第 3 次尝试才成功", out)
        # 持续失败：重试用尽后才判 FAIL
        self.flag("flaky_proxy", "99")
        rc, out, err = self.run_cli("test", extra_env={"PROXYCTL_TEST_RETRIES": "2"})
        self.assertEqual(rc, 1)
        self.assertIn("[FAIL] 1.", out)
        self.assertIn("共尝试 2 次", out)
        self.assertEqual(self.fake_file("flaky_proxy").strip(), "97")

    def test_selftest_dae_bypassed(self):
        self._libdir()
        self.flag("ip_proxy", "203.0.113.7\n")
        self.flag("ip_proxyctl-probe", "198.51.100.1\n")
        self.flag("ip_proxyctl-direct", "198.51.100.1\n")
        rc, out, err = self.run_cli("test")
        self.assertEqual(rc, 1)
        self.assertIn("[FAIL] 2.", out)
        self.assertIn("故障即放行", out)
        self.assertIn("[PASS] 3.", out)

    def test_selftest_proxy_down_and_missing(self):
        self._libdir()
        self.flag("ip_proxy", "curl: (7) Failed to connect\n")
        self.flag("rc_proxy", "7")
        self.flag("ip_proxyctl-probe", "")
        self.flag("ip_proxyctl-direct", "203.0.113.7\n")
        rc, out, err = self.run_cli("test")
        self.assertEqual(rc, 1)
        self.assertIn("[FAIL] 1.", out)
        self.env["PROXYCTL_LIBDIR"] = os.path.join(self.tmp, "nowhere")
        rc, out, err = self.run_cli("test")
        self.assertIn("install.sh", out)

    def test_run_guard(self):
        self.flag("state", "inactive")
        rc, out, err = self.run_cli("run", "--", "echo", "hello")
        self.assertEqual(rc, 1)
        self.assertIn("已阻止启动 echo", err)
        self.assertIn("已阻止启动", self.fake_file("notify.log"))
        self.flag("state", "active")
        rc, out, err = self.run_cli("run", "--wait", "1", "--", "echo", "hello")
        self.assertEqual(rc, 1)
        self.assertIn("未在监听", err)
        self.listen()
        rc, out, err = self.run_cli("run", "--wait", "3", "--", "echo", "hello", "--wait", "%U")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.strip(), "hello --wait %U")

    def test_backups_empty(self):
        rc, out, err = self.run_cli("backups")
        self.assertEqual(rc, 0)
        self.assertIn("暂无备份", out)


class TestChainProbe(FakeEnv):
    """代理链路探测：dae、端口、xray 都正常，但 xray 连不上节点。"""

    def setUp(self):
        super().setUp()
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(5)
        self.addCleanup(s.close)
        self.env["PROXYCTL_SOCKS"] = "127.0.0.1:%d" % s.getsockname()[1]
        self.add_proc(66511, "xray")
        self.state_file = os.path.join(self.tmp, "proxyctl-chain.json")

    def probes(self):
        log = self.fake_file("curl.log") or ""
        return [l for l in log.splitlines() if "generate_204" in l]

    def timer(self):
        return self.run_cli("check", "--quiet", "--notify", "--basic")

    def test_ok_and_cached(self):
        rc, out, err = self.run_cli("check", "--basic")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("代理链路正常：经 SOCKS5 访问 https://www.gstatic.com/generate_204 成功", out)
        self.assertEqual(len(self.probes()), 1)
        self.assertIn("-x socks5h://%s" % self.env["PROXYCTL_SOCKS"], self.probes()[0])
        self.assertNotIn("--noproxy", self.probes()[0])  # --noproxy 会让 curl 忽略 -x
        # 定时器在间隔内复用结果，不重复发请求
        rc, out, err = self.run_cli("check", "--basic")
        self.assertIn("代理链路正常（", out)
        self.assertEqual(len(self.probes()), 1)
        # 间隔到期后重新探测
        rc, out, err = self.run_cli("check", "--basic", extra_env={"PROXYCTL_CHAIN_INTERVAL": "0"})
        self.assertEqual(len(self.probes()), 2)
        # 完整检查（非 --basic）总是重新探测
        self.run_cli("check")
        self.assertEqual(len(self.probes()), 3)

    def test_two_failures_then_alert_and_recover(self):
        self.timer()  # 先成功一次
        self.flag("rc_proxy", "28")
        # 间隔内复用成功结果，不会发现故障
        rc, out, err = self.timer()
        self.assertEqual(rc, 0)
        # 间隔到期后探测，第 1 次失败：只 WARN，不弹通知
        rc, out, err = self.run_cli("check", "--quiet", "--notify", "--basic",
                                    extra_env={"PROXYCTL_CHAIN_INTERVAL": "0"})
        self.assertEqual(rc, 1, out + err)
        self.assertIn("代理链路探测失败 1 次", out)
        self.assertIsNone(self.fake_file("notify.log"))
        # 第 2 次失败：FAIL + 通知
        rc, out, err = self.timer()
        self.assertEqual(rc, 2, out + err)
        self.assertIn("代理链路不通", out)
        self.assertIn("代理链路不通", self.fake_file("notify.log"))
        # 继续失败：文案固定，通知去重
        os.unlink(os.path.join(self.fake, "notify.log"))
        rc, out, err = self.timer()
        self.assertEqual(rc, 2)
        self.assertIsNone(self.fake_file("notify.log"))
        self.assertEqual(len(self.probes()), 4)
        # 恢复：立即重新探测（不用缓存），发“已恢复”通知
        self.flag("rc_proxy", "0")
        rc, out, err = self.timer()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("代理已恢复正常", self.fake_file("notify.log"))
        with open(self.state_file) as f:
            import json
            self.assertEqual(json.load(f)["fails"], 0)

    def test_slow_chain_warns_after_streak(self):
        env = {"PROXYCTL_SLOW_SECONDS": "0.2"}
        self.flag("sleep_proxy", "0.4")
        # 第 1、2 次偏慢：OK（注明偏慢），不警告、不通知；偏慢时不使用缓存
        for n in (1, 2):
            rc, out, err = self.run_cli("check", "--notify", "--basic", extra_env=env)
            self.assertEqual(rc, 0, out + err)
            self.assertIn("偏慢", out)
            self.assertIn("连续第 %d 次" % n, out)
        self.assertEqual(len(self.probes()), 2)
        self.assertIsNone(self.fake_file("notify.log"))
        # 第 3 次：WARN + 普通级别通知
        rc, out, err = self.run_cli("check", "--quiet", "--notify", "--basic", extra_env=env)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("代理链路很慢", out)
        log = self.fake_file("notify.log")
        self.assertIn("proxyctl：代理很慢", log)
        self.assertIn("<byte 1>", log)
        # 继续很慢：30 分钟内不重复通知
        os.unlink(os.path.join(self.fake, "notify.log"))
        rc, out, err = self.run_cli("check", "--quiet", "--notify", "--basic", extra_env=env)
        self.assertIn("连续 4 次", out)
        self.assertIsNone(self.fake_file("notify.log"))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "proxyctl-slow.json")))
        # 速度恢复：不再警告，清除状态，下一次检查又可以使用缓存
        os.unlink(os.path.join(self.fake, "sleep_proxy"))
        rc, out, err = self.run_cli("check", "--quiet", "--notify", "--basic", extra_env=env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(out.strip(), "")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "proxyctl-slow.json")))
        n = len(self.probes())
        rc, out, err = self.run_cli("check", "--basic", extra_env=env)
        self.assertIn("秒前探测", out)
        self.assertEqual(len(self.probes()), n)
        with open(self.state_file) as f:
            import json
            st = json.load(f)
        self.assertEqual(st["slow"], 0)
        self.assertIn("elapsed", st)

    def test_skipped_when_dae_down(self):
        self.timer()
        self.assertTrue(os.path.exists(self.state_file))
        self.flag("state", "inactive")
        rc, out, err = self.timer()
        self.assertIn("dae 服务未运行", out)
        self.assertNotIn("链路", out)
        self.assertEqual(len(self.probes()), 1)
        self.assertFalse(os.path.exists(self.state_file))  # 恢复后重新计数


class TestNotify(FakeEnv):
    def run_notify(self, title, body, urgency="critical"):
        code = ("import sys; sys.path.insert(0, %r); from tests.helpers import P; P.notify(%r, %r, %r)"
                % (os.path.dirname(os.path.dirname(os.path.abspath(__file__))), title, body, urgency))
        subprocess.run([sys.executable, "-c", code], env=self.env, check=True, timeout=30)

    def test_gnome_shell_direct(self):
        self.run_notify("proxyctl：代理故障", "第一行\n含'引号'和\\反斜杠 [FAIL] 123", "critical")
        log = self.fake_file("gdbus.log")
        self.assertIn("--dest org.gnome.Shell", log)
        self.assertIn("--object-path /org/freedesktop/Notifications", log)
        self.assertIn("'proxyctl：代理故障'", log)
        self.assertIn("'第一行\\n含\\'引号\\'和\\\\反斜杠 [FAIL] 123'", log)
        self.assertIn("{'urgency': <byte 2>}", log)
        self.assertNotIn("notify-send", self.fake_file("notify.log"))

    def test_fallback_to_notify_send(self):
        self.flag("gdbus_fail")
        self.run_notify("标题", "正文", "normal")
        self.assertIn("gdbus", self.fake_file("gdbus.log"))
        self.assertIn("notify-send -u normal -a proxyctl 标题 正文", self.fake_file("notify.log"))

    def test_gvariant_str(self):
        self.assertEqual(P.gvariant_str("a'b\\c\nd"), "'a\\'b\\\\c\\nd'")

    def test_arguments_parse_as_notify_signature(self):
        """用 GLib 自己的 GVariant 解析器校验 gdbus 参数，类型必须与 Notify(susssasa{sv}i) 一致。"""
        try:
            from gi.repository import GLib
        except ImportError:
            self.skipTest("没有 PyGObject")
        self.run_notify("标题'x", "正文\n[FAIL] 123 \\ end", "normal")
        log = self.fake_file("gdbus.log").strip()
        # 用真实的参数列表（而不是日志中空格拼接的文本）重新构造
        args = [P.gvariant_str("proxyctl"), "uint32 0", P.gvariant_str(""), P.gvariant_str("标题'x"),
                P.gvariant_str("正文\n[FAIL] 123 \\ end"), "@as []", "{'urgency': <byte 1>}", "int32 -1"]
        for a in args:
            self.assertIn(a, log)
        types = [GLib.Variant.parse(None, a, None, None).get_type_string() for a in args]
        self.assertEqual("".join(types), "susssasa{sv}i")
        self.assertEqual(GLib.Variant.parse(None, args[3], None, None).get_string(), "标题'x")
        self.assertEqual(GLib.Variant.parse(None, args[4], None, None).get_string(), "正文\n[FAIL] 123 \\ end")


class TestGuardDesktop(FakeEnv):
    def setUp(self):
        super().setUp()
        self.sys_apps = os.path.join(self.tmp, "share", "applications")
        self.user_apps = os.path.join(self.tmp, "home-apps")
        os.makedirs(self.sys_apps)
        self.env["PROXYCTL_DESKTOP_DIRS"] = self.sys_apps
        self.env["PROXYCTL_APPS_DIR"] = self.user_apps
        self.env["PROXYCTL_SELF"] = "/usr/local/bin/proxyctl"
        self.src = ("[Desktop Entry]\nName=Signal\nExec=/opt/Signal/signal-desktop --no-sandbox %U\n"
                    "Type=Application\n\n[Desktop Action new]\nName=New\nExec=/opt/Signal/signal-desktop --new\n")
        with open(os.path.join(self.sys_apps, "signal-desktop.desktop"), "w") as f:
            f.write(self.src)

    def target(self):
        with open(os.path.join(self.user_apps, "signal-desktop.desktop")) as f:
            return f.read()

    def test_guard_and_undo(self):
        rc, out, err = self.run_cli("guard-desktop", "signal-desktop.desktop")
        self.assertEqual(rc, 0, err)
        t = self.target()
        self.assertIn("Exec=/usr/local/bin/proxyctl run --wait 30 -- /opt/Signal/signal-desktop --no-sandbox %U", t)
        self.assertIn("Exec=/usr/local/bin/proxyctl run --wait 30 -- /opt/Signal/signal-desktop --new", t)
        self.assertEqual(t.count("X-Proxyctl-Guarded=true"), 1)
        # 幂等
        rc, out, err = self.run_cli("guard-desktop", "signal-desktop")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.target(), t)
        rc, out, err = self.run_cli("guard-desktop", "signal-desktop.desktop", "--undo")
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.exists(os.path.join(self.user_apps, "signal-desktop.desktop")))

    def test_user_copy_preserved(self):
        os.makedirs(self.user_apps)
        custom = self.src.replace("Name=Signal", "Name=My Signal")
        with open(os.path.join(self.user_apps, "signal-desktop.desktop"), "w") as f:
            f.write(custom)
        rc, out, err = self.run_cli("guard-desktop", "signal-desktop.desktop")
        self.assertEqual(rc, 0, err)
        self.assertIn("Name=My Signal", self.target())
        self.assertIn("proxyctl run --wait 30 --", self.target())
        rc, out, err = self.run_cli("guard-desktop", "signal-desktop.desktop", "--undo")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.target(), custom)

    def test_missing_and_bad_name(self):
        rc, out, err = self.run_cli("guard-desktop", "nope.desktop")
        self.assertEqual(rc, 1)
        rc, out, err = self.run_cli("guard-desktop", "a b;rm.desktop")
        self.assertEqual(rc, 1)


DAE = shutil.which("dae") or "/usr/bin/dae"


@unittest.skipUnless(os.access(DAE, os.X_OK), "系统中没有 dae")
class TestRealDaeValidate(unittest.TestCase):
    """把生成的配置写成当前用户所有的 0600 文件，用真实的 dae validate 校验。"""

    def validate(self, text):
        d = tempfile.mkdtemp(prefix="proxyctl-realdae-")
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "config.dae")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(path, 0o600)
        p = subprocess.run([DAE, "validate", "-c", path], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, timeout=60)
        return p.returncode, p.stdout

    def setUp(self):
        rc, out = self.validate(BASIC)
        if rc != 0:
            self.skipTest("非 root 下无法执行真实 dae validate：%s" % out)

    def test_generated_configs_validate(self):
        from tests.test_config import VARIANT_COMMENTS, VARIANT_ORDER
        t, _ = P.op_init(BASIC)
        cases = [t]
        for op, n in ((P.op_add, "firefox"), (P.op_add, "A.b_c+d-e"), (P.op_protect, "python3"),
                      (P.op_remove, "curl"), (P.op_unprotect, "NetworkManager")):
            t, _ = op(t, n)
            cases.append(t)
        t, _ = P.op_remove(t, "signal-desktop")
        t, _ = P.op_remove(t, "firefox")
        t, _ = P.op_remove(t, "A.b_c+d-e")
        t, _ = P.op_unprotect(t, "python3")
        cases.append(t)  # 空的 direct / proxy 区块
        cases.append(P.op_init(VARIANT_ORDER)[0])
        cases.append(P.op_init(VARIANT_COMMENTS)[0])
        cases.append(P.op_init(BASIC.replace("\n", "\r\n"))[0])
        for i, c in enumerate(cases):
            rc, out = self.validate(c)
            self.assertEqual(rc, 0, "case %d: %s\n%s" % (i, out, c))

    def test_real_dae_rejects_garbage(self):
        rc, out = self.validate(P.op_init(BASIC)[0].replace("fallback: direct", "fallback: direct\n    bogus"))
        self.assertNotEqual(rc, 0)


class TestScripts(unittest.TestCase):
    def test_bash_syntax(self):
        for s in ("install.sh", "uninstall.sh"):
            p = subprocess.run(["bash", "-n", os.path.join(ROOT, s)], stderr=subprocess.PIPE, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)

    def test_scripts_require_root(self):
        if os.geteuid() == 0:
            self.skipTest("以 root 运行")
        for s in ("install.sh", "uninstall.sh"):
            p = subprocess.run(["bash", os.path.join(ROOT, s)], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, timeout=10)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn("root", p.stdout)

    def test_py_compile(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        py_compile.compile(PROXYCTL, cfile=os.path.join(d, "p.pyc"), doraise=True)


def _has_gui():
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    p = subprocess.run([sys.executable, "-c", "import gi"], stderr=subprocess.DEVNULL)
    return p.returncode == 0


class TestGuiRowActions(unittest.TestCase):
    VIEW = {"safety": [list(e) for e in P.SAFETY_RULES], "direct": ["NetworkManager"],
            "proxy": ["signal-desktop"], "unmanaged_pname": []}

    def acts(self, name, status):
        return [(t, sub, ok) for t, sub, ok, _ in P.gui_row_actions(name, status, self.VIEW)]

    def test_safety_rows_have_no_buttons(self):
        # 截图中的问题：xray（直连保护）一行的“加入代理”可以点，点了会被拒绝
        for n in ("xray", "sing-box", "mihomo"):
            self.assertEqual(self.acts(n, P.STATUS_DIRECT), [])
        self.assertEqual(self.acts("proxyctl-probe", P.STATUS_PROXY), [])

    def test_only_valid_actions(self):
        self.assertEqual(self.acts("signal-desktop", P.STATUS_PROXY),
                         [("移除代理", ["remove", "signal-desktop"], True)])
        self.assertEqual(self.acts("NetworkManager", P.STATUS_DIRECT),
                         [("取消保护", ["unprotect", "NetworkManager"], True)])
        self.assertEqual(self.acts("chrome", P.STATUS_DEFAULT),
                         [("加入代理", ["add", "chrome"], True), ("直连保护", ["protect", "chrome"], True)])
        self.assertEqual(self.acts("codex", P.STATUS_CHILD)[0], ("加入代理", ["add", "codex"], True))

    def test_blacklisted_and_invalid(self):
        self.assertEqual(self.acts("java", P.STATUS_DEFAULT),
                         [("加入代理", ["add", "java"], False), ("直连保护", ["protect", "java"], True)])
        self.assertEqual(self.acts("(未知进程)", P.STATUS_DEFAULT), [])
        self.assertEqual(self.acts("Web Content", P.STATUS_DEFAULT), [])

    def test_tooltips_mention_reload(self):
        for _, _, ok, tip in P.gui_row_actions("chrome", P.STATUS_DEFAULT, self.VIEW):
            self.assertIn("重新加载", tip)


class TestGui(FakeEnv):
    @unittest.skipUnless(_has_gui(), "没有图形环境或 PyGObject")
    def test_gui_starts_and_quits(self):
        self.write_config(INIT)
        # 图形会话的 Wayland 套接字位于真实的 XDG_RUNTIME_DIR 中
        env = {"PROXYCTL_GUI_AUTOQUIT": "3"}
        if os.environ.get("XDG_RUNTIME_DIR"):
            env["XDG_RUNTIME_DIR"] = os.environ["XDG_RUNTIME_DIR"]
        env["PROXYCTL_GUI_DUMP"] = "1"
        self.flag("state", "inactive")
        rc, out, err = self.run_cli("gui", extra_env=env, timeout=60)
        if "无法连接图形显示" in err:
            self.skipTest("图形显示不可用")
        import json
        dump = json.loads(out.strip().splitlines()[-1])
        rows = {r["name"]: r for r in dump["rows"]}
        self.assertEqual(rows["xray"]["buttons"], [])                    # 安全区块：没有按钮
        self.assertEqual(rows["xray"]["status"], "固定规则（直连）")
        self.assertEqual(rows["signal-desktop"]["buttons"], [["移除代理", True]])
        self.assertEqual(rows["chrome"]["buttons"], [["加入代理", True], ["直连保护", True]])
        self.assertEqual(rows["java"]["buttons"], [["加入代理", False], ["直连保护", True]])
        self.assertTrue(dump["banner"])                                   # dae 未运行：显示警告横幅
        self.assertEqual(rc, 0, err)
        self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()


class TestChildren(unittest.TestCase):
    """代理名单程序中未代理的子进程（例如 ChatGPT 的 codex）。"""

    VIEW = {"safety": [list(e) for e in P.SAFETY_RULES], "direct": ["NetworkManager"],
            "proxy": ["ChatGPT", "a-very-long-process-name"], "unmanaged_pname": [["zoom", "direct"]]}

    TABLE = {
        1: ("systemd", 0),
        100: ("ChatGPT", 1),
        101: ("codex", 100),          # 应被找出
        102: ("bash", 101),           # 通用运行时：不列出
        103: ("curl", 102),           # 在 bash 之下：不再向下查找
        104: ("ChatGPT", 100),        # 同名子进程：本身就在名单中
        105: ("ChatGPT-helper", 104), # 孙进程：应被找出
        106: ("NetworkManager", 100), # 已在直连保护：不列出
        107: ("zoom", 100),           # 已有不受管理的规则：不列出
        108: ("inner", 106),          # 在名单内进程之下：不再向下查找
        109: ("codex", 104),          # 同名的另一个 codex：合并
        200: ("a-very-long-pro", 1),  # 内核截断后的名字也算在名单中
        201: ("worker", 200),
        300: ("unrelated", 1),
        301: ("child", 300),          # 父进程不在名单中：不列出
        400: ("proxyctl-probe", 1),
        401: ("helper", 400),         # 安全区块中的 proxy 规则也算
        500: ("xray", 1),
        501: ("xray-child", 500),     # 父进程是直连的：不列出
    }

    def test_found(self):
        found = P.unproxied_children(self.VIEW, self.TABLE)
        self.assertEqual(sorted(found), ["ChatGPT-helper", "codex", "helper", "worker"])
        self.assertEqual(found["codex"]["pids"], {101, 109})
        self.assertEqual(found["codex"]["parents"], {"ChatGPT"})
        self.assertEqual(found["worker"]["parents"], {"a-very-long-pro"})

    def test_proxied_child_not_listed(self):
        view = dict(self.VIEW, proxy=self.VIEW["proxy"] + ["codex", "ChatGPT-helper"])
        self.assertEqual(sorted(P.unproxied_children(view, self.TABLE)), ["helper", "worker"])

    def test_no_view(self):
        self.assertEqual(P.unproxied_children(None, self.TABLE), {})


class TestProcessTable(FakeEnv):
    def test_stat_parsing(self):
        self.add_proc(10, "Web (Content) x", ppid=7)
        self.add_proc(11, "plain")
        d = os.path.join(self.proc, "12")
        os.makedirs(d)
        with open(os.path.join(d, "comm"), "w") as f:
            f.write("nostat\n")  # 没有 stat 时退回读 comm
        os.makedirs(os.path.join(self.proc, "self"))
        os.environ["PROXYCTL_PROC"] = self.proc
        try:
            t = P.process_table()
        finally:
            del os.environ["PROXYCTL_PROC"]
        self.assertEqual(t[10], ("Web (Content) x", 7))
        self.assertEqual(t[11], ("plain", 1))
        self.assertEqual(t[12], ("nostat", 0))

    def test_list_and_pick_show_children(self):
        self.write_config(INIT)  # 代理名单：curl、signal-desktop
        self.add_proc(200, "signal-desktop")
        self.add_proc(201, "signal-helper", ppid=200)  # 没有网络连接的子进程
        self.add_proc(5725, "chrome", ppid=200)        # ss 样本中有连接的子进程
        rc, out, err = self.run_cli("list")
        self.assertEqual(rc, 0, err)
        helper = [l for l in out.splitlines() if l.startswith("signal-helper")][0]
        self.assertIn("未代理子进程", helper)
        self.assertIn("父进程：signal-desktop", helper)
        chrome = [l for l in out.splitlines() if l.startswith("chrome")][0]
        self.assertIn("未代理子进程", chrome)
        self.assertIn("父进程：signal-desktop, 172.217.114.4:443", chrome)
        self.assertIn("proxyctl add <进程名>", out)
        # pick 可以直接选中没有连接的子进程
        rc, out, err = self.run_cli("pick", input="\n")
        idx = [l.split()[1] for l in out.splitlines() if l[:1].isdigit()].index("signal-helper") + 1
        rc, out, err = self.run_cli("pick", input="%d\ny\n" % idx)
        self.assertEqual(rc, 0, err)
        self.assertIn("pname(signal-helper) -> proxy", self.read_config())
        rc, out, err = self.run_cli("list")
        helper = [l for l in out.splitlines() if l.startswith("signal-helper")]
        self.assertEqual(helper, [])  # 已加入代理且没有连接：不再列出


class TestMergeRecent(unittest.TestCase):
    def row(self, name, conns=1):
        return {"name": name, "pids": 1, "conns": conns, "samples": ["1.2.3.4:443"], "status": P.STATUS_DEFAULT}

    def test_keeps_recently_gone(self):
        h = {}
        out = P.merge_recent(h, [self.row("codex"), self.row("chrome")], 100.0, 300)
        self.assertEqual([r["name"] for r in out], ["codex", "chrome"])
        out = P.merge_recent(h, [self.row("chrome")], 110.0, 300)
        self.assertEqual([r["name"] for r in out], ["chrome", "codex"])
        gone = out[1]
        self.assertEqual(gone["conns"], 0)
        self.assertEqual(gone["gone"], 10.0)
        self.assertEqual(gone["samples"], ["1.2.3.4:443"])
        out = P.merge_recent(h, [self.row("chrome")], 401.0, 300)
        self.assertEqual([r["name"] for r in out], ["chrome"])
        self.assertNotIn("codex", h)

    def test_reappears_and_zero_conn_rows(self):
        h = {}
        P.merge_recent(h, [self.row("codex")], 0.0, 300)
        out = P.merge_recent(h, [self.row("codex")], 50.0, 300)
        self.assertEqual(len(out), 1)
        self.assertNotIn("gone", out[0])
        # 没有连接的行（未代理子进程）不进入历史
        out = P.merge_recent(h, [self.row("kid", conns=0)], 60.0, 300)
        self.assertNotIn("kid", h)
        self.assertEqual(P.gone_text(5), "刚刚断开")
        self.assertEqual(P.gone_text(150), "2 分钟前断开")
