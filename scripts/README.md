# Anthropic Watch — quota monitor

`anthropic_quota_watch.py` is the single brain behind the dashboard's two
quota widgets and the e-mail alerts. It runs periodically (Windows Task
Scheduler), writes a JSON snapshot that Glance renders, and e-mails you when
something quota-relevant changes.

## What it does

- **Subscription windows** — queries the Claude OAuth usage endpoint and
  records the 5-hour and 7-day window utilization + reset times.
- **Quota / credit announcements** — scans Anthropic News (RSS), the
  `anthropics/claude-code` GitHub releases, Hacker News and a Tavily web
  search, then asks an AI gateway to confirm and summarize anything about
  usage limits, weekly limits, rate limits, free credits or quota resets.
  Deduplicated by a stable per-event key so the same event reported by many
  URLs only alerts once.
- **E-mail** — on a genuinely new event (or a window roll-over) it sends one
  e-mail, reusing the SMTP settings from your existing `.env`.

The snapshot is written to `assets/anthropic_quota.json`; dedup state to
`data/quota_state.json`; a log to `data/watch.log`. All three are git-ignored.

## Configuration

No secrets or personal paths are committed. Copy the template and fill it in:

```
cp scripts/aw_config.example.env scripts/aw_config.local.env
```

`aw_config.local.env` is git-ignored. Required keys:

| Key | Meaning |
| --- | --- |
| `AW_TOKENS_SECRET` | YAML file holding the AI gateway key (`providers.gateway_newapi.keys[name==default-consumer]`) |
| `AW_EMAIL_ENV` | `.env`-style file providing `EMAIL_SENDER`, `EMAIL_PASSWORD`, optional `TAVILY_API_KEYS` |
| `AW_GATEWAY_URL` | OpenAI-compatible chat-completions endpoint of your AI gateway |

Optional overrides (`AW_GATEWAY_MODEL`, `AW_DAYS`, `AW_PYTHON`, …) are listed
in `aw_config.example.env`. Every key may also be set as an OS environment
variable, which takes precedence over the file.

Each external dependency degrades independently: a missing gateway key only
disables announcement classification, a missing e-mail config only disables
e-mail — the rest of the run still completes.

## Running

One-off:

```
python scripts/anthropic_quota_watch.py            # full run
python scripts/anthropic_quota_watch.py --no-email  # snapshot only
python scripts/anthropic_quota_watch.py --test-email
```

Scheduled (Windows, every 15 min, windowless, no elevation required):

```
pwsh -File scripts/register_task.ps1                 # register
pwsh -File scripts/register_task.ps1 -IntervalMinutes 10
pwsh -File scripts/register_task.ps1 -Unregister     # remove
schtasks /Run /TN AnthropicWatchQuota                # run now
```

Requires Python 3.10+ with `pyyaml` and `feedparser`.
