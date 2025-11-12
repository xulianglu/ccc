# -*- coding:utf-8 -*-

import serial
import serial.tools.list_ports
import datetime
import re
import os, time, threading, queue
import logging
import stat
from ecdsa import SigningKey
from hashlib import sha256
import sys
import platform
import argparse

logging.basicConfig(level = logging.INFO,format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def find_target_device():
    """统一的设备查找方法"""
    system = platform.system()
    
    # Linux系统优先检查映射路径
    if system == "Linux":
        target_path = "/dev/serial/by-name/cicd-vw/vw-mcu"
        if os.path.exists(target_path):
            try:
                stat_info = os.stat(target_path)
                if stat.S_ISCHR(stat_info.st_mode):
                    logger.info(f"Found target character device: {target_path}")
                    return target_path
                else:
                    actual_port = os.path.realpath(target_path)
                    if os.path.exists(actual_port):
                        logger.info(f"Validated character device: {actual_port}")
                        return actual_port
            except Exception as e:
                logger.warning(f"Failed to resolve mapped path {target_path}: {e}")
    
    # 扫描所有可用串口
    try:
        ports = serial.tools.list_ports.comports()
        if not ports:
            logger.error("No serial ports found")
            return None
        
        logger.info("Available serial ports:")
        for port in ports:
            logger.info(f"  {port.device} - {port.description}")
        
        # 智能选择MCU设备
        for port in ports:
            if any(keyword in port.description.lower() for keyword in ['mcu', 'usb', 'serial', 'ch340', 'cp210', 'ft232', 'ftdi']):
                logger.info(f"Auto-selected MCU device: {port.device}")
                return port.device
        
        # 返回第一个可用串口
        selected_port = ports[0].device
        logger.info(f"Using first available port: {selected_port}")
        return selected_port
        
    except Exception as e:
        logger.error(f"Error scanning serial ports: {e}")
        return None

def parse_arguments():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description='MCU Security Debug Unlock Tool')
    parser.add_argument('port', nargs='?', help='串口号 (例如: COM6, /dev/ttyUSB0)')
    parser.add_argument('-p', '--port', dest='port_arg', help='串口号 (另一种指定方式)')
    parser.add_argument('-b', '--baudrate', type=int, default=921600, help='波特率 (默认: 921600)')
    parser.add_argument('--list-ports', action='store_true', help='列出所有可用串口并退出')
    parser.add_argument('--key-file', default='debug_pkcs8.key', help='私钥文件路径')
    parser.add_argument('--timeout', type=int, default=1, help='串口超时时间(秒)')
    return parser.parse_args()

class SerialConnect:
    def __init__(self, port, baudrate, timeout=1, key_file='debug_pkcs8.key'):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.key_file = key_file
        self._mcu_responsive = None  # MCU响应状态缓存
        
        try:
            self.ser = serial.Serial(self.port, self.baudrate, timeout=self.timeout)
            if not self.ser.isOpen():
                self.ser.open()
            logger.info(f"MCU Serial connected: {self.port}")
        except Exception as e:
            logger.error(f"Failed to initialize serial: {e}")
            raise

    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def isOpen(self):
        return self.ser is not None and self.ser.isOpen()

    def close(self):
        if self.ser:
            self.ser.close()
            self.ser = None

    def _send_command(self, data, read_response=True):
        """统一的命令发送方法"""
        if not self.isOpen():
            raise ConnectionError("Serial connection not open")
        
        try:
            # 清空输入缓冲区
            self.ser.reset_input_buffer()
            
            # 逐字符发送命令
            for char in data:
                self.ser.write(char.encode('UTF-8'))
                time.sleep(0.01)
            
            self.ser.write(b'\r\n')
            self.ser.flush()
            
            if not read_response:
                return True
                
            # 读取响应
            time.sleep(0.2)
            all_data = b''
            attempts = 0
            
            while attempts < 3:
                waiting_bytes = self.ser.in_waiting
                if waiting_bytes > 0:
                    all_data += self.ser.read(waiting_bytes)
                    attempts = 0
                else:
                    attempts += 1
                    if attempts < 3:
                        time.sleep(0.1)
            
            if not all_data:
                all_data = self.ser.read(6000)
            
            return all_data.decode('utf-8', 'ignore')
            
        except Exception as e:
            logger.error(f"Command send failed: {e}")
            return None

    def detect_mcu_responsiveness(self):
        """检测MCU是否响应 - 缓存结果"""
        if self._mcu_responsive is not None:
            return self._mcu_responsive
        
        logger.info("Detecting MCU responsiveness...")
        
        for cmd in ["help", ""]:
            try:
                response = self._send_command(cmd)
                if response and response.strip():
                    logger.info("MCU is responsive")
                    self._mcu_responsive = True
                    return True
            except:
                continue
        
        logger.info("MCU appears non-responsive (Card Platform mode)")
        self._mcu_responsive = False
        return False

    def mcu_write(self, data):
        """兼容性方法 - 保持原接口"""
        logger.info(f"Write MCU serial command: {data}")
        result = self._send_command(data)
        if result is not None:
            logger.info(f"Read serial data is {result}")
        return result

    def _send_cert_fragments(self, fragments, blind_mode=False):
        """统一的证书片段发送方法"""
        for i, fragment in enumerate(fragments, 1):
            if i % 5 == 1:
                logger.info(f"Processing fragments {i}-{min(i+4, len(fragments))}...")
            
            try:
                self._send_command(fragment, read_response=not blind_mode)
                if blind_mode:
                    time.sleep(0.8)  # 盲发送模式延迟
            except Exception as e:
                logger.warning(f"Fragment {i} failed: {e}")

    def _get_certificate_fragments(self):
        """获取证书片段列表"""
        return [
            "shell_cmd_SentCert 1232 1 0 60 308202643082020AA00302010202146C5837436B7B1C176EABC6B15BD711",
            "shell_cmd_SentCert 1232 2 0 60 C0281760B9300A06082A8648CE3D04030230818F310B3009060355040613",
            "shell_cmd_SentCert 1232 3 0 60 02434E3111300F06035504080C085368616E676861693111300F06035504",
            "shell_cmd_SentCert 1232 4 0 60 070C085368616E676861693110300E060355040A0C07434152495A4F4E31",
            "shell_cmd_SentCert 1232 5 0 60 0C300A060355040B0C034D43553113301106035504030C0A7169616E2E7A",
            "shell_cmd_SentCert 1232 6 0 60 686F6E673125302306092A864886F70D01090116167169616E2E7A686F6E",
            "shell_cmd_SentCert 1232 7 0 60 6740636172697A6F6E2E636F6D301E170D3235303432313037343935395A",
            "shell_cmd_SentCert 1232 8 0 60 170D3335303431393037343935395A30818F310B30090603550406130243",
            "shell_cmd_SentCert 1232 9 0 60 4E3111300F06035504080C085368616E676861693111300F06035504070C",
            "shell_cmd_SentCert 1232 10 0 60 085368616E676861693110300E060355040A0C07434152495A4F4E310C30",
            "shell_cmd_SentCert 1232 11 0 60 0A060355040B0C034D43553113301106035504030C0A7169616E2E7A686F",
            "shell_cmd_SentCert 1232 12 0 60 6E673125302306092A864886F70D01090116167169616E2E7A686F6E6740",
            "shell_cmd_SentCert 1232 13 0 60 636172697A6F6E2E636F6D3059301306072A8648CE3D020106082A8648CE",
            "shell_cmd_SentCert 1232 14 0 60 3D03010703420004863EF095C63298BBA03C712D6C414EACE3EA1838AA0B",
            "shell_cmd_SentCert 1232 15 0 60 EF41F0500532AA1A016B6124EDD228C634C5D849E80404F920156CD2732A",
            "shell_cmd_SentCert 1232 16 0 60 E916D292F1479E606BD3E3D8A3423040301D0603551D0E0416041405A250",
            "shell_cmd_SentCert 1232 17 0 60 53E47F7500163FB538EFB1F8B74F6DB5E2301F0603551D23041830168014",
            "shell_cmd_SentCert 1232 18 0 60 DADD4DFDB56C976FD1DA55D2C4BDF41BA123797F300A06082A8648CE3D04",
            "shell_cmd_SentCert 1232 19 0 60 030203480030450220785C165AF2ECC71B541FAA135BFB152CD01B104612",
            "shell_cmd_SentCert 1232 20 0 60 7678CC209C25A255802693022100C20FC93E12EB01D3E30FC87AF73B0E84",
            "shell_cmd_SentCert 1232 21 1 32 E8253CD2AD20F165B714CBA3DC2E29AE"
        ]

    def _extract_random_number(self, text):
        """从文本中提取随机数"""
        if not text or "Rondom numbers are:" not in text:
            return None
        
        try:
            random_part = text.split("Rondom numbers are:")[-1]
            
            # 正则表达式提取64位十六进制
            match = re.search(r'([0-9A-Fa-f]{64})', random_part)
            if match:
                random_value = match.group(1)
                logger.info(f"Random challenge extracted: {random_value}")
                return random_value
            
            # 手动清理方法
            cleaned = re.sub(r'[^0-9A-Fa-f]', '', random_part)
            if len(cleaned) >= 64:
                random_value = cleaned[:64]
                logger.info(f"Random challenge extracted (cleaned): {random_value}")
                return random_value
                
        except Exception as e:
            logger.error(f"Error extracting random number: {e}")
        
        return None

    def _get_random_interactive(self):
        """交互式获取随机数"""
        logger.info("=" * 60)
        logger.info("请查看串口终端，MCU应该已经生成随机数")
        logger.info("随机数格式: Rondom numbers are:xxxxxxxxx...")
        logger.info("请复制64位十六进制随机数并手动输入")
        logger.info("=" * 60)
        
        for attempt in range(3):
            try:
                user_input = input(f"请输入随机数 (尝试 {attempt + 1}/3): ").strip()
                
                if len(user_input) == 64 and all(c in '0123456789ABCDEFabcdef' for c in user_input):
                    logger.info(f"用户输入的随机数: {user_input}")
                    return user_input.upper()
                else:
                    logger.warning("随机数格式错误，必须是64位十六进制字符串")
                    
            except KeyboardInterrupt:
                logger.info("\n用户取消输入")
                return None
            except Exception as e:
                logger.error(f"输入错误: {e}")
        
        logger.error("多次输入失败")
        return None

    def Shell_SentCertCmd(self):
        """主证书发送方法 - 自适应模式"""
        logger.info("Starting certificate transmission...")
        
        # 发送版本命令
        self.mcu_write("mcu_version_show")
        
        # 获证书片段
        fragments = self._get_certificate_fragments()
        is_responsive = self.detect_mcu_responsiveness()
        
        if is_responsive:
            # 响应模式：标准发送
            logger.info("Using standard mode (responsive MCU)")
            self._send_cert_fragments(fragments[:-1])  # 前20个片段
            
            # 发送最后一个片段并等待随机数
            logger.info("Sending final certificate fragment...")
            result = self.mcu_write(fragments[-1])
            
            random_value = self._extract_random_number(result)
            if random_value:
                return random_value
                
            # 尝试触发
            logger.info("Attempting to trigger random number...")
            trigger_result = self.mcu_write("")
            return self._extract_random_number(trigger_result)
            
        else:
            # 非响应模式：盲发送 + 手动输入
            logger.info("Using blind mode (non-responsive MCU)")
            self._send_cert_fragments(fragments, blind_mode=True)
            return self._get_random_interactive()

    def Gen_Signature(self, random_data):
        """生成ECDSA签名"""
        logger.info("Generating ECDSA signature...")
        
        if not os.path.exists(self.key_file):
            raise FileNotFoundError(f"Private key file not found: {self.key_file}")
            
        logger.info(f"Loading private key from: {self.key_file}")
        
        data = bytes.fromhex(random_data)
        with open(self.key_file, "rb") as f:
            private_key = SigningKey.from_pem(f.read(), hashfunc=sha256)
        
        signature = private_key.sign(data, hashfunc=sha256)
        logger.info(f"Signature generated: {signature.hex()}")
        return signature

    def Shell_SentSignatureCmd(self, signature):
        """发送签名验证 - 自适应模式"""
        logger.info("Sending signature for verification...")
        
        # 分割签名数据
        sig_hex = signature.hex()
        fragments = [
            f"shell_cmd_SentSignature 128 1 0 50 {sig_hex[0:50]}",
            f"shell_cmd_SentSignature 128 2 0 50 {sig_hex[50:100]}",
            f"shell_cmd_SentSignature 128 3 1 28 {sig_hex[100:]}"
        ]
        
        if self._mcu_responsive:
            # 响应模式
            logger.info("Sending signature fragments...")
            for fragment in fragments[:-1]:
                self.mcu_write(fragment)
            
            result = self.mcu_write(fragments[-1])
            
            if result and "Signature Verify Ok" in result:
                logger.info("Signature verification successful!")
                return True
            else:
                logger.error("Signature verification failed!")
                return False
        else:
            # 盲发送模式
            logger.info("Using blind signature verification...")
            
            for i, fragment in enumerate(fragments, 1):
                logger.info(f"Blind sending signature fragment {i}/3...")
                self._send_command(fragment, read_response=False)
                time.sleep(1)
            
            logger.info("=" * 60)
            logger.info("签名片段已发送，请查看串口终端确认:")
            logger.info("   - 'Signature Verify Ok!'")
            logger.info("   - 'Debug mode ON!'")
            logger.info("=" * 60)
            
            try:
                user_confirm = input("是否看到签名验证成功? (y/n): ").strip().lower()
                if user_confirm in ['y', 'yes', '是', '1']:
                    logger.info("用户确认签名验证成功")
                    return True
                else:
                    logger.error("用户确认签名验证失败")
                    return False
            except KeyboardInterrupt:
                logger.info("\n用户取消操作")
                return False

def main():
    """主函数 - 精简版"""
    args = parse_arguments()
    
    # 列出串口
    if args.list_ports:
        ports = serial.tools.list_ports.comports()
        if ports:
            print("\n可用串口列表:")
            print("=" * 50)
            for port in ports:
                print(f"  {port.device:<15} - {port.description}")
            print("=" * 50)
        else:
            print("未找到可用串口")
        sys.exit(0)
    
    logger.info("Starting FAST MCU unlock process...")
    
    # 确定串口
    port = args.port or args.port_arg
    if not port:
        if platform.system() == "Linux":
            port = find_target_device()
        else:
            logger.error("在Windows/Mac系统上必须手动指定串口号")
            logger.info("使用方法: python SecureDebug_Serial_MCU.py COM6")
            sys.exit(1)
    
    if not port:
        logger.error("未找到可用串口")
        sys.exit(1)
    
    # 检查私钥文件
    if not os.path.exists(args.key_file):
        logger.error(f"私钥文件未找到: {args.key_file}")
        sys.exit(1)
    
    try:
        start_time = time.time()
        
        logger.info(f"使用串口: {port}")
        logger.info(f"使用波特率: {args.baudrate}")
        logger.info(f"使用私钥文件: {args.key_file}")
        
        # 执行解锁流程
        with SerialConnect(port, args.baudrate, args.timeout, args.key_file) as ser:
            # Step 1: 读取MCU版本
            logger.info("Step 1: Reading MCU version...")
            ser.mcu_write("mcu_version_show")
            
            # Step 2: 发送证书并获取挑战
            logger.info("Step 2: Sending certificate and getting challenge...")
            random_value = ser.Shell_SentCertCmd()
            
            if not random_value:
                logger.error("Failed to get random challenge from MCU")
                sys.exit(1)
            
            # Step 3: 生成签名
            logger.info("Step 3: Generating signature...")
            signature = ser.Gen_Signature(random_value)
            
            # Step 4: 验证签名
            logger.info("Step 4: Verifying signature...")
            if not ser.Shell_SentSignatureCmd(signature):
                logger.error("Signature verification failed")
                sys.exit(1)
            
            # Step 5: 最终检查
            logger.info("Step 5: Final MCU version check...")
            ser.mcu_write("mcu_version_show")
            
            end_time = time.time()
            total_time = end_time - start_time
            
            logger.info("=" * 50)
            logger.info("MCU unlock process completed successfully!")
            logger.info(f"Total time: {total_time:.2f} seconds")
            logger.info("=" * 50)
            
    except Exception as e:
        logger.error(f"MCU unlock process failed: {e}")
        sys.exit(1)

if __name__ == '__main__':
    main()