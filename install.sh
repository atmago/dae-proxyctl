#!/bin/bash
# proxyctl 安装脚本：需要 root，可重复执行。
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "错误：install.sh 需要 root 权限，请执行：sudo ./install.sh" >&2
    exit 1
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN=/usr/local/bin/proxyctl
LIB=/usr/local/lib/proxyctl
STATE=/var/lib/proxyctl
DESKTOP=/usr/share/applications/proxyctl.desktop
ICON=/usr/share/icons/hicolor/scalable/apps/proxyctl.svg
DROPIN_DIR=/etc/systemd/system/dae.service.d
DROPIN="$DROPIN_DIR/proxyctl.conf"
CONF=/etc/proxyctl.conf

echo "== 检查依赖 =="
missing=0
for cmd in python3 dae curl ss systemctl; do
    if command -v "$cmd" >/dev/null 2>&1; then
        echo "  [OK]   $cmd -> $(command -v "$cmd")"
    else
        echo "  [缺少] $cmd"
        missing=1
    fi
done
if ! command -v notify-send >/dev/null 2>&1; then
    echo "  [警告] 缺少 notify-send（sudo apt install libnotify-bin），桌面通知将不可用。"
fi
if ! command -v pkexec >/dev/null 2>&1; then
    echo "  [警告] 缺少 pkexec（sudo apt install pkexec），GUI 中的写操作将不可用。"
fi
if ! python3 -c 'import gi' >/dev/null 2>&1; then
    echo "  [警告] 缺少 PyGObject（sudo apt install python3-gi gir1.2-gtk-4.0），GUI 不可用，CLI 不受影响。"
fi
if [ "$missing" -ne 0 ]; then
    echo "错误：缺少必需的依赖，已中止安装（没有做任何修改）。" >&2
    exit 1
fi

echo "== 安装程序 =="
install -D -m 0755 -o root -g root "$SRC_DIR/proxyctl" "$BIN"
echo "  $BIN"
install -d -m 0755 -o root -g root "$LIB"
CURL_REAL="$(readlink -f "$(command -v curl)")"
install -m 0755 -o root -g root "$CURL_REAL" "$LIB/proxyctl-probe"
install -m 0755 -o root -g root "$CURL_REAL" "$LIB/proxyctl-direct"
echo "  $LIB/proxyctl-probe、$LIB/proxyctl-direct（$CURL_REAL 的副本）"

install -d -m 0755 -o root -g root "$STATE"
install -d -m 0700 -o root -g root "$STATE/backups"
echo "  $STATE/backups（0700）"

echo "== 配置文件 =="
if [ -f "$CONF" ]; then
    echo "  $CONF 已存在，保持不变"
else
    # dae 配置中恰好有一个 socks5 节点时，直接用它作为 proxyctl 检查的 SOCKS 地址
    SOCKS_FOUND="$(grep -v '^[[:space:]]*#' /etc/dae/config.dae 2>/dev/null \
        | grep -oE "socks5h?://([^@'\" ]*@)?[^'\" /#?]+" \
        | sed -E 's#^socks5h?://([^@]*@)?##; s#^localhost:#127.0.0.1:#' | sort -u || true)"
    if [ -n "$SOCKS_FOUND" ] && [ "$(printf '%s\n' "$SOCKS_FOUND" | wc -l)" -eq 1 ]; then
        SOCKS_LINE="socks = $SOCKS_FOUND"
        echo "  从 dae 配置中识别到 SOCKS5 节点：$SOCKS_FOUND"
    else
        SOCKS_LINE="# socks = 127.0.0.1:10808"
        echo "  [提示] 未能从 dae 配置中唯一识别 SOCKS5 节点，使用默认 127.0.0.1:10808；如不同请编辑 $CONF"
    fi
    cat > "$CONF.tmp" <<EOF
# proxyctl 配置。修改后立即生效（包括 GUI 和健康检查定时器），无需重新安装。
# 环境变量 PROXYCTL_<大写键名> 优先于这里的设置。

# 代理核心（xray / sing-box / mihomo）的本地 SOCKS5 地址，应与 dae 配置中的 socks5 节点一致。
# v2rayN 默认 127.0.0.1:10808；Clash / mihomo 常见 127.0.0.1:7891。
$SOCKS_LINE

# dae 配置文件与服务名
# config = /etc/dae/config.dae
# service = dae

# 除默认路由接口外，还要检查 IPv6 的接口（逗号分隔）
# iface =

# test 用来获取出口 IP 的地址；check 用来探测代理链路的地址
# test_url = https://ifconfig.me/ip
# probe_url = https://www.gstatic.com/generate_204

# 链路探测：正常时每隔多少秒真正探测一次；单次超过多少秒算慢
# chain_interval = 120
# slow_seconds = 3
# test 中新建代理连接的中位耗时超过多少秒给出警告；每个请求最多尝试几次
# connect_slow = 1.5
# test_retries = 3

# GUI 中保留最近断开进程的秒数
# gui_recent = 300
EOF
    chmod 0644 "$CONF.tmp"
    chown root:root "$CONF.tmp"
    mv -f "$CONF.tmp" "$CONF"
    echo "  $CONF"
fi

echo "== 安装图形界面启动器 =="
# 先装图标再写启动器：GNOME Shell 一看到启动器变化就会去找图标，找不到会缓存旧图标直到重新登录
install -D -m 0644 "$SRC_DIR/proxyctl.svg" "$ICON"
gtk-update-icon-cache -q -t /usr/share/icons/hicolor 2>/dev/null || true
echo "  $ICON"
cat > "$DESKTOP.tmp" <<'EOF'
[Desktop Entry]
Type=Application
Name=proxyctl
Name[zh_CN]=按应用代理（proxyctl）
Comment=Manage per-application proxy rules for dae
Comment[zh_CN]=管理 dae 的按应用代理名单
Exec=/usr/local/bin/proxyctl gui
Icon=proxyctl
StartupWMClass=io.github.proxyctl.gui
Terminal=false
Categories=Network;Settings;
EOF
chmod 0644 "$DESKTOP.tmp"
mv -f "$DESKTOP.tmp" "$DESKTOP"
echo "  $DESKTOP"

echo "== systemd drop-in：dae 异常退出时自动重启 =="
install -d -m 0755 "$DROPIN_DIR"
printf '[Service]\nRestart=on-failure\n' > "$DROPIN.tmp"
chmod 0644 "$DROPIN.tmp"
mv -f "$DROPIN.tmp" "$DROPIN"
systemctl daemon-reload
echo "  $DROPIN（Restart=on-failure），已执行 systemctl daemon-reload"

echo "== 为登录用户安装健康检查定时器（每 15 秒） =="
TARGET_USER="${SUDO_USER:-}"
if [ -z "$TARGET_USER" ] || [ "$TARGET_USER" = "root" ]; then
    echo "  [跳过] 未检测到 SUDO_USER（请用 sudo 从你的普通用户执行），未安装用户定时器。"
else
    USER_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
    USER_UID="$(id -u "$TARGET_USER")"
    USER_GID="$(id -g "$TARGET_USER")"
    UNIT_DIR="$USER_HOME/.config/systemd/user"
    install -d -m 0755 -o "$USER_UID" -g "$USER_GID" "$USER_HOME/.config" "$USER_HOME/.config/systemd" "$UNIT_DIR"
    cat > "$UNIT_DIR/proxyctl-check.service" <<'EOF'
[Unit]
Description=proxyctl 健康检查（dae 状态与代理端口）

[Service]
Type=oneshot
ExecStart=/usr/local/bin/proxyctl check --quiet --notify --basic
# 退出码 1=有 WARN，2=有 FAIL；这是检查结果而不是服务故障
SuccessExitStatus=1 2
# 检查结果以 warning 级别记录；丢弃每 15 秒一次的 Starting/Finished（info 级别）
SyslogLevel=warning
LogLevelMax=warning
EOF
    cat > "$UNIT_DIR/proxyctl-check.timer" <<'EOF'
[Unit]
Description=每 15 秒运行一次 proxyctl 健康检查

[Timer]
OnBootSec=30
OnUnitActiveSec=15
AccuracySec=1

[Install]
WantedBy=timers.target
EOF
    chown "$USER_UID:$USER_GID" "$UNIT_DIR/proxyctl-check.service" "$UNIT_DIR/proxyctl-check.timer"
    chmod 0644 "$UNIT_DIR/proxyctl-check.service" "$UNIT_DIR/proxyctl-check.timer"
    if [ -d "/run/user/$USER_UID" ]; then
        if runuser -u "$TARGET_USER" -- env XDG_RUNTIME_DIR="/run/user/$USER_UID" \
                DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$USER_UID/bus" \
                sh -c 'systemctl --user daemon-reload && systemctl --user enable proxyctl-check.timer && systemctl --user restart proxyctl-check.timer'; then
            echo "  已为用户 $TARGET_USER 启用 proxyctl-check.timer"
        else
            echo "  [警告] 启用用户定时器失败，请以 $TARGET_USER 身份手动执行："
            echo "         systemctl --user daemon-reload && systemctl --user enable --now proxyctl-check.timer"
        fi
    else
        echo "  [警告] 用户 $TARGET_USER 当前没有登录会话，请登录后执行："
        echo "         systemctl --user daemon-reload && systemctl --user enable --now proxyctl-check.timer"
    fi
fi

echo "== 初始化 dae 配置（proxyctl init） =="
if ! "$BIN" init; then
    echo "错误：proxyctl init 失败（配置未被修改或已自动回滚）。请根据上面的提示处理后重新执行 sudo ./install.sh。" >&2
    exit 1
fi

cat <<EOF

========================================================================
安装完成。后续步骤：
  1. 端到端自检：            proxyctl test
  2. 完整健康检查：          sudo proxyctl check
  3. 查看正在联网的进程：    proxyctl list      （或打开“按应用代理（proxyctl）”图形界面）
  4. 加入代理名单：          proxyctl add <进程名>
  5. 给 Signal 加启动守卫：  proxyctl guard-desktop signal-desktop.desktop
     （以你自己的身份执行，不要加 sudo）

注意：本脚本没有执行 systemctl enable dae，是否开机自启由你决定。
EOF
if [ "$(systemctl is-enabled dae 2>/dev/null || true)" = "enabled" ]; then
    echo "  当前状态：dae 已设置为开机自启（enabled）。"
else
    echo "  当前状态：dae 未设置开机自启。如需开机自启：sudo systemctl enable dae"
fi
echo "  dae 未运行时，代理名单中的程序会直接连接外网（故障即放行）。"
echo "========================================================================"
