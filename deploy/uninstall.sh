#!/usr/bin/env bash
# Remove the boot-time fan curve unit and hand fan control back to the firmware.
# /etc/r9700/fan-curve.toml and the modprobe ppfeaturemask setting are left in place:
# revert the control interface itself with the steps in README.md, not by this script.
#
#   sudo -A deploy/uninstall.sh
set -eu -o pipefail

# Same paths as deploy/install.sh and r9700-fan-curve.service.
SCRIPT_PATH=/usr/local/lib/r9700-fan-control/fan_control.py
SCRIPT_BIN=/usr/local/sbin/r9700-fan-curve.sh
UNIT=r9700-fan-curve.service
UNIT_PATH=/etc/systemd/system/r9700-fan-curve.service
RESUME_UNIT=r9700-fan-curve-resume.service
RESUME_UNIT_PATH=/etc/systemd/system/r9700-fan-curve-resume.service
CONFIG_PATH=/etc/r9700/fan-curve.toml

PYTHON=${PYTHON:-/usr/bin/python3}

if [[ ${EUID} -ne 0 ]]; then
    echo "run as root: sudo -A ${BASH_SOURCE[0]}" >&2
    exit 1
fi

# disable removes both the multi-user.target link and the sleep target links.
systemctl disable --now "${UNIT}" 2> /dev/null || true
systemctl disable --now "${RESUME_UNIT}" 2> /dev/null || true
rm -f "${UNIT_PATH}" "${RESUME_UNIT_PATH}"

# Restore automatic control while the installed copy is still readable.
if [[ -r ${SCRIPT_PATH} ]]; then
    if ! "${PYTHON}" "${SCRIPT_PATH}" auto; then
        echo "warning: automatic control was not restored; run fan_control.py auto" >&2
    fi
fi

rm -f "${SCRIPT_BIN}" "${SCRIPT_PATH}"
rmdir "/usr/local/lib/r9700-fan-control" 2> /dev/null || true
systemctl daemon-reload

echo "removed ${UNIT}, ${RESUME_UNIT}, ${SCRIPT_BIN} and ${SCRIPT_PATH}"
echo "kept ${CONFIG_PATH}; delete it manually if it is no longer needed"
