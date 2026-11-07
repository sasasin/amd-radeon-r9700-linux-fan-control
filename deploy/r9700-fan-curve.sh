#!/usr/bin/env bash
# Apply the saved fan curve through fan_control.py at boot.
# Installed by deploy/install.sh and started as root from r9700-fan-curve.service.
#
# Nothing here validates the curve. fan_control.py reads the VBIOS limits and rejects
# out-of-range or malformed input before any sysfs write, and it restores automatic
# control when a write fails part-way. This script only waits for the interface and
# invokes the CLI. As root, fan_control.py writes sysfs directly and never re-execs
# itself through sudo.
set -u -o pipefail

PYTHON="${PYTHON:-/usr/bin/python3}"
SCRIPT="${SCRIPT:-/usr/local/lib/r9700-fan-control/fan_control.py}"
CONFIG="${CONFIG:-/etc/r9700/fan-curve.toml}"
SETTLE="${SETTLE:-0}"
ATTEMPTS="${ATTEMPTS:-60}"
# Optional PCI BDF or DRM card name. Leave unset when the machine has one fan-bearing
# amdgpu device; fan_control.py then discovers it. Set it from the unit or a drop-in.
GPU="${GPU:-}"
# The OverDrive attribute appears only after amdgpu finishes driver initialization.
# Polling this path avoids depending on an escaped sysfs device unit name, and avoids
# udev rules, which cannot run anything that waits or sleeps.
# A single path or one glob pattern without whitespace.
INTERFACE_GLOB="${INTERFACE_GLOB:-/sys/bus/pci/devices/*/gpu_od/fan_ctrl/fan_curve}"

interface_ready() {
    compgen -G "${INTERFACE_GLOB}" > /dev/null
}

if [[ ! -r "${SCRIPT}" ]]; then
    echo "installed script is missing: ${SCRIPT}; re-run deploy/install.sh" >&2
    exit 1
fi
if [[ ! -r "${CONFIG}" ]]; then
    echo "config is missing: ${CONFIG}; edit it and restart r9700-fan-curve.service" >&2
    exit 1
fi

attempt=1
until interface_ready; do
    if (( attempt >= ATTEMPTS )); then
        echo "amdgpu OverDrive fan interface did not appear within ${ATTEMPTS}s: ${INTERFACE_GLOB}" >&2
        echo "the boot fan curve was not applied" >&2
        exit 1
    fi
    sleep 1
    attempt=$((attempt + 1))
done

arguments=(curve --config "${CONFIG}" --settle "${SETTLE}")
if [[ -n "${GPU}" ]]; then
    arguments+=(--gpu "${GPU}")
fi

# Additional arguments, such as --dry-run, are passed through to fan_control.py.
exec "${PYTHON}" "${SCRIPT}" "${arguments[@]}" "$@"
