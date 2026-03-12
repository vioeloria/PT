#!/bin/bash
# ================================================
# Unified BBR Variant Installer
# Supports: BBRy, BBRx, BBRz
# Based on: https://github.com/guowanghushifu/Seedbox-Components
# ================================================

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# ---- 选择要安装的模块 ----
echo -e "${CYAN}=============================${NC}"
echo -e "${CYAN}   BBR Variant Installer     ${NC}"
echo -e "${CYAN}=============================${NC}"
echo ""
echo "请选择要安装的 BBR 变体 / Select BBR variant to install:"
echo "  1) BBRy"
echo "  2) BBRx"
echo "  3) BBRz"
echo ""
read -rp "输入选项 [1-3]: " choice

case "$choice" in
    1)
        ALGO="bbry"
        BBR_FILE="tcp_bbry"
        SOURCE_URL="https://raw.githubusercontent.com/guowanghushifu/Seedbox-Components/main/BBR/BBRx/tcp_bbry.c"
        echo -e "${GREEN}[*] 将安装 BBRy${NC}"
        ;;
    2)
        ALGO="bbrx"
        BBR_FILE="tcp_bbrx"
        SOURCE_URL="https://raw.githubusercontent.com/guowanghushifu/Seedbox-Components/main/BBR/BBRx/tcp_bbrx.c"
        echo -e "${GREEN}[*] 将安装 BBRx${NC}"
        ;;
    3)
        ALGO="bbrz"
        BBR_FILE="tcp_bbrz"
        SOURCE_URL="https://raw.githubusercontent.com/guowanghushifu/Seedbox-Components/main/BBR/BBRx/tcp_bbrz.c"
        echo -e "${GREEN}[*] 将安装 BBRz${NC}"
        ;;
    *)
        echo -e "${RED}[!] 无效选项，退出。${NC}"
        exit 1
        ;;
esac

MODULE_VER="1.0.0"
BBR_SRC="${BBR_FILE}.c"

echo ""
echo -e "${YELLOW}[*] 开始安装，请稍候...${NC}"
sleep 3

cd "$HOME" || exit 1

# ---- 安装 dkms ----
if [ ! -x /usr/sbin/dkms ]; then
    echo "[*] 安装 dkms..."
    apt-get -y install dkms
    if [ ! -x /usr/sbin/dkms ]; then
        echo -e "${RED}[!] Error: dkms 安装失败${NC}" >&2
        exit 1
    fi
fi

# ---- 清理旧版本 ----
if dkms status | grep -q "${ALGO}/"; then
    for mod_ver in $(dkms status | grep "${ALGO}/" | awk -F, '{print $1}' | awk -F/ '{print $2}' | sort -u); do
        echo "[*] 移除旧模块 ${ALGO} v${mod_ver}..."
        dkms remove -m "$ALGO" -v "$mod_ver" --all
    done
fi

# ---- 安装内核头文件 ----
ARCH=$(dpkg --print-architecture 2>/dev/null || uname -m)
UNAME_R=$(uname -r)

if echo "$UNAME_R" | grep -q '\-cloud-'; then
    FLAVOR="cloud"
else
    FLAVOR="generic"
fi

case "$ARCH" in
    amd64)
        HEADER_META_PKG="linux-headers-${FLAVOR}-amd64"
        # cloud flavor uses different naming
        [ "$FLAVOR" = "cloud" ] && HEADER_META_PKG="linux-headers-cloud-amd64" || HEADER_META_PKG="linux-headers-amd64"
        ;;
    arm64|aarch64)
        [ "$FLAVOR" = "cloud" ] && HEADER_META_PKG="linux-headers-cloud-arm64" || HEADER_META_PKG="linux-headers-arm64"
        ;;
    *)
        HEADER_META_PKG=""
        ;;
esac

if [ -n "$HEADER_META_PKG" ]; then
    echo "[*] 安装内核头文件 meta 包: $HEADER_META_PKG"
    apt-get -y install "$HEADER_META_PKG"
fi

if [ ! -f "/usr/src/linux-headers-$(uname -r)/.config" ]; then
    if [[ -z $(apt-cache search "linux-headers-$(uname -r)") ]]; then
        echo -e "${RED}[!] Error: linux-headers-$(uname -r) 未找到${NC}" >&2
        exit 1
    fi
    echo "[*] 安装指定内核头文件: linux-headers-$(uname -r)"
    apt-get -y install "linux-headers-$(uname -r)"
    if [ ! -f "/usr/src/linux-headers-$(uname -r)/.config" ]; then
        echo -e "${RED}[!] Error: linux-headers-$(uname -r) 安装失败${NC}" >&2
        exit 1
    fi
fi

# ---- 下载源码 ----
echo "[*] 下载 ${ALGO} 源码..."
wget -O "$HOME/$BBR_SRC" "$SOURCE_URL"
if [ ! -f "$HOME/$BBR_SRC" ]; then
    echo -e "${RED}[!] Error: 源码下载失败，请检查 URL 是否正确:${NC}" >&2
    echo "  $SOURCE_URL" >&2
    echo ""
    echo -e "${YELLOW}提示: 请前往以下地址确认 .c 文件名称是否正确:${NC}"
    echo "  https://github.com/guowanghushifu/Seedbox-Components/tree/main/BBR/BBRx"
    exit 1
fi

# ---- 准备 DKMS 目录 ----
mkdir -p "$HOME/.bbr/src"
mv "$HOME/$BBR_SRC" "$HOME/.bbr/src/$BBR_SRC"
cd "$HOME/.bbr/src" || exit 1

# 创建 Makefile
cat > ./Makefile << EOF
obj-m:=${BBR_FILE}.o
EOF

# 创建 dkms.conf
cd "$HOME/.bbr" || exit 1
cat > ./dkms.conf << EOF
PACKAGE_NAME=${ALGO}
PACKAGE_VERSION=${MODULE_VER}
MAKE="make -C \${kernel_source_dir} M=\${dkms_tree}/${ALGO}/${MODULE_VER}/build/src modules"
CLEAN="make -C \${kernel_source_dir} M=\${dkms_tree}/${ALGO}/${MODULE_VER}/build/src clean"
BUILT_MODULE_NAME=${BBR_FILE}
BUILT_MODULE_LOCATION=src/
DEST_MODULE_LOCATION=/updates/net/ipv4
AUTOINSTALL=yes
EOF

# ---- DKMS 安装 ----
cp -R . "/usr/src/${ALGO}-${MODULE_VER}"

_dkms_fail() {
    echo -e "${RED}[!] $1 失败，清理并退出...${NC}"
    sed -i "/${BBR_FILE}/d" /etc/modules
    dkms remove -m "${ALGO}/${MODULE_VER}" --all 2>/dev/null
    exit 1
}

echo "[*] dkms add..."
dkms add -m "$ALGO" -v "$MODULE_VER" || _dkms_fail "dkms add"

echo "[*] dkms build..."
dkms build -m "$ALGO" -v "$MODULE_VER" || _dkms_fail "dkms build"

echo "[*] dkms install..."
dkms install -m "$ALGO" -v "$MODULE_VER" || _dkms_fail "dkms install"

# ---- 加载模块测试 ----
echo "[*] 测试加载模块..."
modprobe "$BBR_FILE"
if [ $? -ne 0 ]; then
    echo -e "${RED}[!] Error: 模块加载失败${NC}" >&2
    exit 1
fi

# ---- 开机自动加载 ----
sed -i "/${BBR_FILE}/d" /etc/modules
echo "$BBR_FILE" | tee -a /etc/modules

# ---- 应用 sysctl ----
sed -i '/net.core.default_qdisc/d' /etc/sysctl.conf
echo "net.core.default_qdisc = fq" >> /etc/sysctl.conf
sed -i '/net.ipv4.tcp_congestion_control/d' /etc/sysctl.conf
echo "net.ipv4.tcp_congestion_control = ${ALGO}" >> /etc/sysctl.conf
sysctl -p > /dev/null

# ---- 清理临时文件 ----
cd "$HOME"
rm -rf "$HOME/.bbr"

echo ""
echo -e "${GREEN}=============================${NC}"
echo -e "${GREEN}  ${ALGO} 安装成功！${NC}"
echo -e "${GREEN}=============================${NC}"
echo ""
echo -e "当前拥塞控制算法: ${CYAN}$(sysctl -n net.ipv4.tcp_congestion_control)${NC}"
echo -e "当前队列规则:     ${CYAN}$(sysctl -n net.core.default_qdisc)${NC}"
echo ""
echo -e "${YELLOW}系统将在 1 分钟后重启以完成安装...${NC}"
shutdown -r +1
