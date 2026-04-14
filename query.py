#!/usr/bin/env python3
"""Quick exploration of the rules database."""

import sys
import duckdb

DB_PATH = "rules.db"
con = duckdb.connect(DB_PATH, read_only=True)


def summary():
    total = con.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
    print(f"Total rules: {total}\n")

    print("By source:")
    for r in con.execute("SELECT source, COUNT(*) FROM rules GROUP BY 1 ORDER BY 2 DESC").fetchall():
        print(f"  {r[0]:35s} {r[1]:>6}")

    print("\nBy file type:")
    for r in con.execute("SELECT file_type, COUNT(*) FROM rules GROUP BY 1 ORDER BY 2 DESC").fetchall():
        print(f"  {r[0]:35s} {r[1]:>6}")

    print("\nTop repos by rule count:")
    for r in con.execute("""
        SELECT repo_name, COUNT(*) as n, MAX(repo_stars) as stars
        FROM rules WHERE repo_name IS NOT NULL
        GROUP BY 1 ORDER BY 2 DESC LIMIT 20
    """).fetchall():
        print(f"  {str(r[0]):45s} rules={r[1]:>4}  stars={r[2]}")


def search(query: str):
    rows = con.execute("""
        SELECT source_url, file_type, content_len,
               SUBSTRING(content, 1, 300) as preview
        FROM rules
        WHERE LOWER(content) LIKE ?
        LIMIT 20
    """, [f"%{query.lower()}%"]).fetchall()
    print(f"Found {len(rows)} results for '{query}':\n")
    for r in rows:
        print(f"  {r[0]}")
        print(f"  type={r[1]}  len={r[2]}")
        print(f"  {r[3][:200].strip()}")
        print()


def show(url_fragment: str):
    row = con.execute("""
        SELECT source_url, file_type, repo_name, content
        FROM rules WHERE source_url LIKE ?
        LIMIT 1
    """, [f"%{url_fragment}%"]).fetchone()
    if not row:
        print("Not found")
        return
    print(f"URL:   {row[0]}")
    print(f"Type:  {row[1]}  Repo: {row[2]}")
    print("-" * 60)
    print(row[3])


if __name__ == "__main__":
    if len(sys.argv) == 1:
        summary()
    elif sys.argv[1] == "search" and len(sys.argv) > 2:
        search(" ".join(sys.argv[2:]))
    elif sys.argv[1] == "show" and len(sys.argv) > 2:
        show(sys.argv[2])
    else:
        print("Usage:")
        print("  python3 query.py              # summary stats")
        print("  python3 query.py search <kw>  # search content")
        print("  python3 query.py show <url>   # show full content")
