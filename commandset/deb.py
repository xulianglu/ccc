import logging
import sys
import argparse
import json
import importlib.util
import subprocess
import os
import shutil
import re
from cicd.state import state_machine


class DebianPackage():
    src_deb_dir = '/tmp'
    dst_deb_dir = '/tmp'
    download_deb_timeout = 50
    send_deb_timeout = 10
    install_deb_timeout = 10

    def __init__(self, logger=logging.getLogger()):
        self.logger = logger

    def __download_deb(self, name: str, platform: str, arch: str):
        curdir = os.getcwd()
        apt_options=''

        deb_dir = f'{self.src_deb_dir}/{name}'
        if os.path.exists(deb_dir):
            shutil.rmtree(deb_dir)
        os.mkdir(deb_dir)
        os.chdir(deb_dir)

        result = False
        try:
            platform_name = platform.lower()
            self.logger.debug(f'The platform_name is :  {platform_name}')
            platform_urls = {
                "j6h": "https://jfrog.carizon.work/artifactory/api/storage/aarch64-bsp-j6h/pool/runtime-pkg",
                "j6m": "https://jfrog.carizon.work/artifactory/api/storage/aarch64-bsp/pool/runtime-pkg"
            }
            for key, url in platform_urls.items():
                if key in platform_name:
                    debian_package_url = url
                    self.logger.debug(f'... find package url:  {debian_package_url}')
                    break
                debian_package_url = "unknown"
            if debian_package_url == "unknown" :
                self.logger.debug(f'...Please confirm the platform {platform} is not support, url is {debian_package_url}')
            packages = json.loads(subprocess.run(f'curl -s {debian_package_url}',
                                                 shell=True,
                                                 timeout=10,
                                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                                 text=True, check=True).stdout)['children']
            package_info_url = ''
            for package in packages:
                if len(re.findall(f'{name}.*{arch}', package['uri'])) > 0:
                    package_info_url = debian_package_url + package['uri']
                    break
            if not package_info_url:
                self.logger.error(f'didn\'t find {name}:{arch} in packages:\n{packages}\n')
                raise

            package_info = json.loads(subprocess.run(f'curl -s {package_info_url}',
                                                     shell=True,
                                                     timeout=10,
                                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                                     text=True, check=True).stdout)
            if 'downloadUri' not in package_info:
                self.logger.error(f'didn\'t find downloadUri key in {name}:{arch} package info:\n{package_info}\n')
                raise

            result = subprocess.run(f'wget {package_info["downloadUri"]}',
                                    shell=True,
                                    timeout=self.download_deb_timeout,
                                    text=True, check=True).returncode == 0

        except subprocess.TimeoutExpired:
            self.logger.error(f'time out to download {name} deb package in {self.download_deb_timeout}s')
        except subprocess.CalledProcessError as e:
            self.logger.error(f'failed to download {name} deb package, and return non-zero: {e.returncode}\n{e}')
        finally:
            os.chdir(curdir)
            return result

    def __push_deb_to_device(self, package_path, devinfo):
        try:
            return subprocess.run(f'scp {package_path} {devinfo["user"]}@{devinfo["addr"]}:{self.dst_deb_dir}',
                                  shell=True,
                                  timeout=self.send_deb_timeout,
                                  text=True, check=True).returncode == 0
        except subprocess.TimeoutExpired:
            self.logger.error(f'time out to send {package_path} deb package to {devinfo["name"]} in {self.send_deb_timeout}s')
        except subprocess.CalledProcessError as e:
            self.logger.error(f'send {package_path} deb package to {devinfo["name"]}, and return non-zero: {e.returncode}\n{e}')

        return False

    def __install_deb_on_device(self, name, devinfo):
        dpkg_options = ''

        try:
            return subprocess.run(f'ssh {devinfo["user"]}@{devinfo["addr"]} dpkg -i {dpkg_options} {self.dst_deb_dir}/{name}',
                                  shell=True,
                                  timeout=self.send_deb_timeout,
                                  text=True, check=True).returncode == 0
        except subprocess.TimeoutExpired:
            self.logger.error(f'time out to install {name} deb package to {devinfo["name"]} in {self.send_deb_timeout}s')
        except subprocess.CalledProcessError as e:
            self.logger.error(f'install {name} deb package to {devinfo["name"]}, and return non-zero: {e.returncode}\n{e}')

        return False

    def install(self, package: str = None, platform: str = 'j6', arch: str = 'arm64'):
        self.logger.debug(f'it\'s going to run install deb: {package}, platform: {platform} arch: {arch}')

        if not self.__download_deb(package, platform, arch):
            self.logger.error(f'failed to download {platform} {package} deb package to {self.src_deb_dir}/{package}/')
            return False

        try:
            package_name = subprocess.run(f'ls {self.src_deb_dir}/{package}/{package}_*.deb', shell=True,
                                          capture_output=True, text=True, check=True).stdout.strip().split('/')[-1]
        except FileNotFoundError as e:
            self.logger.error(f'didn\'t find {package}_.*deb in {self.src_deb_dir}/{package}\n{e}')
            return False

        connect_param = json.loads(open(
            importlib.util.find_spec('cicd').submodule_search_locations[0] + "/config/device/connect_param.json", 'rb').read().decode('utf-8'))['ssh']

        devinfo = {
            'name': connect_param['soc']['name'],
            'user': connect_param['soc']['user'],
            'addr': connect_param['soc']['addr'],
            'port': connect_param['soc']['port'],
            'pswd': connect_param['soc']['pswd']
        }

        if not state_machine(self.logger).entry_kernel('normal'):
            self.logger.error(f'failed to get into kernel normal mode while installing debian package')
            return False

        if not self.__push_deb_to_device(f'{self.src_deb_dir}/{package}/{package_name}', devinfo):
            self.logger.error(f'failed to send {package_name} deb package to {devinfo["name"]}')
            return False

        if not self.__install_deb_on_device(package_name, devinfo):
            self.logger.error(f'failed to install {package_name} deb package on {devinfo["name"]}')
            return False

        self.logger.debug(f'succeed to install {package_name} deb package on {devinfo["name"]}')
        return True

def main(args=None):
    if args is None:
        args = sys.argv[1:]

    parser = argparse.ArgumentParser(description="Reboot control")
    parser.add_argument('-p', dest='package', type=str, help='debian package name')
    parser.add_argument('-f', dest='platform', type=str, default='j6m', help='debian platform:j6m/j6h')
    parser.add_argument('-a', dest='arch', type=str, choices=['arm64', 'amd64'], default='arm64', help='debian package arch')
    parser.add_argument('-l', dest='level', choices=logging._nameToLevel.keys(), default='DEBUG', type=str, help='log level')
    args = parser.parse_args()

    logging.basicConfig(level=logging._nameToLevel[args.level],
                        format='%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S',
                        handlers=[
                            logging.StreamHandler(sys.stdout)
                        ])
    print(f'{"succeed" if DebianPackage(logging.getLogger(__name__)).install(args.package, args.arch) == True else "failed"} to install {args.package}:{args.arch}')
