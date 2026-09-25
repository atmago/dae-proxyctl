"""配置解析与变换（纯函数）测试。"""

import unittest

from tests.helpers import BASIC, P, fixture

EXPECTED_INIT = fixture("config_basic_init.dae")


def outside(text):
    """返回管理区块之外的所有行（逐字节）。"""
    parsed = P.Parsed(text)
    b, e = parsed.blocks["safety"][0], parsed.blocks["proxy"][1]
    return "".join(parsed.lines[:b]), "".join(parsed.lines[e + 1:])


VARIANT_ORDER = """global {
    wan_interface: auto
}
node {
    v2rayn: 'socks5://127.0.0.1:10808'
}
group {
    proxy {
        filter: name(v2rayn)
        policy: fixed(0)
    }
}
routing {
    pname(signal-desktop) -> proxy
    fallback: direct
    dip(224.0.0.0/3, 'ff00::/8') -> direct
    pname(xray) -> must_direct
    pname(NetworkManager) -> must_direct
}
"""

VARIANT_COMMENTS = """# 顶部注释 { 里面有大括号 }
global {
    wan_interface: auto # 行尾注释 }
    log_level: info
}

node {
    # 注释里的 routing { 不应被当成块
    v2rayn: 'socks5://127.0.0.1:10808#tag{}'
}

group {
    proxy {
        filter: name(v2rayn)
        policy: fixed(0)
    }
}

dns {
    upstream {
        alidns: 'udp://223.5.5.5:53'
    }
    routing {
        request {
            fallback: alidns
        }
    }
}

routing {
    # 多行注释第一行
    # 多行注释第二行
    pname(telegram-desktop) -> proxy

    # 单行注释
    pname(firefox) -> proxy # 行尾注释
    domain(geosite:cn) -> direct
    pname(foo) && dport(443) -> proxy
    pname(bar) -> direct
    fallback: direct
}
"""


class TestParse(unittest.TestCase):
    def test_basic_uninitialized(self):
        p = P.Parsed(BASIC)
        self.assertFalse(p.initialized)
        self.assertEqual(p.lines[p.rs].strip(), "routing {")
        self.assertEqual(p.lines[p.re].strip(), "}")
        self.assertEqual(p.indent, "    ")

    def test_missing_routing(self):
        text = BASIC[:BASIC.index("routing {")]
        with self.assertRaises(P.ProxyctlError) as cm:
            P.Parsed(text)
        self.assertIn("找不到 routing", str(cm.exception))

    def test_routing_brace_next_line(self):
        with self.assertRaises(P.ProxyctlError):
            P.Parsed(BASIC.replace("routing {", "routing\n{"))

    def test_routing_one_line(self):
        with self.assertRaises(P.ProxyctlError):
            P.Parsed("global {\n}\nrouting { fallback: direct }\n")

    def test_unbalanced(self):
        with self.assertRaises(P.ProxyctlError):
            P.Parsed(BASIC + "}\n")
        with self.assertRaises(P.ProxyctlError):
            P.Parsed(BASIC.replace("routing {", "routing {\n    foo {"))

    def test_two_routing(self):
        with self.assertRaises(P.ProxyctlError):
            P.Parsed(BASIC + "routing {\n    fallback: direct\n}\n")

    def test_dns_routing_not_confused(self):
        p = P.Parsed(VARIANT_COMMENTS)
        self.assertTrue(p.lines[p.rs].startswith("routing {"))
        self.assertIn("pname(telegram-desktop)", p.lines[p.rs + 3])

    def test_global_value(self):
        p = P.Parsed(BASIC)
        self.assertIsNone(p.global_value("dial_mode"))
        p = P.Parsed(BASIC.replace("log_level: info", "log_level: info\n    dial_mode: ip"))
        self.assertEqual(p.global_value("dial_mode"), "ip")
        self.assertEqual(p.global_value("log_level"), "info")


class TestInit(unittest.TestCase):
    def test_golden(self):
        new, msgs = P.op_init(BASIC)
        self.assertEqual(new, EXPECTED_INIT)
        self.assertTrue(any("curl" in m and "警告" in m for m in msgs))

    def test_idempotent(self):
        once, _ = P.op_init(BASIC)
        twice, msgs = P.op_init(once)
        self.assertEqual(once, twice)
        self.assertTrue(any("已存在" in m for m in msgs))

    def test_migration_lists(self):
        new, _ = P.op_init(BASIC)
        p = P.Parsed(new)
        self.assertEqual(p.names("proxy"), ["curl", "signal-desktop"])
        self.assertEqual(p.names("direct"), ["NetworkManager"])
        self.assertEqual(p.entries["safety"], list(P.SAFETY_RULES))
        self.assertEqual([c for _, c in p.unmanaged_rules()], ["fallback: direct"])
        self.assertNotIn("防止 v2rayN", new)
        self.assertIn("# 其他所有程序默认直连", new)

    def test_prefix_suffix_bytes_unchanged(self):
        new, _ = P.op_init(BASIC)
        head = BASIC[:BASIC.index("routing {\n") + len("routing {\n")]
        self.assertTrue(new.startswith(head))
        tail = BASIC[BASIC.index("    # 其他所有程序默认直连"):]
        self.assertTrue(new.endswith(tail))

    def test_order_variant(self):
        new, _ = P.op_init(VARIANT_ORDER)
        p = P.Parsed(new)
        self.assertEqual(p.names("proxy"), ["signal-desktop"])
        self.assertEqual(p.names("direct"), ["NetworkManager"])
        self.assertEqual([c for _, c in p.unmanaged_rules()],
                         ["fallback: direct", "dip(224.0.0.0/3, 'ff00::/8') -> direct"])
        self.assertEqual(P.op_init(new)[0], new)

    def test_comments_and_dns_variant(self):
        new, msgs = P.op_init(VARIANT_COMMENTS)
        p = P.Parsed(new)
        self.assertEqual(p.names("proxy"), ["telegram-desktop", "firefox"])
        # 多行注释块保留，单行注释删除
        self.assertIn("# 多行注释第一行", new)
        self.assertIn("# 多行注释第二行", new)
        self.assertNotIn("# 单行注释\n", new)
        # 复合规则与 -> direct 规则不迁移
        rest = [c for _, c in p.unmanaged_rules()]
        self.assertIn("pname(foo) && dport(443) -> proxy", rest)
        self.assertIn("pname(bar) -> direct", rest)
        # dns 段与 routing 之前的全部内容逐字节不变
        head = VARIANT_COMMENTS[:VARIANT_COMMENTS.index("\nrouting {") + len("\nrouting {\n")]
        self.assertTrue(new.startswith(head))

    def test_conflicting_rules_kept(self):
        text = BASIC.replace("    fallback: direct",
                             "    pname(signal-desktop) -> must_direct\n    pname(xray) -> proxy\n    fallback: direct")
        new, msgs = P.op_init(text)
        p = P.Parsed(new)
        self.assertEqual(p.names("proxy"), ["curl", "signal-desktop"])
        self.assertNotIn("signal-desktop", p.names("direct"))
        rest = [c for _, c in p.unmanaged_rules()]
        self.assertIn("pname(signal-desktop) -> must_direct", rest)
        self.assertIn("pname(xray) -> proxy", rest)
        self.assertTrue(any("冲突" in m for m in msgs))

    def test_quoted_or_invalid_name_not_migrated(self):
        text = BASIC.replace("    fallback: direct",
                             "    pname('quoted') -> proxy\n    pname(a,b) -> proxy\n    fallback: direct")
        new, msgs = P.op_init(text)
        rest = [c for _, c in P.Parsed(new).unmanaged_rules()]
        self.assertIn("pname('quoted') -> proxy", rest)
        self.assertIn("pname(a,b) -> proxy", rest)
        self.assertTrue(any("不符合命名规则" in m for m in msgs))

    def test_safety_names_not_duplicated(self):
        text = BASIC.replace("    fallback: direct", "    pname(mihomo) -> must_direct\n    fallback: direct")
        new, _ = P.op_init(text)
        p = P.Parsed(new)
        self.assertEqual(p.names("direct"), ["NetworkManager"])
        self.assertEqual(new.count("pname(mihomo)"), 1)

    def test_crlf_preserved(self):
        text = BASIC.replace("\n", "\r\n")
        new, _ = P.op_init(text)
        self.assertNotIn("\n", new.replace("\r\n", ""))
        self.assertEqual(new, EXPECTED_INIT.replace("\n", "\r\n"))

    def test_tab_indent(self):
        text = BASIC.replace("\n    pname", "\n\tpname").replace("\n    #", "\n\t#").replace("\n    fallback", "\n\tfallback")
        new, _ = P.op_init(text)
        self.assertIn("\t# >>> proxyctl:safety (do not edit) >>>", new)

    def test_repair_safety(self):
        good, _ = P.op_init(BASIC)
        broken = good.replace("    pname(mihomo) -> must_direct\n", "")
        fixed, msgs = P.op_init(broken)
        self.assertEqual(fixed, good)
        self.assertTrue(any("已修复" in m for m in msgs))

    def test_repair_keeps_extra_safety_rule(self):
        good, _ = P.op_init(BASIC)
        extra = good.replace("    pname(proxyctl-probe) -> proxy\n",
                             "    pname(proxyctl-probe) -> proxy\n    pname(hysteria) -> must_direct\n")
        fixed, _ = P.op_init(extra)
        self.assertIn("pname(hysteria) -> must_direct", fixed)

    def test_existing_blocks_no_migration(self):
        good, _ = P.op_init(BASIC)
        manual = good.replace("    fallback: direct", "    pname(zoom) -> proxy\n    fallback: direct")
        again, _ = P.op_init(manual)
        self.assertEqual(again, manual)


class TestCorruptMarkers(unittest.TestCase):
    def setUp(self):
        self.good, _ = P.op_init(BASIC)

    def assertCorrupt(self, text):
        with self.assertRaises(P.ProxyctlError):
            P.op_init(text)
        with self.assertRaises(P.ProxyctlError):
            P.op_add(text, "firefox")

    def test_missing_end(self):
        self.assertCorrupt(self.good.replace("    # <<< proxyctl:direct <\n", ""))

    def test_missing_block(self):
        text = self.good.replace("    # >>> proxyctl:proxy >>>\n", "").replace("    # <<< proxyctl:proxy <\n", "")
        self.assertCorrupt(text)

    def test_wrong_order(self):
        text = self.good.replace("proxyctl:direct >>>", "proxyctl:TMP >>>") \
            .replace("proxyctl:proxy >>>", "proxyctl:direct >>>").replace("proxyctl:TMP >>>", "proxyctl:proxy >>>")
        self.assertCorrupt(text)

    def test_duplicate(self):
        self.assertCorrupt(self.good.replace("    # <<< proxyctl:proxy <\n",
                                             "    # <<< proxyctl:proxy <\n    # <<< proxyctl:proxy <\n"))

    def test_garbled_marker(self):
        self.assertCorrupt(self.good.replace("# >>> proxyctl:direct >>>", "# >>> proxyctl:direkt >>>"))

    def test_marker_outside_routing(self):
        self.assertCorrupt("# >>> proxyctl:proxy >>>\n" + self.good)

    def test_junk_inside_block(self):
        self.assertCorrupt(self.good.replace("    pname(signal-desktop) -> proxy\n",
                                             "    pname(signal-desktop) -> proxy\n    domain(x.com) -> proxy\n"))

    def test_wrong_outbound_inside_block(self):
        self.assertCorrupt(self.good.replace("pname(signal-desktop) -> proxy", "pname(signal-desktop) -> direct"))


class TestOps(unittest.TestCase):
    def setUp(self):
        self.base, _ = P.op_init(BASIC)

    def assertOutsideSame(self, a, b):
        self.assertEqual(outside(a), outside(b))

    def test_add(self):
        new, msgs = P.op_add(self.base, "firefox")
        self.assertEqual(P.Parsed(new).names("proxy"), ["curl", "signal-desktop", "firefox"])
        self.assertOutsideSame(self.base, new)
        self.assertIn("    pname(firefox) -> proxy\n    # <<< proxyctl:proxy <", new)

    def test_add_duplicate(self):
        new, msgs = P.op_add(self.base, "signal-desktop")
        self.assertEqual(new, self.base)
        self.assertTrue(any("无需重复" in m for m in msgs))

    def test_add_conflict_direct(self):
        with self.assertRaises(P.ProxyctlError) as cm:
            P.op_add(self.base, "NetworkManager")
        self.assertIn("冲突", str(cm.exception))

    def test_add_safety(self):
        with self.assertRaises(P.ProxyctlError):
            P.op_add(self.base, "xray")
        new, _ = P.op_add(self.base, "proxyctl-probe")
        self.assertEqual(new, self.base)

    def test_add_blacklist(self):
        for name in ("python3", "python3.12", "python3.", "node", "java", "curl", "wget", "git", "bash",
                     "wine64", "Python3", "electron", "ssh", "sudo"):
            with self.assertRaises(P.ProxyctlError, msg=name) as cm:
                P.op_add(self.base, name)
            self.assertIn("通用运行时", str(cm.exception))

    def test_add_not_initialized(self):
        with self.assertRaises(P.ProxyctlError) as cm:
            P.op_add(BASIC, "firefox")
        self.assertIn("init", str(cm.exception))

    def test_injection(self):
        for bad in ("a) -> direct\n", "a) -> direct", "", "x" * 65, "a b", "a\nb", "a\n", "pname(x)",
                    "a#b", "a{", "a'", 'a"', "a,b", "中文", "a;b", "a\tb", "a\r", "../x", "a/b"):
            with self.assertRaises(P.ProxyctlError, msg=repr(bad)):
                P.op_add(self.base, bad)
            with self.assertRaises(P.ProxyctlError, msg=repr(bad)):
                P.op_protect(self.base, bad)
            with self.assertRaises(P.ProxyctlError, msg=repr(bad)):
                P.op_remove(self.base, bad)

    def test_name_boundaries(self):
        for good in ("a", "x" * 64, "A.b_c+d-e", "Telegram", "1"):
            new, _ = P.op_add(self.base, good)
            self.assertIn(good, P.Parsed(new).names("proxy"))

    def test_long_name_warns(self):
        _, msgs = P.op_add(self.base, "a-very-long-process-name")
        self.assertTrue(any("15" in m for m in msgs))

    def test_remove(self):
        new, _ = P.op_remove(self.base, "signal-desktop")
        self.assertEqual(P.Parsed(new).names("proxy"), ["curl"])
        self.assertOutsideSame(self.base, new)
        with self.assertRaises(P.ProxyctlError):
            P.op_remove(self.base, "firefox")
        with self.assertRaises(P.ProxyctlError):
            P.op_remove(self.base, "proxyctl-probe")
        with self.assertRaises(P.ProxyctlError):
            P.op_remove(self.base, "xray")

    def test_remove_last_then_add(self):
        t, _ = P.op_remove(self.base, "curl")
        t, _ = P.op_remove(t, "signal-desktop")
        self.assertIn("    # >>> proxyctl:proxy >>>\n    # <<< proxyctl:proxy <\n", t)
        t, _ = P.op_add(t, "firefox")
        self.assertEqual(P.Parsed(t).names("proxy"), ["firefox"])

    def test_protect(self):
        new, _ = P.op_protect(self.base, "python3")  # 运行时名称允许直连
        self.assertEqual(P.Parsed(new).names("direct"), ["NetworkManager", "python3"])
        self.assertOutsideSame(self.base, new)
        with self.assertRaises(P.ProxyctlError):
            P.op_protect(self.base, "signal-desktop")
        same, _ = P.op_protect(self.base, "xray")
        self.assertEqual(same, self.base)
        with self.assertRaises(P.ProxyctlError):
            P.op_protect(self.base, "proxyctl-probe")
        same, _ = P.op_protect(self.base, "NetworkManager")
        self.assertEqual(same, self.base)

    def test_unprotect(self):
        new, _ = P.op_unprotect(self.base, "NetworkManager")
        self.assertEqual(P.Parsed(new).names("direct"), [])
        for n in ("xray", "sing-box", "mihomo", "proxyctl-probe"):
            with self.assertRaises(P.ProxyctlError):
                P.op_unprotect(self.base, n)
        with self.assertRaises(P.ProxyctlError):
            P.op_unprotect(self.base, "signal-desktop")

    def test_safety_always_present(self):
        t = self.base
        for op, n in ((P.op_remove, "curl"), (P.op_unprotect, "NetworkManager"), (P.op_add, "zoom"),
                      (P.op_protect, "python3"), (P.op_remove, "signal-desktop")):
            t, _ = op(t, n)
            self.assertTrue(P.Parsed(t).safety_complete())

    def test_ops_repair_safety(self):
        broken = self.base.replace("    pname(xray) -> must_direct\n", "", 1)
        new, _ = P.op_add(broken, "zoom")
        self.assertTrue(P.Parsed(new).safety_complete())

    def test_unmanaged_note(self):
        t = self.base.replace("    fallback: direct", "    pname(zoom) -> direct\n    fallback: direct")
        _, msgs = P.op_add(t, "zoom")
        self.assertTrue(any("不受 proxyctl 管理" in m for m in msgs))

    def test_no_trailing_newline(self):
        t = self.base.rstrip("\n")
        new, _ = P.op_add(t, "zoom")
        self.assertFalse(new.endswith("\n"))
        self.assertTrue(new.endswith("}"))


class TestBlacklist(unittest.TestCase):
    def test_list(self):
        for n in ("python", "python3", "python3.", "python3.14", "node", "nodejs", "java", "electron", "bash",
                  "sh", "zsh", "fish", "dash", "perl", "ruby", "php", "dotnet", "mono", "wine", "wine64",
                  "wineserver", "systemd", "sudo", "ssh", "curl", "wget", "git"):
            self.assertTrue(P.is_blacklisted(n), n)
        for n in ("signal-desktop", "firefox", "python3x", "nodes", "gitk", "sshd", "telegram-desktop"):
            self.assertFalse(P.is_blacklisted(n), n)


if __name__ == "__main__":
    unittest.main()
