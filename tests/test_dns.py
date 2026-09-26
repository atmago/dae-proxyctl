"""DNS 走向：解析 dae 的 dns / dial_mode 设置、识别系统解析器、推演每个程序的 DNS 路径。"""

import ipaddress
import os
import shutil
import subprocess
import unittest

from tests.helpers import FakeEnv, P, fixture

INIT = fixture("config_basic_init.dae")

DNS_CONF = INIT.replace("global {\n", "global {\n    dial_mode: ip\n").replace("routing {", """dns {
    upstream {
        googledns: 'https://dns.google/dns-query'
        private: 'https://dns.nextdns.io/abc123secret'
        alidns: 'udp://223.5.5.5:53'
    }
    routing {
        request {
            qname(geosite:cn) -> alidns
            fallback: googledns
        }
    }
}

routing {""").replace("    fallback: direct", "    dip(8.8.8.8) -> proxy\n    fallback: direct")

RESOLVED = {"kind": "resolved", "nameservers": ["127.0.0.53"], "upstreams": ["192.0.2.1"], "dot": False}


def view_of(text):
    return P.rules_view_from_parsed(P.Parsed(text))


def conn(proc, ip, port):
    return {"procs": [(proc, 1)], "peer_ip": ipaddress.ip_address(ip), "peer_port": port}


class TestConfig(FakeEnv):
    def test_defaults_without_dns_section(self):
        d = view_of(INIT)["dns"]
        self.assertEqual(d["dial_mode"], "domain")
        self.assertFalse(d["has_dns"])
        self.assertEqual(d["routing_fallback"], "direct")
        self.assertEqual(d["other_rules"], [])

    def test_dns_section(self):
        d = view_of(DNS_CONF)["dns"]
        self.assertEqual(d["dial_mode"], "ip")
        self.assertTrue(d["has_dns"])
        self.assertEqual(d["request_fallback"], "googledns")
        self.assertEqual(d["request_rules"], 1)
        self.assertEqual(d["upstreams"]["googledns"], {"scheme": "https", "host": "dns.google", "port": 443})
        self.assertEqual(d["upstreams"]["alidns"], {"scheme": "udp", "host": "223.5.5.5", "port": 53})
        self.assertEqual(d["dip_rules"], [["8.8.8.8", "proxy"]])
        self.assertEqual(d["other_rules"], [])
        self.assertFalse(d["protect"])
        # 缓存对所有人可读：不能带上 DoH 地址中的私有路径
        self.assertNotIn("abc123secret", repr(d))

    def test_routing_parse_unaffected(self):
        self.assertEqual(view_of(DNS_CONF)["proxy"], ["curl", "signal-desktop"])


class TestRoute(FakeEnv):
    def test_route_of(self):
        v = view_of(INIT)
        self.assertEqual(P.route_of("signal-desktop", v)[0], "proxy")
        self.assertEqual(P.route_of("NetworkManager", v)[0], "must_direct")
        self.assertEqual(P.route_of("xray", v)[0], "must_direct")
        self.assertEqual(P.route_of(P.RESOLVED_COMM, v), ("direct", "routing 的 fallback"))

    def test_resolved_plain_direct(self):
        r = P.dns_route("signal-desktop", view_of(INIT), RESOLVED, [])
        self.assertEqual(r["facts"]["sender"], "systemd-resolve")
        self.assertFalse(r["facts"]["encrypted"])
        self.assertIn("本地网络", r["facts"]["visible_to"])
        self.assertTrue(r["facts"]["poison_safe"])
        self.assertIn("192.0.2.1", " ".join(d for _, d, _ in r["steps"]))

    def test_resolver_protected_is_not_hijacked(self):
        v = view_of(INIT)
        v["direct"].append("systemd-resolved")  # 超过 15 个字符，按截断后的进程名匹配
        r = P.dns_route("signal-desktop", v, RESOLVED, [])
        self.assertEqual(r["facts"]["outbound"], "must_direct")
        self.assertIn("dae 不接管", " ".join(d for _, d, _ in r["steps"]))

    def test_resolved_dot(self):
        r = P.dns_route("signal-desktop", view_of(INIT), dict(RESOLVED, dot=True), [])
        self.assertTrue(r["facts"]["encrypted"])

    def test_dae_doh_upstream_and_dial_ip(self):
        r = P.dns_route("signal-desktop", view_of(DNS_CONF), RESOLVED, [])
        self.assertTrue(r["facts"]["encrypted"])
        self.assertFalse(r["facts"]["poison_safe"])
        text = " ".join(d for _, d, _ in r["steps"])
        self.assertIn("DoH", text)
        # 上游是域名 dns.google，dip(8.8.8.8) 管不到它，按 fallback 直连
        self.assertEqual(r["facts"]["outbound"], "direct")

    def test_app_with_own_dns_goes_through_proxy(self):
        conns = [conn("signal-desktop", "8.8.8.8", "53"), conn("other", "1.1.1.1", "53")]
        r = P.dns_route("signal-desktop", view_of(INIT), RESOLVED, conns)
        self.assertEqual(r["facts"]["sender"], "signal-desktop")
        self.assertEqual(r["facts"]["outbound"], "proxy")
        self.assertIn("代理节点", r["facts"]["visible_to"])

    def test_direct_app_is_affected_by_poison(self):
        r = P.dns_route("firefox", view_of(INIT), RESOLVED, [])
        self.assertFalse(r["facts"]["poison_safe"])

    def test_groups(self):
        conns = [conn("curl", "8.8.8.8", "53")]
        v = view_of(INIT)
        v["proxy"].append("claude")
        groups = P.dns_groups(["signal-desktop", "curl", "claude"], v, RESOLVED, conns)
        self.assertEqual([m for m, _ in groups], [["signal-desktop", "claude"], ["curl"]])
        self.assertIn("这些程序", groups[0][1]["steps"][0][1])


class TestResolver(FakeEnv):
    def write(self, name, text):
        p = os.path.join(self.tmp, name)
        with open(p, "w") as f:
            f.write(text)
        return p

    def with_env(self, **kw):
        old = {k: os.environ.get(k) for k in kw}
        os.environ.update(kw)
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
                                 for k, v in old.items()])

    def test_resolved_stub(self):
        self.with_env(PROXYCTL_RESOLV_CONF=self.write("stub", "nameserver 127.0.0.53\noptions edns0\n"),
                      PROXYCTL_RESOLVED_CONF=self.write("up", "nameserver 192.0.2.1\nnameserver fe80::1%wlan0\n"),
                      PROXYCTL_RESOLVECTL="/bin/false")
        info = P.resolver_info()
        self.assertEqual(info["kind"], "resolved")
        self.assertEqual(info["upstreams"], ["192.0.2.1", "fe80::1"])
        self.assertIsNone(info["dot"])

    def test_plain_and_missing(self):
        self.with_env(PROXYCTL_RESOLV_CONF=self.write("plain", "nameserver 9.9.9.9\n"))
        self.assertEqual(P.resolver_info()["kind"], "direct")
        self.with_env(PROXYCTL_RESOLV_CONF=self.write("local", "nameserver 127.0.0.1\n"))
        self.assertEqual(P.resolver_info()["kind"], "local")
        self.with_env(PROXYCTL_RESOLV_CONF=os.path.join(self.tmp, "nope"))
        self.assertEqual(P.resolver_info()["kind"], "unknown")


class TestCli(FakeEnv):
    def test_dns_command(self):
        self.write_config(INIT)
        stub = os.path.join(self.tmp, "stub")
        with open(stub, "w") as f:
            f.write("nameserver 127.0.0.53\n")
        rc, out, err = self.run_cli("dns", extra_env={"PROXYCTL_RESOLV_CONF": stub,
                                                      "PROXYCTL_RESOLVED_CONF": stub,
                                                      "PROXYCTL_RESOLVECTL": "/bin/false"})
        self.assertEqual(rc, 0, err)
        self.assertIn("【curl、signal-desktop】", out)
        self.assertIn("systemd-resolve", out)
        rc, out, err = self.run_cli("dns", "bad name")
        self.assertEqual(rc, 1)


class TestBadge(FakeEnv):
    def badge(self, name, view, resolver=RESOLVED, conns=()):
        return P.dns_badge(P.dns_route(name, view, resolver, list(conns)), P.route_of(name, view)[0])

    def test_proxied_app_plain_direct_dns_is_warned(self):
        text, level, detail = self.badge("signal-desktop", view_of(INIT))
        self.assertEqual((text, level), ("DNS 直连·明文", "warn"))
        self.assertIn("systemd-resolve", detail)

    def test_direct_app_is_not_warned(self):
        self.assertEqual(self.badge("firefox", view_of(INIT))[:2], ("DNS 直连·明文", "plain"))

    def test_encrypted_or_proxied(self):
        self.assertEqual(self.badge("signal-desktop", view_of(INIT), dict(RESOLVED, dot=True))[:2],
                         ("DNS 直连·加密", "ok"))
        own = [conn("signal-desktop", "8.8.8.8", "53")]
        self.assertEqual(self.badge("signal-desktop", view_of(INIT), conns=own)[:2], ("DNS 代理", "ok"))

    def test_unknown_resolver(self):
        self.assertEqual(self.badge("signal-desktop", view_of(INIT), {"kind": "unknown", "nameservers": []})[:2],
                         ("DNS 未知", "plain"))


PROTECTED = P.op_dns_protect(INIT, True)[0]


class TestProtect(FakeEnv):
    def test_roundtrip_and_idempotent(self):
        self.assertTrue(P.Parsed(PROTECTED).dns_protected)
        again, msgs = P.op_dns_protect(PROTECTED, True)
        self.assertEqual(again, PROTECTED)
        self.assertIn("无需重复", msgs[0])
        self.assertEqual(P.op_dns_protect(PROTECTED, False)[0], INIT)
        self.assertEqual(P.op_dns_protect(INIT, False)[0], INIT)

    def test_other_ops_keep_regions(self):
        text = P.op_add(PROTECTED, "firefox")[0]
        self.assertTrue(P.Parsed(text).dns_protected)
        self.assertIn("pname(firefox) -> proxy\n    # <<< proxyctl:proxy <\n    # >>> proxyctl:dns-route", text)
        self.assertEqual(P.op_init(text)[0], text)
        self.assertNotIn("dip(", " ".join(c for _, c in P.Parsed(text).unmanaged_rules()))

    def test_refuses_users_own_dns(self):
        with self.assertRaises(P.ProxyctlError) as cm:
            P.op_dns_protect(DNS_CONF, True)
        self.assertIn("你自己写的 dns", str(cm.exception))

    def test_requires_init(self):
        with self.assertRaises(P.ProxyctlError):
            P.op_dns_protect(fixture("config_basic.dae"), True)

    def test_broken_regions(self):
        half = PROTECTED.replace("# <<< proxyctl:dns <\n", "")
        with self.assertRaises(P.ProxyctlError):
            P.Parsed(half)
        moved = PROTECTED.replace("    # >>> proxyctl:dns-route >>>\n    dip(8.8.8.8) -> proxy\n"
                                  "    # <<< proxyctl:dns-route <\n", "")
        moved = moved.replace("routing {\n", "routing {\n    # >>> proxyctl:dns-route >>>\n"
                              "    # <<< proxyctl:dns-route <\n", 1)
        with self.assertRaises(P.ProxyctlError):
            P.Parsed(moved)

    def test_crlf(self):
        on = P.op_dns_protect(INIT.replace("\n", "\r\n"), True)[0]
        self.assertNotIn("\n", on.replace("\r\n", ""))
        self.assertEqual(P.op_dns_protect(on, False)[0], INIT.replace("\n", "\r\n"))

    def test_route_after_protect(self):
        v = view_of(PROTECTED)
        self.assertTrue(v["dns"]["protect"])
        r = P.dns_route("signal-desktop", v, RESOLVED, [])
        self.assertEqual(r["facts"]["outbound"], "proxy")
        self.assertIn("防泄漏已开启", " ".join(d for _, d, _ in r["steps"]))
        self.assertEqual(P.dns_badge(r, "proxy")[:2], ("DNS 代理", "ok"))

    def test_cli_on_off(self):
        self.write_config(INIT)
        rc, out, err = self.run_cli("dns", "--protect", "on")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.read_config(), PROTECTED)
        self.assertTrue(self.backups()[-1].endswith("-dns-protect-on.dae"))
        rc, out, err = self.run_cli("dns", "--protect", "off")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.read_config(), INIT)
        self.assertEqual(self.reload_count(), 2)


@unittest.skipUnless(shutil.which("dae"), "本机没有 dae")
class TestRealDaeValidate(FakeEnv):
    def test_protected_config_validates(self):
        path = os.path.join(self.tmp, "on.dae")
        with open(path, "w") as f:
            f.write(PROTECTED)
        os.chmod(path, 0o600)
        p = subprocess.run(["dae", "validate", "-c", path], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.assertEqual(p.returncode, 0, p.stdout)
