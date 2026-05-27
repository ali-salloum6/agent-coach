# Live server access

**Deployments:** change the repo locally, **commit**, **push**, then on the server `cd ~/repo/agent-coach && git pull` and **`systemctl restart agent-coach.service agent-german.service agent-lifecoach.service agent-supernova.service`**. Do not edit application code on the server outside git (env files under `EnvironmentFile` are the exception).

To inspect or debug the production deployment:

1. **SSH**: `ssh tae`
2. **Repo** on the server: `~/repo/agent-coach` (same project as this repository)
3. **systemd** — four services, all running `bot.py` from that repo with different env files:
  - **Gym coach**: `agent-coach.service` (`.env.gym`)
  - **German exam coach**: `agent-german.service` (`.env.german`)
  - **Life coach**: `agent-lifecoach.service` (`.env.lifecoach`)
  - **Gym coach (supernova)**: `agent-supernova.service` (`.env.supernova`)

Useful commands (on the server):

```bash
systemctl status agent-coach.service agent-german.service agent-lifecoach.service agent-supernova.service
journalctl -u agent-coach.service -f
journalctl -u agent-german.service -f
journalctl -u agent-lifecoach.service -f
journalctl -u agent-supernova.service -f
```

List related units: `systemctl list-units 'agent-*'`.

Telegram traffic may use a **WireGuard + tinyproxy** path to Finland; see [outbound-proxy.md](outbound-proxy.md).