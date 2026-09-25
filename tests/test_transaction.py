"""写配置事务：备份、validate、原子替换、reload、回滚、文件锁（使用假的 dae / systemctl）。"""

import fcntl
import os
import subprocess
import sys
import time

from tests.helpers import BASIC, PROXYCTL, FakeEnv, P, fixture

INIT = fixture("config_basic_init.dae")


class TestTransaction(FakeEnv):
    def test_init_success_and_idempotent(self):
        self.write_config(BASIC)
        rc, out, err = self.run_cli("init")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.read_config(), INIT)
        self.assertEqual(oct(os.stat(self.config).st_mode & 0o777), "0o600")
        self.assertEqual(self.reload_count(), 1)
        self.assertEqual(len(self.backups()), 1)
        with open(os.path.join(self.state, "backups", self.backups()[0]), encoding="utf-8") as f:
            self.assertEqual(f.read(), BASIC)
        self.assertIn("curl", out)
        # 第二次：无变化、不备份、不 reload
        rc, out, err = self.run_cli("init")
        self.assertEqual(rc, 0, err)
        self.assertIn("无变化", out)
        self.assertEqual(self.read_config(), INIT)
        self.assertEqual(self.reload_count(), 1)
        self.assertEqual(len(self.backups()), 1)

    def test_validate_called_on_temp_in_same_dir(self):
        self.write_config(INIT)
        rc, out, err = self.run_cli("add", "firefox")
        self.assertEqual(rc, 0, err)
        calls = self.fake_file("calls.log")
        line = [l for l in calls.splitlines() if l.startswith("dae validate")][0]
        tmp = line.split()[-1]
        self.assertEqual(os.path.dirname(tmp), self.etc)
        self.assertTrue(tmp.endswith(".dae"))
        self.assertNotEqual(tmp, self.config)
        self.assertEqual(sorted(os.listdir(self.etc)), ["config.dae"])  # 临时文件已清理
        # 顺序：validate 在 reload 之前
        self.assertLess(calls.index("dae validate"), calls.index("systemctl reload"))
        self.assertIn("pname(firefox) -> proxy", self.fake_file("reloaded.1"))

    def test_validate_failure_rolls_back(self):
        self.write_config(INIT)
        self.flag("validate_fail")
        rc, out, err = self.run_cli("add", "firefox")
        self.assertEqual(rc, 1)
        self.assertIn("未通过 dae validate", err)
        self.assertIn("fake dae: validate failed", err)
        self.assertEqual(self.read_config(), INIT)
        self.assertEqual(self.reload_count(), 0)
        self.assertEqual(sorted(os.listdir(self.etc)), ["config.dae"])

    def test_reload_failure_rolls_back(self):
        self.write_config(INIT)
        self.flag("reload_fail")
        rc, out, err = self.run_cli("add", "firefox")
        self.assertEqual(rc, 1)
        self.assertIn("reload", err)
        self.assertIn("回滚", err)
        self.assertEqual(self.read_config(), INIT)
        self.assertEqual(self.reload_count(), 2)  # 失败后再次 reload

    def test_dae_dies_after_reload_rolls_back(self):
        self.write_config(INIT)
        self.flag("die_on_first_reload")
        self.flag("revive_on_reload")
        rc, out, err = self.run_cli("add", "firefox")
        self.assertEqual(rc, 1)
        self.assertIn("不是 active", err)
        self.assertIn("已恢复为原配置", err)
        self.assertEqual(self.read_config(), INIT)
        self.assertEqual(self.reload_count(), 2)
        self.assertIn("pname(firefox)", self.fake_file("reloaded.1"))
        self.assertNotIn("pname(firefox)", self.fake_file("reloaded.2"))

    def test_dae_stays_dead_reports(self):
        self.write_config(INIT)
        self.flag("die_on_first_reload")
        rc, out, err = self.run_cli("add", "firefox")
        self.assertEqual(rc, 1)
        self.assertIn("故障即放行", err)
        self.assertEqual(self.read_config(), INIT)

    def test_reload_disconnect_note(self):
        self.write_config(INIT)
        rc, out, err = self.run_cli("add", "firefox")
        self.assertEqual(rc, 0, err)
        self.assertIn("dae 重新加载会断开所有正在走代理的连接", out)
        self.assertIn("Signal", out)
        # 没有变化时不 reload，也不提示
        rc, out, err = self.run_cli("add", "firefox")
        self.assertNotIn("重新加载会断开", out)

    def test_dae_not_running_no_reload(self):
        self.write_config(INIT)
        self.flag("state", "inactive")
        rc, out, err = self.run_cli("add", "firefox")
        self.assertEqual(rc, 0, err)
        self.assertIn("dae 当前未运行", out)
        self.assertNotIn("重新加载会断开", out)
        self.assertEqual(self.reload_count(), 0)
        self.assertIn("pname(firefox) -> proxy", self.read_config())

    def test_errors_leave_file_untouched(self):
        self.write_config(INIT)
        for args in (("add", "curl"), ("add", "NetworkManager"), ("remove", "zoom"), ("unprotect", "xray"),
                     ("remove", "proxyctl-probe"), ("add", "a) -> direct\n"), ("protect", "signal-desktop")):
            rc, out, err = self.run_cli(*args)
            self.assertEqual(rc, 1, args)
            self.assertIn("错误", err)
        self.assertEqual(self.read_config(), INIT)
        self.assertEqual(self.backups(), [])
        self.assertEqual(self.reload_count(), 0)

    def test_unparseable_config_untouched(self):
        broken = BASIC.replace("routing {", "routing\n{")
        self.write_config(broken)
        rc, out, err = self.run_cli("init")
        self.assertEqual(rc, 1)
        self.assertEqual(self.read_config(), broken)
        no_routing = BASIC[:BASIC.index("routing {")]
        self.write_config(no_routing)
        rc, out, err = self.run_cli("init")
        self.assertEqual(rc, 1)
        self.assertIn("找不到 routing", err)
        self.assertEqual(self.read_config(), no_routing)

    def test_add_remove_protect_cycle(self):
        self.write_config(INIT)
        self.add_proc(100, "firefox")
        rc, out, err = self.run_cli("add", "firefox")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("当前没有名为", out)
        self.assertIn("以 dae 日志中实际显示的进程名为准", out)
        rc, out, err = self.run_cli("add", "zoom")
        self.assertIn("当前没有名为“zoom”的进程", out)
        rc, out, err = self.run_cli("protect", "python3")
        self.assertEqual(rc, 0, err)
        rc, out, err = self.run_cli("remove", "zoom")
        self.assertEqual(rc, 0, err)
        rc, out, err = self.run_cli("unprotect", "python3")
        self.assertEqual(rc, 0, err)
        p = P.Parsed(self.read_config())
        self.assertEqual(p.names("proxy"), ["curl", "signal-desktop", "firefox"])
        self.assertEqual(p.names("direct"), ["NetworkManager"])
        self.assertEqual(len(self.backups()), 5)
        rc, out, err = self.run_cli("rules")
        self.assertEqual(rc, 0, err)
        self.assertIn("firefox", out)
        self.assertIn("fallback: direct", out)
        self.assertIn("建议 remove", out)  # curl

    def test_backup_rotation(self):
        self.write_config(INIT)
        os.makedirs(os.path.join(self.state, "backups"))
        for i in range(35):
            open(os.path.join(self.state, "backups", "config-20200101-000000-%06d-add.dae" % i), "w").close()
        rc, out, err = self.run_cli("add", "firefox")
        self.assertEqual(rc, 0, err)
        names = self.backups()
        self.assertEqual(len(names), 30)
        self.assertTrue(names[-1].startswith("config-20"))
        self.assertTrue(names[-1].endswith("-add.dae"))
        self.assertNotIn("config-20200101-000000-000000-add.dae", names)

    def test_restore(self):
        self.write_config(BASIC)
        self.assertEqual(self.run_cli("init")[0], 0)
        self.assertEqual(self.run_cli("add", "firefox")[0], 0)
        first = self.backups()[0]
        rc, out, err = self.run_cli("backups")
        self.assertEqual(rc, 0, err)
        self.assertIn(first, out)
        rc, out, err = self.run_cli("restore", first)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.read_config(), BASIC)
        self.assertEqual(len(self.backups()), 3)
        for bad in ("../../etc/passwd", "nope.dae", "config-x.dae"):
            rc, out, err = self.run_cli("restore", bad)
            self.assertEqual(rc, 1)
        self.assertEqual(self.read_config(), BASIC)

    def test_restore_invalid_backup_rejected(self):
        self.write_config(INIT)
        self.assertEqual(self.run_cli("add", "firefox")[0], 0)
        name = self.backups()[0]
        with open(os.path.join(self.state, "backups", name), "a") as f:
            f.write("FAILME\n")
        before = self.read_config()
        rc, out, err = self.run_cli("restore", name)
        self.assertEqual(rc, 1)
        self.assertEqual(self.read_config(), before)

    def test_rules_cache_written(self):
        self.write_config(BASIC)
        self.assertEqual(self.run_cli("init")[0], 0)
        import json
        with open(os.path.join(self.state, "rules.json"), encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["proxy"], ["curl", "signal-desktop"])
        self.assertEqual(oct(os.stat(os.path.join(self.state, "rules.json")).st_mode & 0o777), "0o644")
        self.assertNotIn("unmanaged", data)

    def test_needs_root_without_escalation(self):
        self.write_config(INIT)
        os.chmod(self.etc, 0o500)
        try:
            rc, out, err = self.run_cli("add", "firefox")
        finally:
            os.chmod(self.etc, 0o700)
        self.assertEqual(rc, 1)
        self.assertIn("sudo proxyctl add firefox", err)
        self.assertEqual(self.read_config(), INIT)


class TestLock(FakeEnv):
    def test_concurrent_adds_serialized(self):
        self.write_config(INIT)
        self.flag("validate_sleep", "0.3")
        names = ["app%d" % i for i in range(6)]
        env = dict(self.env)
        procs = [subprocess.Popen([sys.executable, PROXYCTL, "add", n], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for n in names]
        for p in procs:
            out, err = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0, err)
        final = P.Parsed(self.read_config()).names("proxy")
        for n in names:
            self.assertIn(n, final)
        self.assertEqual(len(self.backups()), 6)

    def test_lock_timeout(self):
        self.write_config(INIT)
        os.makedirs(self.state, exist_ok=True)
        fd = os.open(os.path.join(self.state, "lock"), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            t0 = time.monotonic()
            rc, out, err = self.run_cli("add", "firefox", extra_env={"PROXYCTL_LOCK_TIMEOUT": "0.5"})
            self.assertLess(time.monotonic() - t0, 10)
        finally:
            os.close(fd)
        self.assertEqual(rc, 1)
        self.assertIn("另一个 proxyctl 正在修改配置", err)
        self.assertEqual(self.read_config(), INIT)


if __name__ == "__main__":
    import unittest
    unittest.main()
