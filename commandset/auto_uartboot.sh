#!/bin/bash

set -e

# 固定路径配置（相对于cicd目录）
DEFAULT_UART_OPT="mcu goto uart"
DEVICE_CONFIG_PATH="/dev/serial/by-name/cicd-vw/device.json"
BSP_BASE_URL="https://jfrog.carizon.work/artifactory/project-snapshot-local/Dev/Common/j6/bsp/daily/Release/1230"
BSP_API_URL="https://jfrog.carizon.work/ui/api/v1/ui/nativeBrowser/project-snapshot-local/Dev/Common/j6/bsp/daily/Release/1230"

# 函数：根据hostname获取对应的固件包路径
get_firmware_path_by_hostname() {
    local hostname="$1"
    
    case "$hostname" in
        "j6m-lite-ngx-b2")
            echo "../config/mcu_firmware/uart_LiteB2"
            ;;
        "j6m-lite-ngx-b1")
            echo "../config/mcu_firmware/uart_LiteB1"
            ;;
        "j6m-pro-acar-a1")
            echo "../config/mcu_firmware/uart_Acar"
            ;;
        *)
            # 默认使用Acar固件包，并给出警告
            echo "Warning: 未知的hostname '$hostname'，使用默认固件包" >&2
            echo "../config/mcu_firmware/uart_Acar_0715"
            ;;
    esac
}

# 函数：获取主机名
get_hostname() {
    if [[ -f "$DEVICE_CONFIG_PATH" ]]; then
        hostname=$(python3 -c "
import json
try:
    with open('$DEVICE_CONFIG_PATH', 'r') as f:
        data = json.load(f)
    print(data.get('hostname', ''))
except:
    print('')
")
        if [[ -n "$hostname" ]]; then
            echo "$hostname"
            return 0
        fi
    fi
    
    echo "Error: 无法从 $DEVICE_CONFIG_PATH 获取主机名" >&2
    return 1
}

# 函数：获取最新BSP包
get_latest_bsp_package() {
    # 检查网络连接（静默）
    if ! curl -s --connect-timeout 10 --max-time 30 "$BSP_API_URL" > /dev/null 2>&1; then
        echo "Error: 无法连接到BSP仓库，请检查网络连接" >&2
        return 1
    fi
    
    # 获取最新包名（重定向stderr避免干扰输出）
    local latest_package=$(curl -s "$BSP_API_URL" 2>/dev/null | \
        python3 -c "
import json
import sys
import re

try:
    data = json.load(sys.stdin)
    files = data.get('children', [])
    
    # 筛选BSP包文件
    bsp_files = []
    for file in files:
        name = file.get('name', '')
        if name.endswith('-daily.zip') and 'Dev_Common_J6E-1230_BSP_V' in name:
            # 提取时间戳
            match = re.search(r'(\d{14})-daily\.zip$', name)
            if match:
                timestamp = match.group(1)
                bsp_files.append((name, timestamp))
    
    # 按时间戳排序，获取最新的
    if bsp_files:
        bsp_files.sort(key=lambda x: x[1], reverse=True)
        print(bsp_files[0][0])
    else:
        print('')
except Exception as e:
    sys.exit(1)
" 2>/dev/null)
    
    if [[ -z "$latest_package" ]]; then
        echo "Error: 无法获取最新BSP包信息" >&2
        return 1
    fi
    
    echo "$latest_package"
}

# 函数：下载并解压BSP包
download_and_extract_bsp() {
    local package_name="$1"
    local download_url="$BSP_BASE_URL/$package_name"
    local firmware_dir=$(dirname "$MCU_FIRMWARE_PATH")
    local bsp_dir="$firmware_dir/BSP"
    
    echo "正在下载BSP包: $package_name"
    echo "下载地址: $download_url"
    
    # 检查磁盘空间（BSP包通常2-3GB）
    local available_space=$(df "$firmware_dir" | awk 'NR==2 {print $4}')
    if [[ $available_space -lt 3145728 ]]; then  # 3GB in KB
        echo "Warning: 磁盘可用空间不足3GB，可能影响BSP包下载和解压" >&2
    fi
    
    # 创建BSP目录
    mkdir -p "$bsp_dir"
    
    # 下载BSP包到固件目录同级
    local package_path="$firmware_dir/$package_name"
    echo "开始下载，请稍候..."
    if ! curl -L --progress-bar --connect-timeout 30 --max-time 1800 -o "$package_path" "$download_url"; then
        echo "Error: BSP包下载失败" >&2
        echo "下载URL: $download_url" >&2
        # 清理可能的部分下载文件
        [[ -f "$package_path" ]] && rm "$package_path"
        return 1
    fi
    
    echo "正在解压BSP包到: $bsp_dir"
    # 解压到BSP目录
    if ! unzip -q -o "$package_path" -d "$bsp_dir"; then
        echo "Error: BSP包解压失败" >&2
        # 清理下载的文件
        rm "$package_path" 2>/dev/null
        return 1
    fi
    
    # 删除压缩包
    rm "$package_path"
    echo "BSP包解压完成，压缩包已删除"
    
    echo "$bsp_dir"
}

# 函数：执行fastboot命令
run_cmd() {
    local max_retries=3
    local retry_count=0
    
    while [[ $retry_count -lt $max_retries ]]; do
        echo "执行: $*"
        if "$@"; then
            return 0
        else
            retry_count=$((retry_count + 1))
            if [[ $retry_count -lt $max_retries ]]; then
                echo "命令失败，2秒后重试 ($retry_count/$max_retries)..."
                sleep 2
            else
                echo "Error: 命令执行失败（已重试$max_retries次）: $*" >&2
                return 1
            fi
        fi
    done
}

# 函数：执行BSP烧录
flash_bsp() {
    local hostname="$1"
    local board_ip="$2"
    local firmware_dir=$(dirname "$MCU_FIRMWARE_PATH")
    local bsp_dir="$firmware_dir/BSP"
    
    echo ""
    echo "========================================="
    echo "开始执行BSP烧录"
    echo "========================================="
    echo "主机名: $hostname"
    echo "板卡IP: $board_ip"
    echo "BSP目录: $bsp_dir"
    echo ""
    
    # 查找BSP内容目录
    local bsp_content_dir=""
    echo "查找BSP内容目录..."
    
    # 首先检查BSP目录本身是否包含GPT文件
    if [[ -f "$bsp_dir/gpt_main_${hostname}_emmc.img" ]]; then
        bsp_content_dir="$bsp_dir"
        echo "找到GPT文件在BSP根目录: $bsp_content_dir"
    else
        # 如果根目录没有，查找子目录
        echo "在BSP根目录未找到GPT文件，查找子目录..."
        ls -la "$bsp_dir/"
        
        for dir in "$bsp_dir"/*; do
            if [[ -d "$dir" ]]; then
                echo "检查目录: $dir"
                if [[ -f "$dir/gpt_main_${hostname}_emmc.img" ]]; then
                    bsp_content_dir="$dir"
                    echo "找到匹配的BSP目录: $bsp_content_dir"
                    break
                else
                    echo "目录中未找到 gpt_main_${hostname}_emmc.img 文件"
                    ls -la "$dir/" | head -10
                fi
            fi
        done
    fi
    
    if [[ -z "$bsp_content_dir" ]]; then
        echo "Error: 找不到BSP内容目录或GPT文件 gpt_main_${hostname}_emmc.img" >&2
        echo "可用的GPT文件:" >&2
        find "$bsp_dir" -name "gpt_main_*_emmc.img" -type f 2>/dev/null | head -10 >&2
        return 1
    fi
    
    echo "BSP内容目录: $bsp_content_dir"
    
    # 进入BSP内容目录
    cd "$bsp_content_dir"
    
    # 验证必要文件是否存在
    echo "验证必要文件..."
    local missing_files=()
    
    local required_files=(
        "gpt_main_${hostname}_emmc.img"
        "acore_cfg_hsm_signed.img"
        "bl31.img"
        "optee-hsm.img"
        "uboot.img"
        "vbmeta.img"
        "boot.img"
        "system-b1.img"
        "basesystem-b1.img"
        "spl_ddr.img"
    )
    
    for file in "${required_files[@]}"; do
        if [[ ! -f "$file" ]]; then
            missing_files+=("$file")
        fi
    done
    
    if [[ ${#missing_files[@]} -gt 0 ]]; then
        echo "Error: 缺少以下必要文件:" >&2
        printf '  %s\n' "${missing_files[@]}" >&2
        echo "当前目录文件列表:" >&2
        ls -la | head -20 >&2
        return 1
    fi
    
    echo "所有必要文件验证通过！"
    
    # 执行fastboot命令序列
    echo "开始执行Fastboot烧录命令..."
    echo ""
    
    # 烧录过程中如果失败，不删除BSP包便于调试
    local flash_success=true

    run_cmd fastboot -s udp:${board_ip}:5554 oem interface:blk
    run_cmd fastboot -s udp:${board_ip}:5554 oem bootdevice:mmc
    run_cmd fastboot -s udp:${board_ip}:5554 flash gpt gpt_main_${hostname}_emmc.img
    run_cmd fastboot -s udp:${board_ip}:5554 flash acore_cfg_a acore_cfg_hsm_signed.img
    run_cmd fastboot -s udp:${board_ip}:5554 flash acore_cfg_b acore_cfg_hsm_signed.img
    run_cmd fastboot -s udp:${board_ip}:5554 flash bl31_a bl31.img
    run_cmd fastboot -s udp:${board_ip}:5554 flash bl31_b bl31.img
    run_cmd fastboot -s udp:${board_ip}:5554 flash optee_a optee-hsm.img
    run_cmd fastboot -s udp:${board_ip}:5554 flash optee_b optee-hsm.img
    run_cmd fastboot -s udp:${board_ip}:5554 flash uboot_a uboot.img
    run_cmd fastboot -s udp:${board_ip}:5554 flash uboot_b uboot.img
    run_cmd fastboot -s udp:${board_ip}:5554 flash vbmeta_a vbmeta.img
    run_cmd fastboot -s udp:${board_ip}:5554 flash vbmeta_b vbmeta.img
    run_cmd fastboot -s udp:${board_ip}:5554 flash boot_a boot.img
    run_cmd fastboot -s udp:${board_ip}:5554 flash boot_b boot.img
    run_cmd fastboot -s udp:${board_ip}:5554 flash system_a system-b1.img
    run_cmd fastboot -s udp:${board_ip}:5554 flash system_b system-b1.img
    run_cmd fastboot -s udp:${board_ip}:5554 flash basesystem_a basesystem-b1.img
    run_cmd fastboot -s udp:${board_ip}:5554 flash basesystem_b basesystem-b1.img
    run_cmd fastboot -s udp:${board_ip}:5554 oem interface:mtd
    run_cmd fastboot -s udp:${board_ip}:5554 flash spl_ddr_a spl_ddr.img
    run_cmd fastboot -s udp:${board_ip}:5554 flash spl_ddr_b spl_ddr.img
    
    echo ""
    echo "========================================="
    if [[ "$flash_success" == true ]]; then
    echo "BSP烧录完成!"
    echo "========================================="

    # 烧录成功后清理BSP包
        echo ""
        echo "正在清理BSP包..."
        if [[ -d "$bsp_dir" ]]; then
            local bsp_size=$(du -sh "$bsp_dir" 2>/dev/null | cut -f1)
            echo "BSP包大小: $bsp_size"
            
            # 回到安全目录，避免删除当前工作目录
            cd /tmp
            
            if rm -rf "$bsp_dir"; then
                echo "BSP包已成功删除: $bsp_dir"
            else
                echo "BSP包删除失败: $bsp_dir" >&2
            fi
        else
            echo "BSP目录不存在，无需删除"
        fi
        
        return 0
    else
        echo "BSP烧录失败!"
        echo "========================================="
        echo "⚠️  由于烧录失败，保留BSP包以便调试: $bsp_dir" >&2
        return 1
    fi
}

# 函数：从输出中提取IP地址（改进版）
extract_board_ip_from_output() {
    local output_file="$1"
    
    # 方法1：使用cat读取并过滤，然后提取IP
    local ip=$(cat "$output_file" | tr -cd '[:print:]\n\r\t' | grep -oP "板卡IP地址: \K\d+\.\d+\.\d+\.\d+" | tail -1)
    
    # 方法2：如果第一种模式没找到，尝试其他模式
    if [[ -z "$ip" ]]; then
        ip=$(cat "$output_file" | tr -cd '[:print:]\n\r\t' | grep -oP "成功进入fastboot模式，板卡IP: \K\d+\.\d+\.\d+\.\d+" | tail -1)
    fi
    
    # 方法3：尝试直接搜索IP模式
    if [[ -z "$ip" ]]; then
        ip=$(cat "$output_file" | tr -cd '[:print:]\n\r\t' | grep -oE "([0-9]{1,3}\.){3}[0-9]{1,3}" | grep -E "^192\.168\." | tail -1)
    fi
    
    echo "$ip"
}

# 函数：显示帮助信息
show_help() {
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "默认执行完整流程：UART Boot → 进入uboot → fastboot udp → 获取IP → BSP烧录"
    echo ""
    echo "支持的主机名及对应固件包:"
    echo "  j6m-lite-ngx-b2  → ../config/mcu_firmware/uart_LiteB2_0826"
    echo "  j6m-pro-acar-a1  → ../config/mcu_firmware/uart_Acar_0715"
    echo ""
    echo "Options:"
    echo "  -u, --package PATH    MCU 固件包路径 (默认: 根据hostname自动选择)"
    echo "  -b, --board HOSTNAME  主机名 (默认: 自动从设备配置文件获取)"
    echo "  -t, --type TYPE       UART 启动方式 (默认: $DEFAULT_UART_OPT)"
    echo "  --uart-only          仅执行UART Boot到获取IP，不执行BSP烧录"
    echo "  --bsp-only           跳过UART Boot，仅执行BSP烧录"
    echo "  --ip IP_ADDRESS      手动指定板卡IP (配合 --bsp-only 使用)"
    echo "  -h, --help           显示此帮助信息"
    echo ""
    echo "Examples:"
    echo "  $0                                           # 执行完整流程（自动选择固件）"
    echo "  $0 --uart-only                              # 仅执行到获取IP"
    echo "  $0 --bsp-only --ip 192.168.2.62             # 仅执行BSP烧录"
    echo "  $0 -b j6m-lite-ngx-b2                       # 指定主机名（自动选择LiteB2固件）"
    echo "  $0 -u ../config/mcu_firmware/other_version  # 手动指定固件路径"
}

# 解析命令行参数
MCU_FIRMWARE_PATH=""  # 初始为空，后续根据hostname自动设置
HOSTNAME=""
UART_OPT="$DEFAULT_UART_OPT"
UART_ONLY=false
BSP_ONLY=false
MANUAL_IP=""
MANUAL_FIRMWARE_PATH=""  # 标记是否手动指定了固件路径

while [[ $# -gt 0 ]]; do
    case $1 in
        -u|--package)
            MCU_FIRMWARE_PATH="$2"
            MANUAL_FIRMWARE_PATH="true"
            shift 2
            ;;
        -b|--board)
            HOSTNAME="$2"
            shift 2
            ;;
        -t|--type)
            UART_OPT="$2"
            shift 2
            ;;
        --uart-only)
            UART_ONLY=true
            shift
            ;;
        --bsp-only)
            BSP_ONLY=true
            shift
            ;;
        --ip)
            MANUAL_IP="$2"
            shift 2
            ;;
        -h|--help)
            show_help
            exit 0
            ;;
        *)
            echo "Error: 未知参数 $1" >&2
            show_help
            exit 1
            ;;
    esac
done

# 如果未指定主机名，则自动获取
if [[ -z "$HOSTNAME" ]]; then
    echo "正在自动获取主机名..."
    HOSTNAME=$(get_hostname)
    if [[ $? -ne 0 || -z "$HOSTNAME" ]]; then
        echo "Error: 无法获取主机名，请手动指定 -b 参数" >&2
        exit 1
    fi
    echo "检测到主机名: $HOSTNAME"
fi

# 如果未手动指定固件路径，则根据hostname自动选择
if [[ -z "$MANUAL_FIRMWARE_PATH" ]]; then
    MCU_FIRMWARE_PATH=$(get_firmware_path_by_hostname "$HOSTNAME")
    echo "根据主机名 '$HOSTNAME' 自动选择固件包: $MCU_FIRMWARE_PATH"
fi

# 如果仅执行BSP烧录
if [[ "$BSP_ONLY" == true ]]; then
    if [[ -z "$MANUAL_IP" ]]; then
        echo "Error: 使用 --bsp-only 时必须指定 --ip 参数" >&2
        show_help
        exit 1
    fi
    
    echo "========================================="
    echo "仅执行BSP烧录"
    echo "========================================="
    echo "主机名: $HOSTNAME"
    echo "指定IP: $MANUAL_IP"
    echo ""
    
    # 获取最新BSP包
    echo "正在获取最新BSP包信息..."
    LATEST_BSP=$(get_latest_bsp_package)
    if [[ $? -ne 0 || -z "$LATEST_BSP" ]]; then
        echo "Error: 获取最新BSP包失败" >&2
        exit 1
    fi
    
    echo "最新BSP包: $LATEST_BSP"
    
    # 下载并解压BSP包
    BSP_DIR=$(download_and_extract_bsp "$LATEST_BSP")
    if [[ $? -ne 0 ]]; then
        echo "Error: BSP包下载解压失败" >&2
        exit 1
    fi
    
    # 执行BSP烧录
    if flash_bsp "$HOSTNAME" "$MANUAL_IP"; then
        echo ""
        echo "========================================="
        echo "BSP烧录流程执行成功!"
        echo "BSP包已自动清理"
        echo "========================================="
        exit 0
    else
        echo ""
        echo "========================================="
        echo "BSP烧录流程执行失败!"
        echo "BSP包已保留用于调试: $(dirname "$MCU_FIRMWARE_PATH")/BSP"
        echo "========================================="
        exit 1
    fi
fi

# 检查固件路径是否存在
if [[ ! -d "$MCU_FIRMWARE_PATH" ]]; then
    echo "Error: MCU 固件路径不存在: $MCU_FIRMWARE_PATH" >&2
    echo "当前工作目录: $(pwd)"
    echo ""
    echo "支持的主机名及固件包:"
    echo "  j6m-lite-ngx-b2  → ../config/mcu_firmware/uart_LiteB2_0826"
    echo "  j6m-pro-acar-a1  → ../config/mcu_firmware/uart_Acar_0715"
    echo ""
    echo "请检查:"
    echo "1. 主机名是否正确: $HOSTNAME"
    echo "2. 固件目录是否存在"
    echo "3. 或手动指定固件路径: $0 -u /path/to/firmware"
    exit 1
fi

# 检查 uartboot.py 是否存在
UARTBOOT_SCRIPT="uartboot.py"
if [[ ! -f "$UARTBOOT_SCRIPT" ]]; then
    echo "Error: uartboot.py 脚本不存在: $UARTBOOT_SCRIPT" >&2
    exit 1
fi

# 显示执行信息
echo "========================================="
echo "开始执行完整固件烧录流程"
echo "========================================="
echo "步骤1: UART Boot 烧录固件"
echo "步骤2: 板卡进入 uboot"
echo "步骤3: 执行 fastboot udp 获取IP"
if [[ "$UART_ONLY" != true ]]; then
    echo "步骤4: 下载最新BSP包"
    echo "步骤5: 执行BSP烧录"
fi
echo ""
echo "脚本路径: $UARTBOOT_SCRIPT"
echo "固件路径: $MCU_FIRMWARE_PATH"
echo "主机名:   $HOSTNAME"
echo "启动方式: $UART_OPT"
if [[ -z "$MANUAL_FIRMWARE_PATH" ]]; then
    echo "固件选择: 根据主机名自动选择"
else
    echo "固件选择: 手动指定"
fi
echo "========================================="
echo ""

# 第一步：执行uartboot
echo "步骤1: 开始执行 UART Boot..."
CMD="python3 uartboot.py -u \"$MCU_FIRMWARE_PATH\" -b \"$HOSTNAME\" -t \"$UART_OPT\""
echo "执行命令: $CMD"
echo ""

# 使用保持TTY特性的方式执行，让进度条动起来
TEMP_OUTPUT=$(mktemp)

# 检查是否在真实的TTY中运行
if [[ -t 1 ]] && [[ -t 2 ]]; then
    # 在TTY中，使用script保持终端特性
    if command -v script >/dev/null 2>&1; then
        echo "使用script命令执行，保持进度条显示..."
        # Linux系统的script命令
        if script -qec "$CMD" /dev/null | tee "$TEMP_OUTPUT"; then
            UARTBOOT_RESULT=0
        else
            UARTBOOT_RESULT=${PIPESTATUS[0]}
        fi
    elif command -v unbuffer >/dev/null 2>&1; then
        echo "使用unbuffer命令执行，保持进度条显示..."
        # 使用unbuffer保持输出
        if unbuffer $CMD | tee "$TEMP_OUTPUT"; then
            UARTBOOT_RESULT=0
        else
            UARTBOOT_RESULT=${PIPESTATUS[0]}
        fi
    else
        echo "使用stdbuf执行，禁用缓冲..."
        # 使用stdbuf禁用缓冲
        if stdbuf -oL -eL bash -c "$CMD" | tee "$TEMP_OUTPUT"; then
            UARTBOOT_RESULT=0
        else
            UARTBOOT_RESULT=${PIPESTATUS[0]}
        fi
    fi
else
    # 不在TTY中，使用普通方式
    echo "非TTY环境，使用普通执行方式..."
    if eval "$CMD" | tee "$TEMP_OUTPUT"; then
        UARTBOOT_RESULT=0
    else
        UARTBOOT_RESULT=${PIPESTATUS[0]}
    fi
fi

echo ""
echo "uartboot.py 执行完成，返回码: $UARTBOOT_RESULT"

# 检查uartboot执行结果
if [[ $UARTBOOT_RESULT -eq 0 ]]; then
    echo ""
    echo "========================================="
    echo "步骤1-3: UART Boot 执行成功!"
    echo "板卡已进入uboot并执行fastboot udp"
    echo "========================================="
    
    # 第二步：从输出文件中提取板卡IP
    BOARD_IP=$(extract_board_ip_from_output "$TEMP_OUTPUT")
    if [[ -z "$BOARD_IP" ]]; then
        echo "自动提取IP失败，请从上面的输出中找到板卡IP地址"
        echo "通常显示为: '板卡IP地址: xxx.xxx.xxx.xxx'"
        echo ""
        read -p "请输入板卡IP地址: " BOARD_IP
        
        if [[ -z "$BOARD_IP" ]]; then
            echo "Error: 未输入IP地址" >&2
            rm "$TEMP_OUTPUT"
            exit 1
        fi
        
        # 验证IP地址格式
        if [[ ! "$BOARD_IP" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
            echo "Error: IP地址格式不正确" >&2
            rm "$TEMP_OUTPUT"
            exit 1
        fi
    fi
    echo "使用板卡IP: $BOARD_IP"
    
    # 清理临时文件
    rm "$TEMP_OUTPUT"
    
    # 如果仅执行到获取IP
    if [[ "$UART_ONLY" == true ]]; then
        echo ""
        echo "========================================="
        echo "UART Boot流程完成!"
        echo "板卡IP: $BOARD_IP"
        echo "如需继续BSP烧录，请执行:"
        echo "$0 --bsp-only --ip $BOARD_IP"
        echo "========================================="
        exit 0
    fi
    
    # 第三步：执行BSP烧录
    echo ""
    echo "步骤4-5: 开始BSP包下载和烧录流程..."
    
    # 获取最新BSP包
    echo "正在获取最新BSP包信息..."
    LATEST_BSP=$(get_latest_bsp_package)
    if [[ $? -ne 0 || -z "$LATEST_BSP" ]]; then
        echo "Error: 获取最新BSP包失败" >&2
        echo "板卡IP: $BOARD_IP (可用于手动BSP烧录)" >&2
        exit 1
    fi
    
    echo "最新BSP包: $LATEST_BSP"
    
    # 下载并解压BSP包
    BSP_DIR=$(download_and_extract_bsp "$LATEST_BSP")
    if [[ $? -ne 0 ]]; then
        echo "Error: BSP包下载解压失败" >&2
        echo "板卡IP: $BOARD_IP (可用于手动BSP烧录)" >&2
        exit 1
    fi
    
    # 执行BSP烧录
    if flash_bsp "$HOSTNAME" "$BOARD_IP"; then
        echo ""
        echo "========================================="
        echo "完整烧录流程执行成功!"
        echo "UART Boot + Fastboot UDP + BSP烧录 全部完成"
        echo "BSP包已自动清理"
        echo "========================================="
    else
        echo ""
        echo "========================================="
        echo "BSP烧录流程执行失败!"
        echo "BSP包已保留用于调试: $(dirname "$MCU_FIRMWARE_PATH")/BSP"
        echo "========================================="
        exit 1
    fi
else
    echo ""
    echo "========================================="
    echo "UART Boot 执行失败! 返回码: $UARTBOOT_RESULT"
    echo "========================================="
    rm "$TEMP_OUTPUT"
    exit 1
fi