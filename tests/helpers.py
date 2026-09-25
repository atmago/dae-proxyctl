"""测试公用工具：加载单文件主程序、构造隔离的假环境。"""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PROXYCTL = os.path.join(ROOT, "proxyctl")
FIXTURES = os.path.join(HERE, "fixtures")
FAKES = os.path.join(HERE, "fakes")


def load_module():
    loader = SourceFileLoader("proxyctl_mod", PROXYCTL)
    spec = importlib.util.spec_from_loader("proxyctl_mod", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


# 测试不能读到本机真实的 /etc/proxyctl.conf
os.environ["PROXYCTL_CONF"] = os.path.join(HERE, "nonexistent.conf")
P = load_module()


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


BASIC = fixture("config_basic.dae")


class FakeEnv(unittest.TestCase):
    """每个测试一个临时目录：假的 /etc/dae、状态目录、/proc 和外部程序。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="proxyctl-test-")
        self.etc = os.path.join(self.tmp, "etc")
        os.mkdir(self.etc)
        self.config = os.path.join(self.etc, "config.dae")
        self.state = os.path.join(self.tmp, "state")
        self.fake = os.path.join(self.tmp, "fake")
        os.mkdir(self.fake)
        self.proc = os.path.join(self.tmp, "proc")
        os.mkdir(self.proc)
        with open(os.path.join(self.fake, "ip_proxy"), "w") as f:
            f.write("203.0.113.7\n")
        self.env = dict(os.environ)
        for k in list(self.env):
            if k.startswith("PROXYCTL_"):
                del self.env[k]
        self.env.update({
            "PROXYCTL_CONFIG": self.config,
            "PROXYCTL_DAE_BIN": os.path.join(FAKES, "dae"),
            "PROXYCTL_SYSTEMCTL": os.path.join(FAKES, "systemctl"),
            "PROXYCTL_STATE_DIR": self.state,
            "PROXYCTL_SS": os.path.join(FAKES, "ss"),
            "PROXYCTL_CURL": os.path.join(FAKES, "curl"),
            "PROXYCTL_NOTIFY_SEND": os.path.join(FAKES, "notify-send"),
            "PROXYCTL_GDBUS": os.path.join(FAKES, "gdbus"),
            "PROXYCTL_SUDO": "/bin/false",
            "PROXYCTL_PKEXEC": "/bin/false",
            "PROXYCTL_PROC": self.proc,
            "PROXYCTL_RELOAD_SETTLE": "0",
            "PROXYCTL_NO_ESCALATE": "1",
            "PROXYCTL_SOCKS": "127.0.0.1:1",  # 必然不在监听
            "PROXYCTL_CONF": os.path.join(self.tmp, "proxyctl.conf"),
            "FAKE_DIR": self.fake,
            "FAKE_SS_SAMPLE": os.path.join(FIXTURES, "ss_sample.txt"),
            "FAKE_SS_LISTEN": os.path.join(FIXTURES, "ss_listen.txt"),
            "XDG_RUNTIME_DIR": self.tmp,
            "NO_COLOR": "1",
        })

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- 辅助 --
    def write_config(self, text):
        with open(self.config, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.chmod(self.config, 0o600)

    def read_config(self):
        with open(self.config, encoding="utf-8", newline="") as f:
            return f.read()

    def flag(self, name, content=""):
        with open(os.path.join(self.fake, name), "w") as f:
            f.write(content)

    def fake_file(self, name):
        p = os.path.join(self.fake, name)
        if not os.path.exists(p):
            return None
        with open(p) as f:
            return f.read()

    def add_proc(self, pid, comm, ppid=1):
        d = os.path.join(self.proc, str(pid))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "comm"), "w") as f:
            f.write(comm + "\n")
        with open(os.path.join(d, "stat"), "w") as f:
            f.write("%d (%s) S %d %d %d 0 -1 4194560 100 0 0 0\n" % (pid, comm, ppid, pid, pid))

    def run_cli(self, *args, input=None, extra_env=None, timeout=60):
        env = dict(self.env)
        if extra_env:
            env.update(extra_env)
        p = subprocess.run([sys.executable, PROXYCTL] + list(args), env=env, input=input,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr

    def backups(self):
        d = os.path.join(self.state, "backups")
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def reload_count(self):
        c = self.fake_file("reload_count")
        return int(c) if c else 0
