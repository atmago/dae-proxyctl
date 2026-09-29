"""备用方案（fallback）：启动器生成与撤销、Claude Code 设置、run --fallback、状态判断。"""

import json
import os
import socket
import stat

from tests.helpers import BASIC, P, FakeEnv


def write(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, mode)


class TestFallbackPure(FakeEnv):
    def test_exec_argv(self):
        self.assertEqual(P.exec_argv("claude-desktop %U"), ["claude-desktop", "%U"])
        self.assertEqual(P.exec_argv("/usr/local/bin/proxyctl run --wait 30 -- /opt/S/signal %U"),
                         ["/opt/S/signal", "%U"])
        self.assertEqual(P.exec_argv("env FOO=1 BAR=2 app --x"), ["app", "--x"])
        self.assertEqual(P.exec_argv('"unterminated'), [])

    def test_exe_matches_script(self):
        d = os.path.join(self.tmp, "app")
        write(os.path.join(d, "ChatGPT"), "bin", 0o755)
        write(os.path.join(d, "launcher"), '#!/bin/sh\nexec "$(dirname "$0")/ChatGPT" "$@"\n', 0o755)
        write(os.path.join(d, "other"), "#!/bin/sh\necho hi\n", 0o755)
        self.assertTrue(P.exe_matches(os.path.join(d, "ChatGPT"), "ChatGPT"))
        self.assertTrue(P.exe_matches(os.path.join(d, "launcher"), "ChatGPT"))
        # 同目录有 ChatGPT，但脚本没有调用它
        self.assertFalse(P.exe_matches(os.path.join(d, "other"), "ChatGPT"))

    def test_fallback_text(self):
        src = ("[Desktop Entry]\nX-Proxyctl-Guarded=true\nName=C\n"
               "Exec=/usr/local/bin/proxyctl run --wait 30 -- claude-desktop %U\nX-Claude-Generated=true\n\n"
               "[Desktop Action New]\nExec=claude-desktop \"claude://new\"\n")
        out = P.fallback_text(src, "claude-desktop")
        self.assertNotIn("Guarded", out)
        self.assertNotIn("X-Claude-Generated", out)
        self.assertIn("X-Proxyctl-Fallback=claude-desktop\n", out)
        self.assertIn("Exec=/usr/local/bin/proxyctl run --fallback --wait 30 -- claude-desktop %U\n", out)
        self.assertIn('Exec=/usr/local/bin/proxyctl run --fallback --wait 30 -- claude-desktop "claude://new"', out)
        self.assertEqual(P.fallback_text(out, "claude-desktop"), out)  # 幂等

    def test_socks_conns(self):
        conns = P.parse_ss(
            'tcp ESTAB 0 0 127.0.0.1:5000 127.0.0.1:10808 users:(("claude",pid=9,fd=1))\n'
            'tcp ESTAB 0 0 192.168.1.2:5001 160.79.104.10:443 users:(("claude",pid=9,fd=2))\n'
            'tcp ESTAB 0 0 192.168.1.2:5002 192.168.1.1:53 users:(("claude",pid=9,fd=3))\n'
            'tcp ESTAB 0 0 127.0.0.1:5003 127.0.0.1:10808 users:(("other",pid=8,fd=1))\n')
        os.environ["PROXYCTL_SOCKS"] = "127.0.0.1:10808"
        try:
            self.assertEqual(P.socks_conns("claude", conns), (1, 1))
        finally:
            del os.environ["PROXYCTL_SOCKS"]


class TestFallbackCli(FakeEnv):
    def setUp(self):
        super().setUp()
        self.sys_apps = os.path.join(self.tmp, "share", "applications")
        self.user_apps = os.path.join(self.tmp, "home-apps")
        self.app_dir = os.path.join(self.tmp, "opt", "Signal")
        self.settings = os.path.join(self.tmp, "claude", "settings.json")
        write(os.path.join(self.app_dir, "signal-desktop"), "#!/bin/sh\n", 0o755)
        self.src = ("[Desktop Entry]\nName=Signal\nExec=%s/signal-desktop %%U\nType=Application\n"
                    % self.app_dir)
        write(os.path.join(self.sys_apps, "signal-desktop.desktop"), self.src)
        write(os.path.join(self.sys_apps, "unrelated.desktop"),
              "[Desktop Entry]\nName=X\nExec=/nonexistent/x\n")
        self.env.update({
            "PROXYCTL_DESKTOP_DIRS": self.sys_apps,
            "PROXYCTL_APPS_DIR": self.user_apps,
            "PROXYCTL_SELF": "/usr/local/bin/proxyctl",
            "PROXYCTL_CLAUDE_SETTINGS": self.settings,
        })
        self.write_config(BASIC)
        for args in (("init",), ("add", "signal-desktop"), ("add", "codex")):
            rc, out, err = self.run_cli(*args)
            self.assertEqual(rc, 0, err)

    def target(self):
        with open(os.path.join(self.user_apps, "signal-desktop.desktop")) as f:
            return f.read()

    def test_launcher_on_off(self):
        rc, out, err = self.run_cli("fallback", "on", "signal-desktop")
        self.assertEqual(rc, 0, err)
        t = self.target()
        self.assertIn("X-Proxyctl-Fallback=signal-desktop", t)
        self.assertIn("Exec=/usr/local/bin/proxyctl run --fallback --wait 30 -- %s/signal-desktop %%U"
                      % self.app_dir, t)
        self.assertFalse(os.path.exists(os.path.join(self.user_apps, "unrelated.desktop")))
        rc, out, err = self.run_cli("fallback", "on", "signal-desktop")
        self.assertEqual(rc, 0, err)
        self.assertIn("已经是备用启动器", out)
        self.assertEqual(self.target(), t)
        rc, out, err = self.run_cli("fallback", "status")
        self.assertEqual(rc, 0, err)
        self.assertIn("备用方案：直连", out)
        rc, out, err = self.run_cli("fallback", "off", "signal-desktop")
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.exists(os.path.join(self.user_apps, "signal-desktop.desktop")))
        rc, out, err = self.run_cli("fallback", "off", "signal-desktop")
        self.assertIn("无需改回", out)

    def test_user_copy_restored(self):
        # 程序自己生成的用户启动器（Claude Desktop 带 X-Claude-Generated）：撤销后原样恢复
        custom = self.src.replace("Type=Application", "Type=Application\nX-Claude-Generated=true")
        write(os.path.join(self.user_apps, "signal-desktop.desktop"), custom)
        rc, out, err = self.run_cli("fallback", "on", "signal-desktop")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("X-Claude-Generated", self.target())
        rc, out, err = self.run_cli("fallback", "off", "signal-desktop")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.target(), custom)
        self.assertFalse(os.path.exists(os.path.join(self.user_apps, "signal-desktop.desktop" + P.FALLBACK_ORIG)))

    def test_claude_code_settings(self):
        write(self.settings, json.dumps({"hooks": {"x": 1}, "env": {"FOO": "bar"}}))
        os.chmod(self.settings, 0o600)
        rc, out, err = self.run_cli("fallback", "on", "claude", extra_env={"PROXYCTL_SOCKS": "127.0.0.1:10808"})
        self.assertEqual(rc, 0, err)
        with open(self.settings) as f:
            data = json.load(f)
        self.assertEqual(data["hooks"], {"x": 1})
        self.assertEqual(data["env"]["HTTPS_PROXY"], "http://127.0.0.1:10808")
        self.assertEqual(data["env"]["NO_PROXY"], P.FALLBACK_NO_PROXY)
        self.assertEqual(data["env"]["FOO"], "bar")
        self.assertEqual(stat.S_IMODE(os.stat(self.settings).st_mode), 0o600)
        rc, out, err = self.run_cli("fallback", "off", "claude")
        self.assertEqual(rc, 0, err)
        with open(self.settings) as f:
            self.assertEqual(json.load(f), {"hooks": {"x": 1}, "env": {"FOO": "bar"}})

    def test_claude_code_bad_json_untouched(self):
        write(self.settings, "{not json")
        rc, out, err = self.run_cli("fallback", "on", "claude")
        self.assertEqual(rc, 1)
        self.assertIn("不是合法的 JSON", err)
        with open(self.settings) as f:
            self.assertEqual(f.read(), "{not json")

    def test_no_launcher_suggests_parent(self):
        self.add_proc(100, "ChatGPT")
        self.add_proc(101, "codex", ppid=100)
        rc, out, err = self.run_cli("fallback", "on", "codex")
        self.assertEqual(rc, 1)
        self.assertIn("由 ChatGPT 启动", err)
        rc, out, err = self.run_cli("fallback", "on", "--all")
        self.assertEqual(rc, 0, err)  # --all 时跳过没有启动器的程序
        self.assertIn("signal-desktop：已生成备用启动器", out)

    def test_bad_name(self):
        rc, out, err = self.run_cli("fallback", "on", "a;b")
        self.assertEqual(rc, 1)


class TestFallbackLive(FakeEnv):
    def test_live_detection(self):
        self.add_proc(200, "signal-desktop")
        with open(os.path.join(self.proc, "200", "cmdline"), "wb") as f:
            f.write(b"/opt/Signal/signal-desktop\0--proxy-server=http://127.0.0.1:10808\0")
        with open(os.path.join(self.proc, "200", "environ"), "wb") as f:
            f.write(b"\0" * 64)  # Chromium 覆盖后的样子
        self.add_proc(201, "codex")
        with open(os.path.join(self.proc, "201", "environ"), "wb") as f:
            f.write(b"HOME=/x\0PROXYCTL_FALLBACK=1\0")
        self.add_proc(202, "ChatGPT")
        with open(os.path.join(self.proc, "202", "environ"), "wb") as f:
            f.write(b"HOME=/x\0")
        os.environ["PROXYCTL_PROC"] = self.proc
        try:
            self.assertTrue(P.proc_uses_fallback(200))
            self.assertTrue(P.proc_uses_fallback(201))
            self.assertFalse(P.proc_uses_fallback(202))
            self.assertIsNone(P.proc_uses_fallback(999))
            info = P.fallback_info("codex", P.process_table(), launchers_on={}, found={"codex": []})
            self.assertEqual(P.fallback_describe(info), ("随上层进程走备用方案", "ok"))
            info = P.fallback_info("ChatGPT", P.process_table(), launchers_on={"ChatGPT": ["chatgpt.desktop"]})
            self.assertEqual(P.fallback_describe(info)[1], "warn")  # 已切换但程序是之前启动的
        finally:
            del os.environ["PROXYCTL_PROC"]


class TestRunFallback(FakeEnv):
    def setUp(self):
        super().setUp()
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(5)
        self.addCleanup(s.close)
        self.env["PROXYCTL_SOCKS"] = "127.0.0.1:%d" % s.getsockname()[1]
        self.url = "http://" + self.env["PROXYCTL_SOCKS"]
        self.flag("state", "inactive")  # 备用方案不管 dae 是否在运行
        self.show = "#!/bin/sh\necho \"ARGS:$*\"\necho \"ENV:$HTTPS_PROXY|$no_proxy|$PROXYCTL_FALLBACK\"\n"

    def test_plain_program_gets_env_only(self):
        exe = os.path.join(self.tmp, "bin", "tool")
        write(exe, self.show, 0o755)
        rc, out, err = self.run_cli("run", "--fallback", "--wait", "2", "--", exe, "a", "%U")
        self.assertEqual(rc, 0, err)
        self.assertIn("ARGS:a %U", out)
        self.assertIn("ENV:%s|%s|1" % (self.url, P.FALLBACK_NO_PROXY), out)

    def test_chromium_gets_flags(self):
        exe = os.path.join(self.tmp, "opt", "App", "app")
        write(exe, self.show, 0o755)
        write(os.path.join(self.tmp, "opt", "App", "resources.pak"), "")
        rc, out, err = self.run_cli("run", "--fallback", "--wait", "2", "--", exe, "%U")
        self.assertEqual(rc, 0, err)
        self.assertIn("ARGS:--proxy-server=%s --proxy-bypass-list=localhost;127.0.0.1;::1 %%U" % self.url, out)

    def test_blocked_when_port_down(self):
        self.env["PROXYCTL_SOCKS"] = "127.0.0.1:1"
        rc, out, err = self.run_cli("run", "--fallback", "--", "echo", "x")
        self.assertEqual(rc, 1)
        self.assertIn("代理端口", err)
