# Memory Writer rollout to production

These steps replace the old free-form append extractor with the new
structured memory writer (`memory_writer.py`) and swap the bloated
`data/gym_coach.md` with the freshly seeded curated file.

Server context: `ssh tae`, repo at `~/repo/agent-coach`, two systemd
services (`agent-coach.service` for gym coach, `agent-german.service` for
the German tutor). See [live-server.md](live-server.md).

## 0. One-time local prep

Already done on your laptop (this directory):

1. `python3 scripts/export_telegram_chat.py` — exports the full chat to
   `exports/telegram_chat/messages.jsonl`.
2. `python3 scripts/seed_memory_from_export.py` — runs every exchange
   through the new memory writer and produces:
   - `data/gym_coach.md` — curated long-term memory
   - `data/gym_coach.recent.md` — only the last few days of recent notes
     (older ones expired and were pruned automatically)
   - `data/gym_coach.memory_ops.jsonl` — audit log

Verify both files look sane before deploying:

```bash
wc -c data/gym_coach.md data/gym_coach.recent.md
head -200 data/gym_coach.md
```

If the canonical file is still too big, hand-trim it now. Removing items
is safe; the writer will refill anything important from new exchanges.

## 1. Push the code change

From this repo:

```bash
git add -A
git commit -m "Memory writer: structured JSON ops + recent TTL + remove /summarize"
git push
```

## 2. SSH and back up the old memory

```bash
ssh tae
cd ~/repo/agent-coach
```

Snapshot what's running today (the 180k-token file plus any backups
`forget`/`summarize` have created):

```bash
ts=$(date -u +%Y%m%d_%H%M%S)
mkdir -p backups
cp -a data/gym_coach.md "backups/gym_coach.md.$ts"
ls -la backups/ data/
```

Keep this around for at least a few weeks in case anything looks wrong
post-rollout.

## 3. Pull the new code

```bash
git fetch
git pull
pip install -r requirements.txt   # picks up `telethon` if not yet installed
```

## 4. Upload the seeded memory files

From your laptop (NOT on the server):

```bash
scp data/gym_coach.md          tae:~/repo/agent-coach/data/gym_coach.md
scp data/gym_coach.recent.md   tae:~/repo/agent-coach/data/gym_coach.recent.md
```

Skip uploading `data/gym_coach.memory_ops.jsonl`. The server will start
its own audit log from the next reply onward.

If you also want to clear it server-side, on the server run:

```bash
rm -f data/gym_coach.memory_ops.jsonl
```

## 5. (Optional) Configure the writer

The defaults are already correct, but the env file accepts overrides:

```ini
# /etc/systemd/system/agent-coach.service.d/override.conf
# or wherever EnvironmentFile= lives
MEMORY_WRITER_MODE=structured     # default; set to "legacy" only for emergency rollback
MEMORY_WRITER_MODEL=google/gemini-3.1-pro-preview  # default
```

If you change the env file, `systemctl daemon-reload`.

## 6. Restart and tail

```bash
sudo systemctl restart agent-coach.service agent-german.service
journalctl -u agent-coach.service -f
```

On startup you should see logs like:

```
Bot starting with agent=Gym Coach model=google/gemini-3.1-pro-preview
Pruned N expired recent notes on startup        # (only if any expired entries exist)
```

Send a test message in Telegram. After a reply you should see a line
like:

```
memory_writer: accepted=1 rejected=0 pruned=0
```

The audit log will grow:

```bash
wc -l data/gym_coach.memory_ops.jsonl
tail -1 data/gym_coach.memory_ops.jsonl | jq .
```

## 7. Verify with the bot

In Telegram:

- `/start` — greeting should no longer list `/summarize`.
- `/memory` — should show the curated canonical sections plus the
  auto-expiring recent notes block. No giant wall of stale facts.
- Send a measurement message, e.g. "I slept 6 hours and weighed 95.2
  kg." After the bot replies, `data/gym_coach.md` should have a new
  entry under `## Time Series > ### Sleep` and `### Weight`.
- Send a transient detail, e.g. "Had a banana for breakfast." It should
  land in `data/gym_coach.recent.md` (with `ttl_days=2`), NOT in
  canonical memory.

## 8. Roll back (only if something is wrong)

In the unlikely case the new writer misbehaves:

```bash
# stop services
sudo systemctl stop agent-coach.service

# restore the previous canonical memory and remove the new files
cp -a backups/gym_coach.md.YYYYMMDD_HHMMSS data/gym_coach.md
rm -f data/gym_coach.recent.md data/gym_coach.memory_ops.jsonl

# switch the writer back to the legacy extractor
# (in the systemd EnvironmentFile)
MEMORY_WRITER_MODE=legacy

sudo systemctl daemon-reload
sudo systemctl start agent-coach.service
```

The legacy free-form append path is preserved in `memory.py`
specifically for this rollback case. Once the new path is proven stable,
that branch can be deleted.

## 9. Periodic hygiene

The new writer prunes expired recent notes automatically on every reply,
`/new`, and bot startup, so there is no manual maintenance step. If you
want a sanity check from time to time:

```bash
wc -l data/gym_coach.memory_ops.jsonl
# trim periodically if it gets unreasonably large:
tail -n 10000 data/gym_coach.memory_ops.jsonl > /tmp/audit.jsonl
mv /tmp/audit.jsonl data/gym_coach.memory_ops.jsonl
```

Same for the canonical file: open `data/gym_coach.md` in an editor
occasionally and remove anything that is no longer useful. Hand-edits
are safe — the writer only appends/annotates, it does not rewrite the
file structure.
