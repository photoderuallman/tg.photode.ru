# Telegram VPN automatic failover

The VPS routes only the `tgapp` service account through `tg-vpn`. Xray provides the
localhost SOCKS transport and sing-box attaches that transport to the TUN interface.
The failover controller changes Xray profiles; it does not alter the VPS public route,
SSH, Nginx, or root traffic.

## Health policy

- Every two minutes, make a lightweight payload check through the active SOCKS proxy.
- Every 30 minutes, run three full quality rounds.
- Confirm two quick failures before switching; an inactive Xray service switches
  immediately.
- Ping each candidate first and use TCP setup latency when ICMP is blocked.
- Never accept ping or TCP alone. A candidate must complete verified TLS to Telegram,
  connect to a Telegram DC, and complete verified TLS to an independent 204 endpoint.
- Try candidates from lowest link latency to highest until one passes.
- Validate candidates on temporary localhost ports before touching production.
- Back up the active config, replace it atomically, and roll back if production
  verification fails.

The two-minute watchdog is intentionally faster than the requested 30-minute deep
quality interval. It limits a hard outage to roughly four minutes without repeatedly
changing regions after one transient packet-loss event.

## Secure profile directory

Put one complete Xray client JSON file per region in:

```text
/etc/tg-vpn/profiles.d/REGION.json
```

The directory must be `0700` and profiles must be `0600`, owned by root. Profiles hold
provider credentials and must never be committed to Git or copied into deployment
archives. The controller normalizes every profile to a localhost-only SOCKS listener
on port 18082 and forces that listener through the profile's first proxy outbound.

Seed the current profile server-side without exposing it:

```bash
install -d -o root -g root -m 0700 /etc/tg-vpn/profiles.d
install -o root -g root -m 0600 \
  /usr/local/etc/xray/config.json \
  /etc/tg-vpn/profiles.d/current.json
```

Add at least two provider-exported regional profiles before enabling automatic switching.
When one subscription credential is valid across the provider's regional hostnames, seed
profiles on the VPS without revealing that credential:

```bash
/usr/local/libexec/tg-vpn-seed-regions \
  --source /etc/tg-vpn/profiles.d/current.json \
  --profile-dir /etc/tg-vpn/profiles.d \
  --base-domain convert-flow.net \
  de nl fr gb pl
```

Every generated profile is still rejected unless it carries real Telegram and neutral
HTTPS payloads through a temporary Xray process.

## Installation

```bash
install -o root -g root -m 0755 \
  ops/tg_vpn_failover.py /usr/local/libexec/tg-vpn-failover
install -o root -g root -m 0644 \
  ops/tg-vpn-failover.service /etc/systemd/system/tg-vpn-failover.service
install -o root -g root -m 0644 \
  ops/tg-vpn-failover.timer /etc/systemd/system/tg-vpn-failover.timer
systemctl daemon-reload
systemctl enable --now tg-vpn-failover.timer
```

Run a non-switching candidate evaluation and read status:

```bash
/usr/local/libexec/tg-vpn-failover --force-failover --dry-run
/usr/local/libexec/tg-vpn-failover --status
journalctl -u tg-vpn-failover.service --since today --no-pager
```
