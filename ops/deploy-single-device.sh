#!/usr/bin/env bash
set -euo pipefail

archive_path="${1:-/tmp/TG-PHOTODE-VPS-SINGLE-DEVICE-20260809.tar.gz}"
app_directory="/opt/tg-photode"
environment_file="/etc/tg-photode/tg-photode.env"
xcconfig_file="/root/TGPhotode-DeviceSecrets.xcconfig"
stamp="$(date -u +%Y%m%d-%H%M%S)"
code_backup="/root/tg-photode-before-single-device-${stamp}.tar.gz"
environment_backup="/root/tg-photode-env-before-single-device-${stamp}"
deployment_succeeded=false

restore_previous_release() {
    if [[ "${deployment_succeeded}" == "true" ]]; then
        return
    fi
    echo "Deployment failed; restoring the previous release." >&2
    cp -a "${environment_backup}" "${environment_file}"
    tar -xzf "${code_backup}" -C "${app_directory}"
    systemctl restart tg-photode || true
}
trap restore_previous_release EXIT

test -f "${archive_path}"
test -f "${environment_file}"
test -d "${app_directory}/.venv"

cp -a "${environment_file}" "${environment_backup}"
backup_items=(backend)
for optional_item in frontend ops requirements.txt README.md; do
    if [[ -e "${app_directory}/${optional_item}" ]]; then
        backup_items+=("${optional_item}")
    fi
done
tar \
    --exclude='.venv' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    -czf "${code_backup}" \
    -C "${app_directory}" \
    "${backup_items[@]}"

tar -xzf "${archive_path}" -C "${app_directory}"
"${app_directory}/.venv/bin/python" -m pip install -q -r "${app_directory}/requirements.txt"

set_environment_value() {
    local key="$1"
    local value="$2"
    if grep -q "^${key}=" "${environment_file}"; then
        sed -i "s|^${key}=.*|${key}=${value}|" "${environment_file}"
    else
        printf '\n%s=%s\n' "${key}" "${value}" >> "${environment_file}"
    fi
}

device_token="$(sed -n 's/^IOS_DEVICE_ACCESS_TOKEN=//p' "${environment_file}" | tail -n 1)"
if (( ${#device_token} < 32 )); then
    device_token="$(openssl rand -hex 32)"
fi

set_environment_value TELEGRAM_MULTI_ACCOUNT_ENABLED false
set_environment_value IOS_DEVICE_ACCESS_TOKEN "${device_token}"

umask 077
printf '%s\n' \
    '// Generated for this Mac/iPhone build. Never commit or share this file.' \
    "TG_DEVICE_ACCESS_TOKEN = ${device_token}" \
    > "${xcconfig_file}"

systemctl restart tg-photode
for _ in {1..20}; do
    if curl --fail --silent http://127.0.0.1:8000/api/health >/dev/null; then
        break
    fi
    sleep 1
done

curl --fail --silent http://127.0.0.1:8000/api/health
curl \
    --fail \
    --silent \
    -H "Authorization: Bearer ${device_token}" \
    'http://127.0.0.1:8000/api/chats?limit=1' \
    >/dev/null
unset device_token

deployment_succeeded=true
trap - EXIT
echo
echo "Single-device deployment succeeded."
echo "Code backup: ${code_backup}"
echo "Environment backup: ${environment_backup}"
echo "Xcode credential file: ${xcconfig_file}"
