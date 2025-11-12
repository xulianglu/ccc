# -*- coding:utf-8 -*-

import serial
import time
import logging
import sys
import argparse
import importlib.util
import json
import os
from ecdsa import SigningKey
from hashlib import sha256

logging.basicConfig(
    level=logging.DEBUG,
)


class UnlockMCU(argparse.Action):
    unlock_key = "pkcs8.key"

    def __call__(self, parser, namespace, values, option_string=None):
        self.logger.info("Unlocking MCU...")

        with open(os.path.join(self.mcu_firmware, "certificate.crt"), "rb") as f:
            certficate = f.read().decode("utf-8").strip()

        chunk_size = 60
        chunks = [
            certficate[i : i + chunk_size]
            for i in range(0, len(certficate), chunk_size)
        ]
        for idx, chunk in enumerate(chunks):
            result = self.__send_command(
                f"shell_cmd_SentCert {len(certficate)} {idx + 1} {1 if idx == len(chunks) - 1 else 0} {len(chunk)} {chunk}"
            )

            if "Successfully received data" not in result:
                self.logger.error(f"Failed to send certificate chunk{idx + 1}")
                exit(1)

        random_data = result.split("Rondom numbers are:")[-1]
        self.logger.info(f"random seed: {random_data}")

        signature = self.__gen_signature(random_data).hex()
        self.logger.info(
            f"signature: {signature}",
        )

        chunk_size = 50
        chunks = [
            signature[i : i + chunk_size]
            for i in range(0, len(signature), chunk_size)
        ]
        for idx, chunk in enumerate(chunks):
            result = self.__send_command(
                f"shell_cmd_SentSignature {len(signature)} {idx + 1} {1 if idx == len(chunks) - 1 else 0} {len(chunk)} {chunk}"
            )

            if "Successfully received data" not in result:
                self.logger.error(f"Failed to send signature chunk{idx + 1}")
                exit(1)

        verify_result = "Debug mode ON!" in result

        self.logger.info(f"signature verify {'success' if verify_result else 'failed'}")
        exit(0 if verify_result else 1)

    def __init__(self, logger: logging.Logger = logging.getLogger(), *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.logger = logger
        try:
            config_root_path = importlib.util.find_spec(
                "cicd"
            ).submodule_search_locations[0]
        except Exception:
            config_root_path = "."

        self.mcu_firmware = config_root_path + "/config/mcu_firmware"

        self.serial_param = json.loads(
            open(config_root_path + "/config/device/connect_param.json", "rb")
            .read()
            .decode("utf-8")
        )["serial"]

        self.logger.info(
            f"open mcu serial port: {self.serial_param['mcu']['port']}, baudrate: {self.serial_param['mcu']['baudrate']}"
        )
        self.mcu_serial = serial.Serial(
            self.serial_param["mcu"]["port"],
            self.serial_param["mcu"]["baudrate"],
            timeout=1,
        )

    def __send_command(self, command):
        for byte in f"{command}\r\n".encode("utf-8"):
            self.mcu_serial.write([byte])
            time.sleep(0.01)
        self.mcu_serial.flush()

        output = bytes()
        time_limit = time.time() + 3
        while time.time() < time_limit:
            if not self.mcu_serial.in_waiting:
                if not len(output):
                    time.sleep(0.01)
                else:
                    break
            else:
                while self.mcu_serial.in_waiting > 0 and time.time() < time_limit:
                    output += self.mcu_serial.read(self.mcu_serial.in_waiting)
                    time.sleep(0.1)

        self.logger.debug(output.decode("utf-8", "ignore"))
        return output.decode("utf-8", "ignore")

    def __gen_signature(self, data: str):
        with open(os.path.join(self.mcu_firmware, "pkcs8.key"), "rb") as f:
            private_key = SigningKey.from_pem(f.read(), hashfunc=sha256)

        return private_key.sign(bytes.fromhex(data), hashfunc=sha256)


def main(args=None):
    if args is None:
        args = sys.argv[1:]

    parser = argparse.ArgumentParser(description="Unlock MCU via serial port")
    parser.add_argument(
        "-u", type=str, help="unlock mcu", action=UnlockMCU, metavar="", nargs=0
    )
    parser.add_argument(
        "-l",
        dest="level",
        choices=logging._nameToLevel.keys(),
        default="DEBUG",
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
