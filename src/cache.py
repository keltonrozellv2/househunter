"""
cache.py — SQLite response cache with TTL + the RentCast monthly call budget.

Two jobs:
  1. Cache every API response keyed by (endpoint + sorted params) so nothing is
     ever requested twice inside its TTL. This is what makes the 50-call/month
     RentCast free tier workable.
  2. Count RentCast calls per calendar month, warn at the configured threshold,
     and HARD-STOP at the cap with a clear message (never silently overrun).
"""

from __future__ import annotations

import json
import hashlib
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

DB_PATH = Path(__file__).resolve().parent.parent / "cache.db"


class BudgetExhausted(RuntimeError):
    """Raised when a RentCast call would exceed the monthly free-tier cap."""


def _connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS api_cache (
               key TEXT PRIMARY KEY,
               endpoint TEXT,
               payload TEXT,
               fetched_at REAL
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS api_calls (
               api TEXT,
               month TEXT,
               called_at REAL
           )"""
    )
    return conn


def cache_key(endpoint: str, params: dict) -> str:
    canonical = endpoint + "?" + json.dumps(params, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


class Cache:
    def __init__(self, db_path: Path = DB_PATH):
        self.conn = _connect(db_path)

    # ---------------- response cache ----------------

    def get(self, endpoint: str, params: dict, ttl_days: float) -> Optional[Any]:
        key = cache_key(endpoint, params)
        row = self.conn.execute(
            "SELECT payload, fetched_at FROM api_cache WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        payload, fetched_at = row
        if time.time() - fetched_at > ttl_days * 86400:
            return None  # stale; caller refetches (old row is overwritten on set)
        return json.loads(payload)

    def set(self, endpoint: str, params: dict, payload: Any) -> None:
        key = cache_key(endpoint, params)
        self.conn.execute(
            "INSERT OR REPLACE INTO api_cache (key, endpoint, payload, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            (key, endpoint, json.dumps(payload, default=str), time.time()),
        )
        self.conn.commit()

    # ---------------- call budget ----------------

    @staticmethod
    def _month() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def calls_this_month(self, api: str = "rentcast") -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM api_calls WHERE api = ? AND month = ?",
            (api, self._month()),
        ).fetchone()
        return row[0]

    def record_call(self, api: str = "rentcast") -> int:
        """Record one billable call; returns the new month-to-date total."""
        self.conn.execute(
            "INSERT INTO api_calls (api, month, called_at) VALUES (?, ?, ?)",
            (api, self._month(), time.time()),
        )
        self.conn.commit()
        return self.calls_this_month(api)

    def check_budget(self, cap: int, warn_at: int, api: str = "rentcast",
                     about_to_spend: int = 1) -> None:
        """Call BEFORE a billable request. Raises BudgetExhausted at the cap."""
        used = self.calls_this_month(api)
        if used + about_to_spend > cap:
            raise BudgetExhausted(
                f"{api} monthly budget exhausted: {used}/{cap} calls used this "
                f"month. Running from cache only until the month rolls over. "
                f"(Do NOT create extra accounts — see config.yaml.)"
            )
        if used + about_to_spend > warn_at:
            print(f"  ⚠ {api} budget warning: {used + about_to_spend}/{cap} "
                  f"calls after this request.")

    def close(self) -> None:
        self.conn.close()
