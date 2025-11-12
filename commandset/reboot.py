#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import random
import logging
import sys
import argparse
import time
import requests
import redis
import json
import socket
from abc import ABC, abstractmethod
from pymodbus.client import ModbusTcpClient


class BaseRelay(ABC):
    """
    抽象基类，定义继电器的通用接口
    """

    MAX_PORT_NUM = 16
    PORT_ON = 0
    PORT_OFF = 1
    REBOOT_INTERVAL = 0.5

    def __init__(self, logger=logging.getLogger()):
        device_config: dict = json.loads(
            open("/dev/serial/by-name/cicd-vw/device.json", "rb").read().decode("utf-8")
        )
        relay_config: dict = device_config["relay_intf"]
        self.relay_ip = relay_config["server_addr"]
        # self.relay_lock = redis.Redis(host=relay_config["client_addr"]).lock(...)  # 移除
        self.relay_port = device_config["power_port"]
        self.logger = logger
        self.relay_type = relay_config.get("type") or "default"

    @abstractmethod
    def _port_on(self, port):
        """打开指定端口"""
        pass

    @abstractmethod
    def _port_off(self, port):
        """关闭指定端口"""
        pass

    @abstractmethod
    def _port_reboot(self, port):
        """重启指定端口"""
        pass

    def execute(self, action, port: int = None):
        """
        执行指定的动作（on, off, reboot）
        """
        port = port or self.relay_port
        if port > self.MAX_PORT_NUM:
            self.logger.error(
                f"dest port {port} is over than max port num {self.MAX_PORT_NUM}"
            )
            return False

        self.logger.debug(f"it's going to run power {action}, port: {port}")

        if not hasattr(self, f"_port_{action}"):
            self.logger.error(f"invalid action: {action}")
            return False

        method = getattr(self, f"_port_{action}")
        return method(port)


class Relay_default(BaseRelay):
    """
    继电器品牌 A 的实现
    """

    def __init__(self, logger=logging.getLogger()):
        super().__init__(logger)
        device_config: dict = json.loads(
            open("/dev/serial/by-name/cicd-vw/device.json", "rb").read().decode("utf-8")
        )
        relay_config: dict = device_config["relay_intf"]
        self.relay_lock = redis.Redis(host=relay_config["client_addr"]).lock(
            "carizon_relay",
            timeout=self.REBOOT_INTERVAL + 3.5,
            sleep=0.1,
            blocking=True,
            blocking_timeout=self.REBOOT_INTERVAL + 3.5,
        )

    def __port_ctrl(self, port):
        try:
            req = requests.get(
                f"http://{self.relay_ip}/CN/httpapi.json?sndtime={str(random.random())}&CMD=UART_WRITE&UWHEXVAL={str(port)}"
            )
        except BaseException as e:
            self.logger.error(f"{type(e).__name__}, {e}")
            return None

        ports_status = int(req.text.split(",")[0])
        return [ports_status >> port_num & 1 for port_num in range(self.MAX_PORT_NUM)]

    def __get_port_status(self, port):
        return self.__port_ctrl(0)[port - 1]

    def _port_on(self, port):
        self.__port_ctrl(port)
        return self.__get_port_status(port) == self.PORT_ON

    def _port_off(self, port):
        self.__port_ctrl(port)
        return self.__get_port_status(port) == self.PORT_OFF

    def _port_reboot(self, port):
        return (
            self._port_off(port)
            and not time.sleep(self.REBOOT_INTERVAL)
            and self._port_on(port)
        )

    def execute(self, action, port: int = None):
        port = port or self.relay_port
        if port > self.MAX_PORT_NUM:
            self.logger.error(
                f"dest port {port} is over than max port num {self.MAX_PORT_NUM}"
            )
            return False

        self.logger.debug(f"it's going to run power {action}, port: {port}")

        if not hasattr(self, f"_port_{action}"):
            self.logger.error(f"invalid action: {action}")
            return False

        if port != self.relay_port:
            try:
                force = (
                    input(
                        f"you are trying to set port{port} which not beyond your docker {action}, are you sure? [y/n]\n"
                    )
                    .strip()
                    .lower()
                )
                if force == "n":
                    self.logger.info(f"user canceled dangerous action")
                    return False
                elif force != "y":
                    self.logger.info(
                        f"invalid input. please enter 'y' for yes or 'n' for no"
                    )
                    return False
            except KeyboardInterrupt:
                self.logger.info(f"user canceled dangerous action")
                return False

        if self.relay_lock.acquire():
            try:
                status = self.__get_port_status(port)
                if status == self.PORT_ON and action == "on":
                    return True
                elif status == self.PORT_OFF and action == "off":
                    return True

                ret = getattr(self, f"_port_{action}")(port)
                self.logger.info(
                    f'{"succeed" if ret else "failed"} to set port{port} {action}'
                )
                return ret
            finally:
                self.relay_lock.release()
        else:
            self.logger.error(f"failed to operate relay, someone else may using")
            return False


class Relay_zqwl(BaseRelay):
    """
    智嵌物联，使用 MODBUS TCP 协议
    """

    SERVER_PORT = 1030  # 默认 MODBUS TCP 端口号

    def __init__(self, logger=logging.getLogger()):
        super().__init__(logger)
        try:
            # 连接控制板
            self.client = ModbusTcpClient(
                self.relay_ip, port=Relay_zqwl.SERVER_PORT, timeout=5
            )
            if self.client.connect():
                self.logger.debug("Connected to the controller.")
        except Exception as e:
            self.logger.error(f"Failed to connect to the controller.\n{e}")

    def __get_port_status(self, port):
        return self.client.read_coils(address=port, count=1).bits[0]

    def _port_on(self, port):
        port = port - 1  # MODBUS 寄存器地址从 0 开始
        if self.__get_port_status(port) == False:
            self.logger.info(f"Port {port} is already ON.")
            return True

        self.logger.debug(f"Turning on port {port}")
        return self.client.write_coil(address=port, value=False).isError() == False

    def _port_off(self, port):
        port = port - 1  # MODBUS 寄存器地址从 0 开始
        if self.__get_port_status(port) == True:
            self.logger.info(f"Port {port} is already OFF.")
            return True

        self.logger.debug(f"Turning off port {port}")
        return self.client.write_coil(address=port, value=True).isError() == False

    def _port_reboot(self, port):
        return (
            self._port_off(port)
            and not time.sleep(self.REBOOT_INTERVAL)
            and self._port_on(port)
        )


class Relay_corx(BaseRelay):
    """
    继电器品牌 B 的实现，使用 MODBUS TCP 协议
    """

    SERVER_PORT = 502  # 默认 MODBUS TCP 端口号

    def _send_modbus_command(self, port, action):
        """
        发送 MODBUS TCP 数据包控制继电器
        :param port: 选择第几路（十进制输入，如 19 表示第 19 路）
        :param action: 动作，支持 'on', 'off'
        """
        # 固定的 MODBUS TCP 数据包头部
        header = "0000000000060105"

        # 计算寄存器地址（16 进制表示的端口号）
        port_hex = f"{port - 1:04X}"  # 减 1 是因为寄存器地址从 0 开始

        # 根据 action 决定最后 4 位的内容
        if action == "on":
            action_hex = "FF00"
        elif action == "off":
            action_hex = "0000"
        else:
            raise ValueError("动作参数仅支持 'on' 或 'off'")

        # 拼接完整的 MODBUS TCP 数据包
        modbus_tcp_packet = bytes.fromhex(header + port_hex + action_hex)

        try:
            # 创建 TCP socket
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client_socket:
                # 连接到服务器
                client_socket.connect((self.relay_ip, self.SERVER_PORT))
                self.logger.debug(
                    f"已连接到 MODBUS TCP 服务器 {self.relay_ip}:{self.SERVER_PORT}"
                )

                # 发送数据包
                client_socket.sendall(modbus_tcp_packet)
                self.logger.debug(f"发送数据包: {modbus_tcp_packet.hex()}")

                # 接收服务器的响应
                response = client_socket.recv(1024)  # 接收最多 1024 字节
                self.logger.debug(f"接收到响应: {response.hex()}")
                return True

        except Exception as e:
            self.logger.error(f"发送 MODBUS TCP 命令时发生错误: {e}")
            return False

    def _port_on(self, port):
        self.logger.debug(f"Relay_corx: Turning on port {port}")
        return self._send_modbus_command(port, "on")

    def _port_off(self, port):
        self.logger.debug(f"Relay_corx: Turning off port {port}")
        return self._send_modbus_command(port, "off")

    def _port_reboot(self, port):
        self.logger.debug(f"Relay_corx: Rebooting port {port}")
        return (
            self._port_off(port)
            and not time.sleep(self.REBOOT_INTERVAL)
            and self._port_on(port)
        )


class RelayFactory:
    """
    工厂类，根据继电器类型创建对应的 Relay 实例
    """

    @staticmethod
    def create_relay(relay_type, logger=logging.getLogger()):
        try:
            # 动态拼接类名并获取类对象
            relay_class = globals()[f"Relay_{relay_type}"]
            return relay_class(logger)
        except KeyError:
            raise ValueError(f"Unsupported relay type: {relay_type}")


class Relay:
    """
    兼容旧代码的 Relay 类，内部使用 RelayFactory 动态创建实例
    """

    def __init__(self, logger=logging.getLogger()):
        # 加载设备配置
        device_config: dict = json.loads(
            open("/dev/serial/by-name/cicd-vw/device.json", "rb").read().decode("utf-8")
        )
        relay_config: dict = device_config["relay_intf"]

        # 从配置文件中获取继电器类型和 IP
        self.relay_type = relay_config.get("type", "default")
        self.relay_ip = relay_config.get("server_addr", "192.168.3.133")
        self.relay_port = device_config["power_port"]
        self.logger = logger

        # 使用 RelayFactory 创建具体的继电器实例
        self.relay_instance = RelayFactory.create_relay(self.relay_type, self.logger)

    def execute(self, action, port: int = None):
        """
        兼容旧代码的 execute 方法，调用具体继电器实例的 execute 方法
        """
        port = port or self.relay_port
        return self.relay_instance.execute(action, port)


def main(args=None):
    if args is None:
        args = sys.argv[1:]

    # 加载设备配置
    device_config: dict = json.loads(
        open("/dev/serial/by-name/cicd-vw/device.json", "rb").read().decode("utf-8")
    )
    relay_config: dict = device_config["relay_intf"]

    parser = argparse.ArgumentParser(description="Reboot control")
    parser.add_argument(
        "-a",
        dest="action",
        choices=["on", "off", "reboot"],
        default="reboot",
        type=str,
        help="action to do by relay",
    )
    parser.add_argument(
        "-p",
        dest="port",
        type=int,
        help="port to be used",
        default=device_config["power_port"],
    )
    parser.add_argument(
        "-t",
        dest="relay_type",
        default=relay_config.get("type", "default"),  # 从配置文件中获取默认类型
        type=str,
        help="relay type (default if not specified)",
    )
    parser.add_argument(
        "-l",
        dest="level",
        choices=logging._nameToLevel.keys(),
        default="INFO",
        type=str,
        help="log level",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging._nameToLevel[args.level],
        format="%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    logger = logging.getLogger(__name__)

    # 创建 Relay 实例
    relay = RelayFactory.create_relay(args.relay_type, logger)

    # 执行操作
    print(
        f'{"succeed" if relay.execute(args.action, args.port) else "failed"} to set port {args.port} status {args.action}'
    )


if __name__ == "__main__":
    main()
