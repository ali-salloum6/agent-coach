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


| Role           | SSH alias   | Notes                                                                                            |
| -------------- | ----------- | ------------------------------------------------------------------------------------------------ |
| Bot host       | `tae`       | Runs `agent-coach.service`, WireGuard **client** `wg0` → `10.8.0.2/24`                           |
| Egress / proxy | `stockholm` | **Active** Stockholm VPS, WireGuard **server** `10.8.0.1/24`, UDP `51820`, **tinyproxy** `:8888` |
| Egress / proxy | `fin`       | **Legacy / standby** Finland VPS (same layout; not used unless switched back)                      |


WireGuard subnet: `10.8.0.0/24` (server `.1`, client `.2`). Only the bot’s WG address is allowed to use tinyproxy.

### Stockholm (`stockholm`) — active egress (2026-06)

- **SSH**: `132.243.240.175`, alias `stockholm` in `~/.ssh/config` (key-only; password disabled)
- **WireGuard**: server `10.8.0.1/24`, UDP `51820`, peer = bot (`tae`) public key, `AllowedIPs = 10.8.0.2/32`
- **tinyproxy**: `10.8.0.1:8888`, `Allow 10.8.0.2`
- **UFW**: OpenSSH, `51820/udp`, `8888/tcp` on `wg0`
- **Telegram DNS**: `/etc/hosts` pins `149.154.167.220 api.telegram.org`
- `tae` peer endpoint: `132.243.240.175:51820`

Quick check after deploy:

```bash
ssh tae 'wg show; curl -sS -o /dev/null -w "%{http_code}\n" -x http://10.8.0.1:8888 https://api.telegram.org/'
```

### Finland (`fin`) — legacy / standby

- `wireguard`, `tinyproxy`
- `/etc/wireguard/wg0.conf` — server key, `ListenPort 51820`, peer = bot’s public key, `AllowedIPs` for the client
- `/etc/tinyproxy/tinyproxy.conf` — `Port 8888`, `Listen 10.8.0.1`, `Allow 10.8.0.2`, `Timeout 600`
- `systemctl enable --now wg-quick@wg0 tinyproxy`

**tinyproxy systemd drop-in** (`/etc/systemd/system/tinyproxy.service.d/wg-order-restart.conf`): `After=` and `Requires=` `wg-quick@wg0.service` so tinyproxy starts only after `10.8.0.1` exists; `Restart=on-failure`, `RestartSec=10`, and start limits to reduce crash loops. Recreate this file if you reinstall the `tinyproxy` package and lose the drop-in.

Public endpoint for the tunnel: **UDP `132.243.240.175:51820`** (Stockholm; active). Legacy Finland: `185.231.206.8:51820`.

### Bot host (`tae`) — installed packages

- `wireguard-tools`
- `/etc/wireguard/wg0.conf` — client key, `Address 10.8.0.2/24`, peer = server public key, `Endpoint = 132.243.240.175:51820` (Stockholm; was `185.231.206.8` for Finland), `AllowedIPs = 10.8.0.1/32`, `PersistentKeepalive = 25`
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

- **Restart tunnel after egress host reboot:** `wg-quick@wg0` and `tinyproxy` are enabled on `stockholm`; confirm `wg show` on both sides if the bot loses Telegram connectivity.
- **Telegram DNS on egress host:** Some `api.telegram.org` A records are unreachable from the egress VPS (CONNECT hangs; tinyproxy logs `Could not establish a connection to api.telegram.org`). Pin a working DC in `/etc/hosts` on `stockholm` (or `fin` if using legacy):
  ```bash
  # On fin — pick an IP that answers: curl --resolve api.telegram.org:443:<IP> https://api.telegram.org/
  echo '149.154.167.220 api.telegram.org' >> /etc/hosts
  systemctl restart tinyproxy
  ```
  Re-test from `tae`: `curl -x http://10.8.0.1:8888 -o /dev/null -w '%{http_code}\n' https://api.telegram.org/` (expect `302` quickly).
- **Firewall:** Egress host must allow **UDP 51820** and **TCP 8888 on `wg0`** (Stockholm: `ufw allow in on wg0 to any port 8888`).
- **Secrets:** WireGuard private keys live only under `/etc/wireguard/` on each host; do not commit them.

See also [live-server.md](live-server.md) for SSH paths and unit names.