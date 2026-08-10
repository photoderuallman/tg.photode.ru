#!/usr/bin/env bash
set -euo pipefail

archive_path="${1:-/tmp/TG-PHOTODE-VPS-MULTI-AUTH-20260809.tar.gz}"
app_directory="/opt/tg-photode"
environment_file="/etc/tg-photode/tg-photode.env"
stamp="$(date -u +%Y%m%d-%H%M%S)"
code_backup="/root/tg-photode-before-multi-auth-${stamp}.tar.gz"
environment_backup="/root/tg-photode-env-before-multi-auth-${stamp}"
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

account_token_secret="$(sed -n 's/^TELEGRAM_ACCOUNT_TOKEN_SECRET=//p' "${environment_file}" | tail -n 1)"
if (( ${#account_token_secret} < 32 )); then
    account_token_secret="$(openssl rand -hex 32)"
fi

set_environment_value TELEGRAM_MULTI_ACCOUNT_ENABLED true
set_environment_value TELEGRAM_MAX_ACCOUNT_SESSIONS 3
set_environment_value TELEGRAM_LOGIN_FLOW_TTL_SECONDS 600
set_environment_value TELEGRAM_ACCOUNT_SESSION_TTL_SECONDS 2592000
set_environment_value TDLIB_ACCOUNTS_DIRECTORY /var/lib/tg-photode/accounts
set_environment_value TELEGRAM_ACCOUNT_TOKEN_SECRET "${account_token_secret}"
unset account_token_secret

install -d -o tgapp -g tgapp -m 700 /var/lib/tg-photode/accounts
systemctl restart tg-photode

for _ in {1..20}; do
    if curl --fail --silent http://127.0.0.1:8000/api/health >/tmp/tg-photode-health.json; then
        break
    fi
    sleep 1
done

curl --fail --silent http://127.0.0.1:8000/api/health
main_pid="$(systemctl show tg-photode -p MainPID --value)"
tr '\0' '\n' < "/proc/${main_pid}/environ" \
    | grep -q '^TELEGRAM_MULTI_ACCOUNT_ENABLED=true$'
sudo -u tgapp test -w /var/lib/tg-photode/accounts

deployment_succeeded=true
trap - EXIT
echo
echo "Deployment succeeded."
echo "Code backup: ${code_backup}"
echo "Environment backup: ${environment_backup}"
