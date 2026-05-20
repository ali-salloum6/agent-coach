# Live server access

**Deployments:** change the repo locally, **commit**, **push**, then on the server `cd ~/repo/agent-coach && git pull` and **`systemctl restart agent-coach.service agent-german.service agent-lifecoach.service`**. Do not edit application code on the server outside git (env files under `EnvironmentFile` are the exception).

To inspect or debug the production deployment:

1. **SSH**: `ssh tae`
2. **Repo** on the server: `~/repo/agent-coach` (same project as this repository)
3. **systemd** — three services, all running `bot.py` from that repo with different env files:
  - **Gym coach**: `agent-coach.service`
  - **German exam coach**: `agent-german.service`
  - **Life coach**: `agent-lifecoach.service`

Useful commands (on the server):

```bash
systemctl status agent-coach.service agent-german.service agent-lifecoach.service
journalctl -u agent-coach.service -f
journalctl -u agent-german.service -f
journalctl -u agent-lifecoach.service -f
```

List related units: `systemctl list-units 'agent-*'`.

Telegram traffic may use a **WireGuard + tinyproxy** path to Finland; see [outbound-proxy.md](outbound-proxy.md).