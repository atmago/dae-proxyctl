#!/bin/bash
# proxyctl 卸载脚本：需要 root。
# 不删除 /etc/dae/config.dae 中管理区块里的规则，也不删除 /var/lib/proxyctl 下的备份。
set -uo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "错误：uninstall.sh 需要 root 权限，请执行：sudo ./uninstall.sh" >&2
    exit 1
fi

BIN=/usr/local/bin/proxyctl
LIB=/usr/local/lib/proxyctl
STATE=/var/lib/proxyctl
DESKTOP=/usr/share/applications/proxyctl.desktop
ICON=/usr/share/icons/hicolor/scalable/apps/proxyctl.svg
DROPIN_DIR=/etc/systemd/system/dae.service.d
DROPIN="$DROPIN_DIR/proxyctl.conf"

echo "== 移除用户定时器与 guard-desktop 生成的启动器 =="
TARGET_USER="${SUDO_USER:-}"
if [ -n "$TARGET_USER" ] && [ "$TARGET_USER" != "root" ]; then
    USER_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
    USER_UID="$(id -u "$TARGET_USER")"
    UNIT_DIR="$USER_HOME/.config/systemd/user"
    if [ -d "/run/user/$USER_UID" ]; then
        runuser -u "$TARGET_USER" -- env XDG_RUNTIME_DIR="/run/user/$USER_UID" \
            DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$USER_UID/bus" \
            sh -c 'systemctl --user disable --now proxyctl-check.timer' 2>/dev/null || true
    fi
    rm -f "$UNIT_DIR/proxyctl-check.service" "$UNIT_DIR/proxyctl-check.timer" \
          "$UNIT_DIR/timers.target.wants/proxyctl-check.timer"
    if [ -d "/run/user/$USER_UID" ]; then
        runuser -u "$TARGET_USER" -- env XDG_RUNTIME_DIR="/run/user/$USER_UID" \
            DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$USER_UID/bus" \
            systemctl --user daemon-reload 2>/dev/null || true
    fi
    echo "  已移除 proxyctl-check.timer / .service"

    # guard-desktop 生成的覆盖副本在卸载后会指向不存在的 proxyctl，必须撤销
    APPS="$USER_HOME/.local/share/applications"
    if [ -d "$APPS" ]; then
        for f in "$APPS"/*.desktop; do
            [ -f "$f" ] || continue
            if grep -qx 'X-Proxyctl-Guarded=true' "$f"; then
                if [ -f "$f.proxyctl-orig" ]; then
                    mv -f "$f.proxyctl-orig" "$f"
                    echo "  已恢复你原来的 $f"
                else
                    rm -f "$f"
                    echo "  已删除启动守卫副本 $f"
                fi
            fi
        done
    fi
else
    echo "  [跳过] 未检测到 SUDO_USER，请以普通用户身份手动执行："
    echo "         systemctl --user disable --now proxyctl-check.timer"
    echo "         并删除 ~/.config/systemd/user/proxyctl-check.* 以及 ~/.local/share/applications 中含 X-Proxyctl-Guarded=true 的文件"
fi

echo "== 移除 systemd drop-in =="
if [ -f "$DROPIN" ]; then
    rm -f "$DROPIN"
    rmdir "$DROPIN_DIR" 2>/dev/null || true
    systemctl daemon-reload
    echo "  已移除 $DROPIN，dae 的 Restart 策略恢复为服务文件中的默认值"
fi

echo "== 移除程序与启动器 =="
rm -f "$BIN" "$DESKTOP" "$ICON"
gtk-update-icon-cache -q -t /usr/share/icons/hicolor 2>/dev/null || true
rm -rf "$LIB"
rm -f "$STATE/rules.json" "$STATE/lock"
echo "  已移除 $BIN、$LIB、$DESKTOP、$ICON"

cat <<EOF

========================================================================
卸载完成。以下内容被刻意保留：
  * /etc/dae/config.dae 中 proxyctl 管理区块里的规则（dae 会继续按名单分流）；
  * 配置备份：$STATE/backups/
  * proxyctl 配置文件：/etc/proxyctl.conf（如不再需要：sudo rm /etc/proxyctl.conf）；
EOF
if [ -d "$STATE/backups" ]; then
    first="$(ls -1 "$STATE/backups" 2>/dev/null | grep -- '-init\.dae$' | head -n 1 || true)"
    if [ -n "$first" ]; then
        echo "  最早一次 init 之前的原始配置备份：$STATE/backups/$first"
    fi
fi
cat <<EOF

如需还原配置：
  * 重新安装后执行：sudo proxyctl restore <备份名>（用 sudo proxyctl backups 查看）；
  * 或手工恢复：sudo install -m 0600 -o root -g root $STATE/backups/<备份名> /etc/dae/config.dae
               sudo dae validate -c /etc/dae/config.dae && sudo systemctl reload dae
如需彻底删除备份：sudo rm -rf $STATE
========================================================================
EOF
