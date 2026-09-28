#!/usr/bin/env python3
"""
Re-Search — local full-text search over your claude.ai data exports.
Not affiliated with or endorsed by Anthropic.

Standard library only (Python 3.9+). Nothing leaves your machine except the
optional "Ask" feature, which sends only the matching excerpts to the Claude API
with your own key.

Run:    python re_search.py
        Opens in its own window if pywebview is installed (pip install pywebview),
        otherwise as an Edge/Chrome app window, otherwise in a browser tab.
Build:  python -m PyInstaller --onefile --windowed --icon re-search.ico --collect-all webview --name Re-Search re_search.py

Data lives in %LOCALAPPDATA%\\Re-Search on Windows (~/.re-search elsewhere):
    imports/   drop export zips (or conversations.json / projects.json) here
    index.db   the SQLite full-text index
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sqlite3
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_NAME = "Re-Search"
OLD_APP_NAMES = ("ClaudeSearch",)  # data folders from earlier versions, moved on first run
HOST, DEFAULT_PORT = "127.0.0.1", 8765
API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-5"
WATCH_INTERVAL = 15  # seconds between scans of the imports folder
WINDOW_SIZE = (1400, 900)
APP_BROWSERS = [  # Edge/Chrome locations for the chromeless "app window" fallback on Windows
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]
UI = {"webview": None}  # set when the interface runs in a pywebview window
MARK_OPEN, MARK_CLOSE = "\u0001", "\u0002"

# ----------------------------------------------------------------------------
# Storage
# ----------------------------------------------------------------------------

def data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") if os.name == "nt" else None
    parent = Path(base) if base else Path.home()
    name = APP_NAME if base else f".{APP_NAME.lower()}"
    root = parent / name
    if not root.exists():
        # first run after a rename: carry the old index and imports across
        for old in OLD_APP_NAMES:
            old_root = parent / (old if base else f".{old.lower()}")
            if old_root.exists():
                try:
                    old_root.rename(root)
                    print(f"Moved data folder {old_root} -> {root}")
                except OSError as e:
                    print(f"Could not move {old_root} to {root}: {e}")
                break
    (root / "imports").mkdir(parents=True, exist_ok=True)
    return root


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(
    uuid TEXT PRIMARY KEY, name TEXT, description TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS conversations(
    uuid TEXT PRIMARY KEY, name TEXT, created_at TEXT, updated_at TEXT,
    project_uuid TEXT, message_count INTEGER DEFAULT 0, summary TEXT);
CREATE TABLE IF NOT EXISTS project_overrides(
    conversation_uuid TEXT PRIMARY KEY, project_uuid TEXT);
CREATE TABLE IF NOT EXISTS messages(
    uuid TEXT PRIMARY KEY, conversation_uuid TEXT, sender TEXT,
    created_at TEXT, seq INTEGER, text TEXT);
CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_uuid, seq);
CREATE INDEX IF NOT EXISTS idx_conv_updated ON conversations(updated_at);
CREATE TABLE IF NOT EXISTS imports(
    key TEXT PRIMARY KEY, filename TEXT, imported_at TEXT,
    conversations INTEGER, messages INTEGER);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text, message_uuid UNINDEXED, conversation_uuid UNINDEXED,
    tokenize='unicode61');
CREATE VIRTUAL TABLE IF NOT EXISTS conv_fts USING fts5(
    name, summary, conversation_uuid UNINDEXED,
    tokenize='unicode61');
"""


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        # migrations for indexes built by earlier versions
        cols = [r[1] for r in self.db.execute("PRAGMA table_info(conversations)")]
        if "summary" not in cols:
            self.db.execute("ALTER TABLE conversations ADD COLUMN summary TEXT")
        try:
            self.db.executescript(FTS_SCHEMA)
            self.fts = True
            fcols = [r[1] for r in self.db.execute("PRAGMA table_info(conv_fts)")]
            if "summary" not in fcols:
                self.db.execute("DROP TABLE conv_fts")
                self.db.executescript(FTS_SCHEMA)
                for r in self.db.execute("SELECT uuid, name, summary FROM conversations").fetchall():
                    self.db.execute("INSERT INTO conv_fts VALUES(?,?,?)",
                                    (fold(r["name"]), fold(r["summary"]), r["uuid"]))
        except sqlite3.OperationalError:
            self.fts = False
            print("WARNING: this Python's SQLite has no FTS5; falling back to slow LIKE search.")
        self.db.commit()

    # -- settings -----------------------------------------------------------
    def get_setting(self, key, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key, value):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, value))
            self.db.commit()

    # -- ingest -------------------------------------------------------------
    def ingest_file(self, path: Path):
        convs, projs, mems = [], [], []
        suffix = path.suffix.lower()
        if suffix == ".zip":
            with zipfile.ZipFile(path) as z:
                for n in z.namelist():
                    if not n.lower().endswith((".json", ".jsonl")):
                        continue
                    kind, data = classify_json(z.read(n))
                    if kind == "conversations":
                        convs.extend(data)
                    elif kind == "projects":
                        projs.extend(data)
                    elif kind == "memories":
                        mems.append(data)
        elif suffix in (".json", ".jsonl"):
            kind, data = classify_json(path.read_bytes())
            if kind == "manifest":
                raise ValueError("That is the export manifest, not the data. Open each export_url "
                                 "in it (conversations and projects) to download the zips, then import those.")
            if kind == "conversations":
                convs = data
            elif kind == "projects":
                projs = data
            elif kind == "memories":
                mems = [data]
        else:
            raise ValueError(f"Unsupported file type: {path.name}")
        if not convs and not projs and not mems:
            if suffix == ".zip" and any(os.path.basename(n) in ("users.json", "login_history.json")
                                        for n in zipfile.ZipFile(path).namelist()):
                raise ValueError("account metadata (user record, login history) — deliberately not indexed")
            raise ValueError(f"No conversation, project or memory data recognised in {path.name}")
        for m in mems:
            convs.extend(memory_entries(m))
        return self.ingest_data(convs, projs)

    def ingest_data(self, conversations, projects):
        n_conv = n_msg = 0
        proj_map = {}
        doc_entries = []
        with self.lock:
            cur = self.db.cursor()
            for p in projects or []:
                if not isinstance(p, dict) or not p.get("uuid"):
                    continue
                cur.execute(
                    "INSERT OR REPLACE INTO projects VALUES(?,?,?,?)",
                    (p["uuid"], p.get("name") or "(untitled project)",
                     p.get("description") or "", p.get("created_at") or ""))
                for cu in p.get("conversations") or []:
                    cid = cu.get("uuid") if isinstance(cu, dict) else cu
                    if cid:
                        proj_map[cid] = p["uuid"]
                for d in p.get("docs") or []:
                    if not isinstance(d, dict) or not (d.get("content") or "").strip():
                        continue
                    duid = f"doc:{p['uuid']}:{d.get('uuid') or d.get('filename')}"
                    when = d.get("created_at") or p.get("updated_at") or ""
                    doc_entries.append({
                        "uuid": duid, "name": d.get("filename") or "(untitled file)",
                        "created_at": when, "updated_at": when, "project_uuid": p["uuid"],
                        "chat_messages": [{"uuid": duid + ":0", "sender": "document",
                                           "created_at": when, "text": d["content"]}]})
            overrides = {r[0]: r[1] for r in cur.execute("SELECT conversation_uuid, project_uuid FROM project_overrides")}
            for c in list(conversations or []) + doc_entries:
                if not isinstance(c, dict) or not c.get("uuid"):
                    continue
                cid = c["uuid"]
                msgs = [m for m in (c.get("chat_messages") or c.get("messages") or []) if isinstance(m, dict)]
                upd = c.get("updated_at") or ""
                row = cur.execute(
                    "SELECT updated_at, message_count, project_uuid FROM conversations WHERE uuid=?",
                    (cid,)).fetchone()
                pid = project_of(c) or proj_map.get(cid)
                if cid in overrides:          # the user's own assignment always wins
                    pid = overrides[cid]
                if row and (row["updated_at"] or "") >= upd and (row["message_count"] or 0) >= len(msgs):
                    if pid and not row["project_uuid"]:
                        cur.execute("UPDATE conversations SET project_uuid=? WHERE uuid=?", (pid, cid))
                    continue
                if row and not pid:
                    pid = row["project_uuid"]
                cur.execute("DELETE FROM messages WHERE conversation_uuid=?", (cid,))
                if self.fts:
                    cur.execute("DELETE FROM messages_fts WHERE conversation_uuid=?", (cid,))
                    cur.execute("DELETE FROM conv_fts WHERE conversation_uuid=?", (cid,))
                msgs.sort(key=lambda m: m.get("created_at") or "")
                stored = 0
                for seq, m in enumerate(msgs):
                    text = message_text(m)
                    if not text:
                        continue
                    muid = m.get("uuid") or f"{cid}:{seq}"
                    sender = (m.get("sender") or "unknown").lower()
                    cur.execute(
                        "INSERT OR REPLACE INTO messages VALUES(?,?,?,?,?,?)",
                        (muid, cid, sender, m.get("created_at") or "", seq, text))
                    if self.fts:
                        cur.execute("INSERT INTO messages_fts VALUES(?,?,?)", (fold(text), muid, cid))
                    stored += 1
                name = c.get("name") or "(untitled)"
                summary = (c.get("summary") or "").strip() or None
                cur.execute(
                    "INSERT OR REPLACE INTO conversations VALUES(?,?,?,?,?,?,?)",
                    (cid, name, c.get("created_at") or "", upd, pid, len(msgs), summary))
                if self.fts:
                    cur.execute("INSERT INTO conv_fts VALUES(?,?,?)", (fold(name), fold(summary), cid))
                n_conv += 1
                n_msg += stored
            self.db.commit()
        return n_conv, n_msg

    def record_import(self, key, filename, n_conv, n_msg):
        with self.lock:
            self.db.execute(
                "INSERT OR REPLACE INTO imports VALUES(?,?,?,?,?)",
                (key, filename, datetime.now(timezone.utc).isoformat(timespec="seconds"), n_conv, n_msg))
            self.db.commit()

    def import_seen(self, key):
        return self.db.execute("SELECT 1 FROM imports WHERE key=?", (key,)).fetchone() is not None

    # -- queries ------------------------------------------------------------
    def stats(self):
        q = lambda s: self.db.execute(s).fetchone()[0]
        return {
            "conversations": q("SELECT COUNT(*) FROM conversations WHERE uuid NOT LIKE 'memory:%' AND uuid NOT LIKE 'doc:%'"),
            "memory_notes": q("SELECT COUNT(*) FROM conversations WHERE uuid LIKE 'memory:%'"),
            "project_files": q("SELECT COUNT(*) FROM conversations WHERE uuid LIKE 'doc:%'"),
            "messages": q("SELECT COUNT(*) FROM messages"),
            "projects": q("SELECT COUNT(*) FROM projects"),
            "unlinked": q("SELECT COUNT(*) FROM conversations WHERE project_uuid IS NULL AND uuid NOT LIKE 'memory:%' AND uuid NOT LIKE 'doc:%'"),
            "earliest": q("SELECT MIN(created_at) FROM conversations"),
            "latest": q("SELECT MAX(updated_at) FROM conversations"),
            "fts": self.fts,
            "imports": [dict(r) for r in self.db.execute(
                "SELECT filename, imported_at, conversations, messages FROM imports ORDER BY imported_at DESC LIMIT 20")],
            "data_dir": str(self.path.parent),
        }

    def projects(self):
        rows = self.db.execute("""
            SELECT p.uuid, p.name, COUNT(c.uuid) AS n
            FROM projects p LEFT JOIN conversations c ON c.project_uuid = p.uuid
                 AND c.uuid NOT LIKE 'memory:%' AND c.uuid NOT LIKE 'doc:%'
            GROUP BY p.uuid ORDER BY p.name COLLATE NOCASE""").fetchall()
        out = [dict(r) for r in rows]
        # conversations that reference a project we have no record of
        for r in self.db.execute("""
            SELECT project_uuid AS uuid, COUNT(*) AS n FROM conversations
            WHERE project_uuid IS NOT NULL AND project_uuid NOT IN (SELECT uuid FROM projects)
            AND uuid NOT LIKE 'memory:%' AND uuid NOT LIKE 'doc:%' GROUP BY project_uuid"""):
            out.append({"uuid": r["uuid"], "name": f"(unknown project {r['uuid'][:8]})", "n": r["n"]})
        return out

    def _filters(self, project, sender, date_from, date_to, alias="m", include_memory=False, conversation=None):
        where, params = [], []
        if not include_memory:
            where.append("c.uuid NOT LIKE 'memory:%'")
        if conversation:
            where.append("c.uuid = ?")
            params.append(conversation)
        if project == "__none__":
            where.append("c.project_uuid IS NULL")
        elif project:
            where.append("c.project_uuid = ?")
            params.append(project)
        if sender in ("human", "assistant", "document"):
            where.append(f"{alias}.sender = ?")
            params.append(sender)
        if date_from:
            where.append(f"{alias}.created_at >= ?")
            params.append(date_from)
        if date_to:
            where.append(f"{alias}.created_at < ?")
            params.append(date_to + "T99")  # inclusive of that day
        return (" AND " + " AND ".join(where)) if where else "", params

    def search(self, q, project=None, sender=None, date_from=None, date_to=None,
               limit=60, offset=0, include_memory=False, conversation=None, order="rank"):
        q = (q or "").strip()
        if not q:
            return self.recent(project, limit)
        flt, params = self._filters(project, sender, date_from, date_to,
                                    include_memory=include_memory, conversation=conversation)
        order_sql = "m.seq" if order == "seq" else "rank"
        if self.fts:
            match = build_match(q)
            base = f"""
                FROM messages_fts f
                JOIN messages m ON m.uuid = f.message_uuid
                JOIN conversations c ON c.uuid = m.conversation_uuid
                LEFT JOIN projects p ON p.uuid = c.project_uuid
                WHERE messages_fts MATCH ? {flt}"""
            sel = f"""
                SELECT m.uuid, m.conversation_uuid, m.sender, m.created_at, m.seq,
                       c.name AS conv_name, c.updated_at AS conv_updated,
                       c.project_uuid, p.name AS project_name,
                       m.text, bm25(messages_fts) AS rank
                {base} ORDER BY {order_sql} LIMIT ? OFFSET ?"""
            try:
                rows = self.db.execute(sel, [match, *params, limit, offset]).fetchall()
                total = self.db.execute(f"SELECT COUNT(*) {base}", [match, *params]).fetchone()[0]
            except sqlite3.OperationalError:
                # user typed something FTS could not parse -> quote every token
                match = build_match(q, force_quote=True)
                rows = self.db.execute(sel, [match, *params, limit, offset]).fetchall()
                total = self.db.execute(f"SELECT COUNT(*) {base}", [match, *params]).fetchone()[0]
            per_conv = {r[0]: r[1] for r in self.db.execute(
                f"SELECT m.conversation_uuid, COUNT(*) {base} GROUP BY m.conversation_uuid", [match, *params])}
            hits = self._with_snippets(rows, q)
            # title matches
            title_rows = self.db.execute(f"""
                SELECT c.uuid, c.name, c.updated_at, c.project_uuid, p.name AS project_name, c.message_count, c.summary
                FROM conv_fts f JOIN conversations c ON c.uuid = f.conversation_uuid
                LEFT JOIN projects p ON p.uuid = c.project_uuid
                WHERE conv_fts MATCH ? {self._filters(project, None, None, None, include_memory=include_memory)[0]}
                ORDER BY c.updated_at DESC LIMIT 20""",
                [match, *self._filters(project, None, None, None, include_memory=include_memory)[1]]).fetchall()
        else:
            like = f"%{q}%"
            base = f"""
                FROM messages m JOIN conversations c ON c.uuid = m.conversation_uuid
                LEFT JOIN projects p ON p.uuid = c.project_uuid
                WHERE m.text LIKE ? {flt}"""
            rows = self.db.execute(f"""
                SELECT m.uuid, m.conversation_uuid, m.sender, m.created_at, m.seq,
                       c.name AS conv_name, c.updated_at AS conv_updated, c.project_uuid,
                       p.name AS project_name, m.text, 0 AS rank
                {base} ORDER BY {"m.seq" if order == "seq" else "m.created_at DESC"} LIMIT ? OFFSET ?""",
                [like, *params, limit, offset]).fetchall()
            total = self.db.execute(f"SELECT COUNT(*) {base}", [like, *params]).fetchone()[0]
            per_conv = {r[0]: r[1] for r in self.db.execute(
                f"SELECT m.conversation_uuid, COUNT(*) {base} GROUP BY m.conversation_uuid", [like, *params])}
            hits = self._with_snippets(rows, q)
            title_rows = self.db.execute("""
                SELECT c.uuid, c.name, c.updated_at, c.project_uuid, p.name AS project_name, c.message_count, c.summary
                FROM conversations c LEFT JOIN projects p ON p.uuid = c.project_uuid
                WHERE (c.name LIKE ? OR c.summary LIKE ?) AND c.uuid NOT LIKE 'memory:%' ORDER BY c.updated_at DESC LIMIT 20""", [like, like]).fetchall()
        return {"query": q, "total": total, "hits": hits, "per_conv": per_conv,
                "titles": [dict(r) for r in title_rows]}

    @staticmethod
    def _with_snippets(rows, q):
        terms = query_terms(q)
        hits = []
        for r in rows:
            h = dict(r)
            h["snippet"] = make_snippet(h.pop("text") or "", terms)
            hits.append(h)
        return hits

    def recent(self, project=None, limit=60):
        flt, params = self._filters(project, None, None, None)
        rows = self.db.execute(f"""
            SELECT c.uuid, c.name, c.updated_at, c.project_uuid, p.name AS project_name, c.message_count, c.summary
            FROM conversations c LEFT JOIN projects p ON p.uuid = c.project_uuid
            WHERE c.uuid NOT LIKE 'doc:%' {flt} ORDER BY c.updated_at DESC LIMIT ?""", [*params, limit]).fetchall()
        return {"query": "", "total": 0, "hits": [], "per_conv": {}, "titles": [dict(r) for r in rows]}

    def conversation(self, uuid):
        c = self.db.execute("""
            SELECT c.*, p.name AS project_name FROM conversations c
            LEFT JOIN projects p ON p.uuid = c.project_uuid WHERE c.uuid=?""", (uuid,)).fetchone()
        if not c:
            return None
        msgs = self.db.execute(
            "SELECT uuid, sender, created_at, seq, text FROM messages WHERE conversation_uuid=? ORDER BY seq",
            (uuid,)).fetchall()
        return {"conversation": dict(c), "messages": [dict(m) for m in msgs]}

    def assign_project(self, conversation_uuids, project_uuid):
        """Manually put chats in a project (or none). Remembered across re-imports."""
        if isinstance(conversation_uuids, str):
            conversation_uuids = [conversation_uuids]
        with self.lock:
            project_uuid = project_uuid or None
            for cid in conversation_uuids:
                if not cid or cid.startswith(("memory:", "doc:")):
                    continue
                self.db.execute("INSERT OR REPLACE INTO project_overrides VALUES(?,?)", (cid, project_uuid))
                self.db.execute("UPDATE conversations SET project_uuid=? WHERE uuid=?", (project_uuid, cid))
            self.db.commit()

    def memories(self):
        """Everything from the memories export, grouped for browsing:
        general summary + top-level files, then one group per project."""
        rows = self.db.execute("""
            SELECT c.uuid, c.name, c.updated_at, c.project_uuid, p.name AS project_name, m.text
            FROM conversations c JOIN messages m ON m.conversation_uuid = c.uuid
            LEFT JOIN projects p ON p.uuid = c.project_uuid
            WHERE c.uuid LIKE 'memory:%' ORDER BY c.uuid""").fetchall()
        general = {"summary": None, "files": []}
        projects = {}
        for r in rows:
            uid = r["uuid"]
            entry = {"path": uid.split(":", 2)[-1] if uid.startswith("memory:file:") else None,
                     "content": r["text"], "updated_at": r["updated_at"]}
            if uid == "memory:general":
                general["summary"] = r["text"]
            elif r["project_uuid"]:
                g = projects.setdefault(r["project_uuid"], {
                    "uuid": r["project_uuid"],
                    "name": r["project_name"] or f"Project {r['project_uuid'][:8]}…",
                    "summary": None, "files": []})
                if uid.startswith("memory:project:"):
                    g["summary"] = r["text"]
                else:
                    g["files"].append(entry)
            else:
                general["files"].append(entry)
        return {"general": general,
                "projects": sorted(projects.values(), key=lambda g: g["name"].lower())}

    def context_for(self, question, project=None, budget=60000):
        """Collect the most relevant excerpts for the Ask feature."""
        res = self.search(question, project=project, limit=40, include_memory=True) if self.fts else None
        if not res or not res["hits"]:
            # loosen: OR the tokens
            words = [w for w in re.findall(r"\w+", question) if len(w) > 2]
            if not words:
                return "", []
            res = self.search(" OR ".join(words), project=project, limit=40, include_memory=True)
        chunks, used, seen = [], 0, []
        for h in res["hits"]:
            m = self.db.execute("SELECT text FROM messages WHERE uuid=?", (h["uuid"],)).fetchone()
            text = (m["text"] if m else "")[:2500]
            block = (f"### Conversation: {h['conv_name']} (project: {h['project_name'] or 'none'}, "
                     f"{(h['created_at'] or '')[:10]}, id {h['conversation_uuid']})\n"
                     f"[{h['sender']}]: {text}\n")
            if used + len(block) > budget:
                break
            chunks.append(block)
            used += len(block)
            if h["conversation_uuid"] not in [s["uuid"] for s in seen]:
                seen.append({"uuid": h["conversation_uuid"], "name": h["conv_name"]})
        return "\n".join(chunks), seen


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def fold(text):
    """Strip accents/diacritics from any script (Greek tonos included) and lowercase,
    so 'συνόρασις' matches 'Συνορασις'. Used for the FTS index and for queries."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFD", text)
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return unicodedata.normalize("NFC", stripped).lower().replace("ς", "σ")


def fold_aligned(text):
    """Like fold() but guaranteed to keep the same character positions as the
    original text, so match offsets found in the folded string can be applied
    to the original."""
    out = []
    for ch in text:
        f = fold(ch)
        out.append(f if len(f) == 1 else ch)
    return "".join(out)


def query_terms(q):
    """The plain search terms in a query (folded), for highlighting."""
    terms = []
    for t in re.findall(r'"[^"]*"|\S+', q):
        if t in ("OR", "AND", "NOT"):
            continue
        t = fold(t.strip('"').rstrip("*").strip())
        if t:
            terms.append(t)
    return terms


def make_snippet(text, terms, width=260):
    """Window of the original text around the first match, with MARK_OPEN /
    MARK_CLOSE around every match inside the window."""
    f = fold_aligned(text)
    ranges = []
    for t in terms:
        start = 0
        while True:
            i = f.find(t, start)
            if i < 0:
                break
            ranges.append([i, i + len(t)])
            start = i + len(t)
    ranges.sort()
    merged = []
    for r in ranges:
        if merged and r[0] <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], r[1])
        else:
            merged.append(r)
    if not merged:
        return text[:width].replace("\n", " ") + (" …" if len(text) > width else "")
    s = max(0, merged[0][0] - width // 3)
    if s > 0:
        sp = text.rfind(" ", 0, s)
        if sp > 0 and s - sp < 40:
            s = sp + 1
    e = min(len(text), s + width)
    parts, pos = [], s
    for a, b in merged:
        if b <= s:
            continue
        if a >= e:
            break
        a, b = max(a, s), min(b, e)
        parts.append(text[pos:a])
        parts.append(MARK_OPEN + text[a:b] + MARK_CLOSE)
        pos = b
    parts.append(text[pos:e])
    snip = "".join(parts).replace("\n", " ")
    return ("… " if s > 0 else "") + snip + (" …" if e < len(text) else "")


def classify_json(raw):
    """Work out what an export JSON file holds by looking at its content, not its
    name: ("conversations", list) | ("projects", list) | ("manifest", dict) | (None, None).
    Accepts a JSON array, a wrapper object holding one, or JSON Lines."""
    text = raw.decode("utf-8-sig", "replace")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = []
        for line in text.splitlines():
            line = line.strip()
            if line:
                try:
                    data.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    if isinstance(data, dict):
        if "data_files" in data and "export_url" in json.dumps(data.get("data_files", []))[:2000]:
            return "manifest", data
        if "memory_files" in data or "conversations_memory" in data or "project_memories" in data:
            return "memories", data
        for key in ("conversations", "projects", "data", "items", "results"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [data]
    if not isinstance(data, list):
        return None, None
    items = [d for d in data if isinstance(d, dict)]
    if not items:
        return None, None
    sample = items[0]
    if "chat_messages" in sample or "messages" in sample and "uuid" in sample:
        return "conversations", items
    if "docs" in sample or "prompt_template" in sample or "is_starter_project" in sample:
        return "projects", items
    return None, None


def memory_entries(mem):
    """Turn a memories export into conversation-shaped entries so they index and
    search like everything else. Each becomes one 'conversation' with a single
    message from sender 'memory'; uuids start with 'memory:'."""
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    out = []

    def add(uid, name, text, project=None, when=None):
        text = (text or "").strip()
        if not text:
            return
        when = when or now
        out.append({"uuid": uid, "name": name, "created_at": when, "updated_at": when,
                    "project_uuid": project,
                    "chat_messages": [{"uuid": uid + ":0", "sender": "memory", "created_at": when, "text": text}]})

    add("memory:general", "Memory · general summary", mem.get("conversations_memory"))
    for pid, text in (mem.get("project_memories") or {}).items():
        add(f"memory:project:{pid}", "Memory · project summary", text, pid)
    for f in mem.get("memory_files") or []:
        if not isinstance(f, dict) or not f.get("path"):
            continue
        path = f["path"]
        m = re.match(r"/projects/([^/]+)/", path)
        add(f"memory:file:{path}", f"Memory · {path}", f.get("content"),
            m.group(1) if m else None, f.get("updated_at"))
    return out


def project_of(conv):
    for k in ("project_uuid", "project_id"):
        if conv.get(k):
            return conv[k]
    p = conv.get("project")
    if isinstance(p, dict):
        return p.get("uuid") or p.get("id")
    if isinstance(p, str) and p:
        return p
    return None


def message_text(m):
    parts = []
    if m.get("text"):
        parts.append(m["text"])
    else:
        for block in m.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                parts.append(block["text"])
    for a in m.get("attachments") or []:
        if isinstance(a, dict) and a.get("extracted_content"):
            parts.append(f"[attachment: {a.get('file_name', '')}]\n{a['extracted_content']}")
    return "\n".join(parts).strip()


def build_match(q, force_quote=False):
    """Turn a user query into an FTS5 MATCH expression.
    Supports "quoted phrases", OR / NOT, and trailing * for prefix search."""
    tokens = re.findall(r'"[^"]*"|\S+', q)
    out = []
    for t in tokens:
        if not force_quote:
            if t.startswith('"') and t.endswith('"') and len(t) > 2:
                out.append('"' + fold(t[1:-1]).replace('"', "") + '"')
                continue
            if t in ("OR", "NOT", "AND"):
                out.append(t)
                continue
        t = fold(t.replace('"', ""))
        prefix = t.endswith("*") and not force_quote
        t = t.rstrip("*")
        if not t:
            continue
        out.append(f'"{t}"' + ("*" if prefix else ""))
    return " ".join(out) or '""'


def call_claude(api_key, model, system, user, max_tokens=2000):
    body = json.dumps({
        "model": model, "max_tokens": max_tokens, "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode("utf-8")
    req = urllib.request.Request(API_URL, data=body, method="POST", headers={
        "content-type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    })
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"API error {e.code}: {e.read().decode('utf-8', 'replace')[:500]}")
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


# ----------------------------------------------------------------------------
# Imports folder watcher
# ----------------------------------------------------------------------------

MANIFEST_CATEGORIES = ("conversations", "projects", "memories")  # the zips the index needs


def download_manifest(manifest, imports_dir: Path):
    """Download the data zips listed in an export manifest into the imports folder.
    Each export_url works only once, so a file that already exists is never re-fetched."""
    results = []
    for f in manifest.get("data_files") or []:
        cat, url = f.get("category"), f.get("export_url")
        if cat not in MANIFEST_CATEGORIES or not url:
            continue
        name = os.path.basename(f.get("filename") or f"{cat}-{int(f.get('part') or 0):03d}.zip")
        name = re.sub(r"[^\w.\-]+", "_", name)
        dest = imports_dir / name
        if dest.exists():
            results.append({"file": name, "skipped": "already downloaded"})
            continue
        tmp = imports_dir / (name + ".part")
        print(f"Downloading {name} …")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Re-Search/1.0"})
            with urllib.request.urlopen(req, timeout=600) as r, open(tmp, "wb") as out:
                shutil.copyfileobj(r, out, 1024 * 1024)
            if not zipfile.is_zipfile(tmp):
                tmp.unlink(missing_ok=True)
                raise RuntimeError("the link returned a web page rather than a zip, so it probably "
                                   "requires being signed in; download it in your browser and drop it here")
            tmp.rename(dest)
            results.append({"file": name, "downloaded": dest.stat().st_size})
            print(f"Downloaded {name} ({dest.stat().st_size // 1024} KB)")
        except urllib.error.HTTPError as e:
            tmp.unlink(missing_ok=True)
            if e.code in (401, 403):
                msg = "the link requires being signed in; download it in your browser and drop it here"
            elif e.code in (404, 410):
                msg = "the link has already been used or has expired; request a new export"
            else:
                msg = f"HTTP {e.code}"
            results.append({"file": name, "error": msg})
            print(f"Failed to download {name}: {msg}")
        except Exception as e:  # noqa
            tmp.unlink(missing_ok=True)
            results.append({"file": name, "error": str(e)})
            print(f"Failed to download {name}: {e}")
    return results


def scan_imports(store: Store, folder: Path, force=False, _depth=0):
    results = []
    downloaded = False
    for f in sorted(folder.iterdir()):
        if f.suffix.lower() not in (".zip", ".json", ".jsonl") or not f.is_file():
            continue
        st = f.stat()
        key = f"{f.name}:{st.st_size}:{int(st.st_mtime)}"
        if not force and store.import_seen(key):
            continue
        try:
            if f.suffix.lower() == ".json":
                kind, data = classify_json(f.read_bytes())
                if kind == "manifest":
                    dl = download_manifest(data, folder)
                    store.record_import(key, f"{f.name} (manifest)", 0, 0)
                    results.append({"file": f.name, "manifest": dl})
                    downloaded = downloaded or any("downloaded" in d for d in dl)
                    continue
            n_conv, n_msg = store.ingest_file(f)
            store.record_import(key, f.name, n_conv, n_msg)
            results.append({"file": f.name, "conversations": n_conv, "messages": n_msg})
            print(f"Imported {f.name}: {n_conv} conversations updated, {n_msg} messages")
        except Exception as e:  # noqa
            # remember the failure so the watcher doesn't retry it every 15 s
            store.record_import(key, f"{f.name} (skipped: {e})", 0, 0)
            results.append({"file": f.name, "error": str(e)})
            print(f"Failed to import {f.name}: {e}")
    if downloaded and _depth == 0:
        results.extend(scan_imports(store, folder, False, _depth=1))  # index what was just fetched
    return results


def watcher(store, folder, stop):
    while not stop.is_set():
        try:
            scan_imports(store, folder)
        except Exception as e:  # noqa
            print("watcher error:", e)
        stop.wait(WATCH_INTERVAL)


# ----------------------------------------------------------------------------
# HTTP server
# ----------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    store: Store = None
    imports_dir: Path = None

    def log_message(self, fmt, *args):  # quieter console
        if "/api/" not in (args[0] if args else ""):
            return

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        s = self.store
        try:
          with s.lock:
            if u.path == "/":
                data = HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif u.path == "/api/search":
                self._json(s.search(qs.get("q", ""), qs.get("project") or None, qs.get("sender") or None,
                                    qs.get("from") or None, qs.get("to") or None,
                                    min(int(qs.get("limit", 60)), 1000), int(qs.get("offset", 0)),
                                    conversation=qs.get("conversation") or None,
                                    order=qs.get("order") or "rank"))
            elif u.path.startswith("/api/conversation/"):
                res = s.conversation(u.path.rsplit("/", 1)[1])
                self._json(res if res else {"error": "not found"}, 200 if res else 404)
            elif u.path == "/api/projects":
                self._json(s.projects())
            elif u.path == "/api/memories":
                self._json(s.memories())
            elif u.path == "/api/stats":
                self._json(s.stats())
            elif u.path == "/api/settings":
                key = s.get_setting("api_key", "")
                self._json({"has_key": bool(key), "key_hint": (key[:7] + "…" + key[-4:]) if key else "",
                            "model": s.get_setting("model", DEFAULT_MODEL)})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        s = self.store
        try:
          if u.path == "/api/ask":
                body = json.loads(self._body() or b"{}")
                question = (body.get("question") or "").strip()
                if not question:
                    return self._json({"error": "empty question"}, 400)
                with s.lock:
                    key = s.get_setting("api_key", "")
                    model = s.get_setting("model", DEFAULT_MODEL)
                    if not key:
                        return self._json({"error": "No API key set. Add one under Settings."}, 400)
                    ctx, sources = s.context_for(question, body.get("project") or None)
                if not ctx:
                    return self._json({"answer": "Nothing in the index matched that question.", "sources": []})
                system = ("You answer questions about the user's own archived chat history with Claude. "
                          "Use only the excerpts provided. Cite the conversation titles you drew on. "
                          "If the excerpts do not answer the question, say so plainly.")
                user = f"Excerpts from my chat archive:\n\n{ctx}\n\n---\nQuestion: {question}"
                answer = call_claude(key, model, system, user)  # outside the lock: slow network call
                return self._json({"answer": answer, "sources": sources})
          with s.lock:
            if u.path == "/api/import":
                name = urllib.parse.unquote(self.headers.get("X-Filename") or "upload.zip")
                name = re.sub(r"[^\w.\-]+", "_", os.path.basename(name)) or "upload.zip"
                dest = self.imports_dir / name
                i = 1
                while dest.exists():
                    dest = self.imports_dir / f"{Path(name).stem}_{i}{Path(name).suffix}"
                    i += 1
                dest.write_bytes(self._body())
                self._json({"results": scan_imports(s, self.imports_dir)})
            elif u.path == "/api/quit":
                self._json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                if UI["webview"]:
                    try:
                        UI["webview"].windows[0].destroy()
                    except Exception:  # noqa
                        pass
            elif u.path == "/api/rescan":
                body = json.loads(self._body() or b"{}")
                self._json({"results": scan_imports(s, self.imports_dir, force=bool(body.get("force")))})
            elif u.path == "/api/assign":
                body = json.loads(self._body() or b"{}")
                uuids = body.get("uuids") or ([body["uuid"]] if body.get("uuid") else [])
                if not uuids:
                    return self._json({"error": "missing uuid"}, 400)
                s.assign_project(uuids, body.get("project") or None)
                self._json({"ok": True})
            elif u.path == "/api/settings":
                body = json.loads(self._body() or b"{}")
                if "api_key" in body:
                    s.set_setting("api_key", body["api_key"].strip())
                if "model" in body:
                    s.set_setting("model", body["model"].strip() or DEFAULT_MODEL)
                self._json({"ok": True})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa
            self._json({"error": str(e)}, 500)


# ----------------------------------------------------------------------------
# UI (single page, no external assets)
# ----------------------------------------------------------------------------

HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Re-Search</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#141413;--panel:#1c1b1a;--fg:#e8e6e1;--muted:#8f8b84;--acc:#d97757;--border:#2c2a28;--mark:#6b4423;--human:#25322e;--asst:#1c1b1a}
*{box-sizing:border-box}
body{margin:0;font:14px/1.5 -apple-system,"Segoe UI",system-ui,sans-serif;background:var(--bg);color:var(--fg);height:100vh;display:grid;grid-template-columns:minmax(380px,44%) 1fr}
@media(max-width:900px){body{grid-template-columns:1fr;grid-template-rows:auto 1fr}}
#left,#right{display:flex;flex-direction:column;min-height:0}
#left{border-right:1px solid var(--border)}
header{padding:14px 16px 10px;border-bottom:1px solid var(--border)}
h1{font-size:15px;margin:0 0 10px;display:flex;align-items:center;justify-content:space-between}
h1 small{color:var(--muted);font-weight:normal}
input,select,button,textarea{font:inherit;color:var(--fg);background:var(--panel);border:1px solid var(--border);border-radius:6px;padding:7px 9px}
input:focus,select:focus,textarea:focus{outline:1px solid var(--acc)}
#q{width:100%;font-size:16px;padding:10px 12px}
.filters{display:flex;gap:6px;margin-top:8px;flex-wrap:wrap}
.filters select,.filters input{flex:1;min-width:110px;font-size:12px;padding:5px 7px}
#status{color:var(--muted);font-size:12px;margin-top:6px}
#results{overflow:auto;padding:8px;flex:1}
.conv{border:1px solid var(--border);border-radius:8px;margin-bottom:8px;background:var(--panel);cursor:pointer}
.conv:hover{border-color:var(--acc)}
.conv.active{border-color:var(--acc);box-shadow:0 0 0 1px var(--acc)}
.conv .head{padding:8px 12px;display:flex;justify-content:space-between;gap:8px}
.conv .title{font-weight:600}
.conv .sel{margin:0 8px 0 0;vertical-align:middle;accent-color:var(--acc)}
.conv .meta{color:var(--muted);font-size:12px;white-space:nowrap}
.conv .proj{display:inline-block;font-size:11px;color:var(--acc);border:1px solid var(--acc);border-radius:10px;padding:0 7px;margin-left:6px;vertical-align:middle}
.snip{padding:6px 12px 8px;border-top:1px solid var(--border);color:#cfcbc3;font-size:13px}
.snip.hit:hover{background:#26241f}
.snip.morelink{color:var(--acc);cursor:pointer}
.msg.target{outline:2px solid var(--acc);outline-offset:2px}
.snip .who{color:var(--muted);font-size:11px;text-transform:uppercase;margin-right:6px}
mark{background:var(--mark);color:#ffe4c9;padding:0 2px;border-radius:2px}
#right{overflow:hidden}
nav{display:flex;border-bottom:1px solid var(--border)}
nav button{background:none;border:0;border-bottom:2px solid transparent;border-radius:0;padding:10px 16px;color:var(--muted)}
nav button.on{color:var(--fg);border-bottom-color:var(--acc)}
.pane{display:none;overflow:auto;padding:16px;flex:1}
.pane.on{display:block}
#convhead{position:sticky;top:-16px;background:var(--bg);padding:16px 0 10px;margin-top:-16px;border-bottom:1px solid var(--border);margin-bottom:12px}
#convhead h2{font-size:16px;margin:0 0 4px}
#convhead a{color:var(--acc)}
.msg{padding:10px 14px;border-radius:8px;margin-bottom:10px;border:1px solid var(--border);white-space:pre-wrap;word-break:break-word}
.msg.human{background:var(--human)}
.msg.assistant{background:var(--asst)}
.msg .who{font-size:11px;color:var(--muted);text-transform:uppercase;margin-bottom:4px;display:block}
.msg{position:relative}
.msg .copy{position:absolute;top:6px;right:8px;font-size:11px;padding:2px 8px;color:var(--muted);background:var(--bg);border:1px solid var(--border);border-radius:5px;cursor:pointer;opacity:0;transition:opacity .15s}
.msg:hover .copy{opacity:1}
.msg .copy.done,.copyall.done{color:var(--acc);border-color:var(--acc)}
.copyall{font-size:11px;padding:2px 8px;cursor:pointer;color:var(--muted)}
#conv,#memtree,.md,.snip{user-select:text}
#ctx{position:fixed;z-index:1000;background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:4px;box-shadow:0 6px 24px rgba(0,0,0,.5);min-width:160px;display:none}
#ctx div{padding:6px 12px;border-radius:5px;cursor:pointer;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:320px}
#ctx div:hover{background:var(--bg)}
.drop{border:2px dashed var(--border);border-radius:10px;padding:26px;text-align:center;color:var(--muted);margin-bottom:14px}
.drop.over{border-color:var(--acc);color:var(--fg)}
table{border-collapse:collapse;width:100%;font-size:13px}
td,th{text-align:left;padding:5px 8px;border-bottom:1px solid var(--border)}
th{color:var(--muted);font-weight:normal}
.row{display:flex;gap:8px;margin-bottom:10px;align-items:center}
.row label{min-width:70px;color:var(--muted)}
.row input{flex:1}
#answer{white-space:pre-wrap;background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:12px;margin-top:12px;min-height:40px}
.btn{background:var(--acc);border-color:var(--acc);color:#fff;cursor:pointer}
.btn:disabled{opacity:.5}
.hint{color:var(--muted);font-size:12px}
.mgroup{border:1px solid var(--border);border-radius:8px;background:var(--panel);margin-bottom:12px}
.mgroup>h3{margin:0;padding:10px 14px;font-size:14px;cursor:pointer;display:flex;justify-content:space-between}
.mgroup>h3 small{color:var(--muted);font-weight:normal}
.mgroup.closed>.mbody{display:none}
.mnote{border-top:1px solid var(--border)}
.mnote>h4{margin:0;padding:8px 14px;font-size:13px;font-weight:normal;cursor:pointer;color:var(--acc)}
.mnote>h4 small{color:var(--muted);float:right}
.mnote.closed>.md{display:none}
.md{padding:4px 18px 12px;white-space:pre-wrap;word-break:break-word;font-size:13px;line-height:1.55}
.md h1,.md h2,.md h3{font-size:13px;margin:10px 0 2px;color:var(--fg)}
.md li{margin-left:14px}
.md p{margin:4px 0}
code{background:var(--panel);padding:1px 5px;border-radius:4px}
</style></head><body>
<div id="left">
 <header>
  <h1>Re-Search <small id="count"></small></h1>
  <input id="q" placeholder='Search all conversations…  ("exact phrase", word OR word, NOT word, prefix*)' autofocus>
  <div class="filters">
   <select id="project"><option value="">All projects</option></select>
   <select id="sender"><option value="">Anyone</option><option value="human">Me</option><option value="assistant">Claude</option><option value="document">Project files</option></select>
   <input id="from" type="date" title="From"><input id="to" type="date" title="To">
  </div>
  <div id="status"></div>
 </header>
 <div id="bulk" style="display:none;padding:8px 12px;border-bottom:1px solid var(--border);background:var(--panel);font-size:13px;align-items:center;gap:8px"><span id="bulkn"></span> · assign to <select id="bulkproj" style="font-size:12px;padding:2px 6px"><option value="">none</option></select> <button class="btn" id="bulkgo" style="padding:3px 10px">Apply</button> <a href="#" id="bulkclear" style="color:var(--muted)">clear</a></div>
 <div id="results"></div>
</div>
<div id="ctx"></div>
<div id="right">
 <nav><button data-p="conv" class="on">Conversation</button><button data-p="ask">Ask</button><button data-p="import">Imports</button><button data-p="memories">Memories</button><button data-p="settings">Settings</button></nav>
 <div id="conv" class="pane on"><p class="hint">Search on the left, then click a conversation to read it here. Matches are highlighted; the link opens the original on claude.ai.</p><p class="hint">Claude's export doesn't record which project a chat belongs to, so project counts start at 0. Assign chats with the "in project" selector at the top of a conversation, or tick several in the results list and use the bar that appears. Assignments are kept across re-imports.</p></div>
 <div id="ask" class="pane">
  <p class="hint">Asks Claude (via the API, with your key) a question over the excerpts that best match it. Only those excerpts are sent.</p>
  <textarea id="question" rows="3" style="width:100%" placeholder="e.g. What did I decide about the holiday dates?"></textarea>
  <div class="row" style="margin-top:8px"><button class="btn" id="askbtn">Ask</button><span class="hint">Uses the project filter from the left.</span></div>
  <div id="answer"></div><div id="sources" class="hint"></div>
 </div>
 <div id="import" class="pane">
  <div class="drop" id="drop">Drop the export <b>manifest-….json</b> here — the app downloads the zips and imports them itself — or drop the zips directly, or <label style="color:var(--acc);cursor:pointer"><input type="file" id="file" multiple style="display:none">choose a file</label></div>
  <div id="importlog"></div>
  <p class="hint">Files are also picked up automatically from <code id="ddir"></code> every 15 s. Re-importing merges: newer versions of a chat replace older ones, nothing is lost.</p>
  <div class="row"><button id="rescan">Rescan folder</button><button id="reimport" title="Re-read every file in the folder">Force re-import all</button></div>
  <div id="stats"></div>
 </div>
 <div id="memories" class="pane">
  <p class="hint">What Claude remembers about you, from the memories zip in your export: the general summary and files, then one section per project. Click a heading to expand it.</p>
  <input id="memq" placeholder="Filter memory notes…" style="width:100%;margin-bottom:12px">
  <div id="memtree"></div>
 </div>
 <div id="settings" class="pane">
  <p class="hint">Only needed for the Ask tab. Get a key from the Claude Console; usage is billed to that account. The key is stored locally in the index database.</p>
  <div class="row"><label>API key</label><input id="apikey" type="password" placeholder="sk-ant-…"><span id="keyhint" class="hint"></span></div>
  <div class="row"><label>Model</label><input id="model"></div>
  <div class="row"><button class="btn" id="savesettings">Save</button><span id="savemsg" class="hint"></span></div>
  <hr style="border:0;border-top:1px solid var(--border);margin:18px 0">
  <div class="row"><button id="quit">Quit Re-Search</button><span class="hint">Stops the app. Closing the window does the same.</span></div>
 </div>
</div>
<script>
const $=s=>document.querySelector(s);
const esc=s=>(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const mark=s=>esc(s).replace(/\u0001/g,'<mark>').replace(/\u0002/g,'</mark>');
const fmtDate=s=>s?s.slice(0,10):'';
let current=null,timer=null,lastQuery='',projectList=[];const selected=new Set();const cards=new Map(),expanded=new Set();
const api=(p,o)=>fetch(p,o).then(r=>r.json());

document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>show(b.dataset.p));
function show(p){document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('on',b.dataset.p===p));document.querySelectorAll('.pane').forEach(x=>x.classList.toggle('on',x.id===p));if(p==='import')loadStats();if(p==='settings')loadSettings();if(p==='memories')loadMemories();}

// ---- Memories tab ----
let memData=null;
function md(text,filter){let h=esc(text||'');if(filter){const f=foldStr(h);let out='',pos=0,i=0;const ff=foldStr(filter);while((i=f.indexOf(ff,pos))>=0){out+=h.slice(pos,i)+'<mark>'+h.slice(i,i+ff.length)+'</mark>';pos=i+ff.length;}h=out+h.slice(pos);}
 return h.split('\n').map(l=>{if(/^#{1,3} /.test(l))return `<h3>${l.replace(/^#+ /,'')}</h3>`;if(/^\s*[-*] /.test(l))return `<li>${l.replace(/^\s*[-*] /,'')}</li>`;if(l.trim()==='---')return '<hr style="border:0;border-top:1px solid var(--border)">';return l;}).join('\n').replace(/\*\*(.+?)\*\*/g,'<b>$1</b>').replace(/\[\[(.+?)\]\]/g,'<i>$1</i>');}
function memMatch(t,f){return !f||foldStr(t||'').includes(foldStr(f));}
function note(title,content,updated,filter,open){if(!memMatch(title+'\n'+content,filter))return '';return `<div class="mnote${open?'':' closed'}"><h4>${esc(title)}<small>${updated?fmtDate(updated)+' · ':''}<button class="copyall memcopy" data-text="${esc(content)}">copy</button></small></h4><div class="md">${md(content,filter)}</div></div>`;}
function group(title,g,filter){const f=filter.trim();const notes=[g.summary?note('Summary',g.summary,null,f,!!f):'',...g.files.map(x=>note(x.path,x.content,x.updated_at,f,!!f))].join('');if(!notes)return '';const n=(g.summary?1:0)+g.files.length;return `<div class="mgroup${f?'':' closed'}"><h3>${esc(title)}<small>${n} note${n===1?'':'s'}</small></h3><div class="mbody">${notes}</div></div>`;}
function renderMemories(){if(!memData)return;const f=$('#memq').value;const html=[group('General',memData.general,f),...memData.projects.map(p=>group(p.name,p,f))].join('');$('#memtree').innerHTML=html||'<p class="hint">'+(memData.general.summary||memData.general.files.length||memData.projects.length?'Nothing matches.':'No memories imported yet — drop your export manifest (or memories-000.zip) on the Imports tab.')+'</p>';
 document.querySelectorAll('.mgroup>h3').forEach(h=>h.onclick=()=>h.parentElement.classList.toggle('closed'));document.querySelectorAll('.mnote>h4').forEach(h=>h.onclick=e=>{if(e.target.closest('.memcopy'))return;h.parentElement.classList.toggle('closed');});
 document.querySelectorAll('.memcopy').forEach(b=>b.onclick=e=>{e.stopPropagation();copyText(b.dataset.text,b);});}
async function loadMemories(){memData=await api('/api/memories');renderMemories();}
let memTimer=null;

async function loadProjects(){const ps=await api('/api/projects');projectList=ps;const sel=$('#project');const cur=sel.value;sel.innerHTML='<option value="">All projects</option><option value="__none__">No project</option>'+ps.map(p=>`<option value="${esc(p.uuid)}">${esc(p.name)} (${p.n})</option>`).join('');sel.value=cur;const b=$('#bulkproj');const bc=b.value;b.innerHTML='<option value="">none</option>'+ps.map(p=>`<option value="${esc(p.uuid)}">${esc(p.name)}</option>`).join('');b.value=bc;}
function updateBulk(){const n=selected.size;$('#bulk').style.display=n?'flex':'none';$('#bulkn').textContent=`${n} chat${n===1?'':'s'} selected`;}
function toggleSel(uuid,on){if(on)selected.add(uuid);else selected.delete(uuid);updateBulk();}

function params(){const p=new URLSearchParams();p.set('q',$('#q').value);p.set('project',$('#project').value);p.set('sender',$('#sender').value);p.set('from',$('#from').value);p.set('to',$('#to').value);return p;}

async function search(){lastQuery=$('#q').value.trim();$('#status').textContent='Searching…';const r=await api('/api/search?'+params());if(r.error){$('#status').textContent=r.error;return;}render(r);}

function render(r){const out=[];const byConv=new Map();cards.clear();expanded.clear();
 for(const h of r.hits){if(!byConv.has(h.conversation_uuid))byConv.set(h.conversation_uuid,{uuid:h.conversation_uuid,name:h.conv_name,updated:h.conv_updated,project:h.project_name,project_uuid:h.project_uuid,hits:[],total:(r.per_conv||{})[h.conversation_uuid]||0});byConv.get(h.conversation_uuid).hits.push(h);}
 for(const c of byConv.values())cards.set(c.uuid,c);
 if(r.titles.length){out.push(`<div class="hint" style="margin:4px 8px">${r.query?'Title matches':'Most recent conversations'}</div>`);for(const t of r.titles){if(byConv.has(t.uuid))continue;out.push(card(t.uuid,t.name,t.updated_at,t.project_name,[],t.message_count,t.summary));}}
 if(byConv.size)out.push(`<div class="hint" style="margin:8px 8px 4px">Message matches</div>`);
 for(const c of byConv.values())out.push(card(c.uuid,c.name,c.updated,c.project,c.hits,0,null,c.total));
 $('#results').innerHTML=out.join('')||'<p class="hint" style="padding:12px">No matches.</p>';
 $('#status').textContent=r.query?`${r.total} matching message${r.total===1?'':'s'} in ${byConv.size} conversation${byConv.size===1?'':'s'}${r.total>r.hits.length?' (showing top '+r.hits.length+')':''}`:'';
 if(r.query||r.titles.length){const shown=[...document.querySelectorAll('.conv .sel')].length;if(shown)$('#status').innerHTML+=` · <a href="#" id="selall" style="color:var(--muted)">select all ${shown} shown</a>`;const a=$('#selall');if(a)a.onclick=e=>{e.preventDefault();document.querySelectorAll('.conv .sel').forEach(cb=>{cb.checked=true;selected.add(cb.dataset.uuid);});updateBulk();};}
}
$('#results').onclick=e=>{const cb=e.target.closest('.sel');if(cb){toggleSel(cb.dataset.uuid,cb.checked);return;}
 const more=e.target.closest('.morelink');if(more){toggleMore(more.dataset.more);return;}
 const conv=e.target.closest('.conv');if(!conv)return;const hit=e.target.closest('.snip.hit');openConv(conv.dataset.uuid,hit?hit.dataset.msg:null);};
$('#bulkgo').onclick=async()=>{if(!selected.size)return;await api('/api/assign',{method:'POST',body:JSON.stringify({uuids:[...selected],project:$('#bulkproj').value})});selected.clear();updateBulk();await loadProjects();search();if(current)openConv(current);};
$('#bulkclear').onclick=e=>{e.preventDefault();selected.clear();updateBulk();document.querySelectorAll('.conv .sel').forEach(cb=>cb.checked=false);};
const isMem=u=>(u||'').startsWith('memory:');const isDoc=u=>(u||'').startsWith('doc:');
function card(uuid,name,updated,project,hits,n,summary,total){const mem=isMem(uuid),doc=isDoc(uuid),plain=!mem&&!doc;const open=expanded.has(uuid);const shown=open?hits:hits.slice(0,4);total=Math.max(total||0,hits.length);return `<div class="conv${uuid===current?' active':''}" data-uuid="${esc(uuid)}"><div class="head"><div>${plain?`<input type="checkbox" class="sel" data-uuid="${esc(uuid)}"${selected.has(uuid)?' checked':''} title="select for bulk project assignment">`:''}<span class="title">${esc(name)}</span>${mem?'<span class="proj" style="color:var(--muted);border-color:var(--muted)">memory</span>':''}${doc?'<span class="proj" style="color:var(--muted);border-color:var(--muted)">file</span>':''}${project?`<span class="proj">${esc(project)}</span>`:''}</div><div class="meta">${fmtDate(updated)}${n&&plain?` · ${n} msgs`:''}</div></div>${summary&&!hits.length?`<div class="snip hint" style="font-size:12px">${hl(summary.length>240?summary.slice(0,240)+'…':summary)}</div>`:''}${shown.map(h=>`<div class="snip hit" data-msg="${esc(h.uuid)}" title="Go to this point in the chat"><span class="who">${h.sender==='human'?'me':h.sender==='document'?'file':h.sender}</span>${mark(h.snippet)}</div>`).join('')}${total>4?`<div class="snip morelink" data-more="${esc(uuid)}">${open?'show fewer':`+${total-shown.length} more`}</div>`:''}</div>`;}
async function toggleMore(uuid){const c=cards.get(uuid);if(!c)return;if(expanded.has(uuid)){expanded.delete(uuid);}else{if(c.hits.length<c.total||!c.chrono){const p=params();p.set('conversation',uuid);p.set('limit','1000');p.set('order','seq');const r=await api('/api/search?'+p);if(!r.error){c.hits=r.hits;c.chrono=true;c.total=Math.max(c.total,r.hits.length);}}expanded.add(uuid);}
 const el=document.querySelector(`.conv[data-uuid="${CSS.escape(uuid)}"]`);if(el){const t=document.createElement('div');t.innerHTML=card(c.uuid,c.name,c.updated,c.project,c.hits,0,null,c.total);el.replaceWith(t.firstElementChild);}}

function foldChar(c){const f=c.normalize('NFD').replace(/[\u0300-\u036f]/g,'').normalize('NFC').toLowerCase().replace('ς','σ');return f.length===c.length?f:c;}
function foldStr(s){let o='';for(const c of s)o+=foldChar(c);return o;}
function terms(){const q=lastQuery.replace(/\b(OR|AND|NOT)\b/g,' ');const t=[];for(const m of q.matchAll(/"([^"]+)"|(\S+)/g)){let w=foldStr((m[1]||m[2]).replace(/"/g,'').replace(/\*$/,'').trim());if(w.length>1)t.push(w);}return t;}
function hl(text){const f=foldStr(text);let ranges=[];for(const t of terms()){let i=0;while((i=f.indexOf(t,i))>=0){ranges.push([i,i+t.length]);i+=t.length;}}
 ranges.sort((a,b)=>a[0]-b[0]);const merged=[];for(const r of ranges){if(merged.length&&r[0]<=merged[merged.length-1][1])merged[merged.length-1][1]=Math.max(merged[merged.length-1][1],r[1]);else merged.push(r);}
 let out='',pos=0;for(const [a,b] of merged){out+=esc(text.slice(pos,a))+'<mark>'+esc(text.slice(a,b))+'</mark>';pos=b;}return out+esc(text.slice(pos));}

// Right-click menu inside the app window (pywebview disables the built-in one).
const ctx=$('#ctx');
function hideCtx(){ctx.style.display='none';}
document.addEventListener('contextmenu',e=>{if(!window.pywebview)return;const t=e.target;if(t.closest('input,textarea'))return;
 const selText=(window.getSelection()||'').toString().trim();e.preventDefault();const items=[];
 if(selText){items.push(['Copy',()=>copyText(selText)]);const q=selText.length>40?selText.slice(0,40)+'…':selText;items.push([`Search for "${q}"`,()=>{$('#q').value=selText;show('conv');search();$('#q').focus();}]);}
 const msg=t.closest('.msg');if(msg){const b=msg.querySelector('.copy');if(b)items.push(['Copy this message',()=>b.click()]);}
 if(t.closest('#conv')&&$('#copyall'))items.push(['Copy whole chat',()=>$('#copyall').click()]);
 if(!items.length)return;ctx.innerHTML=items.map((it,i)=>`<div data-i="${i}">${esc(it[0])}</div>`).join('');
 ctx.querySelectorAll('div').forEach(d=>d.onclick=()=>{hideCtx();items[+d.dataset.i][1]();});
 ctx.style.display='block';const w=ctx.offsetWidth,h=ctx.offsetHeight;ctx.style.left=Math.min(e.clientX,innerWidth-w-8)+'px';ctx.style.top=Math.min(e.clientY,innerHeight-h-8)+'px';});
document.addEventListener('click',e=>{if(!e.target.closest('#ctx'))hideCtx();});
document.addEventListener('keydown',e=>{if(e.key==='Escape')hideCtx();});
window.addEventListener('blur',hideCtx);
async function copyText(text,btn){let ok=false;try{await navigator.clipboard.writeText(text);ok=true;}catch(e){const ta=document.createElement('textarea');ta.value=text;ta.style.position='fixed';ta.style.opacity='0';document.body.appendChild(ta);ta.select();try{ok=document.execCommand('copy');}catch(e2){}ta.remove();}
 if(btn){const old=btn.textContent;btn.textContent=ok?'copied':'copy failed';btn.classList.add('done');setTimeout(()=>{btn.textContent=old;btn.classList.remove('done');},1500);}return ok;}
async function openConv(uuid,msgUuid){current=uuid;document.querySelectorAll('.conv').forEach(el=>el.classList.toggle('active',el.dataset.uuid===uuid));show('conv');const r=await api('/api/conversation/'+uuid);if(r.error){$('#conv').innerHTML='<p>'+esc(r.error)+'</p>';return;}const c=r.conversation;
 const assign=(isMem(uuid)||isDoc(uuid))?'':` · in project: <select id="assign" style="font-size:12px;padding:2px 6px"><option value="">none</option>${projectList.map(p=>`<option value="${esc(p.uuid)}"${p.uuid===c.project_uuid?' selected':''}>${esc(p.name)}</option>`).join('')}</select>`;
 $('#conv').innerHTML=`<div id="convhead"><h2>${esc(c.name)}</h2>${c.summary?`<p class="hint" style="margin:0 0 6px">${esc(c.summary)}</p>`:''}<div class="hint">${(isMem(uuid)||isDoc(uuid))&&c.project_name?esc(c.project_name)+' · ':''}${isMem(uuid)?`memory note · ${fmtDate(c.updated_at)}`:isDoc(uuid)?`project file · ${fmtDate(c.created_at)}${c.project_uuid?` · <a href="https://claude.ai/project/${esc(c.project_uuid)}" target="_blank">open project ↗</a>`:''}`:`${fmtDate(c.created_at)} → ${fmtDate(c.updated_at)} · ${r.messages.length} messages · <a href="https://claude.ai/chat/${esc(uuid)}" target="_blank">open on claude.ai ↗</a>`}${c.project_uuid?` · <a href="https://claude.ai/project/${esc(c.project_uuid)}" target="_blank">project ↗</a>`:''}${assign} · <button class="copyall" id="copyall" title="Copy the whole conversation as plain text">Copy chat</button></div></div>`+r.messages.map(m=>`<div class="msg ${esc(m.sender)}" id="m-${esc(m.uuid)}"><button class="copy" data-msg="${esc(m.uuid)}" title="Copy this message">copy</button><span class="who">${m.sender==='human'?'me':m.sender==='document'?'file contents':esc(m.sender)} · ${esc((m.created_at||'').replace('T',' ').slice(0,16))}</span>${hl(m.text)}</div>`).join('');
 const who=x=>x==='human'?'Me':x==='assistant'?'Claude':x==='document'?'File':x;
 const asText=()=>r.messages.map(m=>`${who(m.sender)} · ${(m.created_at||'').replace('T',' ').slice(0,16)}\n${m.text}`).join('\n\n---\n\n');
 const chatHeader=`${c.name}\n${c.project_name?'Project: '+c.project_name+'\n':''}${c.summary?c.summary+'\n':''}\n`;
 $('#copyall').onclick=e=>copyText(chatHeader+asText(),e.target);
 $('#conv').querySelectorAll('.copy').forEach(b=>b.onclick=e=>{e.stopPropagation();const m=r.messages.find(x=>x.uuid===b.dataset.msg);if(m)copyText(m.text,b);});
 const sel=$('#assign');if(sel)sel.onchange=async()=>{await api('/api/assign',{method:'POST',body:JSON.stringify({uuid,project:sel.value})});await loadProjects();search();};
 let target=msgUuid?document.getElementById('m-'+msgUuid):null;
 if(target){const m=target.querySelector('mark');(m||target).scrollIntoView({block:'center'});target.classList.add('target');setTimeout(()=>target.classList.remove('target'),2500);}
 else{const first=$('#conv mark');if(first)first.scrollIntoView({block:'center'});}}

$('#q').oninput=()=>{clearTimeout(timer);timer=setTimeout(search,250)};
$('#memq').oninput=()=>{clearTimeout(memTimer);memTimer=setTimeout(renderMemories,200)};
$('#q').onkeydown=e=>{if(e.key==='Enter'){clearTimeout(timer);search();}};
for(const id of ['project','sender','from','to'])$('#'+id).onchange=search;

async function loadStats(){const s=await api('/api/stats');$('#ddir').textContent=s.data_dir+'\\imports';
 $('#stats').innerHTML=`<p>${s.conversations} conversations · ${s.messages} messages · ${s.projects} projects${s.memory_notes?` · ${s.memory_notes} memory notes`:''}${s.project_files?` · ${s.project_files} project files`:''}${s.unlinked?` · ${s.unlinked} chats not linked to a project`:''}${s.earliest?` · ${fmtDate(s.earliest)} → ${fmtDate(s.latest)}`:''}${s.fts?'':' · <b>FTS5 unavailable, using slow search</b>'}</p>`+(s.imports.length?`<table><tr><th>File</th><th>Imported</th><th>Chats</th><th>Msgs</th></tr>${s.imports.map(i=>`<tr><td>${esc(i.filename)}</td><td>${esc(i.imported_at.replace('T',' ').slice(0,16))}</td><td>${i.conversations}</td><td>${i.messages}</td></tr>`).join('')}</table>`:'<p class="hint">Nothing imported yet. Export your data from claude.ai (Settings → Privacy → Export data), then drop the zip above.</p>');
 $('#count').textContent=s.conversations?`${s.conversations} chats indexed`:'';}
function logResults(res){const lines=[];for(const r of res){if(r.manifest){lines.push(`<b>${esc(r.file)}</b>: manifest read`);for(const d of r.manifest)lines.push(d.downloaded!==undefined?`↓ ${esc(d.file)} downloaded (${Math.round(d.downloaded/1024)} KB)`:d.skipped?`· ${esc(d.file)}: ${esc(d.skipped)}`:`<span style="color:var(--acc)">✕ ${esc(d.file)}: ${esc(d.error)}</span>`);}else if(r.error)lines.push(`<span style="color:var(--acc)">✕ ${esc(r.file)}: ${esc(r.error)}</span>`);else lines.push(`✓ ${esc(r.file)}: ${r.conversations} conversations updated, ${r.messages} messages`);}$('#importlog').innerHTML=lines.length?`<div style="background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin-bottom:12px;font-size:13px">${lines.join('<br>')}</div>`:'';}
async function upload(files){const all=[];for(const f of files){$('#importlog').innerHTML=`<p>Importing ${esc(f.name)}… (a manifest downloads the zips first; large exports can take a few minutes)</p>`;const r=await fetch('/api/import',{method:'POST',headers:{'X-Filename':encodeURIComponent(f.name)},body:f}).then(r=>r.json());if(r.error)all.push({file:f.name,error:r.error});else all.push(...r.results);}logResults(all);memData=null;await Promise.all([loadStats(),loadProjects()]);search();}
$('#file').onchange=e=>upload(e.target.files);
const drop=$('#drop');drop.ondragover=e=>{e.preventDefault();drop.classList.add('over')};drop.ondragleave=()=>drop.classList.remove('over');drop.ondrop=e=>{e.preventDefault();drop.classList.remove('over');upload(e.dataTransfer.files)};
$('#rescan').onclick=async()=>{const r=await api('/api/rescan',{method:'POST',body:'{}'});logResults(r.results||[]);memData=null;await Promise.all([loadStats(),loadProjects()]);search();};
$('#reimport').onclick=async()=>{if(!confirm('Re-read every file in the imports folder?'))return;$('#stats').innerHTML='<p>Re-importing…</p>';await api('/api/rescan',{method:'POST',body:'{"force":true}'});await Promise.all([loadStats(),loadProjects()]);search();};

async function loadSettings(){const s=await api('/api/settings');$('#keyhint').textContent=s.has_key?'saved: '+s.key_hint:'no key saved';$('#model').value=s.model;}
$('#quit').onclick=async()=>{if(!confirm('Quit Re-Search?'))return;try{await api('/api/quit',{method:'POST',body:'{}'});}catch(e){}document.body.innerHTML='<p style="padding:40px;color:var(--muted)">Re-Search has stopped. You can close this window.</p>';};
$('#savesettings').onclick=async()=>{const body={model:$('#model').value};if($('#apikey').value)body.api_key=$('#apikey').value;await api('/api/settings',{method:'POST',body:JSON.stringify(body)});$('#apikey').value='';$('#savemsg').textContent='Saved.';loadSettings();};
$('#askbtn').onclick=async()=>{const q=$('#question').value.trim();if(!q)return;$('#askbtn').disabled=true;$('#answer').textContent='Thinking…';$('#sources').innerHTML='';const r=await api('/api/ask',{method:'POST',body:JSON.stringify({question:q,project:$('#project').value})});$('#askbtn').disabled=false;if(r.error){$('#answer').textContent=r.error;return;}$('#answer').textContent=r.answer;$('#sources').innerHTML=r.sources.length?'Drawn from: '+r.sources.map(s=>`<a href="#" onclick="openConv('${esc(s.uuid)}');return false" style="color:var(--acc)">${esc(s.name)}</a>`).join(' · '):'';};

loadProjects().then(search);loadStats();
</script></body></html>
"""

# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def open_ui(url, force_browser=False):
    """Show the interface. Preference: pywebview window > Edge/Chrome app window > browser tab.
    Returns the webview module when a native window will be used, else None."""
    if not force_browser:
        try:
            import webview  # pip install pywebview
            return webview
        except ImportError:
            pass
        if os.name == "nt":
            for exe in APP_BROWSERS:
                if os.path.exists(exe):
                    subprocess.Popen([exe, f"--app={url}", f"--window-size={WINDOW_SIZE[0]},{WINDOW_SIZE[1]}"])
                    return None
    webbrowser.open(url)
    return None


def main():
    ap = argparse.ArgumentParser(description="Local search over claude.ai exports")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true", help="start the server only; open the URL yourself")
    ap.add_argument("--browser", action="store_true", help="open in a normal browser tab even if pywebview is installed")
    ap.add_argument("--import", dest="import_path", help="import this zip/json before starting")
    ap.add_argument("--rebuild", action="store_true", help="delete the index and re-import everything")
    args = ap.parse_args()

    root = data_dir()
    if getattr(sys, "frozen", False) and (sys.stdout is None or sys.stderr is None):
        # windowed .exe has no console: keep a log file instead
        log = open(root / "log.txt", "a", buffering=1, encoding="utf-8")
        sys.stdout = sys.stderr = log
    imports_dir = root / "imports"
    db_path = root / "index.db"
    if args.rebuild:
        for p in (db_path, root / "index.db-wal", root / "index.db-shm"):
            if p.exists():
                p.unlink()
    store = Store(db_path)
    print(f"{APP_NAME}: data folder {root}")
    SCHEMA_VERSION = "3"
    if store.get_setting("schema") != SCHEMA_VERSION:
        if store.db.execute("SELECT COUNT(*) FROM imports").fetchone()[0]:
            print("Index built by an earlier version; re-reading imports once to pick up new fields …")
            scan_imports(store, imports_dir, force=True)
        store.set_setting("schema", SCHEMA_VERSION)
    if args.import_path:
        src = Path(args.import_path)
        if src.resolve().parent != imports_dir.resolve():
            shutil.copy(src, imports_dir / src.name)
    scan_imports(store, imports_dir)

    Handler.store, Handler.imports_dir = store, imports_dir
    stop = threading.Event()
    threading.Thread(target=watcher, args=(store, imports_dir, stop), daemon=True).start()
    srv = ThreadingHTTPServer((HOST, args.port), Handler)
    url = f"http://{HOST}:{args.port}/"
    print(f"Serving on {url}  (Ctrl+C to stop)")
    server_thread = threading.Thread(target=srv.serve_forever, daemon=True)
    server_thread.start()
    try:
        webview = open_ui(url, args.browser) if not args.no_browser else None
        if webview:
            UI["webview"] = webview
            webview.create_window(APP_NAME, url, width=WINDOW_SIZE[0], height=WINDOW_SIZE[1],
                                  min_size=(900, 600), text_select=True)
            webview.start()          # blocks until the window is closed
        else:
            while server_thread.is_alive():
                server_thread.join(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        srv.shutdown()
        srv.server_close()


if __name__ == "__main__":
    main()
