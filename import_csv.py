#!/usr/bin/env python3
"""
import_csv.py - Import unique_rules.csv (Jenny Ma's vibecoding rules) into the rules DB.

The CSV has two columns:
  rule_text  - short rule title / imperative
  content    - longer elaboration (may be empty)

Each row is already an individual rule, so we insert directly into
processed_rules (skipping the LLM splitting step). A synthetic source
row is written to the rules table for provenance.

Usage:
  python3 import_csv.py [--db rules.db] [--csv unique_rules.csv]
"""

import argparse
import csv
import hashlib
import os
import sys
from datetime import datetime, timezone

import duckdb

SOURCE_LABEL = "jenny_ma_vibecoding"
SOURCE_URL = "local:unique_rules.csv"


def content_id(url: str, content: str) -> str:
    return hashlib.sha256(f"{url}:{content}".encode()).hexdigest()[:16]


def rule_id(source_id: str, index: int, rule_text: str) -> str:
    return hashlib.sha256(f"{source_id}:{index}:{rule_text}".encode()).hexdigest()[:16]


def init_tables(con: duckdb.DuckDBPyConnection):
    con.execute("""
        CREATE TABLE IF NOT EXISTS rules (
            id          VARCHAR PRIMARY KEY,
            source_url  VARCHAR NOT NULL,
            raw_url     VARCHAR,
            file_type   VARCHAR,
            repo_name   VARCHAR,
            repo_stars  INTEGER,
            content     TEXT,
            content_len INTEGER,
            crawled_at  TIMESTAMP,
            source      VARCHAR
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS processed_rules (
            id                    VARCHAR PRIMARY KEY,
            source_id             VARCHAR NOT NULL,
            source_url            VARCHAR,
            repo_name             VARCHAR,
            rule_text             TEXT NOT NULL,
            rule_index            INTEGER,
            enforcement_category  VARCHAR,
            enforcement_mechanism VARCHAR,
            enforcement_notes     TEXT,
            processed_at          TIMESTAMP
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS process_log (
            source_id    VARCHAR PRIMARY KEY,
            rules_found  INTEGER,
            status       VARCHAR,
            error        VARCHAR,
            processed_at TIMESTAMP
        )
    """)


def main():
    parser = argparse.ArgumentParser(description="Import unique_rules.csv into rules DB")
    parser.add_argument("--db", default="rules.db")
    parser.add_argument("--csv", default="unique_rules.csv")
    args = parser.parse_args()

    csv_path = os.path.join(os.path.dirname(__file__), args.csv)
    if not os.path.exists(csv_path):
        print(f"ERROR: {csv_path} not found", file=sys.stderr)
        sys.exit(1)

    # Read CSV
    rows = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rule_text = (row.get("rule_text") or "").strip()
            content = (row.get("content") or "").strip()
            if not rule_text:
                continue
            rows.append((rule_text, content))

    print(f"Read {len(rows)} rows from {args.csv}")

    con = duckdb.connect(args.db)
    init_tables(con)

    now = datetime.now(timezone.utc)

    # Build a combined text blob for the synthetic source row in `rules`
    full_content = "\n\n".join(
        f"{rt}\n{ct}" if ct else rt for rt, ct in rows
    )
    sid = content_id(SOURCE_URL, full_content)

    con.execute("""
        INSERT OR REPLACE INTO rules VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [sid, SOURCE_URL, None, "csv", SOURCE_LABEL, None,
          full_content, len(full_content), now, SOURCE_LABEL])

    # Insert each rule directly into processed_rules (already individual rules)
    inserted = 0
    skipped = 0
    for i, (rule_text, content) in enumerate(rows):
        # Combine title + elaboration into a single self-contained rule string
        combined = rule_text
        if content and content.lower() != rule_text.lower():
            combined = f"{rule_text}\n{content}"

        rid = rule_id(sid, i, rule_text)

        existing = con.execute(
            "SELECT 1 FROM processed_rules WHERE id = ?", [rid]
        ).fetchone()
        if existing:
            skipped += 1
            continue

        con.execute("""
            INSERT INTO processed_rules VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [rid, sid, SOURCE_URL, SOURCE_LABEL, combined, i,
              None, None, None, now])
        inserted += 1

    # Mark source as processed in process_log
    con.execute("""
        INSERT OR REPLACE INTO process_log VALUES (?, ?, ?, ?, ?)
    """, [sid, inserted, "ok", None, now])

    print(f"Inserted {inserted} rules, skipped {skipped} duplicates")
    total = con.execute("SELECT COUNT(*) FROM processed_rules").fetchone()[0]
    print(f"Total processed_rules rows: {total}")
    con.close()


if __name__ == "__main__":
    main()
