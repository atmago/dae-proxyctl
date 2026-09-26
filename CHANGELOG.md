# 版本记录

每个版本发布时，GitHub Releases 页面会自动使用这里对应版本的内容作为说明。

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
