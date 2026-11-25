"""
Shared model record for The Spread Sheet (run by GitHub Actions after snapshot.py).

1. Opens the site in a hidden browser so the site's own model makes today's picks from data/snapshot.json.
2. Adds any new picks to data/model_log.json (the first capture of each pick is kept).
3. Updates each pending pick's latest line before tip-off (its "closing line").
4. Grades finished games. Older pending picks are graded from the proxy's game results;
   picks still unmatched a week after the game are marked "ungradeable" so they stop retrying.

The Record tab reads data/model_log.json, so every browser shows the same record.
"""

import json
import os
import sys
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(__file__))
from snapshot import get, load_json, name_score, parse_dt, save_json  # noqa: E402
from snapshot import find_final as find_final_bet, grade as grade_bet  # noqa: E402

SITE_DIR = os.environ.get("SITE_DIR", ".")
DATA_DIR = os.path.join(SITE_DIR, "data")
MODEL_VERSION = os.environ.get("MODEL_VERSION", "v6-lean")
PORT = 8765
FINAL = ("final", "status_final", "completed", "complete", "post")
PICK_FIELDS = ("id", "gameId", "pickType", "homeTeam", "awayTeam", "gameDate", "capturedAt", "vegasLine",
               "openingLine", "pickTeam", "pickSide", "pickLine", "pickDisplay", "confidence", "winProb",
               "rawWinProb", "edge", "isBestBet", "projectedLine")


def now():
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------
# 1. Let the site make its picks
# ------------------------------------------------------------------

class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def capture_picks():
    from playwright.sync_api import sync_playwright

    server = ThreadingHTTPServer(("127.0.0.1", PORT), partial(QuietHandler, directory=SITE_DIR))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    errors = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            # Only the local site loads; the page must use the snapshot, never live APIs.
            page.route("**/*", lambda r: r.continue_() if r.request.url.startswith(f"http://127.0.0.1:{PORT}") else r.abort())
            page.on("dialog", lambda d: d.dismiss())
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.add_init_script("localStorage.setItem('spreadsheet_last_backup', String(Date.now()));")
            page.goto(f"http://127.0.0.1:{PORT}/index.html")
            page.wait_for_function("window.__snapshotDone === true", timeout=180_000)
            if not page.evaluate("window.__snapshotLoaded === true"):
                raise RuntimeError("Site could not load data/snapshot.json")
            picks = page.evaluate("getAllPredictions()")
            top10 = page.evaluate("""(typeof ssTop10 === 'function' ? ssTop10() : []).map(t => ({
                id: t.o.id, gameId: t.o.gameId, homeTeam: t.o.homeTeam, awayTeam: t.o.awayTeam, startTime: t.o.startTime,
                type: t.o.type, side: t.o.side, line: t.o.line, label: t.o.label, entryPrice: t.o.polyPrice,
                entryWorth: t.o.fairProb, edge: t.o.edge, books: t.o.books, score: t.score, scoreLabel: t.label,
                rank: t.rank, best: !!t.best, model: t.model }))""")
            browser.close()
    finally:
        server.shutdown()
    if errors:
        print("Page errors:", errors[:5])
    return picks, top10


# ------------------------------------------------------------------
# 2-4. Log, closing lines, grading
# ------------------------------------------------------------------

def pick_book(game):
    books = game.get("bookmakers") or []
    for key in ("draftkings", "fanduel", "betmgm"):
        for b in books:
            if b.get("key") == key:
                return b
    return books[0] if books else None


def current_line(pick, odds):
    """Latest home spread or total for the pick's game, using the same book the site uses."""
    g = next((o for o in odds if o.get("id") == pick.get("gameId")), None)
    if not g:
        return None
    b = pick_book(g)
    for m in (b or {}).get("markets") or []:
        if pick["pickType"] == "spread" and m.get("key") == "spreads":
            o = next((x for x in m["outcomes"] if x.get("name") == g.get("home_team")), None)
            return o.get("point") if o else None
        if pick["pickType"] == "total" and m.get("key") == "totals":
            o = next((x for x in m["outcomes"] if x.get("name") == "Over"), None)
            return o.get("point") if o else None
    return None


def find_final(pick, games):
    start = parse_dt(pick["gameDate"])
    best, best_score = None, 0
    for g in games:
        if g.get("homePoints") is None or g.get("awayPoints") is None:
            continue
        if str(g.get("status", "")).lower() not in FINAL:
            continue
        gd = parse_dt(g.get("startDate"))
        if start and gd and abs((gd - start).total_seconds()) > 30 * 3600:
            continue
        straight = min(name_score(g["homeTeam"], pick["homeTeam"]), name_score(g["awayTeam"], pick["awayTeam"]))
        swapped = min(name_score(g["homeTeam"], pick["awayTeam"]), name_score(g["awayTeam"], pick["homeTeam"]))
        sc = max(straight, swapped)
        if sc > best_score:
            hp, ap = g["homePoints"], g["awayPoints"]
            best, best_score = ((hp, ap) if straight >= swapped else (ap, hp)), sc
    return best


def grade(pick, final):
    hp, ap = final
    line = pick.get("vegasLine")
    if line is None:
        return None
    if pick["pickType"] == "spread":
        margin = hp - ap
        cover = (margin + line) if pick["pickSide"] == "home" else (-margin - line)
        return "push" if cover == 0 else ("win" if cover > 0 else "loss")
    total = hp + ap
    if total == line:
        return "push"
    return "win" if (total > line) == (pick["pickSide"] == "over") else "loss"


def clv(pick):
    """Points the closing line moved in the pick's favor (same formula as the site)."""
    cl, vl = pick.get("closingLine"), pick.get("vegasLine")
    if cl is None or vl is None:
        return None
    if pick["pickType"] == "spread":
        return (vl - cl) if pick["pickSide"] == "home" else (cl - vl)
    return (cl - vl) if pick["pickSide"] == "over" else (vl - cl)


def season_of(dt):
    return dt.year + 1 if dt.month >= 10 else dt.year


def main():
    t = now()
    if 5 <= t.month <= 10 and os.environ.get("FORCE") != "1":
        print("Off-season: skipping.")
        return
    snap = load_json(os.path.join(DATA_DIR, "snapshot.json"), None)
    if not snap:
        sys.exit("No data/snapshot.json. Run snapshot.py first.")

    log_path = os.path.join(DATA_DIR, "model_log.json")
    log = load_json(log_path, {"picks": []})
    picks = {p["id"]: p for p in log.get("picks", [])}

    # 1-2. New picks
    added = 0
    picks_now, top10_now = capture_picks()
    for p in picks_now:
        if p.get("id") in picks or p.get("tracked"):
            continue
        st = parse_dt(p.get("gameDate"))
        if not st or st <= t:
            continue
        rec = {k: p.get(k) for k in PICK_FIELDS}
        rec.update(status="pending", result=None, homeScore=None, awayScore=None, modelVersion=MODEL_VERSION)
        picks[rec["id"]] = rec
        added += 1

    # 3. Closing lines (latest line seen before tip-off)
    for p in picks.values():
        st = parse_dt(p.get("gameDate"))
        if p.get("status") == "pending" and st and st > t:
            line = current_line(p, snap.get("odds") or [])
            if line is not None:
                p["closingLine"] = line

    # 4. Grading
    games = list(snap.get("games") or [])
    caught_up = set()  # seasons whose old results loaded successfully
    oldest_recent = t - timedelta(days=44)
    stale = [p for p in picks.values() if p.get("status") == "pending"
             and parse_dt(p["gameDate"]) and parse_dt(p["gameDate"]) < oldest_recent]
    if stale and os.environ.get("PROXY_URL"):
        # One-time catch-up for old picks: fetch those date ranges (cached by the proxy)
        by_season = {}
        for p in stale:
            d = parse_dt(p["gameDate"])
            s = season_of(d)
            lo, hi = by_season.get(s, (d, d))
            by_season[s] = (min(lo, d), max(hi, d))
        for s, (lo, hi) in by_season.items():
            try:
                games += get("/api/games", {"season": s, "startDate": (lo - timedelta(days=1)).date().isoformat(),
                                            "endDate": (hi + timedelta(days=1)).date().isoformat()})
                caught_up.add(s)
                print(f"Catch-up: loaded season {s} games {lo.date()} to {hi.date()}")
            except Exception as e:
                print(f"Catch-up for season {s} skipped: {e}")

    graded = 0
    for p in picks.values():
        if p.get("status") != "pending":
            continue
        st = parse_dt(p["gameDate"])
        if not st or st > t - timedelta(hours=3):
            continue
        final = find_final(p, games)
        if final:
            res = grade(p, final)
            if res:
                p.update(status="graded", result=res, homeScore=final[0], awayScore=final[1],
                         resolvedAt=t.isoformat(), clv=clv(p))
                graded += 1
        elif st < oldest_recent and season_of(st) in caught_up:
            # Results for that period loaded but no matching game: stop retrying
            p.update(status="ungradeable", result=None)

    ordered = sorted(picks.values(), key=lambda p: p.get("gameDate") or "", reverse=True)
    save_json(log_path, {"updatedAt": t.isoformat(), "picks": ordered,
                         "notes": log.get("notes", "")}, pretty=False)
    # Top 10 log: first time a bet makes the list is its entry; graded like value bets
    t_path = os.path.join(DATA_DIR, "top10_log.json")
    tlog = load_json(t_path, {"bets": []})
    tb = {b["id"]: b for b in tlog.get("bets", [])}
    for b in top10_now:
        st = parse_dt(b.get("startTime"))
        if b["id"] not in tb and st and st > t:
            tb[b["id"]] = {**b, "firstSeenAt": t.isoformat(), "result": None}
    t_graded = 0
    for b in tb.values():
        st = parse_dt(b.get("startTime"))
        if b.get("result") is None and st and st < t - timedelta(hours=3):
            final = find_final_bet(b, games)
            if final:
                b["result"], b["finalHome"], b["finalAway"] = grade_bet(b, final), final[0], final[1]
                t_graded += 1
    save_json(t_path, {"updatedAt": t.isoformat(), "bets": sorted(tb.values(), key=lambda b: b.get("startTime") or "", reverse=True)}, pretty=True)
    print(f"Top 10 log: {len(top10_now)} on today's list, {len(tb)} logged, {t_graded} graded")

    counts = {}
    for p in ordered:
        counts[p.get("status")] = counts.get(p.get("status"), 0) + 1
    print(f"Model log: +{added} new, {graded} graded, totals {counts}")


if __name__ == "__main__":
    main()
