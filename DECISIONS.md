# 设计决定记录

以下是需求中没有明确规定、由我自行做出的决定。原则：拿不准时选最保守、最不容易出错或泄露的方案。

## 配置解析与 dae 语法

1. **首条命中语义**：`/etc/dae/example.dae` 在本机只有一行链接（`# see https://github.com/daeuniverse/dae`），
   无法用它核对语法。按任务说明的假设设计：routing 规则从上到下、首条命中生效（这也与 dae 官方文档一致）。
   管理区块因此放在 routing 最前面，顺序为 safety → direct → proxy。
2. **语法核对改用真实 dae**：本机 `dae validate -c` 在非 root 下可以校验当前用户所有的 0600 文件，
   所以测试直接用真实 dae 校验了所有生成的配置（基础配置、各种变体、CRLF、空区块等），全部通过。
3. **dae 要求配置文件名以 `.dae` 结尾**（实测：`must has suffix .dae`）。因此事务中的临时文件命名为
   `/etc/dae/.proxyctl-tmp-XXXXXXXX.dae`。它只在 validate 期间存在，失败时会被删除。
   理论风险：如果你的配置用 `include` 引入了通配符 `*.dae`，临时文件可能被短暂匹配到；目前配置中没有 include。
4. **结构无法可靠解析时一律中止、不做修改**：找不到 routing 块、有多个顶层 routing 块、`routing {` 不在同一行、
   结束 `}` 不单独成行、routing 内出现大括号、大括号不配对。解析时会跳过注释和引号内的内容
   （例如 node 链接中的 `#` 和 `{}`），dns 段中嵌套的 `routing {` 不会被误认。
5. **标记损坏的判定**：任何以 `# >>> proxyctl:` 或 `# <<< proxyctl:` 开头的行都视为标记；必须恰好是 6 个已知标记、
   各出现一次、都在 routing 内、顺序为 safety → direct → proxy。部分存在、重复、乱序、拼写错误或在 routing 外，
   都视为“标记损坏”，所有写命令（包括 init）拒绝修改，提示手工修复或 restore。
6. **管理区块内部只允许 `pname(名称) -> 出口` 和空行**，出现注释或其他规则视为损坏（拒绝修改，而不是静默丢弃）。
7. **换行符和缩进**：保留原文件的换行风格（LF/CRLF）；管理区块使用 routing 块中第一行非空内容的缩进（默认 4 空格）；
   文件末尾没有换行符时保持没有。
8. **proxy 出口名固定为 `proxy`**（即 group 名）。如果以后把 group 改名，validate 会失败并自动回滚，不会写坏配置。

## init 迁移

9. 只迁移**单独一条、形如 `pname(X) -> proxy` 或 `pname(X) -> must_direct`** 的规则（允许行尾注释，迁移后行尾注释丢失）。
   复合规则（`pname(a) && dport(443) -> proxy`）、多名称（`pname(a, b)`）、带引号的名称、`-> direct` 规则、
   以及名称不合法的规则一律保留原位（名称不合法时给出警告）。
10. 同一名称的多条规则：按首条命中确定其实际生效的出口并迁移；后面出口不同的规则本来就不会生效，保留原位并警告。
    与安全区块冲突的规则（例如 `pname(xray) -> proxy`）同理保留原位并警告。
    与安全区块一致的规则（xray / sing-box / mihomo 的 must_direct）删除，不重复加入 direct 区块。
11. “紧邻的单行注释”：只删除规则正上方的那一行注释，并且只在它是**单独一行**的注释时删除；
    如果上方是连续多行注释（可能是说明一组规则的段落），全部保留。
12. 删除规则后，原位置可能留下连续空行，这些由删除造成的连续空行合并为一行；原本就存在的连续空行不受影响。
    管理区块后面如果紧跟非空行，会插入一个空行作为分隔。
13. 管理区块已存在时，init 只检查并修复安全区块，不再迁移（即使 routing 中又出现了手写的 pname 规则）。

## 安全区块与名单操作

14. 修复安全区块时：4 条固定规则总是按固定顺序位于最前面；如果有人在安全区块中额外加入了合法规则，**不删除**，
    保留在固定规则之后（遵守“任何命令都不能删除安全区块中的规则”）；只有与固定规则冲突、永远不会命中的规则会被固定规则取代，并给出警告。
15. 所有写命令在写回时都会顺带修复安全区块（只会让配置更安全）。
16. `add` 已在名单中、`protect` 已在直连保护中：提示“无需重复”，退出码 0，不写文件、不 reload。
    `remove` / `unprotect` 不在名单中：报错，退出码 1。
17. `add` 安全区块中的直连名称（xray 等）→ 拒绝；`add proxyctl-probe` → 提示已在安全区块中。
    `protect proxyctl-probe` → 拒绝；`protect xray` → 提示已保护。
18. 黑名单比较不区分大小写，并把 `python3.` 解释为 `python3.` 加任意数字（python3.12、python3.14 等）。
    黑名单检查在提权之前就执行，被拒绝的名称不会触发 sudo 密码提示。
19. 名称超过 15 个字符时只警告不拒绝（内核 comm 会截断，规则大概率不会命中），因为需求规定长度上限为 64。
20. 如果 routing 中还有不受管理的规则涉及同一名称，`add` / `protect` 给出提示（proxyctl 规则排在前面，优先生效）。

## 事务

21. 锁文件放在 `/var/lib/proxyctl/lock`（不放在 /etc），使用 `flock`，默认最多等待 30 秒。
    锁覆盖“读取 → 计算 → 写入 → reload”全过程，避免并发修改丢失更新（有测试用 6 个并发进程验证）。
22. 内容没有变化时不备份、不写入、不 reload（保证 init 幂等且没有副作用）。
23. dae 未运行时：通过 validate 后照常写入，不 reload，并明确提示“名单中的程序此刻直连外网”。
    proxyctl 不会启动 dae。
24. reload 之后会观察约 2 秒（`PROXYCTL_RELOAD_SETTLE`），期间状态必须保持 active（允许短暂的 reloading）。
25. 回滚时直接写回备份内容（不再 validate，因为它就是之前正在使用的配置）；如已 reload 则再次 reload。
    如果 dae 仍未恢复，只提示用户 `sudo systemctl restart dae`，不自动 restart（遵守“只 reload”的约束）。
26. 新配置文件权限为 0600、root:root（原来是 0640 也会变为 0600，这更严格，dae 接受）。
27. 备份文件名：`config-YYYYmmdd-HHMMSS-微秒-操作.dae`，0600，按文件名排序保留最近 30 份。
    `restore` 只接受符合这个格式的文件名（防止路径穿越），恢复同样走完整事务；恢复前的配置也会备份。

## 提权

28. 判断是否需要提权：当前用户不能读写配置文件、不能写配置目录或状态目录时才需要 root。
    这样测试时（配置在临时目录）无需 root，生产环境（/etc/dae）一定会提权。
29. 终端中（stdin 与 stderr 都是 tty）自动执行 `sudo -- env PROXYCTL_...=... python3 proxyctl <参数>`，
    保留 PROXYCTL_* 环境变量；非终端环境直接报错并给出应执行的 sudo 命令，不尝试无终端的 sudo。
    设置 `PROXYCTL_NO_ESCALATE=1` 可禁止自动提权（测试使用，保证测试绝不调用 sudo）。
30. `backups` 在备份目录不可读时也会自动 sudo（备份目录是 0700）。

## 非 root 读取规则

31. 配置文件是 0600，普通用户（以及 GUI）读不到。为了让 `list`、`rules` 和 GUI 显示状态，
    proxyctl 在以 root 读写配置后，把**只含进程名的名单**写入 `/var/lib/proxyctl/rules.json`（0644）。
    缓存中不包含 node、group、dns 等其他内容，也不包含非 pname 的 routing 规则。非 root 输出会注明“来自缓存”及更新时间。
    我没有选择把配置改成 0640 并改属组，因为那会改变 /etc 下文件的属性。

## list / check / test

32. “局域网地址”按 Python `ipaddress` 判断：回环、私有（含 IPv6 ULA fd00::/8）、链路本地、组播、未指定、保留地址都排除。
    IPv4 映射的 IPv6 地址（`::ffff:1.2.3.4`）按 IPv4 处理。没有进程信息的连接（非 root 看不到的）归为“(未知进程)”。
33. `check` 退出码：0 全部 OK，1 有 WARN，2 有 FAIL。
34. 定时器执行 `proxyctl check --quiet --notify --basic`：新增的 `--basic` 只检查 dae 状态、SOCKS 端口和代理核心进程，
    满足“定时器只做 dae 状态和端口检查”的要求，也避免每分钟因 IPv6 等 WARN 刷日志。
    systemd 服务中设置 `SuccessExitStatus=1 2`，检查发现问题不会让单元本身显示为失败。
35. 通知去重：同一组 FAIL 在 10 分钟内只弹一次通知（状态保存在 `$XDG_RUNTIME_DIR/proxyctl-notify.json`）；
    提醒过故障后恢复正常时，再发一条普通级别的“已恢复”通知并清除状态，下次故障会立即提醒。
35a. 检查间隔：最初为 60 秒。实测停止 dae 后 51 秒才提醒，期间名单程序在直连（故障即放行），
    因此改为 15 秒（每次检查约 0.2 秒）。为避免每 15 秒两条 Starting/Finished 日志刷屏，
    服务设置 `SyslogLevel=warning` + `LogLevelMax=warning`：检查输出的 WARN/FAIL 仍以 warning 级别记入日志，
    systemd 关于该单元的 info 级日志被丢弃。install.sh 会重启定时器使新间隔立即生效。
35b. **代理链路探测**（真实问题：重启后 dae、10808、xray 都正常，但 xray 连不上节点，名单程序全部断网，
    而定时器三项检查全部通过、没有任何提醒）：`check` 在 dae active 且端口监听时，经 SOCKS5 实际访问
    `https://www.gstatic.com/generate_204`（响应无内容，最轻量），超时 15 秒（实测节点较慢时单次请求要 4～7 秒）。
    只要拿到任何 HTTP 响应就算链路通。连续 2 次失败才判 FAIL 并通知，单次失败只 WARN；FAIL 文案固定，保证通知去重。
    定时器（`--basic`）在最近一次成功且不超过 120 秒时复用结果，避免每 15 秒产生一次外网请求；
    一旦失败，每次检查都重新探测。dae 或端口已经 FAIL 时不探测，并清除链路状态，恢复后重新计数。
    结果保存在 `$XDG_RUNTIME_DIR/proxyctl-chain.json`，GUI 只读取它（超过 10 分钟视为未知），自己不发请求。
    探测用的是 curl 本身（进程名 curl，不在任何名单中）并显式 `-x socks5h://`，不加 `--noproxy`。
35c. **桌面通知的发送方式**：实测本机 GNOME 的通知转发进程（gjs `org.gnome.Shell.Notifications`，
    持有 `org.freedesktop.Notifications` 名称）会接收 notify-send 的通知并返回 ID，却不显示，重新登录、重启都不能修复；
    而直接调用 GNOME Shell 自身导出的 `org.freedesktop.Notifications` 接口（`--dest org.gnome.Shell`）能正常弹出。
    notify-send 失败时也返回成功，无法据此回退，所以 `notify()` 先用 `gdbus call` 直接发给 GNOME Shell，
    调用失败（不是 GNOME、没有 gdbus）再退回 notify-send。参数按 GVariant 文本格式转义，
    测试用 GLib 的解析器校验参数类型与 Notify(susssasa{sv}i) 一致；测试环境使用假的 gdbus，不会向真实桌面发通知。
35d. **链路很慢**（真实问题：午夜高峰旧节点经代理每个新连接要 4～7 秒、已建立连接 RTT 约 650 ms 且频繁重传，
    Signal 反复离线，而 `proxyctl test` 与所有检查都通过；换节点后降到约 0.3 秒，Signal 立即恢复）：
    链路探测记录耗时，超过 3 秒记为偏慢，连续 3 次偏慢才 WARN（换节点后第一次连接本来就可能要 5 秒左右）。
    偏慢时不使用缓存，每次检查都重新探测，约 45 秒即可确认，也能尽快发现恢复。
    “很慢”不是故障，不进入 FAIL 通知，而是单独一条普通级别通知，30 分钟内只提醒一次，恢复后清除状态。
35e. **修改名单会让走代理的程序断线**（实测：reload 后 dae 日志 `tcp_conn_deleted=32 udp_conn_deleted=16`，
    Signal 离线约 33 秒到 1～2 分钟）：这是 dae reload 的行为，proxyctl 无法避免；
    因此每次 reload 成功后在输出中说明（GUI 对话框原样显示），内容没有变化时不 reload、不提示。
36. 端口监听判断用 TCP connect（可靠且不需要权限），监听进程用 `ss -ltnpH` 查询；
    进程是否存在读取 `/proc/*/comm`（非 root 也能看到所有进程名）。
37. IPv6 检查只把 2000::/3（全局单播）算作“全局 IPv6”；ULA（fd00::/8）虽然 scope 是 global，但不能直接上外网，不提醒。
    检查的接口为 IPv4/IPv6 默认路由所在接口，加上配置项 `iface` 中列出的接口（默认没有；最初写死了作者机器的 wlp3s0）。
38. `test` 调用 curl 时清除所有 `*_proxy` 环境变量；probe/direct 另加 `--noproxy '*'`，确保不受环境代理影响。
    第 1 步**不能**加 `--noproxy '*'`：实测它会让 curl 连 `-x` 指定的 SOCKS 也一起忽略而直连
    （最初版本有这个 bug，因为旧配置里 curl 在代理名单中被 dae 掩盖了，`remove curl` 后才暴露）。
    假 curl 已模拟这一行为，测试会捕获回归。
    返回内容必须是合法 IP 才算成功。第 3 步只要求“与代理 IP 不同”，本地直连可能返回 IPv6 地址，这也算通过。
    实测中第 1 步（经 SOCKS 直连 xray）偶发 10 秒超时，而同一时刻第 2 步正常，说明是节点/xray 抖动而非配置问题。
    因此每个请求最多尝试 3 次（每次仍 `--max-time 10`），重试后成功的会在输出中注明，持续失败才判 FAIL。

39a. **未代理子进程**（真实问题：ChatGPT 的登录由子进程 `codex` 完成，只加 ChatGPT 时登录被 403）：
    从 /proc/*/stat 构建进程树，以代理名单（含安全区块中的 proxy 规则）中的进程为起点，找出名字不同、
    且不在任何名单（proxy、direct、安全区块、不受管理的 pname 规则）中的子孙进程。遇到这样的进程后不再向下查找；
    遇到直连名单内的进程、通用运行时名称、不合法的名称也停止向下查找，以减少噪音（例如 codex 执行命令时启动的 bash）。
    它只是提示，不会自动加入：子进程不一定需要联网，把它们全部代理并不总是对的。
    名单中的名字按内核的 15 字符截断后比较。
39b. GUI 保留最近 5 分钟内出现过外网连接的进程（灰色、注明断开时间），状态按当前规则实时重算；
    CLI 的 list 是一次性快照，不保留历史。

## run / guard-desktop

39. `run` 的就绪条件：`systemctl is-active dae` 为 active 且 SOCKS 端口可连接。不就绪时每秒重试直到 `--wait` 超时，
    然后 notify-send 并以退出码 1 拒绝启动；就绪后用 exec 替换自身，这样被启动程序的进程名保持不变（pname 规则照常命中）。
40. `guard-desktop` 在系统目录（XDG_DATA_DIRS、flatpak、snap）中查找源文件；如果 `~/.local/share/applications/`
    中已有你自己的同名文件（不是 proxyctl 生成的），先另存为 `*.proxyctl-orig`，`--undo` 时恢复它。
    所有 `Exec=` 行（包括 Desktop Action）都会加守卫；生成的文件带 `X-Proxyctl-Guarded=true` 标记。重复执行幂等。
    若应用声明了 `DBusActivatable=true`，给出警告（桌面环境可能绕过 Exec）。
41. uninstall.sh 会撤销 guard-desktop 生成的副本，否则卸载后这些启动器会指向不存在的 proxyctl 而无法启动应用。

## GUI

42. 同一套代码兼容 GTK4 和 GTK3（优先 GTK4）。没有图形显示或没有 PyGObject 时打印清楚的错误并以退出码 1 退出。
43. GUI 的所有写操作都通过 `pkexec /usr/local/bin/proxyctl <子命令>` 在后台线程执行，完成后把输出原样显示在对话框里；
    GUI 本身只做只读查询（systemctl is-active、端口连接、ss、读规则缓存）。
44. “运行自检”以当前用户身份运行 `proxyctl test`（不需要 root）。
45. 联网进程表每 3 秒刷新，数据没有变化时不重建表格，避免闪烁和误点。
46. 环境变量 `PROXYCTL_GUI_AUTOQUIT=秒数` 用于测试：启动后自动退出。
    开发/测试用的还有 `PROXYCTL_GUI_DUMP=1`（退出时输出每行的按钮状态 JSON）、`PROXYCTL_GUI_SNAPSHOT=文件.png`
    （GTK4 下把窗口渲染成 PNG，用来检查排版）、`PROXYCTL_GUI_PAGE=rules`（直接打开指定页面）。
46a. **GUI 改版**（用户截图发现：远端示例过长把按钮挤出窗口；xray 这类直连保护行的“加入代理”可以点、点了被拒绝）：
    * 每行只给出真正可执行的操作（纯函数 `gui_row_actions`，有单元测试）：已代理→“移除代理”，
      直连保护→“取消保护”，安全区块中的固定规则→无按钮并标注“固定规则”，通用运行时名称的“加入代理”不可点并说明原因。
    * 远端示例放在最后一列并省略显示（悬停看全文），表格不出现横向滚动，按钮始终在窗口内；行高统一。
    * 页面切换放入标题栏，“运行自检”“立即刷新”在右上角；状态改为四个彩色胶囊，警告横幅为圆角色块；
      颜色使用 GNOME 调色板，深色与浅色主题下都清晰；systemd 状态显示为中文。
    * 联网进程页可按名字筛选、可隐藏已断开的进程；规则页标注每个名字是否正在运行，回车即可加入代理。
    * 执行特权操作时内容区变灰、底部显示进度，防止重复点击；结果对话框用等宽字体并可复制输出。
46b. **GUI 质感改版**（用户：功能没问题，但希望更简洁、更有质感）：
    * 有 libadwaita 时使用 Adw（窗口、ViewSwitcher、卡片列表 boxed-list、Clamp 限宽、Toast），颜色取主题语义色，
      跟随系统深浅色和强调色；没有 libadwaita 或 GTK3 时退回同样布局的普通 GTK 控件和固定调色板。
      `PROXYCTL_GUI_NO_ADW=1` 可强制不用 libadwaita（开发时检查退回路径）。
    * 四个状态胶囊＋横幅合并为一张状态卡：一句话结论、四项细节（彩色圆点）、出问题时的处理建议；卡片颜色即严重程度。
    * 联网进程由表格改为分组卡片列表，每行“进程名＋状态标签 / 连接数 · 远端示例”；图例说明和“显示已断开”开关去掉，
      改由分组标题和说明承担。只有“加入代理”是实心按钮，其余操作为扁平按钮，避免满屏蓝色按钮。
    * 规则页同样改为卡片分组，固定规则单独成组并说明为什么不能删除。
    * 成功的特权操作用 Toast 提示（可点“详情”看完整输出），取消授权只提示不弹窗，失败才弹出对话框；
      自检有 WARN 时标题注明“有警告”。快捷键：Ctrl+R 刷新、Ctrl+F 筛选、Ctrl+Q 退出。

46c. 应用图标 `proxyctl.svg`（GNOME 应用图标规格，128×128）：一条线路分岔，一支点亮为绿色（代理），一支为灰色空心端点（直连）。
    install.sh 装到 `/usr/share/icons/hicolor/scalable/apps/proxyctl.svg`，启动器 `Icon=proxyctl`，
    并以 `StartupWMClass=io.github.proxyctl.gui` 让 Dock 把窗口归到这个启动器；GUI 设置默认窗口图标名 proxyctl。

## 安装

47. notify-send、pkexec、PyGObject 缺失只警告；python3、dae、curl、ss、systemctl 缺失则中止安装（此时尚未修改任何东西）。
48. probe/direct 是 `readlink -f $(command -v curl)` 的副本（curl 是动态链接的，复制可执行文件即可运行）。
49. 用户定时器通过 `runuser -u $SUDO_USER -- systemctl --user ...` 启用；如果用户当前没有登录会话，只安装单元文件并提示手动启用。
50. install.sh 最后执行 `proxyctl init`；如果 init 失败，安装脚本以非零状态退出，配置保持原样或已自动回滚。
51. uninstall.sh 保留整个 `/var/lib/proxyctl/backups`，并打印最早一次 init 之前的备份名，方便还原。

## 配置文件

51a. 设置放在 `/etc/proxyctl.conf`，而不是只靠环境变量：sudo 默认清除环境变量，pkexec 和 systemd 用户定时器也不继承，
    只设环境变量会出现“终端里 check 正常、定时器却一直报端口没监听”这类不一致。优先级：环境变量 > 配置文件 > 内置默认值。
51b. 只接受白名单中的面向用户的键（socks、config、service、iface、test_url 等）；替换外部程序路径的
    `PROXYCTL_DAE_BIN`、`PROXYCTL_CURL` 等测试用变量**不能**写进配置文件，避免它成为让 root 执行任意程序的入口。
    文件必须属于 root（或当前用户，便于测试）且不可被组或其他用户写入，否则忽略并警告。
51c. install.sh 在 dae 配置中恰好找到一个 socks5 节点时，把它写成 `socks = …`；找不到或有多个时保留默认值的注释行。
    已存在的配置文件不覆盖。uninstall.sh 保留该文件。
51d. `check` 比较 proxyctl 的 SOCKS 地址与 dae 配置中出现的 socks5:// 地址（localhost 视为 127.0.0.1）；
    配置里有 socks5 节点但都不相同时报 WARN。没有任何 socks5 节点时不报（节点可能来自订阅或其他协议）。

## 测试

52. 真实 `dae validate`：非 root 下可以执行（已验证），因此作为测试的一部分；若将来在没有 dae 的机器上运行，该组测试自动跳过。
53. GUI 测试：`py_compile` 总是执行；有图形环境时启动 GUI 3 秒后自动退出，确认不崩溃。
54. 开发和测试过程中没有使用 sudo，没有启动/停止/reload dae，没有修改 /etc 或网络配置。
    对真实系统只做了只读操作：`systemctl is-active/show`、`ss`、读取 /proc、对本机 SOCKS 端口做 TCP 连接检测。
55. 测试数据 `tests/fixtures/ss_*.txt` 由真机 `ss` 输出脱敏而来：公网地址、局域网地址和部分进程名都已替换为虚构值。
