#!/usr/bin/env python

import argparse
import json
import os
import shutil
import subprocess
import types
import urllib.request
import tempfile

from pathlib import Path


ATH10K_BOARD_FILE = "bdwlan.b58"
ATH10K_BOARD_NAME = "bus=snoc,qmi-board-id=ff,qmi-chip-id=30224"

PATH_PLATFORM = "XIAOMI/BOOK124"
PATH_VENUS = "venus-5.2"

PATH_WDSFR = "Windows/System32/DriverStore/FileRepository"
PATH_THIRDPARTY = Path(__file__).parent / 'third-party'

URL_AARCH64_FIRMWARE_REPO = "https://raw.githubusercontent.com/linux-surface/aarch64-firmware/main/firmware"
URL_LINUX_FIRMWARE_REPO = "https://git.kernel.org/pub/scm/linux/kernel/git/firmware/linux-firmware.git/plain"


class Logger:
    log_pfx = [
        "==> ",
        " -> ",
        "    ",
    ]

    def __init__(self, level=0):
        self.level = level
        self.pfx = Logger.log_pfx[level]

    def sub(self):
        return Logger(level=self.level + 1)

    def info(self, msg):
        print(f"{self.pfx}{msg}")

    def warn(self, msg):
        print(f"{self.pfx}WARNING: {msg}")

    def error(self, msg):
        print(f"{self.pfx}ERROR: {msg}")


class Firmware:
    """Basic firmware source description"""

    @staticmethod
    def _filemap(files):
        if isinstance(files, list):
            return {x: x for x in files}
        elif isinstance(files, dict):
            return files
        else:
            raise Exception("invalid file map")

    def __init__(self, name, target_directory):
        self.name = name
        self.target_directory = Path(target_directory)

    def get(self, log, args):
        raise NotImplementedError()


class WindowsDriverFirmware(Firmware):
    """Firmware extracted from Windows Driver Store File Repository"""

    def __init__(self, name, target_directory, source_directory, files):
        super().__init__(name, target_directory)

        self.source_directory = source_directory
        self.files = Firmware._filemap(files)

    def _find_source_directory(self, args):
        for p in args.path_wdsfr.iterdir():
            if p.name.startswith(self.source_directory):
                return p

    def get(self, log, args):
        base = self._find_source_directory(args)

        for s, t in self.files.items():
            src = base / s
            tgt = args.path_out / self.target_directory / t

            tgt.parent.mkdir(parents=True, exist_ok=True)

            log.info(f"copying '{src}' to '{self.target_directory / t}'")
            shutil.copy(src, tgt)


class DownloadFirmware(Firmware):
    """Firmware downloaded from the internet"""

    def __init__(self, name, target_directory, source_url, files):
        super().__init__(name, target_directory)

        self.source_url = source_url
        self.files = Firmware._filemap(files)

    def get(self, log, args):
        for s, t in self.files.items():
            src = f"{self.source_url}/{s}"
            tgt = args.path_out / self.target_directory / t

            tgt.parent.mkdir(parents=True, exist_ok=True)

            log.info(f"downloading '{src}' to '{self.target_directory / t}'")
            urllib.request.urlretrieve(src, tgt)


class Patch:
    def __init__(self, name, fn) -> None:
        self.name = name
        self.fn = fn

    def apply(self, log, args):
        self.fn(log, args)


def patch_venus_extract(log, args):
    pil_splitter = PATH_THIRDPARTY / "qcom-mbn-tools" / "pil-splitter.py"
    mbn_venus = args.path_out / 'qcom' / PATH_PLATFORM / 'qcvss8180.mbn'
    dir_venus = args.path_out / 'qcom' / PATH_VENUS

    dir_venus.mkdir(parents=True, exist_ok=True)

    subprocess.call(["python", pil_splitter, mbn_venus, dir_venus / 'venus'])
    shutil.copy(mbn_venus, dir_venus / 'venus.mbn')


def patch_ath10k_board(log, args):
    """
    Create ath10k board-2.bin from single bdf file. Note that we need to create
    an entry matching the chip ID instead of the board ID. The board ID is
    0xff, which seems to be used on multiple chips and could be an indicator
    that it should not be used for matching (i.e. redirecting to the chip ID).
    It is currently unclear which bdf file to use for this. Any except the
    '.b5f' files seems to work. Those exceptions cause instant crashes of the
    remote processor.
    """

    ath10k_bdencoder = PATH_THIRDPARTY / "qca-swiss-army-knife" / "tools" / "scripts" / "ath10k" / "ath10k-bdencoder"
    path_boards = args.path_out.resolve() / 'ath10k' / 'WCN3990' / 'hw1.0' / 'boards'
    path_board_out = args.path_out.resolve() / 'ath10k' / 'WCN3990' / 'hw1.0' / 'board-2.bin'

    spec = [
        {
            "data": str(path_boards / ATH10K_BOARD_FILE),
            "names": [ATH10K_BOARD_NAME],
        }
    ]

    file = tempfile.NamedTemporaryFile()

    with open(file.name, "w") as fd:
        json.dump(spec, fd)

    subprocess.call(["python", ath10k_bdencoder, "-c", file.name, "-o", path_board_out])
    shutil.rmtree(path_boards)


def patch_ath10k_firmware(log, args):
    """
    Patch the upstream firmware-5.bin file. The firmware running on the WiFi
    processor seems to send single events per channel instead of event pairs.
    without the 'single-chan-info-per-channel' option set in firmware-5.bin,
    the ath10k driver will complain (somewhat cryptically) that it only
    received a single event. Setting this option shuts up the warning and
    generally seems like the right thing to do.

    See also: https://www.spinics.net/lists/linux-wireless/msg178387.html.
    """

    ath10k_fwencoder = PATH_THIRDPARTY / "qca-swiss-army-knife" / "tools" / "scripts" / "ath10k" / "ath10k-fwencoder"
    fw5_bin = args.path_out / 'ath10k' / 'WCN3990' / 'hw1.0' / 'firmware-5.bin'

    args = ['--modify', '--features=wowlan,mgmt-tx-by-ref,non-bmi,single-chan-info-per-channel', fw5_bin]

    subprocess.call(["python", ath10k_fwencoder] + args)


def patch_qca_bt_symlinks(log, args):
    """
    For some reason the revision/chip ID seems to be read as 0x01 instead of
    0x21. Windows drivers do only provide the files for revision 0x21 and there
    also doesn't seem to be a revision 0x01. Symlinking new 0x01 files to their
    existing 0x21 counterparts works.
    """

    base_path = args.path_out / 'qca'

    files = [
        "crbtfw21.tlv",
        "crnv21.b3c",
        "crnv21.b44",
        "crnv21.b45",
        "crnv21.b46",
        "crnv21.b47",
        "crnv21.b71",
        "crnv21.bin",
    ]

    for file in files:
        link = base_path / file.replace('21', '01')
        link.unlink(missing_ok=True)
        link.symlink_to(file)


sources = [
    # PD Maps for pd-mapper.service
    DownloadFirmware("pd-maps", f"qcom/{PATH_PLATFORM}", f"{URL_AARCH64_FIRMWARE_REPO}/qcom/msft/surface/pro-x-sq2", [
        "adspr.jsn",
        "adspua.jsn",
        "cdspr.jsn",
        "charger.jsn",
        "modemr.jsn",
        "modemuw.jsn",
    ]),

    # Bluetooth
    WindowsDriverFirmware("bluetooth", f"qca", "qcbtfmuart8180", [
        "crbtfw21.tlv",
        "crnv21.b3c",
        "crnv21.b44",
        "crnv21.b45",
        "crnv21.b46",
        "crnv21.b47",
        "crnv21.b71",
        "crnv21.bin",
    ]),

    # GPU (Adreno 680)
    DownloadFirmware("gpu/base", "qcom", f"{URL_AARCH64_FIRMWARE_REPO}/qcom", [
        "a680_gmu.bin",
        "a680_sqe.fw",
    ]),
    WindowsDriverFirmware("gpu/vendor", f"qcom/{PATH_PLATFORM}", "qcdx8180", [
        "qcdxkmsuc8180.mbn",
        "qcvss8180.mbn",
    ]),

    # WLAN
    WindowsDriverFirmware("wlan/vendor", f"qcom/{PATH_PLATFORM}", "qcwlan8180", [
        "wlanmdsp.mbn",
    ]),
    WindowsDriverFirmware("wlan/ath10k/board", "ath10k/WCN3990/hw1.0/boards", "qcwlan8180", [
        "bdwlan.b5f",
        "bdwlan.b36",
        "bdwlan.b37",
        "bdwlan.b46",
        "bdwlan.b47",
        "bdwlan.b48",
        "bdwlan.b58",
        "bdwlan.b71",
        "bdwlan.bin",
        "bdwlanu.b5f",
        "bdwlanu.b58",
    ]),
    DownloadFirmware("wlan/ath10k/firmware-5", f"ath10k/WCN3990/hw1.0", f"{URL_LINUX_FIRMWARE_REPO}/ath10k/WCN3990/hw1.0", [
        "firmware-5.bin",
    ]),

    # MCFG (file map based on inf contents)
    WindowsDriverFirmware("mcfg", f"qcom/{PATH_PLATFORM}", "mcfg_subsys_ext8180", {
        "MCFG/mbn_hw.dig.78": "modem_pr/mcfg/configs/mcfg_hw/mbn_hw.dig",
        "MCFG/mbn_hw.txt.79": "modem_pr/mcfg/configs/mcfg_hw/mbn_hw.txt",
        "MCFG/mbn_sw.dig.221": "modem_pr/mcfg/configs/mcfg_sw/mbn_sw.dig",
        "MCFG/mbn_sw.txt.222": "modem_pr/mcfg/configs/mcfg_sw/mbn_sw.txt",
        "MCFG/mcfg_hw.mbn.10": "modem_pr/mcfg/configs/mcfg_hw/generic/common/mdm9x55/ss/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.11": "modem_pr/mcfg/configs/mcfg_hw/generic/common/mdm9x55_fusion/7+7_mode/dr_dsds/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.12": "modem_pr/mcfg/configs/mcfg_hw/generic/common/mdm9x55_fusion/7+7_mode/sr_dsds/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.13": "modem_pr/mcfg/configs/mcfg_hw/generic/common/mdm9x55_fusion/ss/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.14": "modem_pr/mcfg/configs/mcfg_hw/generic/common/msm8998/cmcc_subsidized/sr_dsds/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.15": "modem_pr/mcfg/configs/mcfg_hw/generic/common/msm8998/la/7+7_mode/dr_dsds/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.16": "modem_pr/mcfg/configs/mcfg_hw/generic/common/msm8998/la/7+7_mode/sr_dsds/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.17": "modem_pr/mcfg/configs/mcfg_hw/generic/common/msm8998/la/ss/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.18": "modem_pr/mcfg/configs/mcfg_hw/generic/common/msm8998/wd/7+7_mode/sr_dsds/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.19": "modem_pr/mcfg/configs/mcfg_hw/generic/common/msm8998/wd/ss/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.1": "modem_pr/mcfg/configs/mcfg_hw/generic/common/default/5g_default/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.20": "modem_pr/mcfg/configs/mcfg_hw/generic/common/msm8998/wp8/7+7_mode/dr_dsds/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.21": "modem_pr/mcfg/configs/mcfg_hw/generic/common/msm8998/wp8/7+7_mode/sr_dsds/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.22": "modem_pr/mcfg/configs/mcfg_hw/generic/common/msm8998/wp8/ss/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.23": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sc8180x/cmcc_subsidized/sr_dsds/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.24": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sc8180x/la/7+7_mode/sr_dsds/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.25": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sc8180x/la/dsds/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.26": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sc8180x/la/ss/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.27": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sc8180x/wd/7+7_mode/sr_dsds/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.28": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sc8180x/wd/dssa/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.29": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sc8180x/wd/ss/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.2": "modem_pr/mcfg/configs/mcfg_hw/generic/common/default/cust_default/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.30": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sc8180x/wp8/7+7_mode/sr_dsds/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.31": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sc8180x/wp8/ss/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.32": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sdm1000/cmcc_subsidized/sr_dsds/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.33": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sdm1000/la/7+7_mode/sr_dsds/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.34": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sdm1000/la/ss/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.35": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sdm1000/wd/7+7_mode/sr_dsds/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.36": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sdm1000/wd/dssa/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.37": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sdm1000/wd/ss/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.38": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sdm1000/wp8/7+7_mode/sr_dsds/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.39": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sdm1000/wp8/ss/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.3": "modem_pr/mcfg/configs/mcfg_hw/generic/common/default/sc8180x.gen.prod/5g_default/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.40": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sdm660/cmcc_subsidized/sr_dsds/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.41": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sdm660/la/7+7_mode/sr_dsds/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.42": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sdm660/la/ss/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.43": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sdm670/cmcc_subsidized/sr_dsds/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.44": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sdm670/la/7+7_mode/sr_dsds/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.45": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sdm670/la/ss/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.46": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sdm845/cmcc_subsidized/sr_dsds/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.47": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sdm845/la/7+7_mode/sr_dsds/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.48": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sdm845/la/ss/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.49": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sdm845/wd/7+7_mode/sr_dsds/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.4": "modem_pr/mcfg/configs/mcfg_hw/generic/common/default/sc8180x.gen.prod/cust_default/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.50": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sdm845/wd/ss/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.51": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sdm845/wp8/7+7_mode/sr_dsds/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.52": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sdm845/wp8/ss/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.53": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sdm855/cmcc_subsidized/sr_dsds/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.54": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sdm855/la/7+7_mode/sr_dsds/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.55": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sdm855/la/ss/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.56": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sdm855/wd/7+7_mode/sr_dsds/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.57": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sdm855/wd/ss/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.58": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sdm855/wp8/7+7_mode/sr_dsds/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.59": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sdm855/wp8/ss/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.5": "modem_pr/mcfg/configs/mcfg_hw/generic/common/default/sc8180x.gen.prod/default/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.60": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sdx20m_fusion/7+7_mode/dr_dsds/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.61": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sdx20m_fusion/7+7_mode/sr_dsds/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.62": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sdx20m_fusion/ss/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.63": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sdx20/7+7_mode/sr_dsds/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.64": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sdx20/ss/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.65": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sdx24/7+7_mode/sr_dsds/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.66": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sdx24/ss/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.67": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sdx24_fusion/7+7_mode/sr_dsds/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.68": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sdx24_fusion/ss/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.69": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sm8150/cmcc_subsidized/sr_dsds/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.6": "modem_pr/mcfg/configs/mcfg_hw/generic/common/default/sc8180x.genimss.prod/default/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.70": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sm8150/la/7+7_mode/sr_dsds/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.71": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sm8150/la/dsds/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.72": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sm8150/la/ss/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.73": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sm8150/la/ss_apq_only/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.74": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sm8150/wd/7+7_mode/sr_dsds/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.75": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sm8150/wd/ss/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.76": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sm8150/wp8/7+7_mode/sr_dsds/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.77": "modem_pr/mcfg/configs/mcfg_hw/generic/common/sm8150/wp8/ss/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.7": "modem_pr/mcfg/configs/mcfg_hw/generic/common/default/sc8180x.genmd.prod/default/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.8": "modem_pr/mcfg/configs/mcfg_hw/generic/common/mdm9x55/7+7_mode/dr_dsds/mcfg_hw.mbn",
        "MCFG/mcfg_hw.mbn.9": "modem_pr/mcfg/configs/mcfg_hw/generic/common/mdm9x55/7+7_mode/sr_dsds/mcfg_hw.mbn",
        "MCFG/mcfg_sw.mbn.100": "modem_pr/mcfg/configs/mcfg_sw/generic/china/ct/commercial/openmkt/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.101": "modem_pr/mcfg/configs/mcfg_sw/generic/china/ct/commercial/volte_openmkt/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.102": "modem_pr/mcfg/configs/mcfg_sw/generic/china/ct/lab/cta/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.103": "modem_pr/mcfg/configs/mcfg_sw/generic/china/ct/lab/eps_only_volte_conf/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.104": "modem_pr/mcfg/configs/mcfg_sw/generic/china/ct/lab/noapn_vo_conf/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.105": "modem_pr/mcfg/configs/mcfg_sw/generic/china/ct/lab/test/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.106": "modem_pr/mcfg/configs/mcfg_sw/generic/china/ct/lab/test_eps_only/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.107": "modem_pr/mcfg/configs/mcfg_sw/generic/china/ct/lab/test_no_apn/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.108": "modem_pr/mcfg/configs/mcfg_sw/generic/china/ct/lab/volte_conf/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.109": "modem_pr/mcfg/configs/mcfg_sw/generic/china/cu/commercial/openmkt/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.110": "modem_pr/mcfg/configs/mcfg_sw/generic/china/cu/commercial/subsidized/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.111": "modem_pr/mcfg/configs/mcfg_sw/generic/china/cu/commercial/volte/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.112": "modem_pr/mcfg/configs/mcfg_sw/generic/china/cu/lab/test/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.113": "modem_pr/mcfg/configs/mcfg_sw/generic/common/default/5g_default/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.114": "modem_pr/mcfg/configs/mcfg_sw/generic/common/default/cust_default/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.115": "modem_pr/mcfg/configs/mcfg_sw/generic/common/default/sc8180x.gen.prod/5g_default/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.116": "modem_pr/mcfg/configs/mcfg_sw/generic/common/default/sc8180x.gen.prod/cust_default/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.117": "modem_pr/mcfg/configs/mcfg_sw/generic/common/default/sc8180x.gen.prod/default/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.118": "modem_pr/mcfg/configs/mcfg_sw/generic/common/default/sc8180x.genimss.prod/default/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.119": "modem_pr/mcfg/configs/mcfg_sw/generic/common/default/sc8180x.genmd.prod/default/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.120": "modem_pr/mcfg/configs/mcfg_sw/generic/common/multimbn/multi_mbn/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.121": "modem_pr/mcfg/configs/mcfg_sw/generic/common/row/commercial/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.122": "modem_pr/mcfg/configs/mcfg_sw/generic/common/w_one/commercial/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.123": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/bouygues/commercial/france/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.124": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/dt/commercial/austria/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.125": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/dt/commercial/croatia/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.126": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/dt/commercial/cz/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.127": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/dt/commercial/greece/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.128": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/dt/commercial/hungary/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.129": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/dt/commercial/nl/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.130": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/dt/commercial/pl/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.131": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/dt/commercial/slovakia/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.132": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/dt/non_volte/commercial/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.133": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/dt/volte/commercial/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.134": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/ee/commercial/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.135": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/elisa/commercial/fi/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.136": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/h3g/commercial/austria/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.137": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/h3g/commercial/denmark/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.138": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/h3g/commercial/italy/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.139": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/h3g/commercial/se/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.140": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/h3g/commercial/uk/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.141": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/orange/commercial/france/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.142": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/orange/commercial/group_non_ims/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.143": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/orange/commercial/poland/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.144": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/orange/commercial/romania/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.145": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/orange/commercial/slovakia/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.146": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/orange/commercial/spain/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.147": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/sfr/commercial/fr/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.148": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/swisscom/commercial/swiss/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.149": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/tdc/commercial/denmark/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.150": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/tele2/commercial/nl/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.151": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/tele2/commercial/sweden/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.152": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/telefonica/commercial/de/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.153": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/telefonica/commercial/uk/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.154": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/telefonica/non_volte/spain/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.155": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/telenor/commercial/denmark/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.156": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/telenor/commercial/norway/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.157": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/telia/commercial/norway/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.158": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/telia/commercial/sweden/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.159": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/tim/commercial/italy/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.160": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/vodafone/commercial/hungary/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.161": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/vodafone/commercial/ireland/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.162": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/vodafone/non_volte/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.163": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/vodafone/volte/cz/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.164": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/vodafone/volte/germany/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.165": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/vodafone/volte/italy/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.166": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/vodafone/volte/netherlands/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.167": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/vodafone/volte/portugal/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.168": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/vodafone/volte/safrica/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.169": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/vodafone/volte/spain/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.170": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/vodafone/volte/turkey/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.171": "modem_pr/mcfg/configs/mcfg_sw/generic/eu/vodafone/volte/uk/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.172": "modem_pr/mcfg/configs/mcfg_sw/generic/korea/kt/commercial_kt_lte/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.173": "modem_pr/mcfg/configs/mcfg_sw/generic/korea/lgu/commercial/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.174": "modem_pr/mcfg/configs/mcfg_sw/generic/korea/skt/commercial/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.175": "modem_pr/mcfg/configs/mcfg_sw/generic/korea/tta/commercial/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.176": "modem_pr/mcfg/configs/mcfg_sw/generic/latam/amx/commercial/mx/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.177": "modem_pr/mcfg/configs/mcfg_sw/generic/latam/amx/non_volte/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.178": "modem_pr/mcfg/configs/mcfg_sw/generic/latam/amx/volte/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.179": "modem_pr/mcfg/configs/mcfg_sw/generic/latam/claro/commercial/colombia/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.180": "modem_pr/mcfg/configs/mcfg_sw/generic/latam/telefonica/commercial/colombia/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.181": "modem_pr/mcfg/configs/mcfg_sw/generic/latam/telefonica/commercial/peru/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.182": "modem_pr/mcfg/configs/mcfg_sw/generic/mea/stc/commercial/sa/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.183": "modem_pr/mcfg/configs/mcfg_sw/generic/na/att/firstnet/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.184": "modem_pr/mcfg/configs/mcfg_sw/generic/na/att/non_volte/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.185": "modem_pr/mcfg/configs/mcfg_sw/generic/na/att/volte/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.186": "modem_pr/mcfg/configs/mcfg_sw/generic/na/bell/commercial/ca/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.187": "modem_pr/mcfg/configs/mcfg_sw/generic/na/rogers/commercial/ca/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.188": "modem_pr/mcfg/configs/mcfg_sw/generic/na/sprint/commercial/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.189": "modem_pr/mcfg/configs/mcfg_sw/generic/na/sprint/vowifi/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.190": "modem_pr/mcfg/configs/mcfg_sw/generic/na/telus/commercial/ca/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.191": "modem_pr/mcfg/configs/mcfg_sw/generic/na/tmo/commercial/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.192": "modem_pr/mcfg/configs/mcfg_sw/generic/na/uscc/commercial/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.193": "modem_pr/mcfg/configs/mcfg_sw/generic/na/verizon/cdmaless/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.194": "modem_pr/mcfg/configs/mcfg_sw/generic/na/verizon/hvolte/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.195": "modem_pr/mcfg/configs/mcfg_sw/generic/na/verizon/imsless/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.196": "modem_pr/mcfg/configs/mcfg_sw/generic/russia/beeline/gen_3gpp/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.197": "modem_pr/mcfg/configs/mcfg_sw/generic/russia/megafon/commercial/ru/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.198": "modem_pr/mcfg/configs/mcfg_sw/generic/russia/mts/commercial/ru/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.199": "modem_pr/mcfg/configs/mcfg_sw/generic/sa/brazil/commercial/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.200": "modem_pr/mcfg/configs/mcfg_sw/generic/sea/3hk/commercial/hk/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.201": "modem_pr/mcfg/configs/mcfg_sw/generic/sea/ais/commercial/thailand/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.202": "modem_pr/mcfg/configs/mcfg_sw/generic/sea/apt/commercial/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.203": "modem_pr/mcfg/configs/mcfg_sw/generic/sea/chunghwatel/commercial/tw/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.204": "modem_pr/mcfg/configs/mcfg_sw/generic/sea/dtac/commercial/volte/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.205": "modem_pr/mcfg/configs/mcfg_sw/generic/sea/fareastone/commercial/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.206": "modem_pr/mcfg/configs/mcfg_sw/generic/sea/globe/commercial/ph/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.207": "modem_pr/mcfg/configs/mcfg_sw/generic/sea/hkt/commercial/hk/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.208": "modem_pr/mcfg/configs/mcfg_sw/generic/sea/m1/commercial/sg/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.209": "modem_pr/mcfg/configs/mcfg_sw/generic/sea/p1/commercial/malaysia/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.210": "modem_pr/mcfg/configs/mcfg_sw/generic/sea/singtel/commercial/singapore/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.211": "modem_pr/mcfg/configs/mcfg_sw/generic/sea/smartfren/commercial/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.212": "modem_pr/mcfg/configs/mcfg_sw/generic/sea/smartfren/commercial/vowifi/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.213": "modem_pr/mcfg/configs/mcfg_sw/generic/sea/smartone/commercial/hk/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.214": "modem_pr/mcfg/configs/mcfg_sw/generic/sea/starhub/commercial/sg/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.215": "modem_pr/mcfg/configs/mcfg_sw/generic/sea/tm/commercial/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.216": "modem_pr/mcfg/configs/mcfg_sw/generic/sea/truemove/commercial/volte/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.217": "modem_pr/mcfg/configs/mcfg_sw/generic/sea/tstar/commercial/tw/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.218": "modem_pr/mcfg/configs/mcfg_sw/generic/sea/umobile/commercial/malaysia/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.219": "modem_pr/mcfg/configs/mcfg_sw/generic/sea/viettel/commercial/vietnam/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.220": "modem_pr/mcfg/configs/mcfg_sw/generic/sea/ytl/commercial/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.81": "modem_pr/mcfg/configs/mcfg_sw/generic/af/cellc/commercial/safrica/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.82": "modem_pr/mcfg/configs/mcfg_sw/generic/af/moroccotel/commercial/ma/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.83": "modem_pr/mcfg/configs/mcfg_sw/generic/apac/dcm/commercial/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.84": "modem_pr/mcfg/configs/mcfg_sw/generic/apac/kddi/commercial/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.85": "modem_pr/mcfg/configs/mcfg_sw/generic/apac/reliance/commercial/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.86": "modem_pr/mcfg/configs/mcfg_sw/generic/apac/sbm/commercial/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.87": "modem_pr/mcfg/configs/mcfg_sw/generic/aunz/optus/commercial/au/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.88": "modem_pr/mcfg/configs/mcfg_sw/generic/aunz/telstra/commercial/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.89": "modem_pr/mcfg/configs/mcfg_sw/generic/aunz/vodafone/commercial/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.90": "modem_pr/mcfg/configs/mcfg_sw/generic/china/cmcc/commercial/volte_openmkt/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.91": "modem_pr/mcfg/configs/mcfg_sw/generic/china/cmcc/lab/agnss_loctech/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.92": "modem_pr/mcfg/configs/mcfg_sw/generic/china/cmcc/lab/conf_volte/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.93": "modem_pr/mcfg/configs/mcfg_sw/generic/china/cmcc/lab/eps_only/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.94": "modem_pr/mcfg/configs/mcfg_sw/generic/china/cmcc/lab/lpp_loctech/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.95": "modem_pr/mcfg/configs/mcfg_sw/generic/china/cmcc/lab/nsiot_volte/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.96": "modem_pr/mcfg/configs/mcfg_sw/generic/china/cmcc/lab/rrlp_loctech/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.97": "modem_pr/mcfg/configs/mcfg_sw/generic/china/cmcc/lab/tgl_comb_attach/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.98": "modem_pr/mcfg/configs/mcfg_sw/generic/china/cmcc/lab/w_irat_comb_attach/mcfg_sw.mbn",
        "MCFG/mcfg_sw.mbn.99": "modem_pr/mcfg/configs/mcfg_sw/generic/china/ct/commercial/hvolte_openmkt/mcfg_sw.mbn",
        "MCFG/oem_hw.txt.80": "modem_pr/mcfg/configs/mcfg_hw/oem_hw.txt",
        "MCFG/oem_sw.txt.223": "modem_pr/mcfg/configs/mcfg_sw/oem_sw.txt",
    }),

    # ADSP
    WindowsDriverFirmware("adsp/vendor", f"qcom/{PATH_PLATFORM}", "qcsubsys_ext_adsp8180", [
        "qcadsp8180.mbn",
    ]),
    # TODO: ADSP directory?

    # CDSP
    WindowsDriverFirmware("cdsp/vendor", f"qcom/{PATH_PLATFORM}", "qcsubsys_ext_cdsp8180", [
        "qccdsp8180.mbn",
    ]),
    # TODO: CDSP directory?

    # MPSS
    WindowsDriverFirmware("mpss/vendor", f"qcom/{PATH_PLATFORM}", "qcsubsys_ext_mpss8180", [
        "qcmpss8180.mbn",
        "qcmpss8180_nm.mbn",
    ]),
    WindowsDriverFirmware("mpss/library", f"qcom/{PATH_PLATFORM}", "mcfg_subsys_ext8180", [
        "qdsp6m.qdb",
    ]),
]

patches = [
    Patch('venus', patch_venus_extract),
    Patch('ath10k/board-2.bin', patch_ath10k_board),
    Patch('ath10k/firmware-5.bin', patch_ath10k_firmware),
    Patch('qca/bt', patch_qca_bt_symlinks),
]


def gather(log, args, sources):
    for src in sources:
        log.info(f"{src.name}")
        src.get(log.sub(), args)


def patch(log, args, patches):
    for patch in patches:
        log.info(f"{patch.name}")
        patch.apply(log.sub(), args)


def main():
    if os.geteuid() == 0:
        print("Please do not run this script as root!")
        exit(1)

    parser = argparse.ArgumentParser(description="Gather firmware files for Xiaomi Book 12.4 (8cx Gen 2)")
    parser.add_argument("-w", "--windows", help="Windows root directory", required=True)
    parser.add_argument("-o", "--output", help="output directory", default="out")
    cli_args = parser.parse_args()

    args = types.SimpleNamespace()
    args.path_wdsfr = Path(cli_args.windows) / PATH_WDSFR
    args.path_out = Path(cli_args.output)

    log = Logger()

    log.info("retrieving base firmware files")
    gather(log.sub(), args, sources)

    log.info("patching firmware files")
    patch(log.sub(), args, patches)

    log.info("done!")


if __name__ == '__main__':
    main()
