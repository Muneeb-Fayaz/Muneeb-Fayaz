#!/usr/bin/env python3
"""
Self-hosted GitHub analytics cards (stats, languages, streak, activity heatmap).

Runs inside GitHub Actions with a personal access token, so PRIVATE repos are
included. Standard library only - no pip install needed.

Usage:
    GH_TOKEN=ghp_xxx python scripts/generate_stats.py
    python scripts/generate_stats.py --demo      # fake data, to preview the design

Environment:
    GH_TOKEN    classic PAT with `repo` + `read:user` scopes (required unless --demo)
    TZ_OFFSET   your UTC offset, e.g. "+05:30" (default). Used to decide what
                "today" is and which day a commit belongs to.
    OUT_DIR     where SVGs are written (default: assets)
"""
import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from xml.sax.saxutils import escape

API = "https://api.github.com/graphql"

# ---------------------------------------------------------------- theme ----
BG = "#1a1b27"
BORDER = "#2a2c3f"
TITLE = "#70a5fd"
TEXT = "#a9b1d6"
TEAL = "#38bdae"
PURPLE = "#bf91f3"
MUTED = "#565f89"
HEAT = ["#232538", "#1f4d5c", "#2a8a8f", "#38bdae", "#7ef0d4"]
FONT = "'Segoe UI', Ubuntu, 'Helvetica Neue', Arial, sans-serif"


# ------------------------------------------------------------- GraphQL -----
def gql(token, query, variables=None):
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        API,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "profile-analytics",
        },
    )
    last = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.load(resp)
            if "errors" in data and not data.get("data"):
                raise RuntimeError(data["errors"])
            if "errors" in data:
                print("  warning:", data["errors"][0].get("message"), file=sys.stderr)
            return data["data"]
        except (urllib.error.URLError, RuntimeError, TimeoutError) as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
    raise SystemExit(f"GitHub API failed: {last}")


def parse_offset(text):
    sign = -1 if text.strip().startswith("-") else 1
    hh, _, mm = text.strip().lstrip("+-").partition(":")
    return timezone(sign * timedelta(hours=int(hh or 0), minutes=int(mm or 0)))


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------- data fetch -----
def fetch_all(token, tz):
    me = gql(token, "query{viewer{login name id createdAt}}")["viewer"]
    login, uid = me["login"], me["id"]
    created = datetime.fromisoformat(me["createdAt"].replace("Z", "+00:00"))
    print(f"Account: {login} (created {created.date()})")

    # 1) contribution calendar, one <=365-day window at a time
    cal, totals = {}, Counter()
    q = """query($from:DateTime!,$to:DateTime!){viewer{contributionsCollection(from:$from,to:$to){
      totalCommitContributions totalPullRequestContributions totalIssueContributions
      totalPullRequestReviewContributions restrictedContributionsCount
      contributionCalendar{weeks{contributionDays{date contributionCount}}}}}}"""
    start, now = created, datetime.now(timezone.utc)
    while start < now:
        end = min(start + timedelta(days=364), now)
        cc = gql(token, q, {"from": iso(start), "to": iso(end)})["viewer"]["contributionsCollection"]
        for k in ("totalCommitContributions", "totalPullRequestContributions",
                  "totalIssueContributions", "totalPullRequestReviewContributions",
                  "restrictedContributionsCount"):
            totals[k] += cc[k]
        for w in cc["contributionCalendar"]["weeks"]:
            for d in w["contributionDays"]:
                cal[d["date"]] = max(cal.get(d["date"], 0), d["contributionCount"])
        start = end + timedelta(seconds=1)

    # 2) every owned repo (public + private): stars, languages, default branch
    repos, cursor = [], None
    rq = """query($c:String){viewer{repositories(first:50,after:$c,ownerAffiliations:OWNER,isFork:false){
      pageInfo{hasNextPage endCursor}
      nodes{nameWithOwner name isPrivate stargazerCount owner{login}
        defaultBranchRef{name}
        languages(first:10,orderBy:{field:SIZE,direction:DESC}){edges{size node{name color}}}}}}}"""
    while True:
        r = gql(token, rq, {"c": cursor})["viewer"]["repositories"]
        repos += r["nodes"]
        if not r["pageInfo"]["hasNextPage"]:
            break
        cursor = r["pageInfo"]["endCursor"]

    # 3) YOUR commits on each default branch -> works even if the profile's
    #    "Private contributions" toggle is off
    hq = """query($o:String!,$n:String!,$uid:ID!,$c:String){repository(owner:$o,name:$n){
      defaultBranchRef{target{... on Commit{history(first:100,after:$c,author:{id:$uid}){
        pageInfo{hasNextPage endCursor} nodes{authoredDate}}}}}}}"""
    commit_days, commit_total = Counter(), 0
    for repo in repos:
        if not repo["defaultBranchRef"]:
            continue
        cur, n_repo = None, 0
        while True:
            h = gql(token, hq, {"o": repo["owner"]["login"], "n": repo["name"], "uid": uid, "c": cur})
            tgt = ((h.get("repository") or {}).get("defaultBranchRef") or {}).get("target") or {}
            hist = tgt.get("history")
            if not hist:
                break
            for node in hist["nodes"]:
                dt = datetime.fromisoformat(node["authoredDate"].replace("Z", "+00:00")).astimezone(tz)
                commit_days[dt.date().isoformat()] += 1
                n_repo += 1
            if not hist["pageInfo"]["hasNextPage"]:
                break
            cur = hist["pageInfo"]["endCursor"]
        commit_total += n_repo
        tag = "private" if repo["isPrivate"] else "public "
        print(f"  {tag} {repo['nameWithOwner']}: {n_repo} commits by you")

    # 4) merge: a day counts if EITHER source saw activity
    merged = dict(cal)
    for d, n in commit_days.items():
        merged[d] = max(merged.get(d, 0), n)
    print(f"Active days - calendar: {sum(1 for v in cal.values() if v)}, "
          f"from commits: {len(commit_days)}, merged: {sum(1 for v in merged.values() if v)}")
    if totals["restrictedContributionsCount"] and sum(1 for v in cal.values() if v) < len(commit_days):
        print("TIP: turn on 'Private contributions' in your profile's contribution settings "
              "so GitHub's own graph matches.")

    langs = Counter()
    colors = {}
    for repo in repos:
        for e in repo["languages"]["edges"]:
            langs[e["node"]["name"]] += e["size"]
            colors[e["node"]["name"]] = e["node"]["color"] or "#8a8fb5"

    return {
        "login": login,
        "name": me["name"] or login,
        "created": created.date(),
        "days": merged,
        "stars": sum(r["stargazerCount"] for r in repos),
        "commits": max(totals["totalCommitContributions"] + totals["restrictedContributionsCount"], commit_total),
        "prs": totals["totalPullRequestContributions"],
        "issues": totals["totalIssueContributions"],
        "reviews": totals["totalPullRequestReviewContributions"],
        "repos_public": sum(1 for r in repos if not r["isPrivate"]),
        "repos_private": sum(1 for r in repos if r["isPrivate"]),
        "langs": langs,
        "colors": colors,
    }


def demo_data(tz):
    rnd = random.Random(7)
    today = datetime.now(tz).date()
    days = {}
    for i in range(420):
        d = today - timedelta(days=i)
        if i < 9 or (i > 20 and rnd.random() < 0.62):
            days[d.isoformat()] = rnd.choice([1, 1, 2, 3, 5, 8])
    langs = Counter({"JavaScript": 52000, "HTML": 31000, "CSS": 18000, "Python": 24000, "Jupyter Notebook": 9000, "Shell": 2500})
    colors = {"JavaScript": "#f1e05a", "HTML": "#e34c26", "CSS": "#663399", "Python": "#3572A5",
              "Jupyter Notebook": "#DA5B0B", "Shell": "#89e051"}
    return {"login": "demo", "name": "Demo", "created": today - timedelta(days=500), "days": days,
            "stars": 4, "commits": sum(days.values()), "prs": 6, "issues": 2, "reviews": 1,
            "repos_public": 5, "repos_private": 9, "langs": langs, "colors": colors}


# ------------------------------------------------------------- streaks -----
def compute_streaks(days, today):
    """Return (total, first_day, current, longest); streak = (length, start, end)."""
    active = sorted(date.fromisoformat(d) for d, n in days.items() if n > 0 and date.fromisoformat(d) <= today)
    total = sum(n for d, n in days.items() if date.fromisoformat(d) <= today)
    if not active:
        return 0, today, (0, today, today), (0, today, today)
    have = set(active)

    longest, run_start, prev = (0, active[0], active[0]), active[0], None
    for d in active:
        if prev is None or d - prev > timedelta(days=1):
            run_start = d
        length = (d - run_start).days + 1
        if length > longest[0]:
            longest = (length, run_start, d)
        prev = d

    # a streak is still alive if you've already contributed today OR did yesterday
    anchor = today if today in have else today - timedelta(days=1)
    if anchor in have:
        s = anchor
        while s - timedelta(days=1) in have:
            s -= timedelta(days=1)
        current = ((anchor - s).days + 1, s, anchor)
    else:
        current = (0, today, today)
    return total, active[0], current, longest


# ----------------------------------------------------------------- SVG -----
def fmt_day(d, today, force_year=False):
    s = f"{d.strftime('%b')} {d.day}"
    return f"{s}, {d.year}" if force_year or d.year != today.year else s


def card(width, height, title, body, extra_defs=""):
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">
  <title>{escape(title)}</title>
  <defs>{extra_defs}</defs>
  <style>
    text {{ font-family: {FONT}; }}
    .t {{ fill: {TITLE}; font-size: 18px; font-weight: 600; }}
    .l {{ fill: {TEXT}; font-size: 13px; }}
    .v {{ fill: {TEAL}; font-size: 14px; font-weight: 700; }}
  </style>
  <rect x="0.5" y="0.5" rx="6" width="{width - 1}" height="{height - 1}" fill="{BG}" stroke="{BORDER}"/>
{body}
</svg>
"""


def svg_stats(d, today):
    rows = [
        ("Total Stars Earned", f"{d['stars']:,}"),
        ("Total Commits", f"{d['commits']:,}"),
        ("Pull Requests", f"{d['prs']:,}"),
        ("Issues Opened", f"{d['issues']:,}"),
        ("Code Reviews", f"{d['reviews']:,}"),
        ("Repositories", f"{d['repos_public'] + d['repos_private']:,}  ({d['repos_private']} private)"),
    ]
    out = [f'  <text x="25" y="35" class="t">{escape(d["name"])}\'s GitHub Stats</text>']
    for i, (k, v) in enumerate(rows):
        y = 68 + i * 26
        out.append(f'  <circle cx="30" cy="{y - 4}" r="3.5" fill="{PURPLE}"/>')
        out.append(f'  <text x="44" y="{y}" class="l">{k}</text>')
        out.append(f'  <text x="470" y="{y}" class="v" text-anchor="end">{escape(v)}</text>')
    return card(495, 215, "GitHub stats", "\n".join(out))


def svg_langs(d):
    langs = d["langs"].most_common(7)
    total = sum(d["langs"].values()) or 1
    other = total - sum(v for _, v in langs)
    items = [(n, v, d["colors"].get(n, "#8a8fb5")) for n, v in langs]
    if other > 0:
        items.append(("Other", other, MUTED))
    out = [f'  <text x="25" y="35" class="t">Most Used Languages</text>']
    # stacked bar, clipped to rounded ends
    bar_x, bar_w, bar_y = 25, 445, 52
    out.append(f'  <clipPath id="bar"><rect x="{bar_x}" y="{bar_y}" width="{bar_w}" height="10" rx="5"/></clipPath>')
    x = bar_x
    out.append('  <g clip-path="url(#bar)">')
    for n, v, c in items:
        w = bar_w * v / total
        out.append(f'    <rect x="{x:.1f}" y="{bar_y}" width="{w + 0.5:.1f}" height="10" fill="{c}"/>')
        x += w
    out.append("  </g>")
    for i, (n, v, c) in enumerate(items):
        col, row = i % 2, i // 2
        px, py = 25 + col * 225, 92 + row * 28
        out.append(f'  <circle cx="{px + 5}" cy="{py - 4}" r="5" fill="{c}"/>')
        out.append(f'  <text x="{px + 17}" y="{py}" class="l">{escape(n)}</text>')
        out.append(f'  <text x="{px + 205}" y="{py}" class="v" text-anchor="end">{100 * v / total:.1f}%</text>')
    return card(495, 215, "Most used languages", "\n".join(out))


def svg_streak(d, today):
    total, first, cur, lng = compute_streaks(d["days"], today)
    W, H = 1000, 210
    cx = [W / 6, W / 2, 5 * W / 6]

    def range_txt(s, e, ongoing):
        if s == e:
            return fmt_day(s, today)
        return f"{fmt_day(s, today)} - {'Present' if ongoing else fmt_day(e, today)}"

    out = []
    # dividers
    for x in (W / 3, 2 * W / 3):
        out.append(f'  <line x1="{x:.0f}" y1="35" x2="{x:.0f}" y2="{H - 35}" stroke="{MUTED}" stroke-width="1"/>')
    # total
    out.append(f'  <text x="{cx[0]:.0f}" y="100" text-anchor="middle" style="fill:{TITLE};font-size:46px;font-weight:700">{total:,}</text>')
    out.append(f'  <text x="{cx[0]:.0f}" y="132" text-anchor="middle" style="fill:{TITLE};font-size:18px">Total Contributions</text>')
    out.append(f'  <text x="{cx[0]:.0f}" y="160" text-anchor="middle" style="fill:{TEAL};font-size:14px">{fmt_day(first, today, True)} - Present</text>')
    # current streak ring
    alive = cur[0] > 0
    ring = TITLE if alive else MUTED
    out.append(f'  <circle cx="{cx[1]:.0f}" cy="88" r="46" fill="none" stroke="{ring}" stroke-width="6"/>')
    out.append(f'  <rect x="{cx[1] - 14:.0f}" y="30" width="28" height="22" fill="{BG}"/>')
    flame = f"M{cx[1]:.0f} 30 c5 6 9 10 9 16 a9 9 0 0 1 -18 0 c0 -4 2 -7 4 -9 c0 3 2 5 4 5 c-1 -4 -1 -8 1 -12 z"
    out.append(f'  <path d="{flame}" fill="{ring}"/>')
    out.append(f'  <text x="{cx[1]:.0f}" y="99" text-anchor="middle" style="fill:{PURPLE};font-size:38px;font-weight:700">{cur[0]}</text>')
    out.append(f'  <text x="{cx[1]:.0f}" y="162" text-anchor="middle" style="fill:{PURPLE};font-size:18px;font-weight:600">Current Streak</text>')
    cur_txt = range_txt(cur[1], cur[2], True) if alive else "Contribute today to start one"
    out.append(f'  <text x="{cx[1]:.0f}" y="186" text-anchor="middle" style="fill:{TEAL};font-size:14px">{escape(cur_txt)}</text>')
    # longest
    out.append(f'  <text x="{cx[2]:.0f}" y="100" text-anchor="middle" style="fill:{TITLE};font-size:46px;font-weight:700">{lng[0]}</text>')
    out.append(f'  <text x="{cx[2]:.0f}" y="132" text-anchor="middle" style="fill:{TITLE};font-size:18px">Longest Streak</text>')
    lng_ongoing = alive and lng[2] == cur[2]
    out.append(f'  <text x="{cx[2]:.0f}" y="160" text-anchor="middle" style="fill:{TEAL};font-size:14px">{escape(range_txt(lng[1], lng[2], lng_ongoing))}</text>')
    return card(W, H, "Contribution streak", "\n".join(out))


def svg_activity(d, today):
    cell, gap = 12, 3
    step = cell + gap
    # grid: columns are weeks (Sun-start), ending with the current week
    end_week_start = today - timedelta(days=(today.weekday() + 1) % 7)
    weeks = 53
    first = end_week_start - timedelta(weeks=weeks - 1)
    left, top = 40, 58
    W = left + weeks * step + 25
    H = top + 7 * step + 38
    vals = [d["days"].get((first + timedelta(days=i)).isoformat(), 0) for i in range(weeks * 7)]
    mx = max([v for v in vals if v] or [1])

    def level(v):
        if v <= 0:
            return 0
        r = v / mx
        return 1 if r <= 0.25 else 2 if r <= 0.5 else 3 if r <= 0.75 else 4

    year_total = sum(vals)
    out = [f'  <text x="25" y="35" class="t">Last 12 months: {year_total:,} contributions</text>']
    for i, name in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        out.append(f'  <text x="{left - 8}" y="{top + i * step + 10}" text-anchor="end" style="fill:{MUTED};font-size:10px">{name}</text>')
    last_month = None
    for w in range(weeks):
        wk = first + timedelta(weeks=w)
        if wk.month != last_month and wk <= today:
            out.append(f'  <text x="{left + w * step}" y="{top - 8}" style="fill:{MUTED};font-size:10px">{wk.strftime("%b")}</text>')
            last_month = wk.month
        for dow in range(7):
            day = wk + timedelta(days=dow)
            if day > today:
                continue
            v = d["days"].get(day.isoformat(), 0)
            out.append(f'  <rect x="{left + w * step}" y="{top + dow * step}" width="{cell}" height="{cell}" rx="2" fill="{HEAT[level(v)]}"><title>{day.isoformat()}: {v}</title></rect>')
    lx = W - 25 - 5 * step - 70
    ly = H - 22
    out.append(f'  <text x="{lx}" y="{ly + 10}" style="fill:{MUTED};font-size:10px">Less</text>')
    for i, c in enumerate(HEAT):
        out.append(f'  <rect x="{lx + 30 + i * step}" y="{ly}" width="{cell}" height="{cell}" rx="2" fill="{c}"/>')
    out.append(f'  <text x="{lx + 34 + 5 * step}" y="{ly + 10}" style="fill:{MUTED};font-size:10px">More</text>')
    return card(W, H, "Contribution activity", "\n".join(out))


# ---------------------------------------------------------------- main -----
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="use fake data (no token needed)")
    args = ap.parse_args()

    tz = parse_offset(os.environ.get("TZ_OFFSET", "+05:30"))
    out_dir = os.environ.get("OUT_DIR", "assets")
    os.makedirs(out_dir, exist_ok=True)

    if args.demo:
        data = demo_data(tz)
    else:
        token = os.environ.get("GH_TOKEN", "").strip()
        if not token:
            raise SystemExit("GH_TOKEN is empty. Add a repo secret named METRICS_TOKEN (classic PAT: repo + read:user).")
        data = fetch_all(token, tz)

    today = datetime.now(tz).date()
    files = {
        "stats.svg": svg_stats(data, today),
        "languages.svg": svg_langs(data),
        "streak.svg": svg_streak(data, today),
        "activity.svg": svg_activity(data, today),
    }
    for name, svg in files.items():
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
            f.write(svg)
        print("wrote", os.path.join(out_dir, name))

    total, first, cur, lng = compute_streaks(data["days"], today)
    print(f"total={total} current={cur[0]} ({cur[1]}..{cur[2]}) longest={lng[0]} ({lng[1]}..{lng[2]})")


if __name__ == "__main__":
    main()
