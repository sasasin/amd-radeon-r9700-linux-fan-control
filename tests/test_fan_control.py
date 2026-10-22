"""Hardware-independent checks for validation, commits and failure recovery."""

import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import fan_control as fc


CURVE = """OD_FAN_CURVE:
0: 30C 20%
1: 50C 35%
2: 70C 55%
3: 90C 80%
4: 100C 100%
OD_RANGE:
FAN_CURVE(hotspot temp): 25C 110C
FAN_CURVE(fan speed): 20% 100%
"""

CUSTOM_POINTS = [(25, 20), (50, 45), (65, 65), (85, 85), (100, 100)]


class FanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.device = Path(self.temp.name) / "0000:0b:00.0"
        self.hwmon = self.device / "hwmon/hwmon1"
        self.hwmon.mkdir(parents=True)
        for name, value in {"name": "amdgpu", "fan1_input": "1065", "fan1_max": "5500",
                            "pwm1": "51", "pwm1_min": "0", "pwm1_max": "255",
                            "pwm1_enable": "2"}.items():
            (self.hwmon / name).write_text(value)
        self.gpu = fc.GPU("card1", self.device.name, self.device, self.hwmon)

    def od(self):
        self.gpu.curve.parent.mkdir(parents=True)
        self.gpu.curve.write_text(CURVE)
        self.gpu.zero_rpm.write_text("OD_FAN_ZERO_RPM_ENABLE:\n1\nOD_RANGE:\n0 1\n")

    def test_no_interface_does_not_become_supported_with_root(self):
        (self.hwmon / "pwm1_enable").unlink()
        (self.hwmon / "pwm1").chmod(0o444)
        self.assertIsNone(self.gpu.backend())
        with self.assertRaises(fc.FanError):
            fc.Controller(self.gpu)

    def test_pwm_rounding_and_automatic_reset(self):
        control = fc.Controller(self.gpu)
        control.set(50)
        self.assertEqual(fc.read_int(self.hwmon / "pwm1"), 128)
        self.assertEqual(fc.read_int(self.hwmon / "pwm1_enable"), 1)
        control.auto()
        self.assertEqual(fc.read_int(self.hwmon / "pwm1_enable"), 2)

    def test_pwm_write_failure_restores_auto(self):
        original = fc.write_value
        def fail(path, value):
            if path.name == "pwm1":
                raise OSError("write failed")
            original(path, value)
        with patch.object(fc, "write_value", side_effect=fail):
            with self.assertRaises(OSError):
                fc.Controller(self.gpu).set(40)
        self.assertEqual(fc.read_int(self.hwmon / "pwm1_enable"), 2)

    def test_od_out_of_range_has_no_side_effects(self):
        self.od()
        with patch.object(fc, "write_value") as write:
            with self.assertRaises(fc.FanError):
                fc.Controller(self.gpu).set(19)
        write.assert_not_called()

    def test_malformed_curve_has_no_side_effects(self):
        self.od()
        self.gpu.curve.write_text("unexpected format")
        with patch.object(fc, "write_value") as write:
            with self.assertRaises(fc.FanError):
                fc.Controller(self.gpu).set(40)
        write.assert_not_called()

    def test_zero_initialized_curve_gets_valid_temperature_points(self):
        self.od()
        self.gpu.curve.write_text("OD_FAN_CURVE:\n" + "\n".join(f"{i}: 0C 0%" for i in range(5))
                                 + "\nOD_RANGE:\nFAN_CURVE(hotspot temp): 25C 100C\nFAN_CURVE(fan speed): 20% 100%\n")
        plan = fc.Controller(self.gpu).plan(40)
        self.assertEqual(plan, [(0, 25, 40), (1, 44, 40), (2, 63, 40), (3, 81, 40), (4, 100, 40)])

    def test_empty_zero_rpm_file_is_not_written_on_reset(self):
        self.od()
        self.gpu.zero_rpm.write_text("")
        with patch.object(fc, "write_value") as write:
            fc.Controller(self.gpu).auto()
        write.assert_called_once_with(self.gpu.curve, "r")

    def test_snapshot_separates_smi_config_from_hwmon_pwm(self):
        self.od()
        (self.hwmon / "pwm1").write_text("102")
        smi = unittest.mock.Mock()
        smi.read.return_value = {"rpm": 2052, "level": 0, "level_max": 100, "level_percent": 0.0}
        snapshot = self.gpu.snapshot(smi)
        self.assertEqual(snapshot["amd_smi"]["level_percent"], 0)
        self.assertEqual(snapshot["hwmon_pwm"]["percent"], 40)

    def test_invalid_temperature_is_rejected_before_writing(self):
        self.od()
        self.gpu.curve.write_text(CURVE.replace("0: 30C", "0: 0C"))
        with patch.object(fc, "write_value") as write:
            with self.assertRaises(fc.FanError):
                fc.Controller(self.gpu).set(40)
        write.assert_not_called()

    def test_od_stages_all_points_and_commits_then_verifies(self):
        self.od()
        staged = {}
        writes = []
        def kernel(path, value):
            writes.append((path.name, str(value)))
            if path == self.gpu.curve:
                if value == "c":
                    rows = "\n".join(f"{i}: {t}C {p}%" for i, (t, p) in sorted(staged.items()))
                    path.write_text("OD_FAN_CURVE:\n" + rows + "\nOD_RANGE:\nFAN_CURVE(fan speed): 20% 100%\n")
                else:
                    index, temp, pwm = map(int, value.split())
                    staged[index] = (temp, pwm)
        with patch.object(fc, "write_value", side_effect=kernel):
            fc.Controller(self.gpu).set(40)
        points, _ = fc.parse_curve(self.gpu.curve.read_text())
        self.assertEqual([p[2] for p in points], [40] * 5)
        self.assertEqual([p[1] for p in points], [30, 50, 70, 90, 100])
        self.assertEqual(writes[:2], [("fan_zero_rpm_enable", "0"), ("fan_zero_rpm_enable", "c")])
        self.assertEqual(writes[-1], ("fan_curve", "c"))

    def test_od_failed_readback_recovers_and_reports_error(self):
        self.od()
        with patch.object(fc, "write_value") as write:
            with self.assertRaisesRegex(fc.FanError, "読み戻し"):
                fc.Controller(self.gpu).set(40)
        self.assertEqual(write.call_args_list[-1].args, (self.gpu.curve, "r"))

    def test_recovery_attempts_curve_even_if_zero_rpm_reset_fails(self):
        self.od()
        calls = []
        def fail(path, value):
            calls.append((path.name, value))
            if path == self.gpu.zero_rpm:
                raise OSError("zero RPM reset failed")
        with patch.object(fc, "write_value", side_effect=fail):
            with self.assertRaisesRegex(fc.FanError, "復帰失敗"):
                fc.Controller(self.gpu).auto()
        self.assertIn(("fan_curve", "r"), calls)

    def test_ambiguous_gpu_is_rejected(self):
        other = fc.GPU("card2", "0000:0c:00.0", self.device, self.hwmon)
        with self.assertRaises(fc.FanError):
            fc.select_gpu([self.gpu, other], None)
        self.assertEqual(fc.select_gpu([self.gpu, other], other.bdf), other)

    def test_numeric_validation_rejects_nan_and_infinite(self):
        for value in ("nan", "inf", "-1", "3601"):
            with self.subTest(value=value), self.assertRaises(fc.argparse.ArgumentTypeError):
                fc.number(value)

    def test_invalid_cli_percent_is_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()):
            for value in ("-1", "101", "nan", "40.5"):
                with self.subTest(value=value), self.assertRaises(SystemExit):
                    fc.parser().parse_args(["set", value])

    def test_bdf_union_layout_matches_installed_header(self):
        self.assertEqual(fc.C.sizeof(fc.BDF), 8)
        smi = object.__new__(fc.SMI)
        with patch.object(smi, "call") as call:
            smi.handle("0001:ab:1f.7")
        self.assertEqual(call.call_args.args[1].as_uint, 0x1ABFF)

    def test_interrupted_measurement_restores_automatic_control(self):
        with patch.object(fc, "discover", return_value=[self.gpu]), \
                patch.object(fc.os, "geteuid", return_value=0), \
                patch.object(fc, "SMI"), patch.object(fc, "show"), \
                patch.object(fc.GPU, "snapshot", return_value={}), \
                patch.object(fc.time, "sleep", side_effect=[KeyboardInterrupt(), None]), \
                patch.object(fc, "Controller") as controller:
            with self.assertRaises(KeyboardInterrupt):
                fc.main(["test", "40"])
        controller.return_value.set.assert_called_once_with(40)
        controller.return_value.auto.assert_called_once()

    def test_auto_works_even_if_smi_initialization_fails(self):
        with patch.object(fc, "discover", return_value=[self.gpu]), \
                patch.object(fc.os, "geteuid", return_value=0), \
                patch.object(fc, "SMI", side_effect=fc.FanError("SMI unavailable")), \
                patch.object(fc, "show"), patch.object(fc.time, "sleep"), \
                patch.object(fc.GPU, "snapshot", return_value={}), \
                patch.object(fc, "Controller") as controller:
            self.assertEqual(fc.main(["auto"]), 0)
        controller.return_value.auto.assert_called_once()

    def test_successful_set_keeps_manual_setting(self):
        with patch.object(fc, "discover", return_value=[self.gpu]), \
                patch.object(fc.os, "geteuid", return_value=0), \
                patch.object(fc, "SMI"), patch.object(fc, "show"), \
                patch.object(fc.GPU, "snapshot", return_value={}), \
                patch.object(fc.time, "sleep"), patch.object(fc, "Controller") as controller:
            self.assertEqual(fc.main(["set", "40"]), 0)
        controller.return_value.set.assert_called_once_with(40)
        controller.return_value.auto.assert_not_called()

    def test_custom_curve_preserves_exact_requested_points(self):
        self.od()
        self.assertEqual(fc.Controller(self.gpu).plan_curve(CUSTOM_POINTS),
                         [(index, temp, pwm) for index, (temp, pwm) in enumerate(CUSTOM_POINTS)])

    def test_invalid_custom_curves_never_write(self):
        self.od()
        invalid = [
            CUSTOM_POINTS[:4],
            [(25, 20), (25, 45), *CUSTOM_POINTS[2:]],
            [(50, 20), (25, 45), *CUSTOM_POINTS[2:]],
            [(24, 20), *CUSTOM_POINTS[1:]],
            [*CUSTOM_POINTS[:4], (111, 100)],
            [(25, 19), *CUSTOM_POINTS[1:]],
            [*CUSTOM_POINTS[:4], (100, 101)],
            [(25, 50), *CUSTOM_POINTS[1:]],
            [(True, 20), *CUSTOM_POINTS[1:]],
            [(25, 20.0), *CUSTOM_POINTS[1:]],
            [(25, 20, 30), *CUSTOM_POINTS[1:]],
        ]
        with patch.object(fc, "write_value") as write:
            for points in invalid:
                with self.subTest(points=points), self.assertRaises(fc.FanError):
                    fc.Controller(self.gpu).set_curve(points)
        write.assert_not_called()

    def test_custom_curve_requires_native_curve_backend(self):
        with patch.object(fc, "write_value") as write:
            with self.assertRaisesRegex(fc.FanError, "対応GPU"):
                fc.Controller(self.gpu).set_curve(CUSTOM_POINTS)
        write.assert_not_called()

    def test_custom_curve_commits_and_checks_readback(self):
        self.od()
        self.gpu.zero_rpm.write_text("")
        staged = {}
        writes = []
        def kernel(path, value):
            self.assertEqual(path, self.gpu.curve)
            writes.append(value)
            if value == "c":
                rows = "\n".join(f"{i}: {t}C {p}%" for i, (t, p) in sorted(staged.items()))
                path.write_text("OD_FAN_CURVE:\n" + rows + "\nOD_RANGE:\nFAN_CURVE(fan speed): 20% 100%\n")
            else:
                index, temp, pwm = map(int, value.split())
                staged[index] = (temp, pwm)
        with patch.object(fc, "write_value", side_effect=kernel):
            fc.Controller(self.gpu).set_curve(CUSTOM_POINTS)
        self.assertEqual(list(staged.values()), CUSTOM_POINTS)
        self.assertEqual(writes[-1], "c")

    def test_custom_curve_readback_mismatch_restores_auto(self):
        self.od()
        with patch.object(fc, "write_value") as write:
            with self.assertRaisesRegex(fc.FanError, "読み戻し"):
                fc.Controller(self.gpu).set_curve(CUSTOM_POINTS)
        self.assertEqual(write.call_args.args, (self.gpu.curve, "r"))

    def test_cli_and_toml_accept_same_curve(self):
        args = fc.parser().parse_args(["curve", *(f"{t}:{p}" for t, p in CUSTOM_POINTS)])
        self.assertEqual(fc.requested_curve(args), CUSTOM_POINTS)
        config = Path(self.temp.name) / "curve.toml"
        config.write_text("points = " + repr([list(p) for p in CUSTOM_POINTS]))
        args = fc.parser().parse_args(["curve", "--config", str(config)])
        self.assertEqual(fc.requested_curve(args), [list(p) for p in CUSTOM_POINTS])

    def test_missing_mixed_or_invalid_toml_input_is_rejected(self):
        config = Path(self.temp.name) / "curve.toml"
        config.write_text("points = [[25, 20]]")
        for argv in (["curve"], ["curve", "25:20", "--config", str(config)]):
            with self.subTest(argv=argv), self.assertRaises(fc.FanError):
                fc.requested_curve(fc.parser().parse_args(argv))
        for contents in ("points = [", "point = [[25,20]]", "points = 5", "points = []\nwrong = 1"):
            config.write_text(contents)
            with self.subTest(contents=contents), self.assertRaises(fc.FanError):
                fc.requested_curve(fc.parser().parse_args(["curve", "--config", str(config)]))

    def test_curve_dry_run_never_elevates_or_initializes_smi(self):
        self.od()
        with patch.object(fc, "discover", return_value=[self.gpu]), \
                patch.object(fc.os, "geteuid", return_value=1000), \
                patch.object(fc.subprocess, "run") as sudo, patch.object(fc, "SMI") as smi, \
                patch.object(fc, "write_value") as write, contextlib.redirect_stdout(io.StringIO()):
            result = fc.main(["curve", *(f"{t}:{p}" for t, p in CUSTOM_POINTS), "--dry-run"])
        self.assertEqual(result, 0)
        sudo.assert_not_called()
        smi.assert_not_called()
        write.assert_not_called()

    def test_temporary_curve_returns_to_auto_but_persistent_curve_does_not(self):
        for suffix in ([], ["--seconds", "0"]):
            with self.subTest(suffix=suffix), patch.object(fc, "discover", return_value=[self.gpu]), \
                    patch.object(fc.os, "geteuid", return_value=0), \
                    patch.object(fc, "SMI"), patch.object(fc, "show"), \
                    patch.object(fc, "show_curve_plan"), \
                    patch.object(fc.GPU, "snapshot", return_value={}), \
                    patch.object(fc.time, "sleep"), patch.object(fc, "Controller") as controller:
                result = fc.main(["curve", *(f"{t}:{p}" for t, p in CUSTOM_POINTS), *suffix])
                self.assertEqual(result, 0)
                controller.return_value.set_curve.assert_called_once_with(CUSTOM_POINTS)
                self.assertEqual(controller.return_value.auto.call_count, bool(suffix))


if __name__ == "__main__":
    unittest.main()
