# 版本记录

每个版本发布时，GitHub Releases 页面会自动使用这里对应版本的内容作为说明。

## 未发布

### 新增

- **备用方案（按程序切换）**：dae 本身出问题时（例如 dae 2.1.1 netkit 模式下连接频繁被重置），
  可以把单个程序切到“备用”：它自己直连代理端口（默认 `http://127.0.0.1:10808`），不经过 dae。
  `proxyctl fallback on|off <进程名...>`（或 `--all`），`proxyctl fallback status` 用 ss 实测每个程序的连接
  是连代理端口还是经 dae。不需要 root，不修改 dae 配置，也不 reload dae；名单中的 pname 规则保留作兜底。
  - 桌面程序：在 `~/.local/share/applications/` 生成启动器，经 `proxyctl run --fallback` 启动，注入代理环境变量；
    Electron / Chromium 程序另加 `--proxy-server`（在 GNOME 下它们只认系统代理，不认环境变量）。改回时原样恢复原来的启动器。
  - Claude Code（进程名 `claude`）：改 `~/.claude/settings.json` 的 `env`，新开的会话生效，不用重启 Claude Desktop。
  - 没有启动器的子进程（如 ChatGPT 启动的 codex）提示对上层程序开启，子进程会继承代理设置。
- GUI“规则”页：代理名单每一行显示当前走 dae 还是备用方案，并有“备用 / 改回 dae”按钮（不弹授权框）；
  已切换但程序还是旧方式运行时提示重启。
- `proxyctl run --fallback`：只要求代理端口在监听（不管 dae 是否在运行），然后按备用方案启动程序。
- 配置项 `fallback_proxy`：备用方案使用的代理地址。

### 文档

- README 改为简版首页（图标、特性、快速开始），软件名统一写作 **Proxyctl**（命令仍是小写的 `proxyctl`）。
- 新增 [安装教程](docs/INSTALL.md)：面向新手，从零开始，每一步都写明成功的样子和失败时怎么办。
- 新增 [使用手册](docs/MANUAL.md)：日常使用、常见问题、故障模式、备用方案、全部命令和配置项。

## v1.2.0 — 2026-09-26

### 新增

- **链路延迟记录**：健康检查定时器每次探测代理链路时，把耗时、是否成功和当时的内核（xray / sing-box / mihomo）
  记录下来，保留 7 天。`proxyctl history [--hours N]` 查看每小时的中位 / 最慢耗时、异常时段和内核切换；
  GUI 新增“延迟”页，画出 6 小时 / 24 小时 / 7 天的耗时曲线，底部色带标出当时的内核，鼠标悬停可看每个点。
- 内核用固定颜色突出显示：sing-box 绿、xray 橙、mihomo 紫（Clash Verge 的 verge-mihomo 同样算 mihomo），
  顶部状态卡、延迟页摘要、曲线色带和内核切换列表都用同一套颜色。状态卡显示的是正在监听 SOCKS 端口的内核。
- **关于窗口**：GUI 右上角菜单 → 关于 Proxyctl，显示图标、版本号、作者、GitHub 地址（可点击、可复制）、许可证，
  可以直接打开“报告问题”和版本记录。

### 改进

- 窗口左上角显示软件图标。
- 所有文字按钮统一改为常驻底色的胶囊样式（联网进程页、规则页、DNS 页、延迟页的时间范围切换、弹窗按钮、“运行自检”），
  不用把鼠标移上去就能看出可以点击；“加入代理”统一用蓝色强调。
- 应用列表和窗口标题统一显示为 **Proxyctl**（不再是“按应用代理（proxyctl）”）；在应用列表里搜索“代理”仍能找到。
- `proxyctl test` 第 1 项的说明不再写死 xray，改为“代理客户端”。

## v1.1.0 — 2026-09-26

### 新增

- **导入 / 导出规则**：`proxyctl export [文件]` 把代理名单和直连保护名单导出为 JSON，
  `sudo proxyctl import <文件>` 导入（默认合并，`--replace` 替换）。文件只含进程名，不含节点或订阅信息，
  可以放心拷到另一台电脑。GUI：右上角菜单 → 导入规则 / 导出规则，导入前会预览合并和替换的结果。
- **DNS 走向**：`proxyctl dns` 和 GUI 新增的“DNS”页显示每个程序的域名解析请求交给谁、是否被 dae 接管、
  经直连还是代理发往哪个上游、是否加密、谁能看到访问的域名，以及本地 DNS 污染会不会影响连接。
- **进程旁的 DNS 标签**：“联网进程”页在每个进程的状态旁显示 DNS 走向（如“DNS 直连·明文”“DNS 代理”），
  代理程序的 DNS 会被本地网络看到时标为橙色，悬停可看完整路径。`proxyctl list` 同步新增 DNS 列。
- **DNS 防泄漏开关**：`sudo proxyctl dns --protect on|off`，GUI 的 DNS 页也可一键开关。开启后国外域名由 dae
  经代理向 8.8.8.8 查询，运营商看不到；国内和局域网域名照旧查询原来的 DNS。对 dae 配置的修改限定在两个带标记的
  区域内，关闭后配置恢复原样；配置中已有自己写的 dns 区块时拒绝开启。

### 改进

- `proxyctl check` 显示 DNS 防泄漏是否开启。

## v1.0.0 — 2026-09-25

首个公开版本。

- 基于 dae 的按应用代理（Proxifier 式）：`add` / `remove` 管理代理名单，`protect` / `unprotect` 管理直连保护，
  `init` 创建管理区块并迁移已有规则；代理核心（xray / sing-box / mihomo）固定直连，防止回环。
- 每次修改都经过“备份 → dae validate → 写入 → reload → 确认仍在运行”，失败自动回滚；`backups` / `restore` 恢复备份。
- `list` / `pick` 查看正在联网的进程并一键加入代理，能发现代理程序启动的、名字不同的子进程。
- `check` 健康检查（可配合定时器弹出桌面通知）、`test` 端到端自检。
- `run` / `guard-desktop` 启动守卫：代理没准备好时不启动程序，避免直连外网。
- 图形界面（GTK4 / libadwaita，跟随系统深浅色），修改通过 pkexec 授权。
