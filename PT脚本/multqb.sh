#!/bin/bash
# qBittorrent 多开配置脚本
# 自动识别 4.x / 5.x 配置格式差异，正确处理双端口字段

# ── 颜色 ────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()      { echo -e "${BLUE}[INFO]${NC} $1"; }
warn()      { echo -e "${YELLOW}[WARN]${NC} $1"; }
error()     { echo -e "${RED}[ERROR]${NC} $1"; }
success()   { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
need_input(){ echo -e "${YELLOW}[INPUT]${NC} $1"; }

# ── 帮助 ────────────────────────────────────────────────────────
show_help() {
    cat << EOF
qBittorrent 多开配置脚本（支持 4.x / 5.x）

用法:
    $0 <实例数量> [起始WebUI端口] [用户名前缀] [基础用户名]
    $0                   # 进入交互模式
    $0 -h / --help

示例:
    $0 3                          # 创建3个实例，WebUI端口从8081起
    $0 2 8033                     # 创建2个实例，WebUI端口8033-8034
    $0 3 9000 qbuser heshui      # 完整参数

版本差异说明:
    4.x: BT端口只有 [Preferences] Connection\PortRangeMin
         下载路径在 [Preferences] Downloads\SavePath

    5.x: BT端口有两处，两处都必须修改才能生效:
           [Preferences] Connection\PortRangeMin
           [BitTorrent]  Session\Port
         下载路径在 [BitTorrent] Session\DefaultSavePath

    脚本会自动检测格式并正确处理，无需手动输入版本号。

EOF
}

# ════════════════════════════════════════════════════════════════
# 配置格式检测
# 判断依据：5.x 在 [BitTorrent] 段里有 Session\Port 字段
# ════════════════════════════════════════════════════════════════
detect_config_version() {
    local config_file="$1"
    if grep -q "^Session\\\\Port=" "$config_file"; then
        echo "5x"
    else
        echo "4x"
    fi
}

# 读取指定字段的值（自动处理转义）
# 用法: read_field <文件> <字段名>
# 字段名示例: "Connection\PortRangeMin" 或 "Session\Port"
read_field() {
    local config_file="$1"
    local field="$2"
    local escaped
    escaped=$(echo "$field" | sed 's/\\/\\\\/g')
    grep "^${escaped}=" "$config_file" | cut -d'=' -f2- | tr -d '\r'
}

# 设置指定字段的值，字段不存在则追加到对应 section 末尾
# 用法: set_field <文件> <字段名> <值> <所属section>
set_field() {
    local config_file="$1"
    local field="$2"
    local value="$3"
    local section="$4"
    local escaped
    escaped=$(echo "$field" | sed 's/\\/\\\\/g')

    if grep -q "^${escaped}=" "$config_file"; then
        sed -i "s|^${escaped}=.*|${escaped}=${value}|" "$config_file"
    else
        warn "字段 $field 不存在，追加到 [$section] 段"
        sed -i "/^\[${section}\]/a ${escaped}=${value}" "$config_file"
    fi
}

# ── 端口占用检查 ─────────────────────────────────────────────────
check_port_free() {
    local port="$1"
    if ss -tulpn 2>/dev/null | grep -q ":${port}[[:space:]]" || \
       netstat -tulpn 2>/dev/null | grep -q ":${port}[[:space:]]"; then
        return 1
    fi
    return 0
}

# ── 创建系统用户 ─────────────────────────────────────────────────
create_system_user() {
    local username="$1"
    local password="$2"

    if id -u "$username" > /dev/null 2>&1; then
        warn "用户 $username 已存在，跳过创建"
        return 0
    fi

    useradd -m -s /bin/bash "$username" || { error "创建用户 $username 失败"; return 1; }
    echo "$username:$password" | chpasswd || { error "设置 $username 密码失败"; return 1; }
    chown -R "$username:$username" "/home/$username"
    success "用户 $username 创建成功（密码: $password）"
}

# ── 获取本机 IP ──────────────────────────────────────────────────
get_host_ip() {
    local ip
    ip=$(ip route get 1.1.1.1 2>/dev/null | grep -oP 'src \K\S+' | head -1)
    [ -z "$ip" ] && ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    [ -z "$ip" ] && ip="localhost"
    echo "$ip"
}

# ── 交互模式 ─────────────────────────────────────────────────────
interactive_input() {
    echo "========================================="
    echo " qBittorrent 多开配置 - 交互模式"
    echo "========================================="
    echo ""

    while true; do
        need_input "创建实例数量 (1-20): "
        read -r NUM_INSTANCES
        [[ "$NUM_INSTANCES" =~ ^[0-9]+$ ]] && \
            [ "$NUM_INSTANCES" -ge 1 ] && [ "$NUM_INSTANCES" -le 20 ] && break
        error "请输入 1-20 之间的整数"
    done

    while true; do
        need_input "WebUI 起始端口 (默认 8081): "
        read -r START_PORT
        [ -z "$START_PORT" ] && START_PORT=8081 && break
        [[ "$START_PORT" =~ ^[0-9]+$ ]] && \
            [ "$START_PORT" -ge 1024 ] && [ "$START_PORT" -le 65535 ] && break
        error "请输入 1024-65535 之间的端口号"
    done

    while true; do
        need_input "用户名前缀 (默认 heshui): "
        read -r USER_PREFIX
        [ -z "$USER_PREFIX" ] && USER_PREFIX="heshui" && break
        [[ "$USER_PREFIX" =~ ^[a-z][a-z0-9]*$ ]] && \
            [ "${#USER_PREFIX}" -le 20 ] && break
        error "只能包含小写字母和数字，以字母开头，长度 ≤ 20"
    done

    while true; do
        need_input "基础配置用户名 (默认 heshui): "
        read -r BASE_USER
        [ -z "$BASE_USER" ] && BASE_USER="heshui" && break
        id -u "$BASE_USER" > /dev/null 2>&1 && break
        error "用户 $BASE_USER 不存在，请输入已存在的用户名"
    done

    echo ""
    info "配置确认："
    info "  实例数量 : $NUM_INSTANCES"
    info "  起始端口 : $START_PORT"
    info "  用户前缀 : $USER_PREFIX"
    info "  基础用户 : $BASE_USER"
    echo ""
    while true; do
        need_input "确认以上配置? (y/n): "
        read -r confirm
        case $confirm in
            [Yy]*) break ;;
            [Nn]*) exit 0 ;;
            *) echo "请输入 y 或 n" ;;
        esac
    done
}

# ════════════════════════════════════════════════════════════════
# 主程序
# ════════════════════════════════════════════════════════════════

[ "$EUID" -ne 0 ] && { error "需要 root 权限，请使用 sudo 运行"; exit 1; }

QB_NOX_PATH=$(which qbittorrent-nox 2>/dev/null)
[ -z "$QB_NOX_PATH" ] && { error "未找到 qbittorrent-nox，请先安装"; exit 1; }

if [ $# -eq 0 ]; then
    interactive_input
elif [ "$1" = "-h" ] || [ "$1" = "--help" ]; then
    show_help; exit 0
else
    NUM_INSTANCES="$1"
    START_PORT="${2:-8081}"
    USER_PREFIX="${3:-heshui}"
    BASE_USER="${4:-heshui}"

    [[ "$NUM_INSTANCES" =~ ^[0-9]+$ ]] && \
        [ "$NUM_INSTANCES" -ge 1 ] && [ "$NUM_INSTANCES" -le 20 ] || \
        { error "实例数量必须是 1-20 之间的整数"; exit 1; }

    [[ "$START_PORT" =~ ^[0-9]+$ ]] && \
        [ "$START_PORT" -ge 1024 ] && [ "$START_PORT" -le 65535 ] || \
        { error "端口号必须在 1024-65535 之间"; exit 1; }

    [[ "$USER_PREFIX" =~ ^[a-z][a-z0-9]*$ ]] && \
        [ "${#USER_PREFIX}" -le 20 ] || \
        { error "用户名前缀只能包含小写字母和数字，以字母开头，长度 ≤ 20"; exit 1; }
fi

DEFAULT_PASSWORD="1wuhongli"
BASE_HOME="/home/$BASE_USER"
BASE_CONFIG_DIR="$BASE_HOME/.config/qBittorrent"
BASE_CONFIG_FILE="$BASE_CONFIG_DIR/qBittorrent.conf"

id -u "$BASE_USER" > /dev/null 2>&1 || { error "基础用户不存在: $BASE_USER"; exit 1; }
[ -d "$BASE_CONFIG_DIR" ]  || { error "基础配置目录不存在: $BASE_CONFIG_DIR"; exit 1; }
[ -f "$BASE_CONFIG_FILE" ] || { error "配置文件不存在: $BASE_CONFIG_FILE"; exit 1; }

# ── 自动检测配置版本格式 ────────────────────────────────────────
CONFIG_VER=$(detect_config_version "$BASE_CONFIG_FILE")

if [ "$CONFIG_VER" = "5x" ]; then
    # 5.x：[BitTorrent] Session\Port 是实际生效的端口
    BASE_BT_PORT=$(read_field "$BASE_CONFIG_FILE" "Session\\Port")
    [ -z "$BASE_BT_PORT" ] && BASE_BT_PORT=6881 && warn "未读到 Session\\Port，使用默认 6881"
    info "检测到 5.x 配置格式"
    info "BT端口来源: [BitTorrent] Session\\Port = $BASE_BT_PORT"
    info "（同时会同步更新 [Preferences] Connection\\PortRangeMin）"
else
    # 4.x：[Preferences] Connection\PortRangeMin
    BASE_BT_PORT=$(read_field "$BASE_CONFIG_FILE" "Connection\\PortRangeMin")
    [ -z "$BASE_BT_PORT" ] && BASE_BT_PORT=6881 && warn "未读到 Connection\\PortRangeMin，使用默认 6881"
    info "检测到 4.x 配置格式"
    info "BT端口来源: [Preferences] Connection\\PortRangeMin = $BASE_BT_PORT"
fi

# ── 端口冲突预检 ────────────────────────────────────────────────
info "检查端口占用情况..."
CONFLICT=()
for i in $(seq 1 "$NUM_INSTANCES"); do
    WEBUI_PORT=$((START_PORT + i - 1))
    BT_PORT=$((BASE_BT_PORT + i * 2))
    check_port_free "$WEBUI_PORT" || CONFLICT+=("WebUI 端口 $WEBUI_PORT 已被占用")
    check_port_free "$BT_PORT"    || CONFLICT+=("BT 端口 $BT_PORT 已被占用")
done

if [ ${#CONFLICT[@]} -gt 0 ]; then
    error "发现端口冲突，请更换起始端口或释放占用："
    for msg in "${CONFLICT[@]}"; do echo "   ✗ $msg"; done
    exit 1
fi
success "端口检查通过"

echo ""
echo "========================================="
echo " qBittorrent 多开配置"
echo "========================================="
info "配置格式   : $CONFIG_VER"
info "实例数量   : $NUM_INSTANCES"
info "起始端口   : $START_PORT"
info "用户前缀   : $USER_PREFIX"
info "基础用户   : $BASE_USER"
info "基础BT端口 : $BASE_BT_PORT  →  实例1=$((BASE_BT_PORT+2))  实例2=$((BASE_BT_PORT+4)) ..."
info "qb路径     : $QB_NOX_PATH"
info "默认密码   : $DEFAULT_PASSWORD"
echo ""

# ════════════════════════════════════════════════════════════════
# 逐实例创建
# ════════════════════════════════════════════════════════════════
CREATED_USERS=()
CREATED_SERVICES=()
PORT_ASSIGNMENTS=()

for i in $(seq 1 "$NUM_INSTANCES"); do
    NEW_USER="${USER_PREFIX}${i}"
    NEW_HOME="/home/$NEW_USER"
    NEW_CONFIG_DIR="$NEW_HOME/.config/qBittorrent"
    NEW_CONFIG_FILE="$NEW_CONFIG_DIR/qBittorrent.conf"
    NEW_WEBUI_PORT=$((START_PORT + i - 1))
    NEW_BT_PORT=$((BASE_BT_PORT + i * 2))

    echo "━━━ 实例 $i / $NUM_INSTANCES : $NEW_USER ━━━"

    # 1. 创建系统用户
    create_system_user "$NEW_USER" "$DEFAULT_PASSWORD" || continue
    CREATED_USERS+=("$NEW_USER")

    # 2. 确保 .config 目录存在
    sudo -u "$NEW_USER" mkdir -p "$NEW_HOME/.config"

    # 3. 复制基础配置目录
    info "复制配置目录 -> $NEW_CONFIG_DIR"
    if command -v rsync > /dev/null 2>&1; then
        rsync -a "$BASE_CONFIG_DIR/" "$NEW_CONFIG_DIR/"
    else
        cp -r "$BASE_CONFIG_DIR" "$NEW_HOME/.config/"
    fi
    chown -R "$NEW_USER:$NEW_USER" "$NEW_CONFIG_DIR"
    success "配置目录复制完成"

    # 4. 创建下载目录
    sudo -u "$NEW_USER" mkdir -p "$NEW_HOME/qbittorrent/Downloads"
    info "下载目录: $NEW_HOME/qbittorrent/Downloads"

    # 5. 修改配置文件
    if [ ! -f "$NEW_CONFIG_FILE" ]; then
        warn "配置文件不存在，跳过修改: $NEW_CONFIG_FILE"
    else
        info "修改配置文件（$CONFIG_VER 格式）..."

        # 5a. WebUI 端口（所有版本相同）
        set_field "$NEW_CONFIG_FILE" "WebUI\\Port" "$NEW_WEBUI_PORT" "Preferences"

        if [ "$CONFIG_VER" = "5x" ]; then
            # 5.x：两处 BT 端口都要改
            # [BitTorrent] Session\Port ← 实际控制监听端口
            set_field "$NEW_CONFIG_FILE" "Session\\Port" "$NEW_BT_PORT" "BitTorrent"
            # [Preferences] Connection\PortRangeMin ← 同步，避免 WebUI 显示不一致
            set_field "$NEW_CONFIG_FILE" "Connection\\PortRangeMin" "$NEW_BT_PORT" "Preferences"
        else
            # 4.x：只有 [Preferences] Connection\PortRangeMin
            set_field "$NEW_CONFIG_FILE" "Connection\\PortRangeMin" "$NEW_BT_PORT" "Preferences"
        fi

        # 5b. 关闭随机端口（两个版本都处理，防止安装时被默认开启）
        set_field "$NEW_CONFIG_FILE" "Connection\\UseRandomPort" "false" "Preferences"

        # 5c. 全文替换所有路径引用（覆盖所有 section）
        sed -i "s|/home/${BASE_USER}/|/home/${NEW_USER}/|g" "$NEW_CONFIG_FILE"

        # 5d. 验证
        VERIFY_WEBUI=$(read_field "$NEW_CONFIG_FILE" "WebUI\\Port")
        VERIFY_RAND=$(read_field  "$NEW_CONFIG_FILE" "Connection\\UseRandomPort")
        VERIFY_OK=true

        if [ "$CONFIG_VER" = "5x" ]; then
            VERIFY_SESSION=$(read_field "$NEW_CONFIG_FILE" "Session\\Port")
            VERIFY_PREF=$(read_field    "$NEW_CONFIG_FILE" "Connection\\PortRangeMin")
            [ "$VERIFY_WEBUI"   != "$NEW_WEBUI_PORT" ] && VERIFY_OK=false
            [ "$VERIFY_SESSION" != "$NEW_BT_PORT"    ] && VERIFY_OK=false
            [ "$VERIFY_PREF"    != "$NEW_BT_PORT"    ] && VERIFY_OK=false
            [ "$VERIFY_RAND"    != "false"            ] && VERIFY_OK=false
            if $VERIFY_OK; then
                success "验证通过: WebUI=$VERIFY_WEBUI | Session\\Port=$VERIFY_SESSION | PortRangeMin=$VERIFY_PREF | UseRandomPort=false"
            else
                warn "验证异常: WebUI='$VERIFY_WEBUI'(应$NEW_WEBUI_PORT) Session\\Port='$VERIFY_SESSION'(应$NEW_BT_PORT) PortRangeMin='$VERIFY_PREF'(应$NEW_BT_PORT) UseRandomPort='$VERIFY_RAND'(应false)"
            fi
        else
            VERIFY_BT=$(read_field "$NEW_CONFIG_FILE" "Connection\\PortRangeMin")
            [ "$VERIFY_WEBUI" != "$NEW_WEBUI_PORT" ] && VERIFY_OK=false
            [ "$VERIFY_BT"    != "$NEW_BT_PORT"    ] && VERIFY_OK=false
            [ "$VERIFY_RAND"  != "false"            ] && VERIFY_OK=false
            if $VERIFY_OK; then
                success "验证通过: WebUI=$VERIFY_WEBUI | PortRangeMin=$VERIFY_BT | UseRandomPort=false"
            else
                warn "验证异常: WebUI='$VERIFY_WEBUI'(应$NEW_WEBUI_PORT) PortRangeMin='$VERIFY_BT'(应$NEW_BT_PORT) UseRandomPort='$VERIFY_RAND'(应false)"
            fi
        fi
    fi

    # 6. 创建 systemd 服务
    SERVICE_NAME="qbittorrent-${NEW_USER}"
    SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
    info "创建服务: $SERVICE_FILE"

    cat > "$SERVICE_FILE" << EOF
[Unit]
Description=qBittorrent Daemon for $NEW_USER
After=network.target

[Service]
Type=forking
User=$NEW_USER
Group=$NEW_USER
UMask=0002
LimitNOFILE=infinity
ExecStart=$QB_NOX_PATH -d --webui-port=$NEW_WEBUI_PORT
ExecStop=/usr/bin/killall -w -s 9 $QB_NOX_PATH
Restart=on-failure
TimeoutStopSec=20
RestartSec=10
WorkingDirectory=$NEW_HOME

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME"
    CREATED_SERVICES+=("$SERVICE_NAME")
    PORT_ASSIGNMENTS+=("${NEW_USER}|${NEW_WEBUI_PORT}|${NEW_BT_PORT}")

    success "实例 $NEW_USER 配置完成"
    echo ""
done

# ════════════════════════════════════════════════════════════════
# 汇总报告
# ════════════════════════════════════════════════════════════════
HOST_IP=$(get_host_ip)

echo "========================================="
success "🎉 完成！共创建 ${#CREATED_USERS[@]} / $NUM_INSTANCES 个实例"
echo "========================================="
echo ""

if [ ${#CREATED_USERS[@]} -eq 0 ]; then
    warn "没有成功创建任何实例，请检查以上错误信息"
    exit 1
fi

if [ "$CONFIG_VER" = "5x" ]; then
    BT_LABEL="BT端口(Session\\Port & PortRangeMin)"
else
    BT_LABEL="BT端口(PortRangeMin)"
fi

info "📊 端口分配："
printf "   %-16s %-12s %-s\n" "用户名" "WebUI端口" "$BT_LABEL"
echo "   ──────────────────────────────────────────────────────"
for entry in "${PORT_ASSIGNMENTS[@]}"; do
    IFS='|' read -r uname wport bport <<< "$entry"
    printf "   %-16s %-12s %-s\n" "$uname" "$wport" "$bport"
done

echo ""
info "📋 BT 端口递增（基础 $BASE_BT_PORT，步长 2）："
for i in $(seq 1 "$NUM_INSTANCES"); do
    echo "   实例 $i (${USER_PREFIX}${i}): $((BASE_BT_PORT + i * 2))"
done

echo ""
info "👤 用户信息（密码均为: $DEFAULT_PASSWORD）："
for uname in "${CREATED_USERS[@]}"; do
    echo "   $uname"
done

echo ""
info "🌐 Web 界面访问地址："
for entry in "${PORT_ASSIGNMENTS[@]}"; do
    IFS='|' read -r uname wport _ <<< "$entry"
    echo "   $uname  →  http://$HOST_IP:$wport"
done

ALL_SERVICES="${CREATED_SERVICES[*]}"

echo ""
info "🚀 服务管理命令："
echo ""
echo "   # 启动全部"
echo "   systemctl start $ALL_SERVICES"
echo ""
echo "   # 停止全部"
echo "   systemctl stop $ALL_SERVICES"
echo ""
echo "   # 重启全部"
echo "   systemctl restart $ALL_SERVICES"
echo ""
echo "   # 查看全部状态"
echo "   systemctl status $ALL_SERVICES"
echo ""
echo "   # 实时日志（以第1个实例为例）"
echo "   journalctl -u ${CREATED_SERVICES[0]} -f"
echo ""
