#!/usr/bin/env python3
"""
process.py - Split raw rule files into individual rules and categorize them.

Reads from all source databases (rules.db, rules2.db, rules3.db) and writes
into a single separate output database (processed.db).

Schema in processed.db:
  processed_rules  - one row per extracted rule
  process_log      - tracks which (source_db, source_id) pairs are done

The (source_db, source_id) pair acts as a logical foreign key back to the
rules table in each source database. DuckDB doesn't enforce cross-file FKs
but the relationship is preserved and queryable via ATTACH.

Categories:
  deterministic - enforceable by a tool (linter, hook, regex, type-checker…)
  llm           - requires LLM judgment (tone, intent, quality, context)
  mixed         - partly deterministic, partly LLM

Usage:
  export OPENAI_API_KEY=sk-...
  python3 process.py [--out processed.db] [--limit 100] [--model gpt-4o-mini]
  python3 process.py --source rules3.db          # process only one source
  python3 process.py --reprocess                 # ignore previous run log
"""

import argparse
import hashlib
import json
import os
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import openai
import duckdb

# Load .env if present
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    for _line in open(_env_path):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SOURCE_DBS = {
    "rules":  os.path.join(BASE_DIR, "rules.db"),
    "rules2": os.path.join(BASE_DIR, "rules2.db"),
    "rules3": os.path.join(BASE_DIR, "rules3.db"),
}
DEFAULT_OUT_DB = os.path.join(BASE_DIR, "processed.db")
DEFAULT_MODEL  = "gpt-4o-mini"

MAX_CONTENT_CHARS = 12_000
MIN_CONTENT_CHARS = 30

SYSTEM_PROMPT = """\
You are a precise technical analyst. You will be given the raw text of a file \
that contains instructions or rules for an AI coding agent. Your job is to:

1. Split the content into individual, self-contained rules. A "rule" is a single \
actionable instruction. Discard non-rule content (headings that are just labels, \
introductory prose, examples, explanations with no imperative).

2. For each rule, assign an enforcement_category:
   - "deterministic": Can be checked or enforced WITHOUT an LLM. A script, linter, \
formatter, regex, type-checker, git hook, CI gate, filesystem check, or database \
constraint could evaluate it reliably. Examples: "use 2-space indentation", \
"never commit .env files", "all functions must have type annotations", \
"filenames must be kebab-case".
   - "llm": Can ONLY be evaluated by an LLM because it requires understanding intent, \
tone, context, or quality. Examples: "write clear commit messages", \
"prefer simple solutions", "be concise", "explain your reasoning".
   - "mixed": Has both a deterministic component AND an LLM component. Example: \
"write tests for all new functions" (deterministic: does a test file exist? \
LLM: are the tests meaningful?).

3. For "deterministic" and "mixed" rules, set enforcement_mechanism to a short label \
for the tool class that would enforce it: one of: linter, formatter, type-checker, \
git-hook, ci-gate, gitignore, regex, filesystem, db-constraint, package-manager, \
shell-script, or other.

4. Set enforcement_notes to a single sentence explaining your reasoning.

Return ONLY a JSON array. Each element:
{
  "rule_text": "<the rule, verbatim or lightly cleaned>",
  "category": "deterministic" | "llm" | "mixed",
  "mechanism": "<tool class, or null if category is llm>",
  "notes": "<one sentence>"
}

If the file contains no extractable rules (it is documentation, an example, a README \
index, etc.), return an empty array: []
"""

# ---------------------------------------------------------------------------
# Output database schema
# ---------------------------------------------------------------------------

def init_out_db(con: duckdb.DuckDBPyConnection):
    con.execute("""
        CREATE TABLE IF NOT EXISTS processed_rules (
            id                    VARCHAR PRIMARY KEY,
            -- logical FK: (source_db, source_id) -> <source_db>.rules.id
            source_db             VARCHAR NOT NULL,
            source_id             VARCHAR NOT NULL,
            source_url            VARCHAR,
            repo_name             VARCHAR,
            rule_source           VARCHAR,   -- crawler origin: github_search, gist, awesome_list, etc.
            rule_text             TEXT    NOT NULL,
            rule_index            INTEGER,
            enforcement_category  VARCHAR,
            enforcement_mechanism VARCHAR,
            enforcement_notes     TEXT,
            processed_at          TIMESTAMP
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS process_log (
            source_db    VARCHAR NOT NULL,
            source_id    VARCHAR NOT NULL,
            rules_found  INTEGER,
            status       VARCHAR,    -- 'ok', 'error', 'empty'
            error        VARCHAR,
            processed_at TIMESTAMP,
            PRIMARY KEY (source_db, source_id)
        )
    """)


def already_processed(out_con, source_db: str, source_id: str) -> bool:
    return out_con.execute(
        "SELECT 1 FROM process_log WHERE source_db = ? AND source_id = ?",
        [source_db, source_id]
    ).fetchone() is not None


def log_processed(out_con, source_db: str, source_id: str,
                  rules_found: int, status: str, error: str = None):
    out_con.execute("""
        INSERT OR REPLACE INTO process_log VALUES (?, ?, ?, ?, ?, ?)
    """, [source_db, source_id, rules_found, status, error, datetime.now(timezone.utc)])


def save_rules(out_con, source_db: str, source_id: str,
               source_url: str, repo_name: str, rule_source: str, rules: list):
    for i, rule in enumerate(rules):
        rule_text = rule.get("rule_text", "").strip()
        if not rule_text:
            continue
        rid = hashlib.sha256(
            f"{source_db}:{source_id}:{i}:{rule_text}".encode()
        ).hexdigest()[:16]
        out_con.execute("""
            INSERT OR REPLACE INTO processed_rules VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            rid,
            source_db,
            source_id,
            source_url,
            repo_name,
            rule_source,
            rule_text,
            i,
            rule.get("category"),
            rule.get("mechanism"),
            rule.get("notes"),
            datetime.now(timezone.utc),
        ])


# ---------------------------------------------------------------------------
# LLM extraction
# ---------------------------------------------------------------------------

def extract_rules(client: openai.OpenAI, content: str, model: str) -> list:
    truncated = content[:MAX_CONTENT_CHARS]
    if len(content) > MAX_CONTENT_CHARS:
        truncated += "\n\n[...truncated...]"

    response = client.chat.completions.create(
        model=model,
        max_tokens=4096,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": truncated},
        ],
    )

    raw = response.choices[0].message.content.strip()

    # Strip markdown code fences if the model wrapped the JSON
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.rsplit("```", 1)[0].strip()

    return json.loads(raw)


# ---------------------------------------------------------------------------
# Process one source database
# ---------------------------------------------------------------------------

def process_source(db_key: str, db_path: str, out_con: duckdb.DuckDBPyConnection,
                   client: openai.OpenAI, model: str,
                   limit: int, reprocess: bool, workers: int) -> dict:
    if not os.path.exists(db_path):
        print(f"  [{db_key}] not found, skipping")
        return {"ok": 0, "err": 0, "skipped": 0, "empty": 0}

    try:
        src_con = duckdb.connect(db_path, read_only=True)
    except Exception as e:
        print(f"  [{db_key}] cannot open: {e}")
        return {"ok": 0, "err": 0, "skipped": 0, "empty": 0}

    query = "SELECT id, source_url, repo_name, content, source FROM rules ORDER BY content_len DESC NULLS LAST"
    if limit:
        query += f" LIMIT {limit}"

    try:
        rows = src_con.execute(query).fetchall()
    except Exception as e:
        print(f"  [{db_key}] query failed: {e}")
        src_con.close()
        return {"ok": 0, "err": 0, "skipped": 0, "empty": 0}

    src_con.close()

    # Pre-filter: check what needs processing (single-threaded, DB access)
    todo = []
    skipped = empty = 0

    for source_id, source_url, repo_name, content, rule_source in rows:
        if not reprocess and already_processed(out_con, db_key, source_id):
            skipped += 1
            continue
        if not content or len(content.strip()) < MIN_CONTENT_CHARS:
            log_processed(out_con, db_key, source_id, 0, "empty")
            empty += 1
            continue
        todo.append((source_id, source_url, repo_name, content, rule_source))

    n = len(todo)
    print(f"\n  [{db_key}] {len(rows)} rows — {n} to process, {skipped} skipped, {empty} empty")

    ok = err = 0
    rate_limit_event = threading.Event()

    # LLM call only — no DB access in this function
    def call_llm(args):
        source_id, source_url, repo_name, content, rule_source = args
        while rate_limit_event.is_set():
            time.sleep(1)
        try:
            rules = extract_rules(client, content, model)
            return ("ok", source_id, source_url, repo_name, rule_source, rules, None)
        except json.JSONDecodeError as e:
            return ("err", source_id, source_url, repo_name, rule_source, [], f"JSON: {e}")
        except openai.RateLimitError:
            print(f"  [{db_key}] rate limit — pausing all threads 60s…")
            rate_limit_event.set()
            time.sleep(60)
            rate_limit_event.clear()
            return ("retry", source_id, source_url, repo_name, rule_source, [], None)
        except Exception as e:
            return ("err", source_id, source_url, repo_name, rule_source, [], str(e)[:200])

    # Dispatch LLM calls in parallel; write results to DB sequentially in main thread
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(call_llm, row): row for row in todo}
        for i, fut in enumerate(as_completed(futures), 1):
            status, source_id, source_url, repo_name, rule_source, rules, error = fut.result()
            label = (source_url or source_id)[:60]
            if status == "ok":
                save_rules(out_con, db_key, source_id, source_url, repo_name, rule_source, rules)
                log_processed(out_con, db_key, source_id, len(rules), "ok")
                print(f"  [{db_key}] {i}/{n} {label} → {len(rules)} rules")
                ok += 1
            elif status == "err":
                log_processed(out_con, db_key, source_id, 0, "error", error)
                print(f"  [{db_key}] {i}/{n} {label} → ERROR: {error}")
                err += 1
            # "retry": don't log, row will be retried on next run

    return {"ok": ok, "err": err, "skipped": skipped, "empty": empty}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Extract and categorize individual rules from all source DBs"
    )
    parser.add_argument("--out", default=DEFAULT_OUT_DB,
                        help=f"Output DuckDB file (default: {DEFAULT_OUT_DB})")
    parser.add_argument("--source", metavar="DB_PATH",
                        help="Process only this source DB file (overrides default set)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Max rows to process per source DB (0 = all)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Anthropic model ID")
    parser.add_argument("--reprocess", action="store_true",
                        help="Ignore previous run log and reprocess everything")
    parser.add_argument("--workers", type=int, default=10,
                        help="Parallel LLM calls (default: 10)")
    args = parser.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("ERROR: OPENAI_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    client = openai.OpenAI(api_key=api_key)

    out_con = duckdb.connect(args.out)
    init_out_db(out_con)
    print(f"[process] Output: {args.out}  Model: {args.model}")

    # Determine which source DBs to process
    if args.source:
        # Single source: derive a key from the filename
        key = os.path.splitext(os.path.basename(args.source))[0]
        sources = {key: os.path.abspath(args.source)}
    else:
        sources = SOURCE_DBS

    totals = {"ok": 0, "err": 0, "skipped": 0, "empty": 0}

    for db_key, db_path in sources.items():
        counts = process_source(db_key, db_path, out_con, client,
                                args.model, args.limit, args.reprocess, args.workers)
        for k in totals:
            totals[k] += counts[k]

    # Summary
    total_rules = out_con.execute("SELECT COUNT(*) FROM processed_rules").fetchone()[0]
    by_db = out_con.execute("""
        SELECT source_db, COUNT(*) as n
        FROM processed_rules GROUP BY 1 ORDER BY 2 DESC
    """).fetchall()
    by_cat = out_con.execute("""
        SELECT enforcement_category, COUNT(*) as n
        FROM processed_rules GROUP BY 1 ORDER BY 2 DESC
    """).fetchall()
    by_mech = out_con.execute("""
        SELECT enforcement_mechanism, COUNT(*) as n
        FROM processed_rules
        WHERE enforcement_mechanism IS NOT NULL
        GROUP BY 1 ORDER BY 2 DESC LIMIT 15
    """).fetchall()

    print(f"\n{'='*52}")
    print(f"Processed: {totals['ok']} ok  {totals['err']} errors  "
          f"{totals['skipped']} skipped  {totals['empty']} empty")
    print(f"Total individual rules in {args.out}: {total_rules}")
    print(f"\nBy source DB:")
    for row in by_db:
        print(f"  {str(row[0]):15s} {row[1]:>6}")
    print(f"\nBy category:")
    for row in by_cat:
        print(f"  {str(row[0]):20s} {row[1]:>6}")
    print(f"\nTop enforcement mechanisms:")
    for row in by_mech:
        print(f"  {str(row[0]):25s} {row[1]:>6}")
    out_con.close()


if __name__ == "__main__":
    main()
