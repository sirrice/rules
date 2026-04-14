#!/usr/bin/env python3
"""
app.py - Rule browser and categorizer.

Source DBs (rules.db, rules2.db, rules3.db) are read-only — browsable as raw files.
processed.db holds the atomic individual rules extracted by process.py, with
(source_db, source_id) columns as logical FKs back to each source DB.

The "processed" tab in the UI shows rules from processed.db filtered by source_db.
Raw tabs show the source DBs directly (for browsing unprocessed content).

Usage:
  python3 app.py
  open http://localhost:5001
"""

import os
import duckdb
from flask import Flask, jsonify, request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Source databases (raw rule files, read-only)
SOURCE_DBS = {
    "rules":  os.path.join(BASE_DIR, "rules.db"),
    "rules2": os.path.join(BASE_DIR, "rules2.db"),
    "rules3": os.path.join(BASE_DIR, "rules3.db"),
}

# Single output database written by process.py
PROCESSED_DB = os.path.join(BASE_DIR, "processed.db")

MECHANISMS = [
    "", "linter", "formatter", "type-checker", "git-hook",
    "ci-gate", "gitignore", "regex", "filesystem",
    "db-constraint", "shell-script", "other",
]

app = Flask(__name__)
_ro_cons = {}   # cached read-only connections keyed by path


def _ro(path: str):
    """Cached read-only connection to any DB file."""
    if path not in _ro_cons:
        if not os.path.exists(path):
            return None
        try:
            _ro_cons[path] = duckdb.connect(path, read_only=True)
        except Exception as e:
            print(f"  [warn] cannot open {path}: {e}")
            return None
    return _ro_cons[path]


def proc_con():
    return _ro(PROCESSED_DB)


def source_con(db_key: str):
    path = SOURCE_DBS.get(db_key)
    return _ro(path) if path else None


def get_rw_processed():
    """Fresh read-write connection to processed.db (caller must close)."""
    try:
        return duckdb.connect(PROCESSED_DB, read_only=False)
    except Exception as e:
        print(f"  [warn] cannot open processed.db for write: {e}")
        return None


def processed_db_exists():
    return os.path.exists(PROCESSED_DB) and proc_con() is not None


def has_processed_table():
    c = proc_con()
    if not c:
        return False
    try:
        return c.execute("SELECT COUNT(*) FROM processed_rules").fetchone()[0] > 0
    except Exception:
        return False


@app.route("/api/dbs")
def api_dbs():
    result = []

    # "processed" tab — reads from processed.db, broken down by source_db
    if has_processed_table():
        c = proc_con()
        for db_key in SOURCE_DBS:
            try:
                total = c.execute(
                    "SELECT COUNT(*) FROM processed_rules WHERE source_db = ?", [db_key]
                ).fetchone()[0]
                if total == 0:
                    continue
                unc = c.execute(
                    "SELECT COUNT(*) FROM processed_rules WHERE source_db = ? AND enforcement_category IS NULL",
                    [db_key]
                ).fetchone()[0]
                det = c.execute(
                    "SELECT COUNT(*) FROM processed_rules WHERE source_db = ? AND enforcement_category = 'deterministic'",
                    [db_key]
                ).fetchone()[0]
                llm = c.execute(
                    "SELECT COUNT(*) FROM processed_rules WHERE source_db = ? AND enforcement_category = 'llm'",
                    [db_key]
                ).fetchone()[0]
                mix = c.execute(
                    "SELECT COUNT(*) FROM processed_rules WHERE source_db = ? AND enforcement_category = 'mixed'",
                    [db_key]
                ).fetchone()[0]
                result.append({
                    "key": db_key, "label": db_key,
                    "total": total, "uncategorized": unc,
                    "deterministic": det, "llm": llm, "mixed": mix,
                    "mode": "processed",
                })
            except Exception:
                pass

        # Also add "all processed" tab
        try:
            total = c.execute("SELECT COUNT(*) FROM processed_rules").fetchone()[0]
            unc   = c.execute("SELECT COUNT(*) FROM processed_rules WHERE enforcement_category IS NULL").fetchone()[0]
            det   = c.execute("SELECT COUNT(*) FROM processed_rules WHERE enforcement_category = 'deterministic'").fetchone()[0]
            llm   = c.execute("SELECT COUNT(*) FROM processed_rules WHERE enforcement_category = 'llm'").fetchone()[0]
            mix   = c.execute("SELECT COUNT(*) FROM processed_rules WHERE enforcement_category = 'mixed'").fetchone()[0]
            result.insert(0, {
                "key": "_all", "label": "all",
                "total": total, "uncategorized": unc,
                "deterministic": det, "llm": llm, "mixed": mix,
                "mode": "processed",
            })
        except Exception:
            pass

    # Raw source DB tabs (for browsing unprocessed content)
    for key, path in SOURCE_DBS.items():
        if not os.path.exists(path):
            continue
        c = source_con(key)
        if not c:
            continue
        try:
            total = c.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
        except Exception:
            total = 0
        result.append({
            "key": f"raw:{key}", "label": f"{key} (raw)",
            "total": total, "uncategorized": total,
            "deterministic": 0, "llm": 0, "mixed": 0,
            "mode": "raw", "source_key": key,
        })

    return jsonify(result)


@app.route("/api/sources")
def api_sources():
    """Return distinct rule_source values for the given db (or all)."""
    db = request.args.get("db", "_all")

    if db.startswith("raw:"):
        source_key = db[4:]
        c = source_con(source_key)
        if not c:
            return jsonify([])
        try:
            rows = c.execute(
                "SELECT source, COUNT(*) as n FROM rules GROUP BY 1 ORDER BY 2 DESC"
            ).fetchall()
            return jsonify([{"source": r[0], "count": r[1]} for r in rows])
        except Exception:
            return jsonify([])

    c = proc_con()
    if not c:
        return jsonify([])
    try:
        wheres, params = [], []
        if db != "_all":
            wheres.append("source_db = ?")
            params.append(db)
        where = ("WHERE " + " AND ".join(wheres)) if wheres else ""
        rows = c.execute(
            f"SELECT rule_source, COUNT(*) as n FROM processed_rules {where} GROUP BY 1 ORDER BY 2 DESC",
            params
        ).fetchall()
        return jsonify([{"source": r[0], "count": r[1]} for r in rows])
    except Exception:
        return jsonify([])


@app.route("/api/rules")
def api_rules():
    db      = request.args.get("db", "_all")
    offset  = int(request.args.get("offset", 0))
    limit   = min(int(request.args.get("limit", 25)), 100)
    cat     = request.args.get("category", "all")
    search  = request.args.get("search", "").strip()
    source  = request.args.get("source", "").strip()

    # ---- Raw source DB view ----
    if db.startswith("raw:"):
        source_key = db[4:]
        c = source_con(source_key)
        if not c:
            return jsonify({"error": "DB not found"}), 404

        wheres, params = [], []
        if source:
            wheres.append("source = ?")
            params.append(source)
        if search:
            wheres.append("LOWER(content) LIKE ?")
            params.append(f"%{search.lower()}%")
        where = ("WHERE " + " AND ".join(wheres)) if wheres else ""

        total = c.execute(f"SELECT COUNT(*) FROM rules {where}", params).fetchone()[0]
        rows  = c.execute(f"""
            SELECT id, content, source_url, repo_name, file_type, source
            FROM rules {where}
            ORDER BY content_len DESC NULLS LAST
            LIMIT ? OFFSET ?
        """, params + [limit, offset]).fetchall()
        rules = [{
            "id": r[0], "text": (r[1] or "")[:3000],
            "source_url": r[2], "repo_name": r[3],
            "category": None, "mechanism": None, "notes": None,
            "file_type": r[4], "source_label": r[5],
        } for r in rows]
        return jsonify({"total": total, "offset": offset, "limit": limit,
                        "rules": rules, "mode": "raw"})

    # ---- Processed DB view ----
    c = proc_con()
    if not c:
        return jsonify({"error": "processed.db not found — run process.py first"}), 404

    wheres, params = [], []
    if db != "_all":
        wheres.append("source_db = ?")
        params.append(db)
    if source:
        wheres.append("rule_source = ?")
        params.append(source)
    if cat == "uncategorized":
        wheres.append("enforcement_category IS NULL")
    elif cat != "all":
        wheres.append("enforcement_category = ?")
        params.append(cat)
    if search:
        wheres.append("LOWER(rule_text) LIKE ?")
        params.append(f"%{search.lower()}%")

    where = ("WHERE " + " AND ".join(wheres)) if wheres else ""

    total = c.execute(f"SELECT COUNT(*) FROM processed_rules {where}", params).fetchone()[0]
    rows  = c.execute(f"""
        SELECT id, rule_text, source_url, repo_name,
               enforcement_category, enforcement_mechanism, enforcement_notes,
               rule_index, source_db, rule_source
        FROM processed_rules {where}
        ORDER BY source_db, rule_index ASC NULLS LAST, id
        LIMIT ? OFFSET ?
    """, params + [limit, offset]).fetchall()
    rules = [{
        "id": r[0], "text": r[1] or "",
        "source_url": r[2], "repo_name": r[3],
        "category": r[4], "mechanism": r[5], "notes": r[6],
        "rule_index": r[7], "source_db": r[8], "rule_source": r[9],
    } for r in rows]

    return jsonify({"total": total, "offset": offset, "limit": limit,
                    "rules": rules, "mode": "processed"})


@app.route("/api/rules/<rule_id>", methods=["PATCH"])
def update_rule(rule_id):
    body      = request.json or {}
    category  = body.get("category")
    mechanism = body.get("mechanism") or None
    notes     = body.get("notes") or None

    if not has_processed_table():
        return jsonify({"error": "processed.db not found"}), 400

    c = get_rw_processed()
    if not c:
        return jsonify({"error": "processed.db is locked"}), 503

    try:
        c.execute("""
            UPDATE processed_rules
            SET enforcement_category  = ?,
                enforcement_mechanism = ?,
                enforcement_notes     = ?
            WHERE id = ?
        """, [category, mechanism, notes, rule_id])
    finally:
        c.close()

    # Drop the cached read-only connection so next read sees the update
    _ro_cons.pop(PROCESSED_DB, None)

    return jsonify({"ok": True})


@app.route("/")
def index():
    return HTML


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Rules Browser</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
       background: #f0f0f5; color: #1a1a2e; height: 100vh; overflow: hidden; }

/* Layout */
.layout { display: flex; height: 100vh; }
.sidebar { width: 240px; background: #1a1a2e; color: #c8c8e0; display: flex;
           flex-direction: column; flex-shrink: 0; overflow: hidden; }
.sidebar-inner { padding: 16px; overflow-y: auto; flex: 1; }
.main { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
.main-header { padding: 14px 20px; background: white; border-bottom: 1px solid #e0e0e8;
               display: flex; align-items: center; justify-content: space-between;
               flex-shrink: 0; }
.main-scroll { flex: 1; overflow-y: auto; padding: 16px 20px; }

/* Sidebar sections */
.sidebar h1 { font-size: 14px; font-weight: 700; color: #fff; letter-spacing: 0.5px;
              padding: 16px 16px 12px; border-bottom: 1px solid #2a2a4a; flex-shrink: 0; }
.section-label { font-size: 10px; text-transform: uppercase; letter-spacing: 1.2px;
                 color: #5a5a7a; margin-bottom: 6px; margin-top: 14px; }

/* DB tabs */
.db-list { display: flex; flex-direction: column; gap: 3px; }
.db-btn { display: flex; justify-content: space-between; align-items: center;
          padding: 7px 10px; border-radius: 6px; border: none; background: transparent;
          color: #9090b0; cursor: pointer; font-size: 13px; width: 100%; text-align: left; }
.db-btn:hover { background: #252545; color: #d0d0f0; }
.db-btn.active { background: #2a2a5a; color: #fff; }
.db-btn .db-count { font-size: 11px; color: #6060a0; }
.db-btn.active .db-count { color: #9090c0; }

/* Category filters */
.cat-list { display: flex; flex-direction: column; gap: 2px; }
.cat-btn { display: flex; justify-content: space-between; align-items: center;
           padding: 6px 10px; border-radius: 6px; border: none; background: transparent;
           color: #9090b0; cursor: pointer; font-size: 13px; width: 100%; text-align: left; }
.cat-btn:hover { background: #252545; color: #d0d0f0; }
.cat-btn.active { background: #252545; color: #fff; }
.cat-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 7px; }

/* Search */
.search-input { width: 100%; padding: 8px 10px; border-radius: 7px; border: 1px solid #2a2a4a;
                background: #0f0f22; color: #e0e0f0; font-size: 13px; margin-top: 4px; }
.search-input:focus { outline: none; border-color: #5050a0; }
.search-input::placeholder { color: #4a4a6a; }

/* Progress */
.progress-wrap { margin-top: 4px; }
.progress-bar { height: 4px; background: #252545; border-radius: 2px; overflow: hidden; }
.progress-fill { height: 100%; background: linear-gradient(90deg, #22c55e, #6366f1);
                 border-radius: 2px; transition: width 0.4s; }
.progress-label { font-size: 11px; color: #5a5a7a; margin-top: 5px; text-align: right; }
.stat-rows { margin-top: 8px; }
.stat-row { display: flex; justify-content: space-between; font-size: 12px;
            color: #7070a0; padding: 2px 0; }
.stat-row span:first-child { display: flex; align-items: center; gap: 6px; }

/* Header */
.total-info { font-size: 13px; color: #666; }
.kbd-hint { font-size: 11px; color: #aaa; background: #f5f5f5; border: 1px solid #ddd;
            border-radius: 4px; padding: 2px 6px; }

/* Rule cards */
.rules-list { display: flex; flex-direction: column; gap: 10px;
              max-width: 820px; margin: 0 auto; }

.rule-card { background: white; border-radius: 10px; padding: 16px 18px;
             box-shadow: 0 1px 3px rgba(0,0,0,0.07);
             border-left: 4px solid #e0e0e8; transition: border-color 0.15s; }
.rule-card.cat-deterministic { border-left-color: #22c55e; }
.rule-card.cat-llm          { border-left-color: #6366f1; }
.rule-card.cat-mixed        { border-left-color: #f59e0b; }

.rule-text { font-size: 14px; line-height: 1.65; white-space: pre-wrap;
             word-break: break-word; margin-bottom: 10px; color: #1a1a2e; }
.rule-text.truncated { max-height: 120px; overflow: hidden; position: relative; }
.rule-text.truncated::after { content: ''; position: absolute; bottom: 0; left: 0; right: 0;
                               height: 40px; background: linear-gradient(transparent, white); }
.expand-btn { font-size: 11px; color: #6366f1; cursor: pointer; border: none;
              background: none; padding: 0; margin-bottom: 8px; }

.rule-meta { font-size: 11px; color: #999; margin-bottom: 10px; display: flex;
             gap: 10px; flex-wrap: wrap; align-items: center; }
.rule-meta a { color: #6366f1; text-decoration: none; }
.rule-meta a:hover { text-decoration: underline; }
.tag { background: #f0f0f8; padding: 2px 7px; border-radius: 4px;
       font-size: 10px; color: #7070a0; }

.rule-actions { display: flex; gap: 7px; flex-wrap: wrap; align-items: center; }
.action-btn { padding: 4px 13px; border-radius: 20px; border: 1.5px solid;
              cursor: pointer; font-size: 12px; font-weight: 500; background: transparent;
              transition: all 0.1s; }
.action-btn.det { border-color: #22c55e; color: #22c55e; }
.action-btn.det:hover, .action-btn.det.active { background: #22c55e; color: white; }
.action-btn.mix { border-color: #f59e0b; color: #f59e0b; }
.action-btn.mix:hover, .action-btn.mix.active { background: #f59e0b; color: white; }
.action-btn.llm { border-color: #6366f1; color: #6366f1; }
.action-btn.llm:hover, .action-btn.llm.active { background: #6366f1; color: white; }
.action-btn.clear { border-color: #ccc; color: #aaa; font-size: 11px; padding: 3px 10px; }
.action-btn.clear:hover { border-color: #999; color: #666; }

.mech-select { padding: 4px 8px; border-radius: 6px; border: 1px solid #e0e0e8;
               font-size: 12px; color: #666; background: white; }

/* Load more */
.load-more { text-align: center; padding: 24px 0; }
.load-more-btn { padding: 9px 28px; border-radius: 8px; border: 1.5px solid #6366f1;
                 color: #6366f1; background: transparent; cursor: pointer; font-size: 14px; }
.load-more-btn:hover { background: #6366f1; color: white; }

.empty { text-align: center; color: #aaa; padding: 60px 20px; font-size: 15px; }
</style>
</head>
<body>
<div class="layout">
  <!-- Sidebar -->
  <div class="sidebar">
    <h1>Rules Browser</h1>
    <div class="sidebar-inner">
      <div class="section-label">Database</div>
      <div class="db-list" id="dbList"></div>

      <div class="section-label">Filter</div>
      <input class="search-input" id="searchInput" placeholder="Search rules…" autocomplete="off">
      <div class="cat-list" style="margin-top:8px" id="catList"></div>

      <div class="section-label" style="margin-top:14px">Source</div>
      <div class="cat-list" id="sourceList"></div>

      <div class="section-label">Progress</div>
      <div class="progress-wrap">
        <div class="progress-bar"><div class="progress-fill" id="progressFill" style="width:0%"></div></div>
        <div class="progress-label" id="progressLabel">—</div>
        <div class="stat-rows" id="statRows"></div>
      </div>
    </div>
  </div>

  <!-- Main -->
  <div class="main">
    <div class="main-header">
      <span class="total-info" id="totalInfo">Loading…</span>
      <span class="kbd-hint">D = deterministic · M = mixed · L = LLM-only</span>
    </div>
    <div class="main-scroll" id="mainScroll">
      <div class="rules-list" id="rulesList"></div>
      <div class="load-more" id="loadMoreDiv" style="display:none">
        <button class="load-more-btn" id="loadMoreBtn">Load more</button>
      </div>
    </div>
  </div>
</div>

<script>
const MECHANISMS = [
  '', 'linter', 'formatter', 'type-checker', 'git-hook',
  'ci-gate', 'gitignore', 'regex', 'filesystem',
  'db-constraint', 'shell-script', 'other',
];

const CAT_COLORS = {
  deterministic: '#22c55e',
  mixed: '#f59e0b',
  llm: '#6366f1',
  null: '#c0c0d0',
};

let S = {
  db: null, dbs: [], category: 'all',
  source: '', search: '', offset: 0, limit: 25,
  total: 0, loading: false, mode: 'raw',
  focusedCard: null,
};

async function apiFetch(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(r.statusText);
  return r.json();
}

async function apiPatch(path, body) {
  const r = await fetch(path, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return r.json();
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

async function init() {
  const dbs = await apiFetch('/api/dbs');
  S.dbs = dbs;
  renderDbList();
  if (dbs.length > 0) await selectDb(dbs[0].key);
}

// ---------------------------------------------------------------------------
// Sidebar rendering
// ---------------------------------------------------------------------------

function renderDbList() {
  document.getElementById('dbList').innerHTML = S.dbs.map(d => `
    <button class="db-btn ${d.key === S.db ? 'active' : ''}" onclick="selectDb('${d.key}')">
      <span>${d.label || d.key}</span>
      <span class="db-count">${d.total.toLocaleString()}</span>
    </button>
  `).join('');
}

function renderCatList(dbInfo) {
  const cats = [
    { key: 'all',            label: 'All',           color: '#9090c0', count: dbInfo?.total },
    { key: 'uncategorized',  label: 'Uncategorized', color: '#c0c0d0', count: dbInfo?.uncategorized },
    { key: 'deterministic',  label: 'Deterministic', color: '#22c55e', count: dbInfo?.deterministic },
    { key: 'mixed',          label: 'Mixed',         color: '#f59e0b', count: dbInfo?.mixed },
    { key: 'llm',            label: 'LLM-only',      color: '#6366f1', count: dbInfo?.llm },
  ];
  document.getElementById('catList').innerHTML = cats.map(c => `
    <button class="cat-btn ${c.key === S.category ? 'active' : ''}" onclick="selectCat('${c.key}')">
      <span><span class="cat-dot" style="background:${c.color}"></span>${c.label}</span>
      <span style="font-size:11px;color:#6060a0">${c.count != null ? c.count.toLocaleString() : ''}</span>
    </button>
  `).join('');
}

function renderProgress(dbInfo) {
  if (!dbInfo) return;
  const total = dbInfo.total || 1;
  const categorized = (dbInfo.deterministic || 0) + (dbInfo.mixed || 0) + (dbInfo.llm || 0);
  const pct = Math.round(categorized / total * 100);

  document.getElementById('progressFill').style.width = pct + '%';
  document.getElementById('progressLabel').textContent = `${categorized.toLocaleString()} / ${total.toLocaleString()} (${pct}%)`;
  document.getElementById('statRows').innerHTML = [
    ['Deterministic', '#22c55e', dbInfo.deterministic || 0],
    ['Mixed',         '#f59e0b', dbInfo.mixed || 0],
    ['LLM-only',      '#6366f1', dbInfo.llm || 0],
    ['Uncategorized', '#6060a0', dbInfo.uncategorized || 0],
  ].map(([label, color, count]) => `
    <div class="stat-row">
      <span><span class="cat-dot" style="background:${color}"></span>${label}</span>
      <span>${count.toLocaleString()}</span>
    </div>
  `).join('');
}

async function refreshSidebar() {
  const dbs = await apiFetch('/api/dbs');
  S.dbs = dbs;
  renderDbList();
  const dbInfo = dbs.find(d => d.key === S.db);
  renderCatList(dbInfo);
  renderProgress(dbInfo);
}

// ---------------------------------------------------------------------------
// Navigation
// ---------------------------------------------------------------------------

async function selectDb(key) {
  S.db = key; S.offset = 0; S.source = '';
  const dbInfo = S.dbs.find(d => d.key === key);
  S.mode = dbInfo?.mode || 'raw';
  renderDbList();
  renderCatList(dbInfo);
  renderProgress(dbInfo);
  await Promise.all([loadRules(true), loadSources()]);
}

async function loadSources() {
  const data = await apiFetch(`/api/sources?db=${S.db}`);
  const el = document.getElementById('sourceList');
  if (!data.length) { el.innerHTML = ''; return; }
  el.innerHTML = [
    `<button class="cat-btn ${S.source === '' ? 'active' : ''}" onclick="selectSource('')">
       <span>All sources</span>
       <span style="font-size:11px;color:#6060a0"></span>
     </button>`,
    ...data.map(d => `
      <button class="cat-btn ${S.source === d.source ? 'active' : ''}"
              onclick="selectSource(${JSON.stringify(d.source)})">
        <span style="word-break:break-all">${esc(d.source || '(none)')}</span>
        <span style="font-size:11px;color:#6060a0">${d.count.toLocaleString()}</span>
      </button>
    `)
  ].join('');
}

async function selectSource(src) {
  S.source = src; S.offset = 0;
  await Promise.all([loadRules(true), loadSources()]);
}

async function selectCat(cat) {
  S.category = cat; S.offset = 0;
  const dbInfo = S.dbs.find(d => d.key === S.db);
  renderCatList(dbInfo);
  await Promise.all([loadRules(true), loadSources()]);
}

// ---------------------------------------------------------------------------
// Rules loading
// ---------------------------------------------------------------------------

async function loadRules(reset = false) {
  if (S.loading) return;
  S.loading = true;

  const params = new URLSearchParams({
    db: S.db, offset: S.offset, limit: S.limit,
    category: S.category, search: S.search, source: S.source,
  });
  const data = await apiFetch(`/api/rules?${params}`);
  S.total = data.total;
  S.offset += data.rules.length;
  S.mode = data.mode || 'raw';
  S.loading = false;

  const dbInfo = S.dbs.find(d => d.key === S.db);
  document.getElementById('totalInfo').textContent =
    `${data.total.toLocaleString()} rules · ${dbInfo?.label || S.db}`;

  const list = document.getElementById('rulesList');
  if (reset) { list.innerHTML = ''; S.focusedCard = null; }

  data.rules.forEach(rule => list.appendChild(makeCard(rule, S.mode === 'processed')));

  const lm = document.getElementById('loadMoreDiv');
  lm.style.display = S.offset < S.total ? 'block' : 'none';
}

// ---------------------------------------------------------------------------
// Rule cards
// ---------------------------------------------------------------------------

function esc(s) {
  if (!s) return '';
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function makeCard(rule, hasProcessed) {
  const card = document.createElement('div');
  card.className = 'rule-card' + (rule.category ? ' cat-' + rule.category : '');
  card.dataset.id = rule.id;
  card.dataset.category = rule.category || '';
  card.dataset.mechanism = rule.mechanism || '';
  card.tabIndex = 0;

  const text = rule.text || '';
  const long = text.length > 400;
  const metaParts = [];
  if (rule.source_db)    metaParts.push(`<span class="tag">${esc(rule.source_db)}</span>`);
  if (rule.rule_source)  metaParts.push(`<span class="tag" style="background:#f0f4ff;color:#4455aa">${esc(rule.rule_source)}</span>`);
  if (rule.repo_name)    metaParts.push(`<span class="tag">${esc(rule.repo_name)}</span>`);
  if (rule.source_url)   metaParts.push(`<a href="${esc(rule.source_url)}" target="_blank">source ↗</a>`);
  if (rule.file_type)    metaParts.push(`<span class="tag">${esc(rule.file_type)}</span>`);
  if (rule.source_label) metaParts.push(`<span class="tag" style="background:#f0f4ff;color:#4455aa">${esc(rule.source_label)}</span>`);

  const mechOpts = MECHANISMS.map(m =>
    `<option value="${m}" ${rule.mechanism === m ? 'selected' : ''}>${m || '— mechanism —'}</option>`
  ).join('');

  const actionHtml = hasProcessed ? `
    <div class="rule-actions">
      <button class="action-btn det ${rule.category === 'deterministic' ? 'active' : ''}"
              onclick="categorize(this, 'deterministic')">Deterministic</button>
      <button class="action-btn mix ${rule.category === 'mixed' ? 'active' : ''}"
              onclick="categorize(this, 'mixed')">Mixed</button>
      <button class="action-btn llm ${rule.category === 'llm' ? 'active' : ''}"
              onclick="categorize(this, 'llm')">LLM-only</button>
      <select class="mech-select" onchange="saveMech(this)">${mechOpts}</select>
      ${rule.category ? `<button class="action-btn clear" onclick="categorize(this, null)">✕ clear</button>` : ''}
    </div>` : '';

  card.innerHTML = `
    <div class="rule-text${long ? ' truncated' : ''}" id="rt-${rule.id}">${esc(text)}</div>
    ${long ? `<button class="expand-btn" onclick="expandText(this, '${rule.id}')">Show more ▾</button>` : ''}
    <div class="rule-meta">${metaParts.join('')}</div>
    ${actionHtml}
  `;

  card.addEventListener('focus', () => { S.focusedCard = card; });
  return card;
}

function expandText(btn, id) {
  const el = document.getElementById('rt-' + id);
  el.classList.remove('truncated');
  btn.remove();
}

// ---------------------------------------------------------------------------
// Categorization
// ---------------------------------------------------------------------------

async function categorize(btn, category) {
  const card = btn.closest('.rule-card');
  const id = card.dataset.id;
  const mechEl = card.querySelector('.mech-select');
  const mechanism = mechEl ? mechEl.value : null;

  await apiPatch(`/api/rules/${id}?db=${S.db}`, { category, mechanism });

  // Update card appearance
  card.className = 'rule-card' + (category ? ' cat-' + category : '');
  card.dataset.category = category || '';

  // Update buttons
  card.querySelectorAll('.action-btn').forEach(b => b.classList.remove('active'));
  if (category) {
    const cls = { deterministic: 'det', mixed: 'mix', llm: 'llm' }[category];
    card.querySelector(`.action-btn.${cls}`)?.classList.add('active');
  }

  // Add/remove clear button
  const actions = card.querySelector('.rule-actions');
  const existing = card.querySelector('.action-btn.clear');
  if (category && !existing) {
    const clr = document.createElement('button');
    clr.className = 'action-btn clear';
    clr.textContent = '✕ clear';
    clr.onclick = () => categorize(clr, null);
    actions.appendChild(clr);
  } else if (!category && existing) {
    existing.remove();
  }

  // Move focus to next uncategorized card if filtering by uncategorized
  if (S.category === 'uncategorized') {
    const all = [...document.querySelectorAll('.rule-card')];
    const idx = all.indexOf(card);
    const next = all[idx + 1];
    if (next) { card.remove(); next.focus(); }
    else card.remove();
  }

  await refreshSidebar();
}

async function saveMech(selectEl) {
  const card = selectEl.closest('.rule-card');
  const id = card.dataset.id;
  const category = card.dataset.category || null;
  if (category) {
    await apiPatch(`/api/rules/${id}?db=${S.db}`, {
      category, mechanism: selectEl.value
    });
  }
}

// ---------------------------------------------------------------------------
// Keyboard shortcuts
// ---------------------------------------------------------------------------

document.addEventListener('keydown', e => {
  // Don't fire when typing in search
  if (e.target === document.getElementById('searchInput')) return;

  const card = S.focusedCard || document.querySelector('.rule-card:hover');
  if (!card || S.mode !== 'processed') return;

  const key = e.key.toLowerCase();
  if (key === 'd') { e.preventDefault(); categorize(card.querySelector('.action-btn.det'), 'deterministic'); }
  if (key === 'm') { e.preventDefault(); categorize(card.querySelector('.action-btn.mix'), 'mixed'); }
  if (key === 'l') { e.preventDefault(); categorize(card.querySelector('.action-btn.llm'), 'llm'); }
  if (key === 'x') { e.preventDefault(); categorize(card, null); }

  // Tab through cards with arrow keys
  if (key === 'arrowdown' || key === 'j') {
    e.preventDefault();
    const all = [...document.querySelectorAll('.rule-card')];
    const next = all[all.indexOf(card) + 1];
    if (next) next.focus();
  }
  if (key === 'arrowup' || key === 'k') {
    e.preventDefault();
    const all = [...document.querySelectorAll('.rule-card')];
    const prev = all[all.indexOf(card) - 1];
    if (prev) prev.focus();
  }
});

// ---------------------------------------------------------------------------
// Load more / infinite scroll
// ---------------------------------------------------------------------------

document.getElementById('loadMoreBtn').addEventListener('click', () => loadRules(false));

document.getElementById('mainScroll').addEventListener('scroll', e => {
  const el = e.target;
  if (el.scrollTop + el.clientHeight >= el.scrollHeight - 200) {
    if (!S.loading && S.offset < S.total) loadRules(false);
  }
});

// ---------------------------------------------------------------------------
// Search (debounced)
// ---------------------------------------------------------------------------

let searchTimer;
document.getElementById('searchInput').addEventListener('input', e => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    S.search = e.target.value;
    S.offset = 0;
    loadRules(true);
  }, 300);
});

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
init();
</script>
</body>
</html>"""


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5001)
    args = parser.parse_args()
    print(f"Rules Browser running at http://{args.host}:{args.port}")
    app.run(debug=False, host=args.host, port=args.port, threaded=False)
