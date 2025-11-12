import sys
import os
import logging
import requests
import argparse
import importlib.util
import json
import zipfile
import shutil
import hashlib
import subprocess
import struct
import uuid
import re
import glob
from cicd.session import session
from http import HTTPStatus

class GPTParse:
    def __init__(self, image):
        self.header = {}
        self.partition_entries = []

        if not os.path.exists(image):
            return

        with open(image, 'rb') as f:
            # 跳过Protective MBR
            f.seek(512)

            # 读取92字节header
            self.header = self.__parse_header(f.read(92))

            # 跳转到partion entry部分
            f.seek(self.header['partition_entries_lba'] * 512)
            self.partition_entries = self.__parse_partion_entries(f, self.header['num_partition_entries'], self.header['size_of_partition_entry'])

    def __str__(self):
        header_info = (
            f'GPT Header:\n'
            f'  Signature: {self.header["signature"].decode()}\n'
            f'  Revision: {self.header["revision"]:#010x}\n'
            f'  Header Size: {self.header["header_size"]} bytes\n'
            f'  Header CRC: {self.header["header_crc32"]:#010x}\n'
            f'  Reserved Field: {self.header["reserved"]:#010x}\n'
            f'  Current LBA: {self.header["current_lba"]}\n'
            f'  Backup LBA: {self.header["backup_lba"]}\n'
            f'  First Usable LBA: {self.header["first_usable_lba"]}\n'
            f'  Last Usable LBA: {self.header["last_usable_lba"]}\n'
            f'  Disk GUID: {self.header["disk_guid"]}\n'
            f'  Partition Entries LBA: {self.header["partition_entries_lba"]}\n'
            f'  Number of Partition Entries: {self.header["num_partition_entries"]}\n'
            f'  Size of Partition Entry: {self.header["size_of_partition_entry"]} bytes\n'
            f'  Partition Array CRC32: {self.header["partition_array_crc32"]:#010x}\n'
        )

        partition_entries_info = '\n'.join(
            f'Partition Entry {number}:\n'
            f'  Partition Type GUID: {entry["partition_type_guid"]}\n'
            f'  Unique Partition GUID: {entry["unique_partition_guid"]}\n'
            f'  Starting LBA: {entry["starting_lba"]}\n'
            f'  Ending LBA: {entry["ending_lba"]}\n'
            f'  Attributes: {entry["attributes"]}\n'
            f'  Partition Name: {entry["name"]}\n'
            for number, entry in enumerate(self.partition_entries)
        )

        return header_info + '\n' + partition_entries_info + '\n'


    def __parse_header(self, header_block) -> dict:
        header = {}
        (
            header['signature'],
            header['revision'],
            header['header_size'],
            header['header_crc32'],
            header['reserved'],
            header['current_lba'],
            header['backup_lba'],
            header['first_usable_lba'],
            header['last_usable_lba'],
            header['disk_guid'],
            header['partition_entries_lba'],
            header['num_partition_entries'],
            header['size_of_partition_entry'],
            header['partition_array_crc32']
        ) = struct.unpack('<8sIIIIQQQQ16sQIII' , header_block)
        header['disk_guid'] = uuid.UUID(bytes_le=header['disk_guid'])
        return header

    def __parse_partion_entries(self, f, num_partition_entries, size_of_partition_entry) -> dict:
        partition_entries = []
        partition_attr_table = [
            {'id': uuid.UUID('C12A7328-F81F-11D2-BA4B-00A0C93EC93B'), 'type': 'PARTITION_SYSTEM_GUID'},
            {'id': uuid.UUID('024DEE41-33E7-11D3-9D69-0008C781F39F'), 'type': 'LEGACY_MBR_PARTITION_GUID'},
            {'id': uuid.UUID('E3C9E316-0B5C-4DB8-817D-F92DF00215AE'), 'type': 'PARTITION_MSFT_RESERVED_GUID'},
            {'id': uuid.UUID('EBD0A0A2-B9E5-4433-87C0-68B6B72699C7'), 'type': 'PARTITION_BASIC_DATA_GUID'},
            {'id': uuid.UUID('0FC63DAF-8483-4772-8E79-3D69D8477DE4'), 'type': 'PARTITION_LINUX_FILE_SYSTEM_DATA_GUID'},
            {'id': uuid.UUID('A19D880F-05FC-4D3B-A006-743F0F84911E'), 'type': 'PARTITION_LINUX_RAID_GUID'},
            {'id': uuid.UUID('0657FD6D-A4AB-43C4-84E5-0933C84B4F4F'), 'type': 'PARTITION_LINUX_SWAP_GUID'},
            {'id': uuid.UUID('E6D6D379-F507-44C2-A23C-238F2A3DF928'), 'type': 'PARTITION_LINUX_LVM_GUID'},
            {'id': uuid.UUID('3DE21764-95BD-54BD-A5C3-4ABE786F38A8'), 'type': 'PARTITION_U_BOOT_ENVIRONMENT'}
        ]

        for _ in range(num_partition_entries):
            entry_data = f.read(size_of_partition_entry)
            if entry_data.strip(b'\x00'):
                partition_entry = {}
                (
                    partition_entry['partition_type_guid'],
                    partition_entry['unique_partition_guid'],
                    partition_entry['starting_lba'],
                    partition_entry['ending_lba'],
                    partition_entry['attributes'],
                    partition_entry['name']
                ) = struct.unpack('<16s16sQQQ72s', entry_data)
                partition_entry['partition_type_guid'] = uuid.UUID(bytes_le=partition_entry['partition_type_guid'])
                for entry_type in partition_attr_table:
                    if partition_entry['partition_type_guid'] == entry_type['id']:
                        partition_entry['partition_type_guid'] = f'{partition_entry["partition_type_guid"]} ({entry_type["type"]})'
                        break
                else:
                    partition_entry['partition_type_guid'] = f'{partition_entry["partition_type_guid"]} (Unknown)'

                partition_entry['unique_partition_guid'] = uuid.UUID(bytes_le=partition_entry['unique_partition_guid'])
                partition_entry['name'] = partition_entry['name'].decode('utf-16').rstrip('\x00')
                partition_entries.append(partition_entry)
        return partition_entries

class Fastboot:
    img_packages=os.path.abspath(f'/tmp/img_packages')
    download_timeout=600 # real    2m6.818s
    target_ipaddr = json.loads(open(importlib.util.find_spec('cicd').submodule_search_locations[0] + "/config/device/connect_param.json", "rb").read().decode("utf-8"))["ssh"]["soc"]["addr"]
    fastboot_options = {"eth": f"-s udp:{target_ipaddr}:5554 ", "usb": f""}
    board_config: dict = json.loads(
        open(
            f"{importlib.util.find_spec('cicd').submodule_search_locations[0]}/config/device/board.json",
            "rb",
        )
        .read()
        .decode("utf-8")
    )
    device_config: dict = json.loads(
        open("/dev/serial/by-name/cicd-vw/device.json", "rb").read().decode("utf-8")
    )
    def __init__(self, logger=logging.getLogger()):
        self.logger = logger

    def __connect_target(self, fastboot_type):
        max_retry_times = 20 # 防止因交换机路由表导致设置ip后网络不通
        if fastboot_type == 'eth':
            connect_cmd = f'ping {self.target_ipaddr} -c 1 -W 1'
        elif fastboot_type == 'usb':
            connect_cmd = f'sudo fastboot devices | grep -E "uboot|fastboot"'

        for retry_times in range(max_retry_times):
            try:
                subprocess.run(
                    connect_cmd,
                    shell=True,
                    timeout=1,
                    text=True,
                    check=True
                )
                self.logger.debug(f'connected target by {fastboot_type}')
                return True
            except Exception as e:
                retry_times += 1
                self.logger.warning(
                    f"try to connect target by {fastboot_type} but failed\n{e}"
                )
        else:
            self.logger.error(f"failed to connect target in {max_retry_times}s")
            return False

    def __get_host_name(self):
        return self.device_config.get("hostname")

    def __download_package(self, module, url, host_name):
        def __get_latest_package_info(module, host_name) -> str:
            jfrog_api_prefix = "https://jfrog.carizon.work/artifactory/api/storage/project-snapshot-local"
            package_path = self.board_config.get(host_name, None)

            if package_path == None:
                self.logger.error(f"no specified update packge path for {host_name}")
                return False

            # 使用jfrog api查询最新包的url
            jfrog_package_dir = f"{jfrog_api_prefix}/{package_path}"
            response = requests.get(jfrog_package_dir, params={'lastModified': ''})
            if response.status_code != HTTPStatus.OK:
                self.logger.error(f'{response.status_code}:\n{response.text}')
                return False

            # 查询到最新包的信息
            jfrog_latest_package_info_url = response.json()['uri']
            response = requests.get(jfrog_latest_package_info_url)
            if response.status_code != HTTPStatus.OK:
                self.logger.error(f'{response.status_code}:\n{response.text}')
                return False

            latest_package_size = int(response.json()['size'])
            latest_package_md5 = response.json()['checksums']['md5']
            latest_package_url = response.json()['downloadUri']
            self.logger.debug(f'latest bsp package url: {latest_package_url}')
            return latest_package_url, latest_package_size, latest_package_md5

        def __check_latest_package(latest_package_size, latest_package_md5) -> bool:
            # 文件大小判断
            cur_size = os.path.getsize(f'{package_path}')
            if cur_size != latest_package_size:
                self.logger.error(f'invalid bsp package size, actual: {cur_size}B, expect: {latest_package_size}B')
                return False
            self.logger.debug(f'bsp package size validate pass')

            # 文件完整性判断
            md5 = hashlib.md5()
            with open(f'{package_path}', 'rb') as file:
                for chunk in iter(lambda: file.read(1024 * 1024), b''):
                    md5.update(chunk)
            cur_md5 = md5.hexdigest()
            if cur_md5 != latest_package_md5:
                self.logger.error(f'invalid bsp package md5, actual: {cur_md5}, expect: {latest_package_md5}')
                return False
            self.logger.debug(f'bsp package md5 validate pass')

            return True

        if url == 'latest':
            package_url, package_size, package_md5 = __get_latest_package_info(
                module, host_name
            )
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
                self.logger.debug(f'succeed download latest bsp package {package_path}')
                break
            except Exception as e:
                retry_times += 1
                self.logger.warning(f'failed to download {package_name} for {retry_times} times\n{e}')
                if os.path.exists(f'{package_path}'):
                    os.remove(f'{package_path}')
        else:
            os.chdir(cur_pwd)
            self.logger.error(
                f"failed download latest bsp package after retry {max_retry_times} times"
            )
            return False, ""

        os.chdir(cur_pwd)

        if url == 'latest' and not __check_latest_package(package_size, package_md5):
            self.logger.error(f'download latest package but check failed')
            return False, ''

        return True, package_path

    def __device_run_fastboot(self, fastboot_type):
        if fastboot_type == 'usb':
            start_fastboot_cmd = 'fastboot 0'
            fastboot_cmd_whitelist = [start_fastboot_cmd]
        elif fastboot_type == 'eth':
            start_fastboot_cmd = f'setenv ipaddr {self.target_ipaddr}; setenv ethact eth1; ping 192.168.2.130; fastboot udp'
            fastboot_cmd_whitelist = [f'Listening for fastboot command on {self.target_ipaddr}']
        else:
            self.logger.error(f'unsupported fastboot type {fastboot_type}')
            return False

        result = session.run_cmd('serial:uboot',
                                 f'{start_fastboot_cmd}',
                                 [],
                                 fastboot_cmd_whitelist,
                                 True,
                                 1,
                                 self.logger) == 0

        self.logger.debug(f'{"succeed" if result else "failed"} to run fastboot command on device: {start_fastboot_cmd}')
        return result

    def __host_run_fastboot(self, fastboot_type, host_name):
        def __host_run_fastboot_format_init_command(
            module, devnum=0, part="uda"
        ) -> list | bool:
            emmc_num_dict = {
                "uda": {"partnum": 0, "has_gpt": True},
                "boot0": {"partnum": 1, "has_gpt": True},
                "boot1": {"partnum": 2, "has_gpt": False},
            }
            fastboot_start_command_table = {
                "soc": [
                    {
                        "content": f"sudo fastboot {self.fastboot_options[fastboot_type]}oem interface:blk",
                        "timeout": 1,
                    },  # eth: 0.000      usb: 0.002
                    {
                        "content": f"sudo fastboot {self.fastboot_options[fastboot_type]}oem bootdevice:mmc",
                        "timeout": 1,
                    },  # eth: 0.000      usb: 0.002
                    {
                        "content": f"sudo fastboot {self.fastboot_options[fastboot_type]}oem runcommand:mmc partconf {devnum} 1 1 {emmc_num_dict.get(part)['partnum']}",
                        "timeout": 1,
                    },  # eth: 0.000      usb: 0.002
                ],
                "mcu": [
                    {
                        "content": f"sudo fastboot {self.fastboot_options[fastboot_type]}oem interface:mtd",
                        "timeout": 1,
                    },  # eth: 0.000      usb: 0.002
                ],
            }

            has_gpt = None
            if module == "soc":
                has_gpt = emmc_num_dict.get(part)["has_gpt"]
            elif module == "mcu":
                has_gpt = True

            return fastboot_start_command_table[module], has_gpt

        def __host_run_fastboot_format_flash_command() -> list:
            partition_flash_attribute = {
                "gpt": {
                    "timeout": {"eth": 1 + 5, "usb": 1 + 5}
                },  # eth: 0.026      usb: 0.039    reserve 5s for gpt backup
                "spl_ddr_a": {
                    "timeout": {"eth": 1000, "usb": 10}
                },  # eth:       usb: 5.915
                "spl_ddr_b": {
                    "timeout": {"eth": 1000, "usb": 10}
                },  # eth:       usb: 5.864s
                "ubootenv": {
                    "timeout": {"eth": 1000, "usb": 10}
                },  # eth:       usb: 0.102s
                "acore_cfg": {
                    "timeout": {"eth": 1, "usb": 1}
                },  # eth: 0.019      usb: 0.033
                "acore_cfg_a": {
                    "timeout": {"eth": 1, "usb": 1}
                },  # eth: 0.019      usb: 0.033
                "acore_cfg_b": {
                    "timeout": {"eth": 1, "usb": 1}
                },  # eth: 0.025      usb: 0.040
                "bl31": {"timeout": {"eth": 1, "usb": 1}},  # eth: 0.046      usb: 0.067
                "bl31_a": {
                    "timeout": {"eth": 1, "usb": 1}
                },  # eth: 0.046      usb: 0.067
                "bl31_b": {
                    "timeout": {"eth": 1, "usb": 1}
                },  # eth: 0.049      usb: 0.071
                "optee": {
                    "timeout": {"eth": 1, "usb": 1}
                },  # eth: 0.070      usb: 0.099
                "optee_a": {
                    "timeout": {"eth": 1, "usb": 1}
                },  # eth: 0.070      usb: 0.099
                "optee_b": {
                    "timeout": {"eth": 1, "usb": 1}
                },  # eth: 0.074      usb: 0.104
                "uboot": {
                    "timeout": {"eth": 10, "usb": 10}
                },  # eth: 0.109      usb: 0.140
                "uboot_a": {
                    "timeout": {"eth": 10, "usb": 10}
                },  # eth: 0.109      usb: 0.140
                "uboot_b": {
                    "timeout": {"eth": 10, "usb": 10}
                },  # eth: 0.107      usb: 0.150
                "vbmeta_a": {
                    "timeout": {"eth": 1, "usb": 1}
                },  # eth: 0.048      usb: 0.077
                "vbmeta_b": {
                    "timeout": {"eth": 1, "usb": 1}
                },  # eth: 0.051      usb: 0.082
                "boot_a": {
                    "timeout": {"eth": 10, "usb": 10}
                },  # eth: 3.792      usb: 4.817
                "boot_b": {
                    "timeout": {"eth": 10, "usb": 10}
                },  # eth: 3.840      usb: 4.807
                "system_a": {
                    "timeout": {"eth": 300, "usb": 500}
                },  # eth: 146.607    usb: 312.929
                "system_b": {
                    "timeout": {"eth": 300, "usb": 500}
                },  # eth: 147.521    usb: 312.503
                "system_verity_a": {
                    "timeout": {"eth": 5, "usb": 5}
                },  # eth: 2.681      usb: 2.869
                "system_verity_b": {
                    "timeout": {"eth": 5, "usb": 5}
                },  # eth: 2.717      usb: 2.885
                "basesystem_a": {
                    "timeout": {"eth": 50, "usb": 100}
                },  # eth: 29.414     usb: 60.875
                "basesystem_b": {
                    "timeout": {"eth": 50, "usb": 100}
                },  # eth: 29.426     usb: 61.013
                "app_param": {
                    "timeout": {"eth": 1000, "usb": 50}
                },  # eth:       usb: 30.662s
                "app_param_bak": {
                    "timeout": {"eth": 1000, "usb": 50}
                },  # eth:       usb: 30.466s
                "emmc_boot1": {
                    "timeout": {"eth": 1000, "usb": 10}
                },  # eth:       usb: 4.873
            }

            target_data_jsons = [
                file
                for file in os.listdir(self.img_packages)
                if re.match(f"data.*{host_name}.*json", file)
            ]
            if not len(target_data_jsons):
                self.logger.error(
                    f"no data json in {self.img_packages} for {host_name}"
                )
                return []

            data_json = None

            lts_data_jsons = [
                data_json
                for data_json in target_data_jsons
                if re.match(
                    f"data.*{host_name}_[Vv]{{1}}[0-9]{{1}}.[0-9]{{1}}.*json", data_json
                )
            ]
            if not len(lts_data_jsons):
                self.logger.info(
                    f"no lts data json in {self.img_packages} for {host_name}, using default {target_data_jsons[0]}"
                )
                data_json = target_data_jsons[0]
            else:
                data_json = sorted(
                    lts_data_jsons,
                    key=lambda x: tuple(
                        map(float, re.search(r"_V(\d+)\.(\d+)", x).groups())
                    ),
                )[-1]

            self.logger.info(f"using data file {data_json} for upgrading {host_name}")

            data_dict: dict = json.loads(
                open(f"{self.img_packages}/{data_json}", "rb").read().decode("utf-8")
            )
            self.logger.info(f"{data_json} version: {data_dict['version']}")
            data_dict["images"] = {
                f"gpt_main_{host_name}_emmc.img": {
                    "name": f"gpt_main_{host_name}_emmc.img",
                    "size": os.path.getsize(
                        f"{self.img_packages}/gpt_main_{host_name}_emmc.img"
                    ),
                    "storages": {"emmc": {"sync": None, "part_info": ["gpt"]}},
                },
                f"gpt_main_{host_name}_emmc_boot0.img": {
                    "name": f"gpt_main_{host_name}_emmc_boot0.img",
                    "size": os.path.getsize(
                        f"{self.img_packages}/gpt_main_{host_name}_emmc_boot0.img"
                    ),
                    "storages": {"emmc_boot0": {"sync": None, "part_info": ["gpt"]}},
                },
                **data_dict["images"],
            }

            flash_commands = []
            for image_info in data_dict["images"].keys():
                image_flash_command = []
                has_gpt = None
                for medium in data_dict["images"][image_info]["storages"].keys():
                    if medium == "emmc":
                        flash_pre_command, has_gpt = (
                            __host_run_fastboot_format_init_command("soc", part="uda")
                        )
                    elif medium == "emmc_boot0":
                        flash_pre_command, has_gpt = (
                            __host_run_fastboot_format_init_command("soc", part="boot0")
                        )
                    elif medium == "emmc_boot1":
                        flash_pre_command, has_gpt = (
                            __host_run_fastboot_format_init_command("soc", part="boot1")
                        )
                    elif medium == "nor":
                        flash_pre_command, has_gpt = (
                            __host_run_fastboot_format_init_command("mcu")
                        )
                    else:
                        self.logger.info(f"unknown medium {medium}")
                        continue

                    image_flash_command.extend(flash_pre_command)

                    for part in data_dict["images"][image_info]["storages"][medium][
                        "part_info"
                    ]:
                        image_flash_part_command = {}
                        image_flash_part_command["timeout"] = (
                            partition_flash_attribute.get(part)["timeout"][
                                fastboot_type
                            ]
                        )

                        image_flash_part_command["content"] = (
                            f"sudo fastboot {self.fastboot_options[fastboot_type]}flash {part if has_gpt else '0'} "
                            f'{"-S 32M " if os.path.getsize(self.img_packages + "/" + data_dict["images"][image_info]["name"]) / (1024 * 1024) > 32 else ""}'
                            f'{self.img_packages}/{data_dict["images"][image_info]["name"]}'
                        )

                        image_flash_command.append(image_flash_part_command)
                flash_commands.extend(image_flash_command)

            self.logger.debug(json.dumps(flash_commands, indent=4, ensure_ascii=False))
            return flash_commands

        def __host_run_fastboot_format_deinit_command() -> list:
            return [
                {
                    "content": f"sudo fastboot {self.fastboot_options[fastboot_type]} reboot",
                    "timeout": 1,
                },  # eth: 0.000      usb: 0.352
            ]

        host_run_fastboot_step = {
            "flash": __host_run_fastboot_format_flash_command,
            "deinit": __host_run_fastboot_format_deinit_command,
        }

        for step in host_run_fastboot_step.keys():
            command_retry_times = 3
            for command in host_run_fastboot_step[step]():
                for retry_times in range(command_retry_times):
                    if (
                        session.run_cmd(
                            f"local",
                            f'{command["content"]}',
                            [],
                            ["Finished."],
                            True,
                            command["timeout"],
                            self.logger,
                        )
                        == 0
                    ):
                        self.logger.debug(
                            f'succeed to run fastboot command: {command["content"]}'
                        )
                        break
                    self.logger.warning(
                        f'failed to run fastboot command: {command["content"]} {retry_times + 1} times'
                    )
                else:
                    self.logger.error(
                        f"failed to run fastboot command, retry times exceed max times ({command_retry_times} times)"
                    )
                    return False
        self.logger.debug(f"succeed to run all fastboot command")
        return True

    def upgrade(
        self,
        fastboot_type: str = "usb",
        link: str = None,
        module: str = "soc",
        host: str = None,
    ):
        host_name = host if host else self.__get_host_name()
        if host_name == None:
            self.logger.error(
                "not specify host and failed to get host name for selecting update package"
            )
            return False

        self.logger.debug(
            f"it's going to run fastboot {fastboot_type} on {host_name}, package: {link}"
        )

        if link is not None:
            if not os.path.exists(self.img_packages):
                os.makedirs(self.img_packages)

            if os.path.isfile(link):
                ota_package = os.path.abspath(link)
            else:
                result, ota_package = self.__download_package(module, link, host_name)
                if not result:
                    self.logger.error(f'link is neither a ota update file nor a valid ota zip url for download: {link}')
                    return False

            self.logger.debug(f'using ota package: {ota_package}')

            try:
                with zipfile.ZipFile(ota_package, 'r') as zip_ref:
                    zip_ref.extractall(self.img_packages)
            except zipfile.BadZipFile as e:
                self.logger.error(f'{e}')
                return False

            if module == 'mcu':
                if not os.path.exists(f'{self.img_packages}/IMG/'):
                    self.logger.error(f'no IMG directory at {self.img_packages}')
                    return False
                for image in os.listdir(f'{self.img_packages}/IMG/'):
                    shutil.copy2(os.path.join(f'{self.img_packages}/IMG/', image), os.path.join(self.img_packages, image))

            self.logger.debug(f'succeed unzip {ota_package} to {self.img_packages}')

        else:
            for path in glob.glob(f'./out/release*/target/product/img_packages'):
                match = re.findall(rf'./out/release.*/target/product/img_packages', path) and os.path.isdir(path)
                if match:
                    self.img_packages = path
                    self.logger.debug(f'not force to get ota package, try to use images in {self.img_packages}')
                    break
            else:
                self.logger.error(
                    f"not force to get ota package, but no img packages dir at ./out/release.*/target/product/img_packages"
                )
                return False

        if not self.__device_run_fastboot(fastboot_type):
            return False

        if not self.__connect_target(fastboot_type):
            return False

        if not self.__host_run_fastboot(fastboot_type, host_name):
            return False

        self.logger.debug(f"succeed to fastboot update target {host_name}")
        return True


def main(args=None):
    if args is None:
        args = sys.argv[1:]

    board_config = json.loads(open(
        importlib.util.find_spec('cicd').submodule_search_locations[0] + "/config/device/board.json", 'rb').read().decode('utf-8'))
    device_config: dict = json.loads(
        open("/dev/serial/by-name/cicd-vw/device.json", "rb").read().decode("utf-8")
    )
    supported_boards = "\n".join(f"\033[32m{key}\033[0m" for key in board_config.keys())
    parser = argparse.ArgumentParser(
        description=f"Fastboot utility, support list:\n{supported_boards}",
        formatter_class=argparse.RawTextHelpFormatter,
    )

    parser.add_argument(
        "-t",
        dest="type",
        choices=["usb", "eth"],
        default="usb",
        type=str,
        help="communication way for fastboot, default: usb",
    )
    parser.add_argument(
        "-u",
        dest="link",
        type=str,
        help='specific the link of package, "path/to/package" or "url to package" or "latest".\nWARNING: Not recommand to use "latest" option when fastboot update "mcu"',
    )
    parser.add_argument(
        "-d",
        dest="host",
        help=f"specify target host, default: {device_config.get('hostname')}",
    )
    parser.add_argument(
        "-m",
        dest="module",
        choices=["soc", "mcu"],
        help="specify flash module, default: soc",
        default="soc",
    )
    parser.add_argument('-l', dest='level', choices=logging._nameToLevel.keys(), default='DEBUG', type=str, help='log level')
    args = parser.parse_args()

    logging.basicConfig(level=logging._nameToLevel[args.level],
                        format='%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S',
                        handlers=[
                            logging.StreamHandler(sys.stdout)
                        ])

    result = Fastboot(logging.getLogger(__name__)).upgrade(
        args.type, args.link, args.module, args.host
    )
    print(
        f'{"succeed" if result == True else "failed"} '
        f"to do fastboot {args.module} upgrade by {args.type}, "
        f'package info: {"latest" if args.link == "latest" else args.link}, '
        f'device info: {"get by device.json" if args.host is None else args.host}'
    )
    exit(not result)
