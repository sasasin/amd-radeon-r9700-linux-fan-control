"""Hardware-independent checks for the boot-time apply script and its unit."""

from pathlib import Path
import os
import stat
import subprocess
import tempfile
import unittest


REPO = Path(__file__).resolve().parent.parent
APPLY = REPO / "deploy" / "r9700-fan-curve.sh"
UNIT = REPO / "deploy" / "r9700-fan-curve.service"
SCRIPT_PATH = "/usr/local/lib/r9700-fan-control/fan_control.py"
SCRIPT_BIN = "/usr/local/sbin/r9700-fan-curve.sh"
UNIT_PATH = "/etc/systemd/system/r9700-fan-curve.service"
CONFIG_PATH = "/etc/r9700/fan-curve.toml"


def unit_settings():
    """Return ExecStart and Environment values from the unit file."""
    result = {}
    for line in UNIT.read_text().splitlines():
        if line.startswith("ExecStart="):
            result["ExecStart"] = line.removeprefix("ExecStart=").strip()
        elif line.startswith("Environment="):
            key, _, value = line.removeprefix("Environment=").strip().partition("=")
            result[key] = value
    return result


class ApplyScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.interface = self.root / "fan_curve"
        self.interface.write_text("")
        self.script = self.root / "fan_control.py"
        self.script.write_text("")
        self.config = self.root / "fan-curve.toml"
        self.config.write_text("points = [[25, 20]]\n")
        self.argv = self.root / "argv"
        # Stands in for `python3 fan_control.py ...`: the shebang shell receives the
        # script path as its first argument, so drop it to recover the CLI arguments.
        self.python = self.root / "python"
        self.python.write_text('#!/bin/sh\nshift\nprintf \'%s\\n\' "$@" > "$ARGV"\n')
        self.python.chmod(self.python.stat().st_mode | stat.S_IXUSR | stat.S_IRUSR)

    def run_apply(self, **overrides):
        env = dict(os.environ, PYTHON=str(self.python), SCRIPT=str(self.script),
                   CONFIG=str(self.config), INTERFACE_GLOB=str(self.interface),
                   SETTLE="0", ATTEMPTS="5", GPU="", ARGV=str(self.argv))
        extra = list(overrides.pop("extra", []))
        env.update(overrides)
        result = subprocess.run([str(APPLY), *extra], env=env, capture_output=True,
                                text=True, check=False)
        arguments = self.argv.read_text().splitlines() if self.argv.exists() else None
        return result, arguments

    def test_settles_at_zero_and_passes_config(self):
        result, arguments = self.run_apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(arguments, ["curve", "--config", str(self.config), "--settle", "0"])

    def test_gpu_is_passed_only_when_set(self):
        _, without = self.run_apply()
        self.assertNotIn("--gpu", without)
        _, with_gpu = self.run_apply(GPU="0000:0b:00.0")
        self.assertEqual(with_gpu[-2:], ["--gpu", "0000:0b:00.0"])

    def test_extra_arguments_reach_the_cli(self):
        _, arguments = self.run_apply(extra=["--dry-run"])
        self.assertEqual(arguments[-1], "--dry-run")

    def test_missing_interface_gives_up_without_running_python(self):
        result, arguments = self.run_apply(INTERFACE_GLOB=str(self.root / "absent/*"),
                                           ATTEMPTS="0")
        self.assertEqual(result.returncode, 1)
        self.assertIn("did not appear", result.stderr)
        self.assertIsNone(arguments)

    def test_missing_config_or_script_fails_before_waiting(self):
        result, arguments = self.run_apply(CONFIG=str(self.root / "absent.toml"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("config is missing", result.stderr)
        self.assertIsNone(arguments)

        result, arguments = self.run_apply(SCRIPT=str(self.root / "absent.py"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("installed script is missing", result.stderr)
        self.assertIsNone(arguments)

    def test_unit_and_script_defaults_address_the_installed_files(self):
        settings = unit_settings()
        self.assertEqual(settings["ExecStart"], SCRIPT_BIN)
        self.assertEqual(settings["CONFIG"], CONFIG_PATH)
        self.assertEqual(settings["SETTLE"], "0")

        apply_text = APPLY.read_text()
        self.assertIn(f"CONFIG:-{CONFIG_PATH}", apply_text)
        self.assertIn(f"SCRIPT:-{SCRIPT_PATH}", apply_text)
        self.assertIn("PYTHON:-/usr/bin/python3", apply_text)
        for name in ("install.sh", "uninstall.sh"):
            text = (REPO / "deploy" / name).read_text()
            self.assertIn(f"SCRIPT_PATH={SCRIPT_PATH}", text, name)
            self.assertIn(f"SCRIPT_BIN={SCRIPT_BIN}", text, name)
            self.assertIn(f"UNIT_PATH={UNIT_PATH}", text, name)


if __name__ == "__main__":
    unittest.main()
