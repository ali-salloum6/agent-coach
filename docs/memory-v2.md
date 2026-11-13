# Memory v2

Approved on 2026-11-11. The gym coach moves first; other agents keep the
current memory until this is proven.

## Why

The current memory is one append-only list of sentences that is pasted whole
into the prompt. It cannot be summed, deduplicated or kept current, and it
grows without limit: it reached about 180,000 tokens in May, was reseeded,
and reached 311,000 characters again by November. Requests then fail for
size, and the model answers questions such as "today's totals?" from
whatever sentences happen to be in view.

## Four kinds of memory

| Kind | Holds | Stored as | The coach sees |
| --- | --- | --- | --- |
| Logs | Weigh-ins, tape, lifts, meals, events | One row per reading, set or meal | Summaries computed by code: latest values, trend, records, today's meals and totals |
| State | Profile, open threads, food reference, playbook | Small cards; a new value replaces the old one | The cards in full (the food reference only by match) |
| Story | How each phase went | One chapter per phase, one line per week | All of it |
| Archive | The old memory file, every chat message, expired notes | Searchable files, never deleted | Only entries that match the current message |

Every part of the prompt has a size limit, so the prompt cannot grow back.
Nothing is ever deleted: rewriting a card saves the previous version first.

## Files

Everything for an agent lives under `data/<slug>/` as plain text.

| Path | Content |
| --- | --- |
| `logs/metrics.jsonl` | `{id, date, metric, value, unit, cond, note, src, added}` |
| `logs/food.jsonl` | Items `{id, date, time, kind: "item", name, qty, kcal, protein, note, …}` and reported day totals `{kind: "day_total", kcal, protein, final, …}` |
| `logs/lifts.jsonl` | `{id, date, exercise, load, unit, reps: [..], note, …}` |
| `logs/events.jsonl` | `{id, date, kind, text, …}` |
| `cards/profile.md` | Durable facts as `- **Key**: value` bullets under fixed headings |
| `cards/threads.md` | Open side topics, a few bullets each |
| `cards/playbook.md` | Current phase, targets, weekly protocol, pivot rules, checkpoints, expectations |
| `cards/tripwires.json` | The pivot rules in checkable form |
| `cards/story.md` | Closed chapters and the current chapter's week lines |
| `foodbook.jsonl` | `{name, aliases, serving, kcal, protein_g, era, updated, note}` |
| `archive/` | Frozen old memory, chat export, expired notes |
| `history/` | Earlier versions of each card |

`ledger.py` owns the logs. Rules it enforces:

- A reading is keyed by date, metric and condition. `cond` is empty for the
  standard protocol (weight: home scale, fasted). A non-standard reading
  sits beside the standard one and stays out of trends.
- A day's food total is the sum of its items. A reported day total is used
  only for days without items (history, or "I ate about 2,300 today").
- A lift row is keyed by date, exercise and load.

## What the coach sees

The prompt is assembled from two parts:

- Stable, built at `/new`: profile, playbook, story, threads.
- Live, attached to every message: the dashboard computed from the logs
  (weight and tape trend, lift records, 7-day intake), today's meals and
  totals, tripwire status, non-expired recent notes, and archive matches for
  names mentioned in the message.

## Saving

`writer_v2.py` runs after each reply. A model call reads the exchange and
proposes operations as JSON; code checks each one and applies it:

| Operation | Writes |
| --- | --- |
| `log_food`, `correct_food`, `remove_food`, `day_total` | food log (corrections by the row id shown to the coach) |
| `log_metric`, `log_lift`, `event` | body, lift and event logs (same-day duplicates are replaced) |
| `set_profile` | one `- **Key**: value` line of the profile card |
| `set_thread`, `close_thread` | the threads card; closed topics go to `archive/closed_threads.md` |
| `food_reference` | `foodbook.jsonl`, matched by name or alias |
| `note` | short-lived notes with an expiry |
| `propose_change`, `apply_change`, `change_playbook` | `cards/pending.json`, then the playbook |
| `set_tripwire`, `remove_tripwire` | `cards/tripwires.json` |

Checks:

- The evidence must be an exact quote from the text it claims, at least 12
  characters or the whole message (Markdown symbols are ignored).
- Every number saved must be written in the exchange: readings and sets in
  the user's message, or in the coach's reply when the user sent a photo.
  Numbers in a profile fact, a playbook line or a pivot rule must come from
  the user's own message, the line being replaced, or a waiting proposal.
- Dates are at most 14 days old and never in the future; non-finite numbers
  and null corrections are refused.
- Profile facts, thread updates, removing food, playbook changes and pivot
  rules need the user's own words; the coach can only propose.
- Rejected operations and their reasons go to `memory_ops.jsonl`. A change
  refused because a card is full also leaves a short-lived note, so the
  coach sees it and can say so.

The bot waits for the previous memory step before building the next reply's
live block, and the step runs at low reasoning effort (`MEMORY_WRITER_EFFORT`)
so the wait stays short.

`review.py` runs on Mondays at noon (`WEEKLY_REVIEW_DAY`, `WEEKLY_REVIEW_TIME`),
on `/review`, and at startup when a scheduled run was missed. It writes the
last finished phase week into the open chapter of the story and refreshes the
chapter's "So far" line. When the story nears its limit, old weeks fold a
calendar month at a time into "Weeks A to B" lines, and the oldest folded
lines merge, so a chapter stays bounded however long the phase runs (checked
over two simulated years of daily data). Weeks up to `protect_through_week`
in `phase.json` keep their approved text. The check-in lists the week's
numbers, the pivot rules, cards above 90% of their limit, and a reminder
when today's weigh-in or tape is missing; it is added to the conversation so
the coach knows it was sent.

Card limits: profile 5,000 characters, threads 4,000, playbook 7,000, story
12,000. Condensing the profile, threads or playbook is still manual.

## Changing phase

A new phase (a cut, a build, maintenance, a diet break) is the user's
decision, saved by the `change_phase` operation from the user's own words. It
starts today or on a date up to 30 days ahead; a future start is kept in
`phase.json` as `next`, shown in the live block, and applied on that day
(`phases.apply_due`, called before each reply and each weekly review).

Starting a phase (`phases.py`):

- closes the open story chapter: the heading gets its date range, the "So
  far" line becomes a "Summary" line computed from the logs, and unprotected
  week lines fold into month lines;
- opens the next chapter with the starting readings from the logs and the
  agreed plan;
- rewrites the playbook's Phase section and leaves a 14-day note asking the
  coach to agree new targets and pivot rules with the user; those changes go
  through the usual playbook and tripwire operations;
- records the old phase in `phase.json` `history` and restarts the weekly
  review's week count.

When the story is still over its limit after folding the open chapter, the
oldest closed chapter moves in full to `archive/story_chapters.md` (where
archive search finds it) and keeps only its summary in the card.

## Switching a bot

1. Freeze the server's old memory files and convert anything added since the
   last freeze (`migrate_memory_v2.py logs` and `merge`; hand-sort the few
   new non-log entries).
2. Copy `data/<slug>/` to the server.
3. Set `MEMORY_VERSION=2` in that bot's env file and restart it. To go back,
   remove the line and restart: the old memory files are untouched.

## Decisions

- Plain text files, no database.
- `/remember` and `/memory` are removed (never used).
- Playbook changes need the user's OK. Story lines do not.
- The old memory path stays available behind `MEMORY_WRITER_MODE` until the
  new one has passed the quiz below.

## Migration

1. Freeze the old files (`data/archive/freeze-<date>/` on the server, a copy
   on the laptop) and export the Telegram chat.
2. `scripts/migrate_memory_v2.py logs` converts the old "Time Series" and
   "Timeline" sections into the logs by rule. Entries the rules cannot place
   are listed for review; hand corrections go in `overrides.jsonl`.
3. `scripts/migrate_memory_v2.py split` numbers the remaining entries. Each
   gets one destination: profile, threads, foodbook, playbook, story or
   archive. `report` shows where every entry went.
4. The story and playbook are drafted and reviewed by the user. Every number
   in them is checked against the logs.
5. A quiz of about 20 questions with known answers is run against the old
   and the new memory. The switch happens only when the new one does as
   well.
6. At the switch, steps 2 and 3 are repeated for entries added since the
   freeze.

## Status

- Stage 1 (convert the saved memory; live bot untouched): drafts done on
  2026-11-13 from the freeze of that morning. Every one of the 1,953 old
  entries has one destination (`coverage.md` in the work folder). Logs:
  125 body readings, 87 day totals, 158 lift rows over 64 training days,
  27 events. Cards: story, playbook and threads written and checked with
  `migrate_memory_v2.py verify`; the profile waits on the food sort. Two
  pivot rules are in `tripwires.json`. The user approved the story and
  playbook on 2026-11-13. Still waiting on a fresh Telegram export (needs
  the user's login).
- Stage 2 (built 2026-11-13, behind `MEMORY_VERSION=2`): prompt assembly
  (`memory_v2.py`), the new memory step (`writer_v2.py`) and the weekly
  review (`review.py`), with tests. An end-to-end run with the real model on
  a copy of the data logged meals, answered "today's totals?" correctly
  after `/new`, applied a correction by id, logged a weigh-in and produced
  the check-in.
- Code review (2026-11-13): an independent review found 13 issues (wrong
  week on non-Monday reviews, replies built before the last save, unbounded
  story growth, validation gaps, non-atomic writes, a sticky fallback note,
  scheduling crashes on bad settings, expired notes missing from search).
  All are fixed and covered by tests.
- Stage 3 (switch the gym coach): done on 2026-11-13 (commit 7aa4f9a). The old
  memory files stay on the server untouched, with a backup in
  `data/archive/switch-<date>/`; removing `MEMORY_VERSION=2` from `.env.gym`
  and restarting goes back to them. The life coach stays on memory v1.
- Phase changes built on 2026-11-13 (see "Changing phase"), tested with the
  real model on a copy of the data.
- Not built yet: automatic condensing of the profile, threads and playbook
  cards; the check-in warns at 90% full.

Provider note (2026-11-13): at peak load GPT-6.1 Sol rejects prompts above
a size that changes with demand ("high demand ... exceeds the maximum usage
size"), with a normal HTTP status. `llm.py` now retries those once with the
configured fallback, like a 429. The old fallback, Gemini 3.8 Flash, is
refused by the account's zero-data-retention setting, so GPT models now fall
back to Kimi K3.1 Pro.

Decisions about the user's own data taken while drafting are recorded in
`data/gym_coach/DECISIONS.md`, which is not committed: everything under
`data/` stays out of git.
