"""
Scheduled refresh for The Spread Sheet (run by GitHub Actions).

Each run:
  1. Pulls fresh data from the proxy (ratings, odds, Polymarket, value, recent games).
  2. Writes data/snapshot.json, which the site loads automatically (visitors use no API calls).
  3. Updates data/value_log.json: every value bet the proxy flags, its first price, its last price
     before tip-off (the "close"), and the graded result. This is the unbiased record of the value finder.

API cost per run: 1 Odds API call (3 credits) and about 2-3 CBBD calls. Polymarket is free.
Only the standard library is used, so no installs are needed.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

PROXY = os.environ.get("PROXY_URL", "").rstrip("/")
SITE_DIR = os.environ.get("SITE_DIR", ".")
DATA_DIR = os.path.join(SITE_DIR, "data")
MIN_EDGE = float(os.environ.get("MIN_EDGE", "0.03"))
KEEP_BOOKS = ("draftkings", "fanduel", "betmgm", "williamhill_us", "betrivers", "bovada")


def now():
    return datetime.now(timezone.utc)


def get(path, params=None, timeout=90, tries=3):
    url = f"{PROXY}{path}" + (("?" + urllib.parse.urlencode(params)) if params else "")
    last = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "spreadsheet-refresh"}), timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="ignore")[:300]
            if e.code in (400, 401, 403, 429):
                raise RuntimeError(f"{path}: HTTP {e.code} {body}")
            last = f"HTTP {e.code} {body}"
        except Exception as e:  # timeouts while Render wakes up
            last = str(e)
        time.sleep(15 * (i + 1))
    raise RuntimeError(f"{path}: failed after {tries} tries: {last}")


def parse_dt(raw):
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def norm(name):
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower().replace("state", "st"))


def name_score(a, b):
    x, y = norm(a), norm(b)
    if not x or not y:
        return 0
    if x == y:
        return 100
    short, long_ = (x, y) if len(x) <= len(y) else (y, x)
    return 50 + len(short) if long_.startswith(short) and len(short) >= 4 else 0


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj, pretty=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=1 if pretty else None, separators=None if pretty else (",", ":"))


# ------------------------------------------------------------------

def trim_odds(odds):
    """Keep only the books the site uses so the file stays small."""
    out = []
    for g in odds or []:
        books = [b for b in g.get("bookmakers") or [] if b.get("key") in KEEP_BOOKS] or (g.get("bookmakers") or [])[:1]
        out.append({**g, "bookmakers": books})
    return out


def trim_teams(teams):
    """Names, abbreviations, colors and logos for the team badges (whatever the API provides)."""
    keep = ("school", "team", "displayName", "shortDisplayName", "abbreviation", "mascot",
            "primaryColor", "secondaryColor", "color", "alternateColor", "logo", "logos")
    return [{k: t.get(k) for k in keep if t.get(k) not in (None, "", [])} for t in teams or [] if isinstance(t, dict)]


def trim_games(games):
    keep = ("id", "homeTeam", "awayTeam", "homePoints", "awayPoints", "status", "startDate", "neutralSite")
    return [{k: g.get(k) for k in keep} for g in games or []]


def find_final(bet, games):
    start = parse_dt(bet["startTime"])
    best, best_score = None, 0
    for g in games:
        if g.get("homePoints") is None or g.get("awayPoints") is None:
            continue
        if str(g.get("status", "")).lower() not in ("final", "status_final", "completed", "complete", "post"):
            continue
        gd = parse_dt(g.get("startDate"))
        if start and gd and abs((gd - start).total_seconds()) > 30 * 3600:
            continue
        straight = min(name_score(g["homeTeam"], bet["homeTeam"]), name_score(g["awayTeam"], bet["awayTeam"]))
        swapped = min(name_score(g["homeTeam"], bet["awayTeam"]), name_score(g["awayTeam"], bet["homeTeam"]))
        sc = max(straight, swapped)
        if sc > best_score:
            hp, ap = g["homePoints"], g["awayPoints"]
            best, best_score = ((hp, ap) if straight >= swapped else (ap, hp)), sc
    return best  # (home points, away points) from the odds feed's home/away view


def grade(bet, final):
    hp, ap = final
    if bet["type"] == "total":
        tot = hp + ap
        if tot == bet["line"]:
            return "push"
        return "win" if (tot > bet["line"]) == (bet["side"] == "over") else "loss"
    is_home = bet["side"] == bet["homeTeam"]
    diff = (hp - ap if is_home else ap - hp) + (bet["line"] if bet["type"] == "spread" else 0)
    return "push" if diff == 0 else ("win" if diff > 0 else "loss")


def summarize(bets):
    graded = [b for b in bets if b.get("result") in ("win", "loss")]
    n = len(graded)
    wins = sum(1 for b in graded if b["result"] == "win")
    profit = sum((1 / b["entryPrice"] - 1) if b["result"] == "win" else -1 for b in graded)
    with_close = [b for b in bets if b.get("closeFair") is not None]
    clv = [b["closeFair"] - b["entryPrice"] for b in with_close]
    return {
        "flagged": len(bets), "graded": n, "wins": wins, "losses": n - wins,
        "profitUnits": round(profit, 2), "roiPct": round(100 * profit / n, 1) if n else None,
        "avgEntryEdgePct": round(100 * sum(b["entryEdge"] for b in bets) / len(bets), 2) if bets else None,
        "avgEdgeVsClosePct": round(100 * sum(clv) / len(clv), 2) if clv else None,
        "beatClosePct": round(100 * sum(1 for x in clv if x > 0) / len(clv), 1) if clv else None,
        "note": "Judge after 100+ graded bets. Beating the close on average is the earliest sign of a real edge.",
    }


def main():
    if not PROXY:
        sys.exit("Set PROXY_URL (repo variable) to your Render proxy address.")
    t = now()
    if 5 <= t.month <= 10 and os.environ.get("FORCE") != "1":
        print("Off-season (May-Oct): skipping to save API credits.")
        return
    print(f"Run at {t.isoformat()}  proxy={PROXY}")
    get("/health", timeout=120)  # wakes the free Render server

    ratings = get("/api/ratings")
    odds = get("/api/odds")
    poly = get("/api/polymarket")
    value_all = get("/api/value", {"minEdge": "-1"})   # every comparison, used for closing prices
    start = (t - timedelta(days=45)).date().isoformat()
    games = get("/api/games", {"startDate": start, "endDate": t.date().isoformat()})
    try:
        teams = trim_teams(get("/api/teams"))
    except Exception as e:  # logos/colors are optional
        print(f"Teams skipped: {e}")
        teams = []
    print(f"ratings={len(ratings)} odds={len(odds)} poly={len(poly)} compared={value_all.get('compared')} games={len(games)}")

    comps = value_all.get("opportunities", [])
    flagged = [o for o in comps if o["edge"] >= MIN_EDGE]

    # ---- Value log ----
    log_path = os.path.join(DATA_DIR, "value_log.json")
    log = load_json(log_path, {"bets": []})
    bets = {b["id"]: b for b in log.get("bets", [])}
    by_id = {o["id"]: o for o in comps}
    stamp = t.isoformat()

    for o in flagged:
        if o["id"] not in bets and parse_dt(o["startTime"]) and parse_dt(o["startTime"]) > t:
            bets[o["id"]] = {**{k: o[k] for k in ("id", "gameId", "homeTeam", "awayTeam", "startTime", "type",
                                                  "side", "line", "label", "tradeUrl")},
                             "firstSeenAt": stamp, "entryPrice": o["polyPrice"], "entryFair": o["fairProb"],
                             "entryEdge": o["edge"], "books": o["books"], "closeFair": None, "closePrice": None,
                             "closeAt": None, "result": None}
    for b in bets.values():
        st = parse_dt(b["startTime"])
        o = by_id.get(b["id"])
        if o and st and st > t and b.get("result") is None:
            b["closeFair"], b["closePrice"], b["closeAt"] = o["fairProb"], o["polyPrice"], stamp
        if b.get("result") is None and st and st < t - timedelta(hours=3):
            final = find_final(b, games)
            if final:
                b["result"], b["finalHome"], b["finalAway"] = grade(b, final), final[0], final[1]
                b["gradedAt"] = stamp

    all_bets = sorted(bets.values(), key=lambda b: b["startTime"], reverse=True)
    save_json(log_path, {"updatedAt": stamp, "minEdge": MIN_EDGE, "summary": summarize(all_bets), "bets": all_bets}, pretty=True)

    # ---- Snapshot for the site ----
    upcoming = [o for o in flagged if parse_dt(o["startTime"]) and parse_dt(o["startTime"]) > t]
    snapshot = {
        "updatedAt": stamp,
        "ratings": ratings,
        "odds": trim_odds(odds),
        "polymarket": poly,
        "value": {"minEdge": MIN_EDGE, "compared": value_all.get("compared", 0), "opportunities": upcoming},
        "games": trim_games(games),
        "teams": teams,
    }
    save_json(os.path.join(DATA_DIR, "snapshot.json"), snapshot)
    s = summarize(all_bets)
    print(f"Value log: {s['flagged']} flagged, {s['graded']} graded, ROI {s['roiPct']}, beat close {s['beatClosePct']}%")


if __name__ == "__main__":
    main()
