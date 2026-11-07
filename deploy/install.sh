#!/usr/bin/env bash
# Install the boot-time fan curve re-apply unit.
#
# Everything below /usr/local and /etc is installed root-owned on purpose. The boot unit
# runs as root, so it must never execute a script a normal user can edit; the working copy
# in this repository stays user-owned and is only the source for the root-owned copy.
#
#   sudo -A deploy/install.sh [path/to/fan-curve.toml]
#
# Without an argument, config/fan-curve.toml from this repository is used.
# An existing /etc/r9700/fan-curve.toml is kept unless FORCE=1.
# SKIP_VALIDATE=1 skips the --dry-run check against the current VBIOS limits.
set -eu -o pipefail

deploy_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_dir=$(cd -- "${deploy_dir}/.." && pwd)

# These are the same paths the unit and r9700-fan-curve.sh default to. Changing one
# without the others is caught by tests/test_deploy.py.
SCRIPT_PATH=/usr/local/lib/r9700-fan-control/fan_control.py
SCRIPT_BIN=/usr/local/sbin/r9700-fan-curve.sh
UNIT=r9700-fan-curve.service
UNIT_PATH=/etc/systemd/system/r9700-fan-curve.service
CONFIG_PATH=/etc/r9700/fan-curve.toml

PYTHON=${PYTHON:-/usr/bin/python3}
config_src=${1:-${repo_dir}/config/fan-curve.toml}

if [[ ${EUID} -ne 0 ]]; then
    echo "run as root: sudo -A ${BASH_SOURCE[0]}" >&2
    exit 1
fi

for path in "${repo_dir}/fan_control.py" "${deploy_dir}/${UNIT}" \
            "${deploy_dir}/r9700-fan-curve.sh" "${config_src}"; do
    if [[ ! -r ${path} ]]; then
        echo "missing readable file: ${path}" >&2
        exit 1
    fi
done

# Reject an unusable curve before changing anything on disk. --dry-run reads the current
# VBIOS limits, writes nothing and does not elevate, so a failure here leaves the system
# exactly as it was. It also confirms the OverDrive interface exists on this machine.
if [[ ${SKIP_VALIDATE:-0} != 1 ]]; then
    echo "== validating ${config_src} against the current VBIOS limits =="
    "${PYTHON}" "${repo_dir}/fan_control.py" curve --config "${config_src}" --dry-run
fi

install -d -m 0755 "/usr/local/lib/r9700-fan-control" "/etc/r9700"
install -m 0644 "${repo_dir}/fan_control.py" "${SCRIPT_PATH}"
install -m 0755 "${deploy_dir}/r9700-fan-curve.sh" "${SCRIPT_BIN}"
install -m 0644 "${deploy_dir}/${UNIT}" "${UNIT_PATH}"

if [[ -e ${CONFIG_PATH} && ${FORCE:-0} != 1 ]]; then
    echo "keeping existing ${CONFIG_PATH}"
else
    install -m 0644 "${config_src}" "${CONFIG_PATH}"
    echo "installed ${config_src} to ${CONFIG_PATH}"
fi

systemctl daemon-reload
systemctl enable --now "${UNIT}"

echo
echo "== ${UNIT} =="
systemctl status --no-pager --lines 5 "${UNIT}" || true
echo
echo "== current state =="
"${PYTHON}" "${SCRIPT_PATH}" status
