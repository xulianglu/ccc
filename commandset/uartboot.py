import sys
import os
import json
import importlib.util
import argparse
import logging
import re
import shutil
import zipfile
import glob
import requests
import hashlib
import subprocess
import time
import serial
import serial.tools.list_ports
from xmodem import XMODEM
from http import HTTPStatus
from functools import partial
from rich.progress import Progress, TextColumn, BarColumn, TimeElapsedColumn, TimeRemainingColumn


class Uartboot:
    download_timeout = 600
    packet_size = dict(
        xmodem=128,
        xmodem1k=1024,
    )

    def __init__(self, logger=logging.getLogger()):
        self.logger = logger
        self.img_packages = os.path.abspath(f"/tmp/img_packages")

        try:
            config_root_path = importlib.util.find_spec(
                "cicd"
            ).submodule_search_locations[0]
        except Exception:
            config_root_path = "."
        self.boot_config = json.loads(
            open(config_root_path + "/config/device/uart_boot.json", "rb")
            .read()
            .decode("utf-8")
        )
        self.serial_param = json.loads(
            open(config_root_path + "/config/device/connect_param.json", "rb")
            .read()
            .decode("utf-8")
        )["serial"]
        self.board_config = json.loads(
            open(config_root_path + "/config/device/board.json", "rb")
            .read()
            .decode("utf-8")
        )
        self.state_config = json.loads(
            open(config_root_path + "/config/device/state.json", "rb")
            .read()
            .decode("utf-8")
        )

        serial_timeout = 30
        try:
            soc_serial = serial.Serial(
                self.serial_param["soc"]["port"],
                self.serial_param["soc"]["baudrate"],
                timeout=serial_timeout,
            )
            mcu_serial = serial.Serial(
                self.serial_param["mcu"]["port"],
                self.serial_param["mcu"]["baudrate"],
                timeout=serial_timeout,
            )
            hsm_serial = serial.Serial(
                self.serial_param["hsm"]["port"],
                self.serial_param["hsm"]["baudrate"],
                timeout=serial_timeout,
            )
        except serial.serialutil.SerialException:
            serial_devices = serial.tools.list_ports.comports()
            serial_devices = [
                device for device in serial_devices if device.manufacturer == "FTDI"
            ]
            if len(serial_devices) != 4:
                raise Exception(
                    f"扫描到的串口设备数量异常, 期望4个, 实际{len(serial_devices)}个"
                )

            serial_devices.sort(key=lambda x: int(x.device[3:]))
            hsm_serial = serial.Serial(
                serial_devices[1].device, 921600, timeout=serial_timeout
            )
            soc_serial = serial.Serial(
                serial_devices[2].device, 921600, timeout=serial_timeout
            )
            mcu_serial = serial.Serial(
                serial_devices[3].device, 921600, timeout=serial_timeout
            )

        self.serial_ports = {"soc": soc_serial, "mcu": mcu_serial, "hsm": hsm_serial}
        self.xmodem_mode = 'xmodem1k'
        self.xmodem = {
            "soc": XMODEM(
                partial(self.__xmodem_get_data, port="soc"),
                partial(self.__xmodem_put_data, port="soc"),
                mode=self.xmodem_mode,
            ),
            "mcu": XMODEM(
                partial(self.__xmodem_get_data, port="mcu"),
                partial(self.__xmodem_put_data, port="mcu"),
                mode=self.xmodem_mode,
            ),
            "hsm": XMODEM(
                partial(self.__xmodem_get_data, port="hsm"),
                partial(self.__xmodem_put_data, port="hsm"),
                mode=self.xmodem_mode,
            ),
        }

    def __xmodem_get_data(self, size, timeout=1, port=None):
        return self.serial_ports[port].read(size) or None

    def __xmodem_put_data(self, data, timeout=1, port=None):
        return self.serial_ports[port].write(data) or None

    def __device_run_uart_start(self, uart_opt):

        def __check_uart_mode():
            """检查MCU是否已经在UART模式"""
            self.logger.info("检查MCU是否已在UART模式...")

            try:
                # 清空缓冲区
                self.serial_ports["mcu"].reset_input_buffer()
                self.serial_ports["mcu"].reset_output_buffer()

                # 发送回车并检测响应
                consecutive_C_count = 0
                for attempt in range(5):  # 尝试5次
                    self.serial_ports["mcu"].write(b'\n')
                    time.sleep(0.3)

                    # 读取响应
                    response = self.serial_ports["mcu"].read_all()
                    if response:
                        try:
                            response_str = response.decode('utf-8', 'ignore')
                        except:
                            response_str = str(response)

                        self.logger.debug(f"MCU响应 (尝试{attempt+1}): {repr(response)} -> {repr(response_str)}")

                        # 检查是否收到C字符（UART模式的标志）
                        if response == b'C' or response_str.strip() == 'C' or 'CCC' in response_str:
                            consecutive_C_count += 1
                            if consecutive_C_count >= 2:  # 连续收到2个C确认
                                self.logger.info("MCU已处于UART模式")
                                return True
                        else:
                            consecutive_C_count = 0
                            # 如果收到其他提示符，说明在shell模式
                            if any(prompt in response_str for prompt in ['horizon:/', '#', '$', 'root@']):
                                self.logger.info("MCU处于shell模式，需要进入UART模式")
                                return False

            except Exception as e:
                self.logger.error(f"检查UART模式时出错: {e}")

            self.logger.info("MCU未处于UART模式")
            return False

        def __execute_secure_debug_unlock():
            """执行SecureDebug_Serial_MCU.py解锁MCU"""
            self.logger.info("检测到UART被锁定，开始执行MCU安全调试解锁...")

            # 关闭当前串口连接，避免冲突
            if self.serial_ports["mcu"].is_open:
                self.serial_ports["mcu"].close()
                self.logger.info("已关闭MCU串口连接")

            try:
                # 查找SecureDebug_Serial_MCU.py脚本
                script_dir = os.path.dirname(os.path.abspath(__file__))
                secure_debug_script = os.path.join(script_dir, "SecureDebug_Serial_MCU.py")

                if not os.path.exists(secure_debug_script):
                    self.logger.error(f"未找到解锁脚本: {secure_debug_script}")
                    return False

                # 获取实际的串口设备路径
                mcu_port = self.serial_param["mcu"]["port"]
                self.logger.info(f"使用MCU串口设备: {mcu_port}")

                # 如果是映射路径，尝试解析为实际路径
                if "/dev/serial/by-name/" in mcu_port:
                    try:
                        real_port = os.path.realpath(mcu_port)
                        if os.path.exists(real_port):
                            self.logger.info(f"映射路径 {mcu_port} -> 实际路径 {real_port}")
                            mcu_port = real_port
                        else:
                            self.logger.warning(f"映射路径解析失败，使用原路径: {mcu_port}")
                    except Exception as e:
                        self.logger.warning(f"解析映射路径失败: {e}，使用原路径")

                # 验证串口设备是否存在
                if not os.path.exists(mcu_port):
                    self.logger.error(f"MCU串口设备不存在: {mcu_port}")
                    # 尝试列出可用的串口设备
                    try:
                        import serial.tools.list_ports
                        ports = serial.tools.list_ports.comports()
                        self.logger.info("可用的串口设备:")
                        for port in ports:
                            self.logger.info(f"  {port.device} - {port.description}")
                    except:
                        pass
                    return False

                def run_unlock_with_progress(cmd, description):
                    """运行解锁命令并显示进度"""
                    self.logger.info(f"{description}")
                    self.logger.info(f"执行命令: {' '.join(cmd)}")
                    self.logger.info("=" * 60)

                    try:
                        # 使用subprocess.Popen实时显示输出
                        process = subprocess.Popen(
                            cmd,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,  # 合并stderr到stdout
                            text=True,
                            bufsize=1,  # 行缓冲
                            universal_newlines=True,
                            cwd=script_dir
                        )

                        output_lines = []
                        unlock_steps = {
                            "Starting MCU unlock process": "开始MCU解锁流程...",
                            "成功连接MCU串口": "MCU串口连接成功",
                            "Write MCU serial command: mcu_version_show": "读取MCU版本信息...",
                            "shell_cmd_SentCert": "发送数字证书...",
                            "certificate received success": "证书接收成功",
                            "Random bytes detected": "获取随机挑战值",
                            "signature:": "生成ECDSA签名...",
                            "shell_cmd_SentSignature": "发送签名验证...",
                            "signature verify success": "签名验证成功",
                            "MCU unlock process completed successfully": "MCU解锁完成!"
                        }

                        # 实时读取输出
                        while True:
                            output = process.stdout.readline()
                            if output == '' and process.poll() is not None:
                                break
                            if output:
                                output = output.strip()
                                output_lines.append(output)

                                # 检查是否匹配已知的解锁步骤
                                step_matched = False
                                for keyword, progress_msg in unlock_steps.items():
                                    if keyword in output:
                                        self.logger.info(f"  {progress_msg}")
                                        step_matched = True
                                        break

                                # 如果没有匹配到步骤，显示原始输出（但过滤掉一些冗余信息）
                                if not step_matched and output:
                                    # 过滤掉一些冗余的调试信息
                                    if not any(skip in output.lower() for skip in [
                                        'debug', 'write mcu serial command: shell_cmd_',
                                        'read serial data is', '- info -', '- debug -'
                                    ]):
                                        # 只显示重要信息
                                        if any(important in output.lower() for important in [
                                            'error', 'failed', 'success', 'complete', 'unlock'
                                        ]):
                                            self.logger.info(f"{output}")

                        # 等待进程完成
                        return_code = process.wait()

                        self.logger.info("=" * 60)

                        if return_code == 0:
                            self.logger.info("MCU解锁成功!")
                            return True
                        else:
                            self.logger.error(f"MCU解锁失败，返回码: {return_code}")
                            # 显示最后几行输出用于调试
                            if output_lines:
                                self.logger.error("最后的输出信息:")
                                for line in output_lines[-10:]:  # 显示最后10行
                                    if line.strip():
                                        self.logger.error(f"  {line}")
                            return False

                    except subprocess.TimeoutExpired:
                        self.logger.error("MCU解锁超时")
                        process.kill()
                        return False
                    except Exception as e:
                        self.logger.error(f"执行解锁命令时出错: {e}")
                        return False

                # 构建解锁命令，首先尝试自动检测模式
                cmd = [sys.executable, secure_debug_script]

                if run_unlock_with_progress(cmd, "自动检测串口模式"):
                    return True

                # 如果自动检测失败，尝试手动指定串口
                self.logger.warning("🔄 自动检测模式失败，尝试手动指定串口...")
                cmd_with_port = cmd + [mcu_port]

                return run_unlock_with_progress(cmd_with_port, f"手动指定串口模式 ({mcu_port})")

            except Exception as e:
                self.logger.error(f"执行解锁脚本失败: {e}")
                return False
            finally:
                # 重新打开MCU串口
                try:
                    if not self.serial_ports["mcu"].is_open:
                        self.logger.info("重新连接MCU串口...")
                        self.serial_ports["mcu"].open()
                        self.logger.info("重新打开MCU串口成功")
                        # 等待串口稳定
                        time.sleep(1)
                except Exception as e:
                    self.logger.error(f"重新打开MCU串口失败: {e}")

        def __device_run_uart_start_by_mcu_goto_uart():
            from cicd.commandset.reboot import Relay

            if Relay(self.logger).execute("reboot") != True:
                self.logger.error("failed to reboot mcu")
                return False
            time.sleep(0.5)

            # 参考自https://carizon.feishu.cn/wiki/FnOAwisfFiHfp3kxldBcufHvnxE
            from cicd.communicate.xserial import xserial

            mcu_serial = xserial(
                self.serial_param["mcu"]["port"],
                self.serial_param["soc"]["baudrate"],
                self.logger,
            )

            max_unlock_attempts = 3  # 最大解锁尝试次数
            unlock_attempt = 0

            while unlock_attempt < max_unlock_attempts:
                self.logger.info(f"尝试发送 mcu_goto_uart 命令 (第{unlock_attempt + 1}次)")

                for _ in range(3):
                    command_result, output = mcu_serial.send_cmd(
                        "mcu_goto_uart\n" + 16 * "\n", 1, ["CCC"], 0.05
                    )
                    self.logger.debug(
                        f"mcu command result {command_result}, serial port output:\n{output}"
                    )

                    # 检查是否成功进入UART模式
                    if command_result:
                        self.logger.info("成功进入UART模式")
                        return True

                    # 检查是否被锁定
                    if "UART locked" in output:
                        self.logger.warning("检测到UART被锁定，需要先解锁")
                        break

                # 如果检测到锁定，执行解锁流程
                if "UART locked" in output:
                    unlock_attempt += 1
                    self.logger.info(f"开始第{unlock_attempt}次解锁尝试...")

                    if __execute_secure_debug_unlock():
                        self.logger.info("解锁成功，等待3秒后重试进入UART模式...")
                        time.sleep(3)

                        # 重新创建xserial对象，确保连接正常
                        mcu_serial = xserial(
                            self.serial_param["mcu"]["port"],
                            self.serial_param["soc"]["baudrate"],
                            self.logger,
                        )
                    else:
                        self.logger.error(f"第{unlock_attempt}次解锁失败")
                        if unlock_attempt >= max_unlock_attempts:
                            self.logger.error("达到最大解锁尝试次数，放弃")
                            break
                else:
                    # 如果不是锁定问题，直接失败
                    break

            self.logger.error(
                f"failed to set mcu into uart download mode after {unlock_attempt} unlock attempts, final output:\n{output}"
            )
            return False

        def __device_run_uart_start_by_mcu_reboot():
            from cicd.commandset.reboot import Relay

            if Relay(self.logger).execute("reboot") != True:
                self.logger.error("failed to reboot mcu")
                return False

            # 参考自https://carizon.feishu.cn/wiki/FnOAwisfFiHfp3kxldBcufHvnxE
            from cicd.communicate.xserial import xserial

            mcu_serial = xserial(self.serial_param['mcu']['port'], self.serial_param['soc']['baudrate'], self.logger)
            for _ in range(8):
                # 兼容mcureboot命令与mcureset命令
                _, output = mcu_serial.send_cmd('mcureboot\nmcureset' + 16 * '\n', 2, self.state_config['prompts']['mcu'], 0.05)
                self.logger.debug(f'mcu serial port output:\n{output}')
                if re.findall("CCC", output):
                    return True

            return False

        def __device_run_uart_start_by_manual_operation():
            self.logger.info("等待手动操作进入UART模式...")

            # 检查是否已经在UART模式
            if __check_uart_mode():
                return True

            # 如果不在UART模式，等待用户手动操作
            self.logger.info("请手动操作设备进入UART模式（等待'C'字符）...")
            output = self.serial_ports["mcu"].read_until(b"CCC")
            self.logger.debug(
                f"while waiting for device run into uart download mode, serial port output:\n"
                f'{output.decode("utf-8", "ignore")}'
            )
            if not output:
                self.logger.error(
                    f'timeout waiting for \'CCC\' in {self.serial_ports["mcu"].timeout} second'
                )
                return False
            return True

        # 首先检查是否已经在UART模式
        if __check_uart_mode():
            self.logger.info("MCU已在UART模式，跳过进入UART流程")
            return True

        # 如果不在UART模式，根据指定方式进入UART模式
        uart_start_method = {
            "mcu goto uart": __device_run_uart_start_by_mcu_goto_uart,
            "mcu reboot": __device_run_uart_start_by_mcu_reboot,
            "manual operation": __device_run_uart_start_by_manual_operation,
        }

        self.logger.info(f"MCU不在UART模式，尝试通过 {uart_opt} 方式进入UART模式")
        if uart_opt not in uart_start_method:
            self.logger.error(f"unsupported mcu boot method: {uart_opt}")
            return False

        result = uart_start_method[uart_opt]()
        self.logger.info(
            f'{"succeed" if result else "failed"} to set mcu into uart boot mode by {uart_opt}'
        )
        return result

    def __download_package(self, url, board_sample):
        def __get_latest_package_info(board_sample) -> str:
            jfrog_api_prefix = "https://jfrog.carizon.work/artifactory"  
                #"https://jfrog.carizon.work/artifactory/api/storage"
            jfrog_bsp_package = (
                "project-snapshot-local/Dev/Common/j6/bsp/daily/Release/"
                #"project-snapshot-local/NGX/Lite/Demo/BSW/bsp/J6/daily/Release/"
            )
            device_to_sdk_map = list(self.board_config.keys())

            for match in device_to_sdk_map:
                if board_sample == match['device']:
                    target_sdk_version = match['sdk']
                    break
            else:
                target_sdk_version = 930

            # 使用jfrog api查询最新包的url
            jfrog_package_dir = f'{jfrog_api_prefix}/{jfrog_bsp_package}/{target_sdk_version}'
            response = requests.get(jfrog_package_dir, params={'lastModified': ''})
            if response.status_code != HTTPStatus.OK:
                self.logger.error(f'{response.status_code}:\n{response.text}')
                return False

            # 查询到最新包的信息
            jfrog_latest_bsp_package_info_url = response.json()['uri']
            response = requests.get(jfrog_latest_bsp_package_info_url)
            if response.status_code != HTTPStatus.OK:
                self.logger.error(f'{response.status_code}:\n{response.text}')
                return False

            latest_bsp_package_size = int(response.json()['size'])
            latest_bsp_package_md5 = response.json()['checksums']['md5']
            latest_bsp_package_url = response.json()['downloadUri']
            self.logger.info(f'latest bsp package url: {latest_bsp_package_url}')
            return latest_bsp_package_url, latest_bsp_package_size, latest_bsp_package_md5

        def __check_latest_package(ota_package_path, latest_bsp_package_size, latest_bsp_package_md5) -> bool:
            # 文件大小判断
            cur_size = os.path.getsize(ota_package_path)
            if cur_size != latest_bsp_package_size:
                self.logger.error(f'invalid bsp package size, actual: {cur_size}B, expect: {latest_bsp_package_size}B')
                return False
            self.logger.info(f'bsp package size validate pass')

            # 文件完整性判断
            md5 = hashlib.md5()
            with open(ota_package_path, 'rb') as file:
                for chunk in iter(lambda: file.read(1024 * 1024), b''):
                    md5.update(chunk)
            cur_md5 = md5.hexdigest()
            if cur_md5 != latest_bsp_package_md5:
                self.logger.error(f'invalid bsp package md5, actual: {cur_md5}, expect: {latest_bsp_package_md5}')
                return False
            self.logger.info(f'bsp package md5 validate pass')

            return True

        if url == 'latest':
            package_url, package_size, package_md5 = __get_latest_package_info(board_sample)
        else:
            package_url = url

        package_name = package_url.split('/')[-1]
        package_dir = '/tmp'
        package_path = f'{package_dir}/{package_name}'

        # 下载最新的升级包
        cur_pwd = os.getcwd()
        os.chdir(package_dir)
        max_retry_times = 10
        for retry_times in range(max_retry_times):
            try:
                subprocess.run(
                    f'wget -c --tries=10 --retry-connrefused --timeout=30 --waitretry=10 {package_url}',
                    shell=True,
                    timeout=self.download_timeout,
                    text=True,
                    check=True
                )
                self.logger.info(f'succeed download latest bsp package {package_path}')
                break
            except Exception as e:
                retry_times += 1
                self.logger.warning(f'failed to download {package_name} for {retry_times} times\n{e}')
                if os.path.exists(package_path):
                    os.remove(package_path)
        else:
            os.chdir(cur_pwd)
            self.logger.error(
                f"failed download latest bsp package after retry {max_retry_times} times"
            )
            return False, ""

        os.chdir(cur_pwd)

        if url == 'latest' and not __check_latest_package(package_path, package_size, package_md5):
            self.logger.error(f'download latest package but check failed')
            return False, ''

        return True, package_path

    def __prepare_mcu_package(self, board_sample, loading_step, mcu_package):
        flag = False

        for step in loading_step:
            for image in step['img_data']:
                if step['uart_port'] != 'soc' and not os.path.exists(f'{self.img_packages}/{image}'):
                    self.logger.info(
                        f"there is no {image} image in {self.img_packages} needed by mcu"
                    )
                    flag = True

        if flag:
            self.logger.info(f'need download mcu images')
            for match in mcu_package:
                if board_sample == match['device']:
                    package_url = match['sdk']
                    break
            else:
                self.logger.error(
                    f"not fount suitable mcu sdk version for {board_sample} version board in config"
                )
                return False

            package_name = package_url.split('/')[-1]
            package_dir = '/tmp'
            package_path = f'{package_dir}/{package_name}'

            # 下载最新的升级包
            cur_pwd = os.getcwd()
            os.chdir(package_dir)
            max_retry_times = 10
            for retry_times in range(max_retry_times):
                try:
                    subprocess.run(
                        f'wget -c --tries=10 --retry-connrefused --timeout=30 --waitretry=10 {package_url}',
                        shell=True,
                        timeout=self.download_timeout,
                        text=True,
                        check=True
                    )
                    self.logger.info(f'succeed download latest mcu package {package_path}')
                    break
                except Exception as e:
                    retry_times += 1
                    self.logger.warning(f'failed to download {package_name} for {retry_times} times\n{e}')
                    if os.path.exists(package_path):
                        os.remove(package_path)
            else:
                os.chdir(cur_pwd)
                self.logger.error(
                    f"failed download latest bsp package after retry {max_retry_times} times"
                )
                return False, ""

            os.chdir(cur_pwd)
            try:
                with zipfile.ZipFile(package_path, 'r') as zip_ref:
                    zip_ref.extractall(self.img_packages)
            except zipfile.BadZipFile as e:
                self.logger.error(f'{e}')
                return False
            self.logger.info(f'succeed unzip {package_path} to {self.img_packages}')

            if os.path.exists(f'{self.img_packages}/IMG/SBL.img'):
                shutil.copy2(f'{self.img_packages}/IMG/SBL.img', f'{self.img_packages}/SBL.img')
            else:
                self.logger.error(f'there is no SBL.img in {self.img_packages}/IMG')
                return False

            if os.path.exists(f'{self.img_packages}/BIN/J6_MCU_DEBUG.bin'):
                shutil.copy2(f'{self.img_packages}/BIN/J6_MCU_DEBUG.bin', f'{self.img_packages}/J6_MCU_DEBUG.bin')
            else:
                self.logger.error(f'there is no J6_MCU_DEBUG.bin in {self.img_packages}/BIN')
                return False

            mcu_firmware_dir = importlib.util.find_spec('cicd').submodule_search_locations[0] + "/config/mcu_firmware"
            if os.path.exists(mcu_firmware_dir):
                for fw in os.listdir(mcu_firmware_dir):
                    shutil.copy2(os.path.join(mcu_firmware_dir, fw), os.path.join(self.img_packages, fw))
            else:
                self.logger.error(f'there is no mcu fw dir at {mcu_firmware_dir}')
                return False

        return True

    def __host_run_uartboot(self, board, loading_step):
        for step in loading_step:
            for image in step["img_data"]:
                if os.path.exists(os.path.join(self.img_packages, image)):
                    image_path = os.path.join(self.img_packages, image)
                elif image == 'hsmfw_se.pkg' and os.path.exists(os.path.join(self.img_packages, f'{board}-{image}')):
                    image_path = os.path.join(self.img_packages, f'{board}-{image}')
                else:
                    self.logger.error(f'there is no {image} in {self.img_packages}')
                    return False

                self.logger.info(f'it\'s going to load {image_path} in {step["uart_port"]} port')

                self.logger.info(f'waiting \'C\' for loading {image_path} in {step["uart_port"]} port')

                # 区分soc和其他端口的检测方式
                if step["uart_port"] == "soc":
                    # soc端口：被动等待，不主动发回车
                    time_limit = time.time() + 10  # soc端口等待10秒
                    output = ""
                    found_C = False
                    consecutive_C_count = 0
                    while time_limit > time.time():
                        time.sleep(0.2)
                        chunk = self.serial_ports[step["uart_port"]].read_all()
                        try:
                            chunk_str = chunk.decode("utf-8", "ignore")
                        except Exception:
                            chunk_str = str(chunk)
                        output += chunk_str

                        # 检查是否收到 'C' 或 'CCC'
                        if chunk == b'C' or chunk_str.strip() == 'C' or 'CCC' in chunk_str:
                            consecutive_C_count += 1
                            if consecutive_C_count >= 1:  # 至少2个C
                                found_C = True
                                break
                        else:
                            consecutive_C_count = 0
                else:
                    # mcu和hsm端口：主动发回车检测
                    time_limit = time.time() + 15
                    output = ""
                    found_C = False
                    consecutive_C_count = 0
                    while time_limit > time.time():
                        self.serial_ports[step["uart_port"]].write("\n".encode())
                        time.sleep(0.2)
                        chunk = self.serial_ports[step["uart_port"]].read_all()
                        try:
                            chunk_str = chunk.decode("utf-8", "ignore")
                        except Exception:
                            chunk_str = str(chunk)
                        output += chunk_str

                        # 检测 SecureDebug 提示并延迟2秒后自动输入 0（不加回车）
                        if "Please enter 1 or 0" in chunk_str:
                            time.sleep(2)
                            self.serial_ports[step["uart_port"]].write(b"0")
                            time.sleep(0.2)

                        # 检查是否连续收到 'C' 或 'CCC'
                        if chunk == b'C' or chunk_str.strip() == 'C' or 'CCC' in chunk_str:
                            consecutive_C_count += 1
                            if consecutive_C_count >= 2:
                                found_C = True
                                break
                        else:
                            consecutive_C_count = 0

                if not found_C:
                    self.logger.error(
                        f'timeout waiting for consecutive \'C\' when send {image_path} at {step["uart_port"]} serial port in {self.serial_ports[step["uart_port"]].timeout} second'
                    )
                    return False

                with Progress(
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(complete_style="yellow", finished_style="green"),
                    TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                    TimeRemainingColumn(),
                    TimeElapsedColumn(),
                ) as progress:
                    xmodem_packets_count = (
                        (
                            os.path.getsize(f"{image_path}")
                            / self.packet_size[self.xmodem_mode]
                        )
                        if os.path.getsize(f"{image_path}")
                        % self.packet_size[self.xmodem_mode]
                        == 0
                        else int(
                            os.path.getsize(f"{image_path}")
                            / self.packet_size[self.xmodem_mode]
                        )
                        + 1
                    )
                    progress_task = progress.add_task(
                        description="{:<30}".format(f"loading {image}..."),
                        total=xmodem_packets_count,
                    )

                    def uart_load_progress_callback(_total_packets, _success_count, _error_count):
                        progress.update(progress_task, advance=1, refresh=True)

                    with open(f'{image_path}', 'rb') as stream:
                        if not self.xmodem[step["uart_port"]].send(
                            stream,
                            timeout=60,
                            quiet=True,
                            callback=uart_load_progress_callback,
                        ):
                            self.logger.error(f'failed to load {image_path} in {step["uart_port"]} port')
                            progress.stop()
                            return False

                        progress.stop()
                end_read_timeout = time.time() + 1
                end_read_output = ""
                while (
                    self.serial_ports[step["uart_port"]].in_waiting and end_read_timeout
                ):
                    end_read_output += (
                        self.serial_ports[step["uart_port"]]
                        .read()
                        .decode("utf-8", "ignore")
                    )
                self.logger.debug(
                    f'after loading {image_path} in {step["uart_port"]} port, serial port output:\n{end_read_output}'
                )

        self.logger.info(f'waiting for SoC run into uboot mode')
        content = ''
        time_limit = time.time() + self.serial_ports["soc"].timeout
        while True:
            if time.time() > time_limit:
                self.logger.error(f"SoC进入uboot超时")
                break

            self.serial_ports["soc"].write("\n".encode())

            output = self.serial_ports["soc"].read_all().decode("utf-8", "ignore")
            content += output

            if any(
                len(re.findall(prompt, content, re.IGNORECASE)) > 0
                for prompt in self.state_config["prompts"]["uboot"]
            ):
                self.logger.debug(f'soc serial port output:\n{content}')
                self.logger.info(f"SoC已进入uboot")

                # 进入uboot后，发送fastboot udp命令
                self.logger.info("发送 fastboot udp 命令进入fastboot模式")
                self.serial_ports["soc"].write("fastboot udp\n".encode())

                # 等待fastboot命令执行完成并解析IP地址
                fastboot_content = ''
                fastboot_time_limit = time.time() + 30  # 等待30秒
                board_ip = None

                while time.time() < fastboot_time_limit:
                    time.sleep(0.5)
                    output = self.serial_ports["soc"].read_all().decode("utf-8", "ignore")
                    fastboot_content += output

                    # 查找IP地址模式，例如: "Listening for fastboot command on 192.168.2.62"
                    ip_match = re.search(r"Listening for fastboot command on (\d+\.\d+\.\d+\.\d+)", fastboot_content)
                    if ip_match:
                        board_ip = ip_match.group(1)
                        self.logger.info(f"板卡IP地址: {board_ip}")
                        print(f"板卡IP地址: {board_ip}")
                        break

                if board_ip:
                    self.logger.info(f"成功进入fastboot模式，板卡IP: {board_ip}")
                else:
                    self.logger.warning("未能获取到板卡IP地址")
                    self.logger.debug(f"fastboot命令输出:\n{fastboot_content}")

                return True

            time.sleep(0.2)

        self.logger.error(f'SoC failed run into uboot mode, output:\n{content}')
        return False

    def boot(self, link: str = None, board: str = None, uart_opt: str = None):
        self.logger.info(f"it's going to run uartboot, link: {link}, board: {board}")

        for boot_method in self.boot_config['uart_boot_methods']:
            if board in list(self.board_config.keys()):
                uart_boot_method = boot_method
                break
        else:
            self.logger.error(f"board: {board}, is not supported to boot by this tool")
            return False

        if not self.__device_run_uart_start(uart_opt):
            return False

        if link is not None:
            # 指定img_package路径升级
            if os.path.isdir(link):
                self.img_packages = os.path.join(link)
                self.logger.info(f"using img package: {self.img_packages}")
            else:
                if not os.path.exists(self.img_packages):
                    os.makedirs(self.img_packages)

                # 指定ota包路径升级
                if os.path.isfile(link):
                    ota_package_path = os.path.abspath(link)
                # 指定url下载ota包并升级
                else:
                    result, ota_package_path = self.__download_package(link, board)
                    if not result:
                        self.logger.error(
                            f"link is neither a ota update file nor a valid ota zip url for download: {link}"
                        )
                        return False

                self.logger.info(f"using ota package: {ota_package_path}")

                try:
                    with zipfile.ZipFile(ota_package_path, "r") as zip_ref:
                        zip_ref.extractall(self.img_packages)
                except zipfile.BadZipFile as e:
                    self.logger.error(f"{e}")
                    return False
                self.logger.info(
                    f"succeed unzip {ota_package_path} to {self.img_packages}"
                )
        else:
            for path in glob.glob(f'./out/release*/target/product/img_packages'):
                if re.findall(rf'./out/release.*/target/product/img_packages', path) and os.path.isdir(path):
                    self.img_packages = path
                    self.logger.info(f'not force to get ota package, try to use images in {self.img_packages}')
                    break
            else:
                self.logger.error(
                    f"not force to get ota package, but no img packages dir at "
                    f"./out/release.*/target/product/img_packages"
                )
                return False

        if not self.__prepare_mcu_package(board, uart_boot_method['loading_step'], uart_boot_method['mcu_package']):
            return False

        if not self.__host_run_uartboot(board, uart_boot_method['loading_step']):
            return False

        self.logger.info(f"succeed to boot {board}")
        return True


def main(args=None):
    if args is None:
        args = sys.argv[1:]

    try:
        config_root_path = importlib.util.find_spec('cicd').submodule_search_locations[
            0
        ]
    except Exception:
        config_root_path = "."
    board_config = json.loads(
        open(config_root_path + "/config/device/board.json", "rb")
        .read()
        .decode("utf-8")
    )
    device_config_path = "/dev/serial/by-name/cicd-vw/device.json"
    default_board_type = None
    if os.path.exists(device_config_path):
        device_config = json.loads(
            open(device_config_path, "rb").read().decode("utf-8")
        )
        default_board_type = device_config.get("hostname", None)

    support_boards = list(board_config.keys())

    parser = argparse.ArgumentParser(description="uart boot tools")
    parser.add_argument(
        "-u",
        dest="link",
        type=str,
        help='specific the link of package, "path/to/package" or "url to package" or "latest"',
    )
    parser.add_argument(
        "-b",
        dest="board",
        choices=support_boards,
        default=default_board_type,
        help=f"specify board type, default: {default_board_type}",
    )
    parser.add_argument(
        "-l",
        dest="level",
        choices=logging._nameToLevel.keys(),
        default="DEBUG",
        type=str,
        help="log level",
    )
    parser.add_argument(
        "-t",
        dest="uart_opt",
        choices=["mcu goto uart", "mcu reboot", "manual operation"],
        default="mcu goto uart",
        type=str,
        help="way to set mcu into uart boot mode, default: mcu goto uart",
    )
    args = parser.parse_args()

    logger = logging.getLogger(__name__)
    logger.setLevel(logging._nameToLevel.get(args.level, logging.INFO))
    formatter = logging.Formatter(
        fmt="%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    result = Uartboot(logger).boot(args.link, args.board, args.uart_opt)
    print(
        f'{"succeed" if result == True else "failed"} '
        f"to boot by uart, "
        f'package info: {"latest" if args.link == "latest" else args.link}, '
        f"device info: {args.board}"
    )
    exit(not result)


if __name__ == "__main__":
    main()
