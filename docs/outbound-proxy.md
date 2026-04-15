# Outbound proxy for Telegram (WireGuard + tinyproxy)

When direct HTTPS to `api.telegram.org` is unreliable (e.g. regional filtering), this project can send **only Telegram Bot API traffic** through an HTTP proxy on another host. OpenRouter calls in `llm.py` stay **direct** unless you add separate proxying (not covered here).

## Application configuration

Set in the bot environment (e.g. `.env` or `EnvironmentFile` in systemd):


| Variable              | Meaning                                     |
| --------------------- | ------------------------------------------- |
| `TELEGRAM_HTTP_PROXY` | HTTP proxy URL, e.g. `http://10.8.0.1:8888` |


If unset, the bot talks to Telegram without a proxy (previous behavior).

The bot uses `telegram.request.HTTPXRequest` with relaxed timeouts (`read` 60s, `connect`/`write` 30s, `media_write` 120s) to tolerate slow paths.

## Reference layout (this deployment)


| Role           | SSH alias | Notes                                                                                          |
| -------------- | --------- | ---------------------------------------------------------------------------------------------- |
| Bot host       | `tae`     | Runs `agent-coach.service`, WireGuard **client** `wg0` → `10.8.0.2/24`                         |
| Egress / proxy | `fin`     | Finland VPS, WireGuard **server** `10.8.0.1/24`, UDP `51820`, **tinyproxy** on `10.8.0.1:8888` |


WireGuard subnet: `10.8.0.0/24` (server `.1`, client `.2`). Only the bot’s WG address is allowed to use tinyproxy.

### Finland (`fin`) — installed packages

- `wireguard`, `tinyproxy`
- `/etc/wireguard/wg0.conf` — server key, `ListenPort 51820`, peer = bot’s public key, `AllowedIPs` for the client
- `/etc/tinyproxy/tinyproxy.conf` — `Port 8888`, `Listen 10.8.0.1`, `Allow 10.8.0.2`, `Timeout 600`
- `systemctl enable --now wg-quick@wg0 tinyproxy`

**tinyproxy systemd drop-in** (`/etc/systemd/system/tinyproxy.service.d/wg-order-restart.conf`): `After=` and `Requires=` `wg-quick@wg0.service` so tinyproxy starts only after `10.8.0.1` exists; `Restart=on-failure`, `RestartSec=10`, and start limits to reduce crash loops. Recreate this file if you reinstall the `tinyproxy` package and lose the drop-in.

Public endpoint for the tunnel: **UDP `185.231.206.8:51820`** (Finland; replace if the host changes).

### Bot host (`tae`) — installed packages

- `wireguard-tools`
- `/etc/wireguard/wg0.conf` — client key, `Address 10.8.0.2/24`, peer = server public key, `Endpoint = 185.231.206.8:51820`, `AllowedIPs = 10.8.0.1/32`, `PersistentKeepalive = 25`
- `systemctl enable --now wg-quick@wg0`

Quick check from the bot host:

```bash
wg show
curl -sS -o /dev/null -w '%{http_code}\n' -x http://10.8.0.1:8888 https://api.telegram.org/
```

### Systemd ordering

If the bot starts before `wg0` is up, the first Telegram requests can fail. On the bot host, `agent-coach.service` includes:

```ini
After=wg-quick@wg0.service
Wants=wg-quick@wg0.service
```

Use a **drop-in** or edit the unit so you do not depend on WireGuard when running without the tunnel.

### Production env

The gym bot uses `EnvironmentFile=/root/repo/agent-coach/.env.gym` with:

```bash
TELEGRAM_HTTP_PROXY=http://10.8.0.1:8888
```

The German bot uses `EnvironmentFile=/root/repo/agent-coach/.env.german` with the same `TELEGRAM_HTTP_PROXY=http://10.8.0.1:8888` (and `agent-german.service` is ordered after `wg-quick@wg0.service` like the gym unit).

## Operations

- **Restart tunnel after Finland reboot:** `wg-quick@wg0` and `tinyproxy` are enabled; confirm `wg show` on both sides if the bot loses Telegram connectivity.
- **Firewall:** Finland must allow **UDP 51820** from the bot host’s public IP (or from anywhere during testing; tighten for production).
- **Secrets:** WireGuard private keys live only under `/etc/wireguard/` on each host; do not commit them.

See also [live-server.md](live-server.md) for SSH paths and unit names.