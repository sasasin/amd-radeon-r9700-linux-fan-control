#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""AMD GPU fan control using amdgpu sysfs and AMD SMI telemetry."""

from __future__ import annotations

import argparse
import ctypes as C
import ctypes.util
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass


class FanError(Exception):
    pass


class BDF(C.Union):
    # amdsmi_bdf_t from the installed amd_smi/amdsmi.h.
    _fields_ = [("as_uint", C.c_uint64)]


class SMI:
    """Small, explicitly typed FFI; no pip package or system Python path needed."""

    def __init__(self):
        candidates = [C.util.find_library("amd_smi"),
                      "/opt/rocm/lib/libamd_smi.so",
                      "/opt/rocm/core/lib/libamd_smi.so"]
        for name in filter(None, candidates):
            try:
                self.lib = C.CDLL(name)
                break
            except OSError:
                continue
        else:
            raise FanError("libamd_smi.so が見つかりません。AMD SMIをインストールしてください。")
        signatures = {
            "amdsmi_init": [C.c_uint64],
            "amdsmi_shut_down": [],
            "amdsmi_status_code_to_string": [C.c_int, C.POINTER(C.c_char_p)],
            "amdsmi_get_processor_handle_from_bdf": [BDF, C.POINTER(C.c_void_p)],
            "amdsmi_get_gpu_fan_rpms": [C.c_void_p, C.c_uint32, C.POINTER(C.c_int64)],
            "amdsmi_get_gpu_fan_speed": [C.c_void_p, C.c_uint32, C.POINTER(C.c_int64)],
            "amdsmi_get_gpu_fan_speed_max": [C.c_void_p, C.c_uint32, C.POINTER(C.c_uint64)],
            "amdsmi_set_gpu_fan_speed": [C.c_void_p, C.c_uint32, C.c_uint64],
            "amdsmi_reset_gpu_fan": [C.c_void_p, C.c_uint32],
        }
        try:
            for name, args in signatures.items():
                fn = getattr(self.lib, name)
                fn.argtypes = args
                fn.restype = C.c_int
        except AttributeError as exc:
            raise FanError(f"AMD SMIのAPIが不足しています: {exc}") from exc
        self.call("amdsmi_init", 2)  # AMDSMI_INIT_AMD_GPUS

    def call(self, name, *args):
        result = getattr(self.lib, name)(*args)
        if result:
            message = C.c_char_p()
            self.lib.amdsmi_status_code_to_string(result, C.byref(message))
            detail = message.value.decode(errors="replace") if message.value else "unknown"
            raise FanError(f"{name}: {detail} (status={result})")

    def handle(self, bdf):
        domain, bus, device, function = (int(x, 16) for x in re.split(r"[:.]", bdf))
        value = (domain << 16) | (bus << 8) | (device << 3) | function
        handle = C.c_void_p()
        self.call("amdsmi_get_processor_handle_from_bdf", BDF(value), C.byref(handle))
        return handle

    def read(self, bdf):
        handle = self.handle(bdf)
        rpm, level, maximum = C.c_int64(), C.c_int64(), C.c_uint64()
        self.call("amdsmi_get_gpu_fan_rpms", handle, 0, C.byref(rpm))
        self.call("amdsmi_get_gpu_fan_speed", handle, 0, C.byref(level))
        self.call("amdsmi_get_gpu_fan_speed_max", handle, 0, C.byref(maximum))
        if maximum.value <= 0 or rpm.value < 0 or not 0 <= level.value <= maximum.value:
            raise FanError("AMD SMIが無効なファン値を返しました。")
        return {"rpm": rpm.value, "level": level.value, "level_max": maximum.value,
                "level_percent": round(100 * level.value / maximum.value, 2)}

    def close(self):
        self.call("amdsmi_shut_down")


class UnavailableSMI:
    """Telemetry failure must not prevent restoring automatic fan control."""

    def __init__(self, error):
        self.error = error

    def read(self, bdf):
        raise FanError(self.error)

    def close(self):
        pass


def read_int(path):
    return int(path.read_text().strip())


def writable_by_root(path):
    return path.is_file() and bool(path.stat().st_mode & stat.S_IWUSR)


@dataclass
class GPU:
    card: str
    bdf: str
    device: Path
    hwmon: Path

    @property
    def curve(self):
        return self.device / "gpu_od/fan_ctrl/fan_curve"

    @property
    def zero_rpm(self):
        return self.device / "gpu_od/fan_ctrl/fan_zero_rpm_enable"

    def zero_rpm_supported(self):
        # Some R9700 VBIOSes expose this file but return an empty string.
        return (writable_by_root(self.zero_rpm)
                and bool(self.zero_rpm.read_text().strip()))

    def backend(self):
        if writable_by_root(self.curve):
            return "fan_curve"
        if all(writable_by_root(self.hwmon / name) for name in ("pwm1", "pwm1_enable")):
            return "pwm1"
        return None

    def unavailable(self):
        mask_path = Path("/sys/module/amdgpu/parameters/ppfeaturemask")
        try:
            mask = int(mask_path.read_text().strip(), 0)
        except (OSError, ValueError):
            mask = None
        text = "書き込み可能なファン制御インターフェースがありません。"
        if mask is not None and not mask & 0x4000:
            text += (f" ppfeaturemask={mask:#x} ではOverDrive(bit 14)が無効です。"
                     f" {mask | 0x4000:#x} を設定して再起動後、statusで再確認してください。"
                     " 詳細はREADME.mdを参照してください。")
        else:
            text += " ドライバ/VBIOSが制御を公開しているか確認してください。"
        return text

    def snapshot(self, smi):
        result = {"card": self.card, "bdf": self.bdf, "backend": self.backend(),
                  "hwmon": str(self.hwmon)}
        if not result["backend"]:
            result["diagnostic"] = self.unavailable()
        temperatures = {}
        for path in sorted(self.hwmon.glob("temp*_input")):
            label = path.with_name(path.name.replace("_input", "_label"))
            temperatures[label.read_text().strip() if label.exists() else path.stem] = read_int(path) / 1000
        result["temperatures_c"] = temperatures
        result["rpm_max"] = read_int(self.hwmon / "fan1_max")
        result["hwmon_rpm"] = read_int(self.hwmon / "fan1_input")
        result["rpm_percent"] = (round(result["hwmon_rpm"] * 100 / result["rpm_max"], 2)
                                 if result["rpm_max"] > 0 else None)
        if (self.hwmon / "pwm1").exists() and (self.hwmon / "pwm1_max").exists():
            pwm, maximum = read_int(self.hwmon / "pwm1"), read_int(self.hwmon / "pwm1_max")
            result["hwmon_pwm"] = {"level": pwm, "level_max": maximum,
                                   "percent": round(pwm * 100 / maximum, 2) if maximum > 0 else None}
        try:
            result["amd_smi"] = smi.read(self.bdf)
        except FanError as exc:
            result["amd_smi_error"] = str(exc)
        if self.backend() == "pwm1":
            result["mode"] = {0: "none", 1: "manual", 2: "auto"}.get(read_int(self.hwmon / "pwm1_enable"), "unknown")
            result["pwm"] = read_int(self.hwmon / "pwm1")
        elif self.backend() == "fan_curve":
            result["fan_curve"] = self.curve.read_text()
            result["zero_rpm_supported"] = self.zero_rpm_supported()
        return result


def discover(root=Path("/sys/class/drm")):
    gpus = []
    for card in sorted(root.glob("card[0-9]*")):
        if not re.fullmatch(r"card\d+", card.name):
            continue
        device = card / "device"
        if not (device / "vendor").exists() or read_int_hex(device / "vendor") != 0x1002:
            continue
        for hwmon in sorted((device / "hwmon").glob("hwmon*")):
            if (hwmon / "name").read_text().strip() == "amdgpu" and (hwmon / "fan1_input").exists():
                gpus.append(GPU(card.name, device.resolve().name, device, hwmon))
    return gpus


def read_int_hex(path):
    return int(path.read_text().strip(), 16)


def select_gpu(gpus, selector):
    if selector is None and len(gpus) == 1:
        return gpus[0]
    matches = [g for g in gpus if selector in (g.card, g.bdf)]
    if len(matches) != 1:
        raise FanError("対象GPUを --gpu card1 または --gpu 0000:0b:00.0 で指定してください。")
    return matches[0]


def parse_curve(text):
    points = [tuple(map(int, match)) for match in re.findall(
        r"^\s*(\d+):\s*(\d+)C\s+(\d+)%\s*$", text, re.M)]
    limits = re.search(r"FAN_CURVE\(fan speed\):\s*(\d+)%\s+(\d+)%", text)
    if len(points) < 2 or not limits or len({p[0] for p in points}) != len(points):
        raise FanError("fan_curveの形式または許容範囲を認識できません。書き込みを中止しました。")
    return points, tuple(map(int, limits.groups()))


def write_value(path, value):
    # Each sysfs command must be a separate write/open, including commit.
    with path.open("w") as stream:
        stream.write(f"{value}\n")


class Controller:
    def __init__(self, gpu):
        self.gpu = gpu
        self.backend = gpu.backend()
        if self.backend is None:
            raise FanError(gpu.unavailable())

    def plan(self, percent):
        if self.backend == "fan_curve":
            text = self.gpu.curve.read_text()
            points, (low, high) = parse_curve(text)
            if not low <= percent <= high:
                raise FanError(f"このGPUの許容範囲は {low}〜{high}% です。")
            limits = re.search(r"FAN_CURVE\(hotspot temp\):\s*(\d+)C\s+(\d+)C", text)
            if not limits:
                raise FanError("カーブの温度許容範囲を認識できません。")
            minimum, maximum = map(int, limits.groups())
            if maximum <= minimum or maximum - minimum < len(points) - 1:
                raise FanError("カーブの温度許容範囲が無効です。")
            if all(temp == pwm == 0 for _, temp, pwm in points):
                # Automatic firmware mode can expose an uninitialized manual curve.
                points = [(index, math.floor(minimum + (maximum - minimum) * i / (len(points) - 1) + 0.5), pwm)
                          for i, (index, _, pwm) in enumerate(points)]
            temperatures = [temp for _, temp, _ in points]
            if (any(not minimum <= temp <= maximum for temp in temperatures)
                    or any(a >= b for a, b in zip(temperatures, temperatures[1:]))):
                raise FanError("既存カーブの温度設定が無効です。書き込みを中止しました。")
            return [(index, temp, percent) for index, temp, _ in points]
        maximum = read_int(self.gpu.hwmon / "pwm1_max")
        minimum = read_int(self.gpu.hwmon / "pwm1_min")
        value = math.floor(maximum * percent / 100 + 0.5)
        if not minimum <= value <= maximum:
            raise FanError("要求PWM値がドライバの許容範囲外です。")
        return value

    def plan_curve(self, requested):
        if self.backend != "fan_curve":
            raise FanError("温度カーブにはgpu_od/fan_ctrl/fan_curve対応GPUが必要です。")
        text = self.gpu.curve.read_text()
        current, (low, high) = parse_curve(text)
        limits = re.search(r"FAN_CURVE\(hotspot temp\):\s*(\d+)C\s+(\d+)C", text)
        if not limits:
            raise FanError("カーブの温度許容範囲を認識できません。")
        minimum, maximum = map(int, limits.groups())
        if minimum >= maximum or low > high:
            raise FanError("ドライバのカーブ許容範囲が無効です。")
        if len(requested) != len(current):
            raise FanError(f"このGPUには正確に{len(current)}点が必要です（指定: {len(requested)}点）。")
        if [index for index, _, _ in current] != list(range(len(current))):
            raise FanError("ドライバのカーブ設定点番号が不正です。")
        for point in requested:
            if (not isinstance(point, (list, tuple)) or len(point) != 2
                    or any(type(value) is not int for value in point)):
                raise FanError("各設定点には整数の[温度°C, PWM%]を指定してください。")
            temp, pwm = point
            if not minimum <= temp <= maximum:
                raise FanError(f"温度{temp}°Cは許容範囲{minimum}〜{maximum}°Cの外です。")
            if not low <= pwm <= high:
                raise FanError(f"PWM {pwm}%は許容範囲{low}〜{high}%の外です。")
        if any(a[0] >= b[0] for a, b in zip(requested, requested[1:])):
            raise FanError("温度は重複なしの昇順で指定してください。")
        if any(a[1] > b[1] for a, b in zip(requested, requested[1:])):
            raise FanError("温度上昇に対してPWMが下がらないカーブを指定してください。")
        return [(index, temp, pwm) for index, (temp, pwm) in enumerate(requested)]

    def auto(self):
        if self.backend == "pwm1":
            write_value(self.gpu.hwmon / "pwm1_enable", 2)
            if read_int(self.gpu.hwmon / "pwm1_enable") != 2:
                raise FanError("自動制御への復帰を確認できません。")
        else:
            # Restore only the fan settings this tool changes, preserving clocks/voltage.
            errors = []
            try:
                if self.gpu.zero_rpm_supported():
                    write_value(self.gpu.zero_rpm, "r")
            except OSError as exc:
                errors.append(str(exc))
            try:
                write_value(self.gpu.curve, "r")
            except OSError as exc:
                errors.append(str(exc))
            if errors:
                raise FanError("ファン設定の復帰失敗: " + "; ".join(errors))

    def set(self, percent):
        self._apply(self.plan(percent))

    def set_curve(self, points):
        self._apply(self.plan_curve(points))

    def _apply(self, plan):
        # Both fixed PWM and temperature curves validate before reaching here.
        try:
            if self.backend == "pwm1":
                write_value(self.gpu.hwmon / "pwm1_enable", 1)
                write_value(self.gpu.hwmon / "pwm1", plan)
                if read_int(self.gpu.hwmon / "pwm1_enable") != 1 or read_int(self.gpu.hwmon / "pwm1") != plan:
                    raise FanError("PWM設定の読み戻しが一致しません。")
            else:
                if self.gpu.zero_rpm_supported():
                    write_value(self.gpu.zero_rpm, 0)
                    write_value(self.gpu.zero_rpm, "c")
                for index, temp, value in plan:
                    write_value(self.gpu.curve, f"{index} {temp} {value}")
                write_value(self.gpu.curve, "c")
                actual, _ = parse_curve(self.gpu.curve.read_text())
                if actual != plan:
                    raise FanError("ファン設定の読み戻しが一致しません。")
        except BaseException as original:
            try:
                self.auto()
            except BaseException as recovery:
                raise FanError(f"設定失敗: {original} / 自動制御への復帰も失敗: {recovery}") from original
            raise


def show(snapshot, title=""):
    print(f"{title}{snapshot['card']} ({snapshot['bdf']}), backend={snapshot['backend'] or 'unavailable'}", flush=True)
    smi = snapshot.get("amd_smi")
    if smi:
        print(f"  AMD SMI: {smi['rpm']} RPM, ドライバ設定値 {smi['level']}/{smi['level_max']} ({smi['level_percent']}%)", flush=True)
        if snapshot.get("backend") == "fan_curve":
            print("  ※SMIの割合は実際のPWM/RPM割合とは異なり、手動制御中も0になる場合があります。", flush=True)
    else:
        print(f"  AMD SMI: {snapshot['amd_smi_error']}", flush=True)
    print(f"  hwmon: {snapshot['hwmon_rpm']} / {snapshot['rpm_max']} RPM ({snapshot['rpm_percent']}% of max RPM)", flush=True)
    if "hwmon_pwm" in snapshot:
        pwm = snapshot["hwmon_pwm"]
        print(f"  hwmon PWM: {pwm['level']}/{pwm['level_max']} ({pwm['percent']}%)", flush=True)
    print("  温度: " + ", ".join(f"{k}={v:g}°C" for k, v in snapshot["temperatures_c"].items()), flush=True)
    if "diagnostic" in snapshot:
        print("  " + snapshot["diagnostic"], flush=True)
    if "fan_curve" in snapshot:
        points, _ = parse_curve(snapshot["fan_curve"])
        if all(temp == pwm == 0 for _, temp, pwm in points):
            print("  手動カーブ: 未設定（既定値）", flush=True)
        else:
            print("  公開カーブ（hotspot °C:PWM%）: "
                  + " ".join(f"{temp}:{pwm}" for _, temp, pwm in points), flush=True)


def temperature_pwm(value):
    match = re.fullmatch(r"(\d+):(\d+)%?", value)
    if not match:
        raise argparse.ArgumentTypeError("温度:PWM（例: 50:40）の整数ペアを指定してください。")
    return tuple(map(int, match.groups()))


def requested_curve(args):
    if args.config is not None:
        if args.points:
            raise FanError("設定点と--configは同時に指定できません。")
        try:
            with args.config.open("rb") as stream:
                config = tomllib.load(stream)
        except tomllib.TOMLDecodeError as exc:
            raise FanError(f"TOMLの構文エラー: {exc}") from exc
        if set(config) != {"points"} or not isinstance(config["points"], list):
            raise FanError("TOMLにはpoints = [[温度, PWM], ...]だけを指定してください。")
        return config["points"]
    if not args.points:
        raise FanError("温度:PWMの設定点、または--configを指定してください。")
    return args.points


def show_curve_plan(gpu, plan):
    print(f"カーブ設定: {gpu.card} ({gpu.bdf}), 温度センサー=hotspot/junction", flush=True)
    print("  " + " ".join(f"{temp}°C:{pwm}%" for _, temp, pwm in plan), flush=True)


def number(value):
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 3600:
        raise argparse.ArgumentTypeError("0〜3600の有限の秒数を指定してください。")
    return number


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status", help="RPM・温度・制御可否を表示（sudo不要）")
    status.add_argument("--gpu", help="DRM card名またはPCI BDF。省略時は全GPU")
    status.add_argument("--json", action="store_true")
    for name in ("set", "test", "auto"):
        p = commands.add_parser(name, help={"set": "固定PWM割合を設定して終了", "test": "一定時間設定し、最後に自動制御へ復帰", "auto": "標準の自動制御へ復帰"}[name])
        p.add_argument("--gpu", help="DRM card名またはPCI BDF")
        if name != "auto":
            p.add_argument("percent", type=int, choices=range(0, 101), metavar="0..100", help="PWM制御割合。実測RPM/max RPMの割合とは異なります")
            p.add_argument("--settle", type=number, default=5, help="変更後のRPM測定までの秒数（既定:5）")
        if name == "test":
            p.add_argument("--seconds", type=number, default=10, help="設定を維持する秒数（既定:10）")
    curve = commands.add_parser("curve", help="hotspot温度とPWMのカーブを設定して終了")
    curve.add_argument("points", nargs="*", type=temperature_pwm, metavar="TEMP:PWM",
                       help="温度°C:PWM%%を昇順で指定。このR9700は5点")
    curve.add_argument("--config", type=Path, help="points配列を持つTOMLファイル")
    curve.add_argument("--gpu", help="DRM card名またはPCI BDF")
    curve.add_argument("--settle", type=number, default=5, help="変更後の測定までの秒数（既定:5）")
    curve.add_argument("--seconds", type=number, default=None,
                       help="指定時だけ一時設定とし、この秒数の後に標準自動制御へ戻す")
    curve.add_argument("--dry-run", action="store_true", help="検証と設定内容の表示のみ。sudo・書き込みなし")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        gpus = discover()
        if not gpus:
            raise FanError("ファンを持つamdgpuデバイスが見つかりません。")
        controller = None
        if args.command == "status":
            selected = [select_gpu(gpus, args.gpu)] if args.gpu else gpus
        else:
            gpu = select_gpu(gpus, args.gpu)
            selected = [gpu]
            controller = Controller(gpu)
            if args.command == "curve":
                curve_points = requested_curve(args)
                curve_plan = controller.plan_curve(curve_points)
                if args.dry_run:
                    show_curve_plan(gpu, curve_plan)
                    print("入力検証完了。設定は変更していません。", flush=True)
                    return 0
            elif args.command != "auto":
                controller.plan(args.percent)
            if os.geteuid() != 0:
                # Elevate only Python + this script, never uv or package installation.
                script = str(Path(__file__).resolve())
                arguments = list(sys.argv[1:] if argv is None else argv)
                print("ファン変更には管理者権限が必要です。sudo -Aで実行します。", flush=True)
                process = subprocess.run(["sudo", "-A", "/usr/bin/python3", script, *arguments], check=False)
                if process.returncode:
                    print("管理者コマンドが失敗しました。NoNewPrivs環境ではホスト側で実行してください。", file=sys.stderr)
                return process.returncode
        try:
            smi = SMI()
        except FanError as exc:
            smi = UnavailableSMI(str(exc))
        try:
            if args.command == "status":
                snapshots = [gpu.snapshot(smi) for gpu in selected]
                if args.json:
                    print(json.dumps(snapshots, ensure_ascii=False, indent=2))
                else:
                    for snapshot in snapshots:
                        show(snapshot)
                return 0
            if args.command == "auto":
                try:
                    show(gpu.snapshot(smi), "変更前: ")
                except (FanError, OSError, ValueError) as exc:
                    print(f"変更前の測定失敗（復帰処理は続行）: {exc}", file=sys.stderr)
                controller.auto()
                time.sleep(2)
                show(gpu.snapshot(smi), "自動制御へ復帰後: ")
            else:
                show(gpu.snapshot(smi), "変更前: ")
                changed = False
                try:
                    if args.command == "curve":
                        controller.set_curve(curve_points)
                    else:
                        controller.set(args.percent)
                    changed = True
                    if args.command == "curve":
                        show_curve_plan(gpu, curve_plan)
                    else:
                        print(f"要求PWM割合: {args.percent}%（実測RPMは以下で確認）", flush=True)
                    time.sleep(args.settle)
                    show(gpu.snapshot(smi), "変更後: ")
                    temporary = args.command == "test" or (args.command == "curve" and args.seconds is not None)
                    if temporary:
                        time.sleep(args.seconds)
                finally:
                    if changed and (args.command == "test"
                                    or (args.command == "curve" and args.seconds is not None)
                                    or sys.exc_info()[0] is not None):
                        controller.auto()
                        time.sleep(2)
                        show(gpu.snapshot(smi), "自動制御へ復帰後: ")
        finally:
            smi.close()
        return 0
    except (FanError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def terminated(signum, frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, terminated)
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("中断しました。", file=sys.stderr)
        sys.exit(130)
