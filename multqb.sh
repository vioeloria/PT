#!/bin/bash
# qBittorrent 多开配置脚本
# 自动检测配置文件中实际存在的端口字段，无需手动输入版本号

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
qBittorrent 多开配置脚本

用法:
    $0 <实例数量> [起始WebUI端口] [用户名前缀] [基础用户名]
    $0                   # 进入交互模式
    $0 -h / --help

示例:
    $0 3                          # 创建3个实例，端口从8081起
    $0 2 8033                     # 创建2个实例，WebUI端口8033-8034
    $0 3 9000 qbuser heshui      # 完整参数

功能说明:
    - 自动检测基础配置文件中实际存在的 BT 端口字段，无需手动输入版本号
    - 支持 Connection\\PortRangeMin（主流写法）和 BitTorrent\\Session\\Port
    - 完整替换配置文件中的所有路径引用（含 [BitTorrent] 段）
    - 创建系统用户、独立配置目录、下载目录、systemd 服务

EOF
}

# ── 端口字段自动检测 ─────────────────────────────────────────────
# 直接读配置文件，检测哪个字段实际存在，不依赖用户输入版本号
detect_port_key() {
    local config_file="$1"

    if grep -q "^Connection\\\\PortRangeMin=" "$config_file"; then
        echo "Connection\\PortRangeMin"
    elif grep -q "^BitTorrent\\\\Session\\\\Port=" "$config_file"; then
        echo "BitTorrent\\Session\\Port"
    else
        echo "MISSING"
    fi
}

# 根据检测到的 key 读取端口值
read_port_by_key() {
    local config_file="$1"
    local key="$2"
    # key 中的 \ 在 grep/sed 里需要转义为 \\
    local escaped_key
    escaped_key=$(echo "$key" | sed 's/\\/\\\\/g')
    grep "^${escaped_key}=" "$config_file" | cut -d'=' -f2 | tr -d '\r'
}

# ── 端口占用检查 ─────────────────────────────────────────────────
check_port_free() {
    local port="$1"
    if ss -tulpn 2>/dev/null | grep -q ":${port} " || \
       netstat -tulpn 2>/dev/null | grep -q ":${port} "; then
        return 1   # 被占用
    fi
    return 0       # 空闲
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

# root 检查
[ "$EUID" -ne 0 ] && { error "需要 root 权限，请使用 sudo 运行"; exit 1; }

# qbittorrent-nox 检查
QB_NOX_PATH=$(which qbittorrent-nox 2>/dev/null)
[ -z "$QB_NOX_PATH" ] && { error "未找到 qbittorrent-nox，请先安装"; exit 1; }

# 参数处理
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

# 基础用户 / 配置校验
id -u "$BASE_USER" > /dev/null 2>&1 || { error "基础用户不存在: $BASE_USER"; exit 1; }
[ -d "$BASE_CONFIG_DIR" ]  || { error "基础配置目录不存在: $BASE_CONFIG_DIR"; exit 1; }
[ -f "$BASE_CONFIG_FILE" ] || { error "配置文件不存在: $BASE_CONFIG_FILE"; exit 1; }

# ── 自动检测端口字段 ────────────────────────────────────────────
PORT_KEY=$(detect_port_key "$BASE_CONFIG_FILE")

if [ "$PORT_KEY" = "MISSING" ]; then
    warn "配置文件中未找到已知的 BT 端口字段"
    warn "将使用默认值 6881 并在新配置中写入 Connection\\PortRangeMin"
    BASE_BT_PORT=6881
    PORT_KEY="Connection\\PortRangeMin"
else
    BASE_BT_PORT=$(read_port_by_key "$BASE_CONFIG_FILE" "$PORT_KEY")
    if [ -z "$BASE_BT_PORT" ]; then
        warn "读取端口值失败，使用默认值 6881"
        BASE_BT_PORT=6881
    fi
    info "自动检测端口字段: $PORT_KEY = $BASE_BT_PORT"
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

# ── 汇总确认 ────────────────────────────────────────────────────
echo ""
echo "========================================="
echo " qBittorrent 多开配置"
echo "========================================="
info "实例数量   : $NUM_INSTANCES"
info "起始端口   : $START_PORT"
info "用户前缀   : $USER_PREFIX"
info "基础用户   : $BASE_USER"
info "BT端口字段 : $PORT_KEY"
info "基础BT端口 : $BASE_BT_PORT"
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

    # 2. 确保 .config 目录存在（以用户身份创建，保证属主正确）
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

    # 4. 创建工作目录 / 下载目录
    sudo -u "$NEW_USER" mkdir -p "$NEW_HOME/qbittorrent/Downloads"
    info "工作目录: $NEW_HOME/qbittorrent/Downloads"

    # 5. 修改配置文件
    if [ -f "$NEW_CONFIG_FILE" ]; then
        info "修改配置文件..."

        # 5a. WebUI 端口
        sed -i "s/^WebUI\\\\Port=.*/WebUI\\\\Port=$NEW_WEBUI_PORT/" "$NEW_CONFIG_FILE"

        # 5b. BT 端口（使用检测到的实际字段）
        #     PORT_KEY 示例: "Connection\PortRangeMin" 或 "BitTorrent\Session\Port"
        #     在 sed 的替换模式中需要将 \ 转义为 \\
        ESCAPED_KEY=$(echo "$PORT_KEY" | sed 's/\\/\\\\/g')
        if grep -q "^${ESCAPED_KEY}=" "$NEW_CONFIG_FILE"; then
            sed -i "s/^${ESCAPED_KEY}=.*/${ESCAPED_KEY}=${NEW_BT_PORT}/" "$NEW_CONFIG_FILE"
        else
            # 字段不存在时追加到 [Preferences] 段后
            warn "未找到 $PORT_KEY 字段，尝试追加..."
            sed -i "/^\[Preferences\]/a ${ESCAPED_KEY}=${NEW_BT_PORT}" "$NEW_CONFIG_FILE"
        fi

        # 5c. 全文替换所有路径引用（涵盖 [Preferences] 和 [BitTorrent] 段）
        #     将 /home/<BASE_USER>/ 全部替换为 /home/<NEW_USER>/
        sed -i "s|/home/${BASE_USER}/|/home/${NEW_USER}/|g" "$NEW_CONFIG_FILE"

        # 5d. 验证关键字段
        VERIFY_WEBUI=$(grep "^WebUI\\\\Port=" "$NEW_CONFIG_FILE" | cut -d'=' -f2 | tr -d '\r')
        VERIFY_BT=$(grep "^${ESCAPED_KEY}=" "$NEW_CONFIG_FILE" | cut -d'=' -f2 | tr -d '\r')

        if [ "$VERIFY_WEBUI" = "$NEW_WEBUI_PORT" ] && [ "$VERIFY_BT" = "$NEW_BT_PORT" ]; then
            success "配置验证通过 (WebUI=$VERIFY_WEBUI, $PORT_KEY=$VERIFY_BT)"
        else
            warn "配置验证异常 (WebUI='$VERIFY_WEBUI'应为$NEW_WEBUI_PORT, BT='$VERIFY_BT'应为$NEW_BT_PORT)"
        fi
    else
        warn "配置文件不存在，跳过修改: $NEW_CONFIG_FILE"
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

# 端口分配表
info "📊 端口分配："
printf "   %-16s %-12s %-12s\n" "用户名" "WebUI端口" "$PORT_KEY"
echo "   ──────────────────────────────────────────"
for entry in "${PORT_ASSIGNMENTS[@]}"; do
    IFS='|' read -r uname wport bport <<< "$entry"
    printf "   %-16s %-12s %-12s\n" "$uname" "$wport" "$bport"
done

# BT 端口递增规则说明
echo ""
info "📋 BT 端口递增规则（基础 $BASE_BT_PORT，步长 2）："
for i in $(seq 1 "$NUM_INSTANCES"); do
    echo "   实例 $i (${USER_PREFIX}${i}): $((BASE_BT_PORT + i * 2))"
done

# 用户密码
echo ""
info "👤 用户信息（密码均为: $DEFAULT_PASSWORD）："
for uname in "${CREATED_USERS[@]}"; do
    echo "   $uname"
done

# Web 访问地址
echo ""
info "🌐 Web 界面访问地址："
for entry in "${PORT_ASSIGNMENTS[@]}"; do
    IFS='|' read -r uname wport _ <<< "$entry"
    echo "   $uname  →  http://$HOST_IP:$wport"
done

# 服务管理命令
echo ""
info "🚀 服务管理命令："

ALL_SERVICES="${CREATED_SERVICES[*]}"

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
echo "   # 单独操作示例（以第1个实例为例）"
echo "   systemctl start  ${CREATED_SERVICES[0]}"
echo "   systemctl stop   ${CREATED_SERVICES[0]}"
echo "   systemctl status ${CREATED_SERVICES[0]}"
echo "   journalctl -u ${CREATED_SERVICES[0]} -f"

echo ""