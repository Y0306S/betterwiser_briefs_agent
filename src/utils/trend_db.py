"""
Entity trend database — persistent JSON store tracking how often each named
entity appears in the briefings, month by month.

This enables quantitative trend annotations like "Third consecutive month" or
"First appearance since October 2025" rather than vague "recurring" labels.

Storage: runs_dir/trend_db.json (one file, grows over time, ~10–50 KB/year).
Format:
  {
    "entities": {
      "harvey ai": {
        "2026-01": 3,
        "2026-02": 5,
        "2026-03": 2
      },
      ...
    },
    "last_updated": "2026-03-01T08:00:00Z"
  }

Public API:
  load(runs_dir)                         → TrendDB
  db.record(month, entity, count=1)      → None
  db.consecutive_months(entity, month)   → int   (how many months in a row)
  db.first_seen(entity)                  → str | None  ("YYYY-MM")
  db.annotation(entity, month)           → str | None  (ready-to-use phrase)
  db.save(runs_dir)                      → None
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DB_FILENAME = "trend_db.json"


class TrendDB:
    """Mutable in-memory entity trend database with load/save."""

    def __init__(self, data: dict) -> None:
        # entities: { entity_key: { "YYYY-MM": mention_count, ... } }
        self._entities: dict[str, dict[str, int]] = data.get("entities", {})

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def record(self, month: str, entity: str, count: int = 1) -> None:
        """Record `count` mentions of `entity` in `month`."""
        key = _normalise(entity)
        if not key:
            return
        if key not in self._entities:
            self._entities[key] = {}
        self._entities[key][month] = self._entities[key].get(month, 0) + count

    def record_all(self, month: str, entities: list[str]) -> None:
        """Record one mention each for a list of entity strings."""
        for entity in entities:
            self.record(month, entity)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def consecutive_months(self, entity: str, through_month: str) -> int:
        """
        Count how many consecutive months ending at `through_month` the entity
        has appeared in.  Returns 0 if the entity was not seen in `through_month`.
        """
        key = _normalise(entity)
        months_data = self._entities.get(key, {})

        if months_data.get(through_month, 0) == 0:
            return 0

        count = 1
        cursor = _prev_month(through_month)
        while cursor and months_data.get(cursor, 0) > 0:
            count += 1
            cursor = _prev_month(cursor)

        return count

    def first_seen(self, entity: str) -> Optional[str]:
        """Return the earliest month the entity was recorded, or None."""
        key = _normalise(entity)
        months = [m for m, c in self._entities.get(key, {}).items() if c > 0]
        return min(months) if months else None

    def total_mentions(self, entity: str) -> int:
        """Return the total number of recorded mentions across all months."""
        key = _normalise(entity)
        return sum(self._entities.get(key, {}).values())

    def annotation(self, entity: str, month: str) -> Optional[str]:
        """
        Return a ready-to-use trend phrase for this entity in this month.

        Examples:
          "Third consecutive month"
          "First appearance since January 2026"
          "Recurring (6 mentions across 3 months)"
          None  (if first appearance with no prior history)
        """
        key = _normalise(entity)
        months_data = self._entities.get(key, {})

        if months_data.get(month, 0) == 0:
            return None

        consecutive = self.consecutive_months(entity, month)

        if consecutive == 1:
            first = self.first_seen(entity)
            if first and first < month:
                # Was seen before but not last month
                first_human = _month_human(first)
                return f"First appearance since {first_human}"
            return None  # True first appearance — no annotation needed

        if consecutive == 2:
            return "Second consecutive month"

        if consecutive >= 3:
            ordinal = _ordinal(consecutive)
            return f"{ordinal} consecutive month"

        return None

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, runs_dir: str) -> None:
        """Write the database to disk."""
        path = Path(runs_dir) / _DB_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "entities": self._entities,
            "last_updated": datetime.now(tz=timezone.utc).isoformat(),
        }
        try:
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            logger.debug(f"Trend DB saved to {path} ({len(self._entities)} entities)")
        except OSError as e:
            logger.warning(f"Trend DB: failed to save to {path}: {e}")


def load(runs_dir: str) -> TrendDB:
    """
    Load the trend database from disk.  Returns an empty TrendDB if the file
    doesn't exist yet (first-ever run).
    """
    path = Path(runs_dir) / _DB_FILENAME
    if not path.exists():
        logger.debug(f"Trend DB: no existing database at {path} — starting fresh")
        return TrendDB({})
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        db = TrendDB(data)
        logger.debug(
            f"Trend DB: loaded {len(db._entities)} entities from {path}"
        )
        return db
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Trend DB: failed to load {path}: {e} — starting fresh")
        return TrendDB({})


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _normalise(entity: str) -> str:
    """Lowercase, strip, and collapse whitespace for consistent keying."""
    import re
    return re.sub(r"\s+", " ", entity.lower().strip())


def _prev_month(month: str) -> Optional[str]:
    """Return the month before `month` in "YYYY-MM" format, or None on error."""
    try:
        year, mon = int(month[:4]), int(month[5:7])
        if mon == 1:
            return f"{year - 1}-12"
        return f"{year}-{mon - 1:02d}"
    except (ValueError, IndexError):
        return None


def _month_human(month: str) -> str:
    """Convert "YYYY-MM" to "Month YYYY" (e.g. "January 2026")."""
    try:
        from datetime import datetime as _dt
        return _dt.strptime(month, "%Y-%m").strftime("%B %Y")
    except ValueError:
        return month


def _ordinal(n: int) -> str:
    """Return English ordinal string: 1→"First", 2→"Second", …, 4→"4th", etc."""
    named = {1: "First", 2: "Second", 3: "Third"}
    if n in named:
        return named[n]
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10 if n % 100 not in (11, 12, 13) else 0, "th")
    return f"{n}{suffix}"
