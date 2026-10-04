"""Persistent download history + stats for PEAK (SQLite, local only)."""
import os
import sqlite3
import time

DB = os.path.join(os.path.expanduser("~"), ".peak_history.db")


def _con():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS dl(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER, title TEXT, artist TEXT, kind TEXT,
        source TEXT, status TEXT, bytes INTEGER)""")
    return c


def record(title, artist="", kind="audio", source="", status="ok", b=0):
    try:
        with _con() as c:
            c.execute(
                "INSERT INTO dl(ts,title,artist,kind,source,status,bytes) "
                "VALUES(?,?,?,?,?,?,?)",
                (int(time.time()), title[:200], artist[:200], kind,
                 source[:40], status, int(b or 0)))
    except Exception:
        pass


def stats():
    try:
        with _con() as c:
            total = c.execute("SELECT count(*) FROM dl WHERE status='ok'"
                              ).fetchone()[0]
            fails = c.execute("SELECT count(*) FROM dl WHERE status!='ok'"
                              ).fetchone()[0]
            bytesum = c.execute("SELECT sum(bytes) FROM dl").fetchone()[0] or 0
            top = c.execute(
                "SELECT artist, count(*) n FROM dl WHERE status='ok' "
                "AND artist != '' GROUP BY artist ORDER BY n DESC LIMIT 5"
            ).fetchall()
            days = c.execute(
                "SELECT date(ts,'unixepoch') d, count(*) FROM dl "
                "GROUP BY d ORDER BY d DESC LIMIT 14").fetchall()
        return {"total": total, "fails": fails, "bytes": bytesum,
                "top": top, "days": days}
    except Exception:
        return {"total": 0, "fails": 0, "bytes": 0, "top": [], "days": []}


def recent(n=50):
    try:
        with _con() as c:
            return c.execute(
                "SELECT ts, title, artist, source, status FROM dl "
                "ORDER BY ts DESC LIMIT ?", (n,)).fetchall()
    except Exception:
        return []


def daily_counts(days=14):
    """[(date_str, ok_count, err_count)] for the last N days."""
    con = _con()
    try:
        rows = con.execute(
            "SELECT date(ts,'unixepoch') d, "
            "SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN status!='ok' THEN 1 ELSE 0 END) "
            "FROM dl WHERE ts >= strftime('%s','now',?) "
            "GROUP BY d ORDER BY d",
            (f"-{int(days)} days",)).fetchall()
        return [(r[0], r[1] or 0, r[2] or 0) for r in rows]
    finally:
        con.close()


def top_artists(n=5):
    """[(artist, count)] most-downloaded artists."""
    con = _con()
    try:
        rows = con.execute(
            "SELECT artist, COUNT(*) c FROM dl "
            "WHERE status='ok' AND artist != '' "
            "GROUP BY artist ORDER BY c DESC LIMIT ?", (n,)).fetchall()
        return [(r[0], r[1]) for r in rows]
    finally:
        con.close()


def daily_ok(days=30):
    """Cumulative-friendly: [(date_str, ok_count)] per day, oldest first."""
    con = _con()
    try:
        rows = con.execute(
            "SELECT date(ts,'unixepoch') d, "
            "SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) "
            "FROM dl WHERE ts >= strftime('%s','now',?) "
            "GROUP BY d ORDER BY d",
            (f"-{int(days)} days",)).fetchall()
        return [(r[0], r[1] or 0) for r in rows]
    finally:
        con.close()


def all_rows(limit=100000):
    """Full history as list of {ts,title,artist,kind,source,status,bytes}."""
    con = _con()
    try:
        rows = con.execute(
            "SELECT ts,title,artist,kind,source,status,bytes "
            "FROM dl ORDER BY ts DESC LIMIT ?", (int(limit),)).fetchall()
        return [{"ts": r[0], "title": r[1], "artist": r[2], "kind": r[3],
                 "source": r[4], "status": r[5], "bytes": r[6]} for r in rows]
    finally:
        con.close()
