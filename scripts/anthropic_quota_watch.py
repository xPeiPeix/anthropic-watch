#!/usr/bin/env python3
"""anthropic_quota_watch.py - Anthropic Watch quota / credit monitor.

Single source of truth for the dashboard's quota features:

  * Part B - queries the Claude OAuth usage endpoint for the 5-hour and
    7-day window utilization + reset timestamps, and writes a JSON snapshot
    consumed by two Glance dashboard widgets.
  * Part A - scans Anthropic's official RSS / Atom feeds, applies a keyword
    pre-filter, then asks the AI gateway to confirm and summarize any
    announcement about quota / credit / rate-limit changes (the pre-filter
    is the "regex" path; the gateway is the fallback for prose that regex
    cannot judge).
  * Notification - sends one email (reusing the proven SMTP settings) when a
    usage window refreshes or a new quota-related announcement is detected.

No secrets are stored in this file or any tracked file. Every credential is
read at runtime from local, git-ignored locations and is never logged.

Exit code is always 0 unless invoked incorrectly; individual sections degrade
independently so a single failure never aborts the rest of the run.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import smtplib
import sys
import tempfile
import urllib.error
import urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

USAGE_ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
USAGE_HEADERS = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "oauth-2025-04-20",
    "User-Agent": "claude-cli/anthropic-watch",
}

# The AI gateway sits behind Cloudflare, which rejects the default urllib
# User-Agent with error 1010. A browser User-Agent is required.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
)

# Anthropic scatters quota / credit / limit changes across blog, help center,
# in-app, X and press - no single feed is reliable. So sources are layered:
#   * Anthropic News RSS (Olshansk mirror, fresh) - official blog posts
#   * claude-code GitHub Releases API (dated, authoritative) - CLI changes.
#     NOTE: the old Olshansk changelog mirror is dead (frozen 2026-04-18).
#   * status.anthropic.com Atom - incidents
#   * Tavily web search - the catch-all that surfaces announcements (and press
#     coverage) even when Anthropic publishes them nowhere machine-readable.
FEEDS = [
    ("Anthropic News",
     "https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds/feed_anthropic_news.xml"),
    ("Anthropic Status",
     "https://status.anthropic.com/history.atom"),
]

GITHUB_RELEASES_API = (
    "https://api.github.com/repos/anthropics/claude-code/releases?per_page=15"
)

# HN Algolia: reliable, free, and the place users post "my limits just reset /
# Anthropic gave me free credits" before any official channel says anything.
HN_SEARCH_API = "https://hn.algolia.com/api/v1/search_by_date"

# Tavily catch-all query. Deliberately covers community/forum phrasing (users
# reporting an early reset / surprise free credits), not just press releases,
# because these quota refreshes surface in forums first. Tavily returns
# reddit/threads/forum URLs, so this also covers Reddit (whose own API blocks
# unauthenticated access). Results bypass the keyword stage (already targeted).
TAVILY_QUERY = (
    "Anthropic Claude Code usage limit OR weekly limit OR rate limit OR "
    "free credits OR extra credits OR quota reset OR limits refreshed early OR "
    "users report limit reset announcement OR forum"
)

# Pre-filter: only entries matching one of these go to the AI gateway. This is
# the cheap "regex first" stage. Primary intent: Anthropic officially resetting
# quotas / granting free credits / promotions, so promo/grant terms are wide.
QUOTA_PATTERN = re.compile(
    r"credit|quota|rate[\s-]?limit|usage[\s-]?limit|spend[\s-]?limit|"
    r"weekly[\s-]?limit|5[\s-]?hour|five[\s-]?hour|token[\s-]?limit|"
    r"free[\s-]?tier|free[\s-]?credits?|promotion|promotional|bonus|"
    r"grant|giveaway|pricing|limit increase|increased? limits?|"
    r"额度|配额|限额|提额|积分|赠送|免费|领取|发放|福利|促销",
    re.IGNORECASE,
)


def _load_local_config() -> dict:
    """Read KEY=VALUE pairs from a local, git-ignored config file so personal
    paths / endpoints never enter version control. Location: $AW_CONFIG_FILE
    or scripts/aw_config.local.env next to this script (see aw_config.example.env).
    """
    path = Path(os.environ.get(
        "AW_CONFIG_FILE",
        str(Path(__file__).resolve().parent / "aw_config.local.env"),
    ))
    cfg: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return cfg


_LOCAL_CFG = _load_local_config()


def env(name: str, default: str) -> str:
    """Precedence: OS environment > local config file > built-in default."""
    if name in os.environ:
        return os.environ[name]
    return _LOCAL_CFG.get(name, default)


def int_env(name: str, default: int) -> int:
    """Tolerant int form of env(). A bad value falls back to the default
    instead of raising at import time — an unhandled error there would make
    the scheduled task fail silently with no log to diagnose from."""
    try:
        return int(env(name, str(default)))
    except (TypeError, ValueError):
        return default


CONFIG = {
    # Generic - same path for every Claude Code install, not personal.
    "credentials": env("AW_CLAUDE_CREDENTIALS",
                       str(Path.home() / ".claude" / ".credentials.json")),
    # Deployment-specific - MUST be set via env or aw_config.local.env.
    "tokens_secret": env("AW_TOKENS_SECRET", ""),
    "email_env": env("AW_EMAIL_ENV", ""),
    "gateway_url": env("AW_GATEWAY_URL", ""),
    # Generic defaults - safe to ship.
    "output": env("AW_OUTPUT", str(REPO_ROOT / "assets" / "anthropic_quota.json")),
    "state": env("AW_STATE", str(REPO_ROOT / "data" / "quota_state.json")),
    "gateway_model": env("AW_GATEWAY_MODEL", "gemini-2.5-flash"),
    "lookback_days": int_env("AW_DAYS", 14),
    "tavily_max": int_env("AW_TAVILY_MAX", 10),
    "no_email": env("AW_NO_EMAIL", "") not in ("", "0", "false", "False"),
}


LOG_FILE = REPO_ROOT / "data" / "watch.log"


def log(msg: str) -> None:
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        if sys.stderr is not None:
            print(line, file=sys.stderr, flush=True)
    except (ValueError, OSError):
        pass
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1_000_000:
            tail = LOG_FILE.read_text(encoding="utf-8").splitlines()[-2000:]
            LOG_FILE.write_text("\n".join(tail) + "\n", encoding="utf-8")
        with open(LOG_FILE, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_iso(value: str) -> dt.datetime | None:
    if not value:
        return None
    try:
        text = value.strip().replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Credentials (runtime only, never logged, never written to a tracked file)
# --------------------------------------------------------------------------- #

def read_oauth_token() -> tuple[str | None, dt.datetime | None]:
    path = Path(CONFIG["credentials"])
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        oauth = data.get("claudeAiOauth", {})
        token = oauth.get("accessToken")
        expires_at = oauth.get("expiresAt")
        expiry = (
            dt.datetime.fromtimestamp(expires_at / 1000, dt.timezone.utc)
            if expires_at else None
        )
        return token, expiry
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # TypeError guards a non-numeric expiresAt (e.g. a string written by
        # another tool) reaching `expires_at / 1000`.
        log(f"oauth token unavailable: {type(exc).__name__}")
        return None, None


def read_gateway_key() -> str | None:
    try:
        import yaml
    except ImportError:
        log("pyyaml missing; cannot read gateway key")
        return None
    try:
        data = yaml.safe_load(Path(CONFIG["tokens_secret"]).read_text(encoding="utf-8"))
        keys = data["providers"]["gateway_newapi"]["keys"]
        for item in keys:
            if isinstance(item, dict) and item.get("name") == "default-consumer":
                return item.get("key")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log(f"gateway key unavailable: {type(exc).__name__}")
    return None


def read_email_creds() -> tuple[str | None, str | None]:
    path = Path(CONFIG["email_env"])
    sender = password = None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            name, value = name.strip(), value.strip().strip('"').strip("'")
            if name == "EMAIL_SENDER":
                sender = value
            elif name == "EMAIL_PASSWORD":
                password = value
    except OSError as exc:
        log(f"email creds unavailable: {type(exc).__name__}")
    return sender, password


def read_tavily_key() -> str | None:
    try:
        for line in Path(CONFIG["email_env"]).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or not line.startswith("TAVILY_API_KEYS="):
                continue
            raw = line.split("=", 1)[1].strip().strip('"').strip("'")
            first = raw.split(",")[0].strip()
            return first or None
    except OSError as exc:
        log(f"tavily key unavailable: {type(exc).__name__}")
    return None


# --------------------------------------------------------------------------- #
# Email (mirrors a proven SMTP-by-domain notification setup)
# --------------------------------------------------------------------------- #

SMTP_BY_DOMAIN = {
    "qq.com": ("smtp.qq.com", 465, True),
    "foxmail.com": ("smtp.qq.com", 465, True),
    "163.com": ("smtp.163.com", 465, True),
    "126.com": ("smtp.126.com", 465, True),
    "gmail.com": ("smtp.gmail.com", 587, False),
    "outlook.com": ("smtp-mail.outlook.com", 587, False),
    "hotmail.com": ("smtp-mail.outlook.com", 587, False),
    "aliyun.com": ("smtp.aliyun.com", 465, True),
}


def send_email(subject: str, text_body: str, html_body: str) -> str:
    """Returns "sent" | "skipped" | "failed".

    "skipped" (disabled / not configured) is a permanent, intentional state -
    callers may safely advance dedup state. "failed" is a transient SMTP
    error - callers must NOT advance dedup state so the alert retries.
    """
    if CONFIG["no_email"]:
        log("email disabled (AW_NO_EMAIL); skipping send")
        return "skipped"
    sender, password = read_email_creds()
    if not sender or not password:
        log("email not configured; skipping send")
        return "skipped"

    domain = sender.rsplit("@", 1)[-1].lower()
    server, port, use_ssl = SMTP_BY_DOMAIN.get(domain, ("smtp." + domain, 465, True))

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("Anthropic Watch", sender))
    msg["To"] = sender
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        if use_ssl:
            client = smtplib.SMTP_SSL(server, port, timeout=30)
        else:
            client = smtplib.SMTP(server, port, timeout=30)
            client.starttls()
        with client:
            client.login(sender, password)
            client.sendmail(sender, [sender], msg.as_string())
        log(f"email sent: {subject}")
        return "sent"
    except (smtplib.SMTPException, OSError) as exc:
        log(f"email send failed: {type(exc).__name__}: {exc}")
        return "failed"


# --------------------------------------------------------------------------- #
# Part B - Claude subscription usage windows
# --------------------------------------------------------------------------- #

def fetch_usage(token: str) -> dict:
    request = urllib.request.Request(
        USAGE_ENDPOINT,
        headers={"Authorization": f"Bearer {token}", **USAGE_HEADERS},
    )
    with urllib.request.urlopen(request, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def shape_window(raw: dict | None) -> dict | None:
    if not isinstance(raw, dict):
        return None
    resets_dt = parse_iso(raw.get("resets_at"))
    remaining = None
    resets_at = raw.get("resets_at")
    if resets_dt is not None:
        # Normalise to second precision so the dashboard's parseTime is happy.
        resets_dt = resets_dt.replace(microsecond=0)
        resets_at = resets_dt.isoformat()
        remaining = max(0, int((resets_dt - now_utc()).total_seconds()))
    return {
        "utilization": raw.get("utilization"),
        "resets_at": resets_at,
        "resets_in_seconds": remaining,
    }


def build_limits_section(token: str | None, expiry: dt.datetime | None) -> dict:
    if not token:
        return {"ok": False, "error": "no_oauth_token",
                "hint": "运行一次 Claude Code 以刷新本地 OAuth 凭据"}
    if expiry is not None and expiry <= now_utc():
        return {"ok": False, "error": "oauth_token_expired",
                "hint": "运行一次 Claude Code 以刷新本地 OAuth 凭据"}
    try:
        usage = fetch_usage(token)
    except urllib.error.HTTPError as exc:
        reason = "oauth_token_expired" if exc.code == 401 else f"http_{exc.code}"
        return {"ok": False, "error": reason,
                "hint": "运行一次 Claude Code 以刷新本地 OAuth 凭据"
                if exc.code == 401 else ""}
    except (urllib.error.URLError, ValueError, OSError) as exc:
        return {"ok": False, "error": type(exc).__name__}

    return {
        "ok": True,
        "five_hour": shape_window(usage.get("five_hour")),
        "seven_day": shape_window(usage.get("seven_day")),
        "seven_day_opus": shape_window(usage.get("seven_day_opus")),
        "extra_usage": usage.get("extra_usage"),
    }


def detect_resets(prev: dict, limits: dict) -> list[str]:
    """A window refreshed iff its previously recorded reset time has elapsed.

    Fires at most once per window roll-over, guarded by a notified marker so
    repeated scheduler runs do not resend the same email.
    """
    if not limits.get("ok"):
        return []
    notified = set(prev.get("notified_resets", []))
    messages: list[str] = []
    labels = {"five_hour": "5 小时额度窗口", "seven_day": "7 天（周）额度窗口"}

    for key, label in labels.items():
        window = limits.get(key)
        if not window:
            continue
        prev_reset = prev.get(f"{key}_resets_at")
        prev_dt = parse_iso(prev_reset) if prev_reset else None
        if prev_dt is not None and now_utc() >= prev_dt and prev_reset not in notified:
            messages.append(
                f"{label} 已刷新：上一周期已于 {prev_reset} 结束，额度恢复，可以继续使用了。"
            )
            notified.add(prev_reset)

    # Persist the latest reset markers and the (capped) notified set.
    for key in labels:
        window = limits.get(key)
        prev[f"{key}_resets_at"] = window.get("resets_at") if window else None
    prev["notified_resets"] = list(notified)[-50:]
    return messages


# --------------------------------------------------------------------------- #
# Part A - official quota / credit announcements
# --------------------------------------------------------------------------- #

def fetch_entries() -> list[dict]:
    try:
        import feedparser
    except ImportError:
        log("feedparser missing; announcement scan skipped")
        return []

    cutoff = now_utc() - dt.timedelta(days=CONFIG["lookback_days"])
    entries: list[dict] = []
    for source, url in FEEDS:
        try:
            parsed = feedparser.parse(url)
        except Exception as exc:  # feedparser is broad by nature
            log(f"feed error {source}: {type(exc).__name__}")
            continue
        for item in parsed.entries:
            published = None
            for attr in ("published_parsed", "updated_parsed"):
                value = item.get(attr)
                if value:
                    published = dt.datetime(*value[:6], tzinfo=dt.timezone.utc)
                    break
            if published is not None and published < cutoff:
                continue
            entries.append({
                "id": item.get("id") or item.get("link") or item.get("title", ""),
                "title": (item.get("title") or "").strip(),
                "summary": re.sub(r"<[^>]+>", " ",
                                  item.get("summary", ""))[:600].strip(),
                "link": item.get("link", ""),
                "published": published.isoformat() if published else None,
                "source": source,
            })
    return entries


def fetch_github_releases() -> list[dict]:
    """claude-code GitHub Releases - dated, authoritative CLI changelog."""
    try:
        request = urllib.request.Request(
            GITHUB_RELEASES_API,
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": "anthropic-watch"},
        )
        with urllib.request.urlopen(request, timeout=20) as resp:
            releases = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, ValueError, OSError) as exc:
        log(f"github releases failed: {type(exc).__name__}")
        return []
    if not isinstance(releases, list):
        return []

    cutoff = now_utc() - dt.timedelta(days=CONFIG["lookback_days"])
    entries: list[dict] = []
    for rel in releases:
        published = parse_iso(rel.get("published_at"))
        if published is not None and published < cutoff:
            continue
        tag = rel.get("tag_name") or rel.get("name") or ""
        entries.append({
            "id": rel.get("html_url") or tag,
            "title": f"Claude Code {tag}",
            "summary": (rel.get("body") or "")[:600].strip(),
            "link": rel.get("html_url", ""),
            "published": published.isoformat() if published else None,
            "source": "Claude Code Release",
        })
    return entries


def fetch_hackernews() -> list[dict]:
    """Hacker News (Algolia) - community signal: users post about limit
    resets / surprise credits here before any official channel."""
    since = int((now_utc() - dt.timedelta(days=CONFIG["lookback_days"])).timestamp())
    url = (
        f"{HN_SEARCH_API}?query=claude%20limit&tags=(story,comment)"
        f"&numericFilters=created_at_i%3E{since}&hitsPerPage=30"
    )
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, ValueError, OSError) as exc:
        log(f"hacker news failed: {type(exc).__name__}")
        return []

    entries: list[dict] = []
    for hit in data.get("hits", []):
        title = hit.get("title") or hit.get("story_title") or ""
        text = re.sub(r"<[^>]+>", " ", hit.get("comment_text", "") or "")
        oid = hit.get("objectID", "")
        entries.append({
            "id": f"hn:{oid}",
            "title": title.strip() or "(HN discussion)",
            "summary": (title + " " + text)[:600].strip(),
            "link": f"https://news.ycombinator.com/item?id={oid}",
            "published": hit.get("created_at"),
            "source": "Hacker News",
        })
    return entries


def fetch_tavily() -> list[dict]:
    """Web search catch-all: surfaces quota/credit/limit announcements (and
    press coverage) even when Anthropic publishes them in no parseable feed."""
    key = read_tavily_key()
    if not key:
        log("tavily key unavailable; web catch-all skipped")
        return []
    payload = json.dumps({
        "api_key": key,
        "query": TAVILY_QUERY,
        "search_depth": "advanced",
        "max_results": CONFIG["tavily_max"],
        "days": CONFIG["lookback_days"],
    }).encode("utf-8")
    try:
        request = urllib.request.Request(
            "https://api.tavily.com/search", data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=40) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, ValueError, OSError) as exc:
        log(f"tavily failed: {type(exc).__name__}")
        return []

    entries: list[dict] = []
    for r in data.get("results", []):
        entries.append({
            "id": r.get("url", ""),
            "title": (r.get("title") or "").strip(),
            "summary": (r.get("content") or "")[:600].strip(),
            "link": r.get("url", ""),
            "published": r.get("published_date"),
            "source": "Web",
        })
    return entries


def keyword_prefilter(entries: list[dict]) -> list[dict]:
    return [
        e for e in entries
        if QUOTA_PATTERN.search(f"{e['title']} {e['summary']}")
    ]


def normalize_event_key(raw: str) -> str:
    """Lowercase ascii slug so minor AI phrasing drift still dedupes."""
    return re.sub(r"[^a-z0-9]+", "-", (raw or "").lower()).strip("-")


def ai_classify(candidates: list[dict], gateway_key: str,
                 known_keys: list[str]) -> list[dict]:
    """Ask the gateway to confirm/summarize quota-related items.

    Each result carries a stable ``event_key`` identifying the underlying
    event (not the article) so the same event reported by many URLs dedupes
    to one, plus ``is_recent``. On any failure returns [] (degrade).
    """
    listing = "\n".join(
        f"[{i}] ({c['source']}) {c['title']} :: {c['summary'][:280]}"
        for i, c in enumerate(candidates)
    )
    known = ", ".join(known_keys[-40:]) or "(无)"
    prompt = (
        "你是严谨的分类器。下面是可能与 Anthropic 相关的条目（含官方与媒体）。"
        "只把【确实在宣布 Anthropic / Claude / Claude Code 的用量额度、周限、"
        "速率限制、积分(credits)、免费额度、extra usage 等发生具体变更，或官方"
        "重置/发放额度、赠送福利】的条目标为 is_quota_related=true。"
        "明确排除：融资/估值/合作/客户案例/人事/安全研究/纯观点评测等即使提到"
        "Anthropic 也与额度变更无关的内容。\n"
        "关键：为每条给一个稳定的 event_key —— 用小写英文短横线标识其背后的"
        "【同一个事件】（不是文章），同一事件被多篇报道必须给同一个 key。"
        "例：spacex-5h-rate-double-2026-05、weekly-limit-plus50pct-jul2026、"
        "peak-hour-limit-change-2026-03、extra-usage-billing-change。"
        f"若条目指的是这些【已知事件】之一，必须复用其原 key：{known}。"
        "只有确实是新事件才新造 key。is_recent: 该变更是否最近约两周内发生"
        "(老调重弹/历史回顾=false)。仅返回 JSON 数组，无多余文字，格式："
        '[{"index":0,"is_quota_related":true,"event_key":"slug",'
        '"is_recent":true,"change_type":"赠送免费额度|额度刷新重置|提额|'
        '周限调整|限额调整|定价变化|其他","summary_zh":"一句话中文摘要"}]\n\n'
        f"条目：\n{listing}"
    )
    payload = json.dumps({
        "model": CONFIG["gateway_model"],
        "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    try:
        request = urllib.request.Request(
            CONFIG["gateway_url"],
            data=payload,
            headers={
                "Authorization": f"Bearer {gateway_key}",
                "Content-Type": "application/json",
                "User-Agent": BROWSER_UA,
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        content = body["choices"][0]["message"]["content"]
    except (urllib.error.URLError, ValueError, KeyError, OSError) as exc:
        log(f"ai gateway failed: {type(exc).__name__}: {exc}")
        return []

    match = re.search(r"\[[\s\S]*?\]", content)
    if not match:
        log("ai gateway returned no JSON array")
        return []
    try:
        verdicts = json.loads(match.group(0))
    except ValueError:
        log("ai gateway JSON parse failed")
        return []

    result: list[dict] = []
    for verdict in verdicts:
        if not isinstance(verdict, dict) or not verdict.get("is_quota_related"):
            continue
        idx = verdict.get("index")
        if not isinstance(idx, int) or not (0 <= idx < len(candidates)):
            continue
        item = dict(candidates[idx])
        item["summary_zh"] = (verdict.get("summary_zh") or "").strip()
        item["change_type"] = (verdict.get("change_type") or "其他").strip()
        item["event_key"] = normalize_event_key(
            verdict.get("event_key") or item["title"]
        )
        item["is_recent"] = bool(verdict.get("is_recent", True))
        result.append(item)
    return result


def _dedupe_by_id(entries: list[dict]) -> list[dict]:
    out, seen = [], set()
    for e in entries:
        key = e.get("id") or e.get("link") or e.get("title")
        if key and key not in seen:
            seen.add(key)
            out.append(e)
    return out


def build_announcements_section(prev: dict, gateway_key: str | None) -> tuple[dict, list[dict]]:
    # Feed/release/HN entries go through the cheap keyword stage (HN is noisy);
    # Tavily results are already query-targeted so they bypass it to the AI.
    feed_entries = _dedupe_by_id(
        fetch_entries() + fetch_github_releases() + fetch_hackernews()
    )
    web_entries = _dedupe_by_id(fetch_tavily())
    scanned = len(feed_entries) + len(web_entries)
    if scanned == 0:
        return {"ok": False, "error": "no_entries", "items": []}, []

    candidates = _dedupe_by_id(
        keyword_prefilter(feed_entries) + web_entries
    )
    if not candidates:
        return {"ok": True, "items": [], "scanned": scanned, "matched": 0}, []
    if not gateway_key or not CONFIG["gateway_url"]:
        return {"ok": False, "error": "no_gateway_config", "items": [],
                "scanned": scanned, "matched": len(candidates)}, []

    seen_keys = list(prev.get("seen_event_keys", []))
    flagged = ai_classify(candidates, gateway_key, seen_keys)

    # Collapse many articles about the same event into one, keeping the most
    # authoritative source. This is what stops "same event, new URL, new email".
    priority = {"Anthropic News": 0, "Claude Code Release": 1,
                "Anthropic Status": 2, "Web": 3, "Hacker News": 4}
    by_event: dict[str, dict] = {}
    for a in flagged:
        key = a.get("event_key") or normalize_event_key(a["title"])
        cur = by_event.get(key)
        if cur is None or priority.get(a["source"], 9) < priority.get(cur["source"], 9):
            by_event[key] = a
    events = list(by_event.values())

    seen_set = set(seen_keys)
    # An event emails only if its key was never seen AND it is recent (old news
    # resurfacing via fresh press never triggers mail, only shows on dashboard).
    fresh = [e for e in events
             if e["event_key"] not in seen_set and e.get("is_recent", True)]

    prev["seen_event_keys"] = list(
        dict.fromkeys(seen_keys + [e["event_key"] for e in events])
    )[-200:]
    prev.pop("seen_ids", None)  # retired: URL-based dedup caused the spam

    fresh_keys = {f["event_key"] for f in fresh}
    section = {
        "ok": True,
        "scanned": scanned,
        "matched": len(candidates),
        "items": [
            {
                "title": e["title"],
                "link": e["link"],
                "source": e["source"],
                "published": e["published"],
                "summary_zh": e["summary_zh"],
                "change_type": e["change_type"],
                "event_key": e["event_key"],
                "is_new": e["event_key"] in fresh_keys,
            }
            for e in events
        ],
    }
    return section, fresh


# --------------------------------------------------------------------------- #
# State + snapshot
# --------------------------------------------------------------------------- #

def load_state() -> dict:
    try:
        return json.loads(Path(CONFIG["state"]).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def atomic_write(path_str: str, payload: str) -> None:
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def compose_email(reset_msgs: list[str], fresh: list[dict]) -> tuple[str, str, str]:
    if reset_msgs and fresh:
        subject = "Anthropic 额度已刷新 + 新额度公告"
    elif reset_msgs:
        subject = "Anthropic 额度已刷新"
    else:
        subject = f"Anthropic 新额度公告 ({len(fresh)})"

    lines = ["Anthropic Watch 检测到以下变化：", ""]
    html = ["<h2>Anthropic Watch</h2>"]
    if reset_msgs:
        lines.append("【额度刷新】")
        html.append("<h3>额度刷新</h3><ul>")
        for m in reset_msgs:
            lines.append(f"  - {m}")
            html.append(f"<li>{m}</li>")
        html.append("</ul>")
        lines.append("")
    if fresh:
        lines.append("【新额度相关公告】")
        html.append("<h3>新额度相关公告</h3><ul>")
        for a in fresh:
            lines.append(f"  - [{a['change_type']}] {a['title']}")
            lines.append(f"    {a['summary_zh']}")
            lines.append(f"    {a['link']}")
            html.append(
                f"<li><b>[{a['change_type']}]</b> "
                f"<a href=\"{a['link']}\">{a['title']}</a><br>"
                f"{a['summary_zh']}</li>"
            )
        html.append("</ul>")
    return subject, "\n".join(lines), "".join(html)


def main() -> int:
    parser = argparse.ArgumentParser(description="Anthropic Watch quota monitor")
    parser.add_argument("--once", action="store_true",
                        help="run a single cycle (default behaviour)")
    parser.add_argument("--no-email", action="store_true",
                        help="never send email this run")
    parser.add_argument("--test-email", action="store_true",
                        help="send one test email then exit")
    args = parser.parse_args()
    if args.no_email:
        CONFIG["no_email"] = True

    if args.test_email:
        status = send_email(
            "Anthropic Watch 邮件通道测试",
            "这是一封来自 Anthropic Watch 的测试邮件，收到说明额度刷新推送通道已打通。",
            "<h3>Anthropic Watch</h3><p>邮件通道测试成功 - 额度刷新推送已就绪。</p>",
        )
        return 0 if status == "sent" else 1

    state = load_state()
    token, expiry = read_oauth_token()
    gateway_key = read_gateway_key()

    limits = build_limits_section(token, expiry)
    reset_msgs = detect_resets(state, limits)

    announcements, fresh = build_announcements_section(state, gateway_key)

    snapshot = {
        "updated_at": now_utc().isoformat(),
        "limits": limits,
        "announcements": announcements,
    }
    atomic_write(CONFIG["output"], json.dumps(snapshot, ensure_ascii=False, indent=2))
    state["last_run"] = now_utc().isoformat()

    # Send before persisting dedup state. detect_resets() / build_announcements
    # already marked these events notified/seen in `state`; persisting that
    # before a failed send would permanently suppress the retry (codex C1).
    email_status = "skipped"
    if reset_msgs or fresh:
        subject, text_body, html_body = compose_email(reset_msgs, fresh)
        email_status = send_email(subject, text_body, html_body)

    if email_status != "failed":
        atomic_write(CONFIG["state"], json.dumps(state, ensure_ascii=False, indent=2))
    else:
        log("email send failed; dedup state NOT persisted so the alert retries next run")

    log(f"snapshot written: limits.ok={limits.get('ok')} "
        f"announcements={len(announcements.get('items', []))} "
        f"resets={len(reset_msgs)} fresh={len(fresh)} email={email_status}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
