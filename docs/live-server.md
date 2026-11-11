# Live server access

**Deployments:** change the repo locally, **commit**, **push**, then on the server `cd ~/repo/agent-coach && git pull --ff-only` and **`systemctl restart agent-coach.service agent-lifecoach.service`**. Restart only the enabled bots: restarting `agent-german` or `agent-supernova` would start them. Do not edit application code on the server outside git (env files under `EnvironmentFile` are the exception).

To inspect or debug the production deployment:

1. **SSH**: `ssh YOUR_SERVER`
2. **Repo** on the server: `~/repo/agent-coach` (same project as this repository)
3. **systemd** — four services, all running `bot.py` from that repo with different env files:
  - **Gym coach**: `agent-coach.service` (`.env.gym`), enabled
  - **Life coach**: `agent-lifecoach.service` (`.env.lifecoach`), enabled
  - **German exam coach**: `agent-german.service` (`.env.german`), installed but disabled
  - **Gym coach (supernova)**: `agent-supernova.service` (`.env.supernova`), installed but disabled

Useful commands (on the server):

```bash
systemctl status agent-coach.service agent-lifecoach.service
journalctl -u agent-coach.service -f
journalctl -u agent-lifecoach.service -f
```

List related units: `systemctl list-unit-files 'agent-*'`.

The server reaches Telegram and OpenRouter directly, so no proxy is set. [outbound-proxy.md](outbound-proxy.md) describes the old WireGuard + tinyproxy setup in case one is needed again.
