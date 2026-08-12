from __future__ import annotations

import base64
import contextlib
import datetime as dt
import fnmatch
import hashlib
import html
import hmac
import json
import os
import secrets
import signal
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
from email.message import EmailMessage
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
import mimetypes
import smtplib
from urllib.parse import urlencode, unquote, urlparse


SESSION_COOKIE = "daily_arxiv_session"
PBKDF2_ITERATIONS = 310_000
RATE_LIMIT_WINDOW_SECONDS = 15 * 60
RATE_LIMIT_MAX_FAILURES = 5
DEFAULT_SESSION_HOURS = 24
REMEMBER_SESSION_DAYS = 30
ALLOWED_PUBLIC_PATHS = {
    "/login.html",
    "/js/api.js",
    "/js/auth.js",
    "/assets/logo2-removebg-preview.png",
}
SETTINGS_SECRET_FIELDS = {
    "ai_api_key",
    "zotero_key",
    "smtp_password",
}


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def isoformat(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value else None


def parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    return dt.datetime.fromisoformat(value)


def split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def api_url(base_url: str, path: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not base.startswith(("https://", "http://")):
        raise ValueError("API Base must be an HTTP(S) URL")
    return f"{base}/{path.lstrip('/')}"


def read_json_response(response: Any) -> Any:
    raw = response.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def test_ai_configuration(
    api_key: str,
    base_url: str,
    model: str,
    opener: Any = None,
) -> dict[str, Any]:
    if not api_key or not base_url or not model:
        raise ValueError("OpenAI API Key, API Base and model are required")
    opener = opener or urllib.request.urlopen
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "Reply with OK."}],
        "max_tokens": 8,
    }).encode("utf-8")
    request = urllib.request.Request(
        api_url(base_url, "chat/completions"),
        method="POST",
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    with opener(request, timeout=20) as response:
        payload = read_json_response(response)
    choices = payload.get("choices") or []
    content = choices[0].get("message", {}).get("content", "") if choices else ""
    return {"ok": True, "model": str(payload.get("model") or model), "response": str(content)[:120]}


def test_zotero_configuration(zotero_id: str, zotero_key: str, opener: Any = None) -> dict[str, Any]:
    if not zotero_id or not zotero_key:
        raise ValueError("Zotero User ID and API Key are required")
    opener = opener or urllib.request.urlopen
    request = urllib.request.Request(
        f"https://api.zotero.org/users/{zotero_id}/items?{urlencode({'limit': 1, 'format': 'json'})}",
        headers={"Zotero-API-Key": zotero_key, "Zotero-API-Version": "3"},
    )
    with opener(request, timeout=20) as response:
        payload = read_json_response(response)
        total = response.headers.get("Total-Results")
    items = payload if isinstance(payload, list) else []
    sample = items[0].get("data", {}).get("title", "") if items else ""
    return {"ok": True, "item_count": int(total) if total is not None else len(items), "sample_title": str(sample)[:160]}


def filter_zotero_corpus(
    corpus: list[dict[str, Any]],
    include_paths: list[str] | None,
    ignore_paths: list[str] | None,
) -> list[dict[str, Any]]:
    includes = include_paths or []
    ignores = ignore_paths or []
    filtered = []
    for paper in corpus:
        paths = [str(path) for path in paper.get("paths", [])]
        if includes and not any(fnmatch.fnmatchcase(path, pattern) for path in paths for pattern in includes):
            continue
        if ignores and any(fnmatch.fnmatchcase(path, pattern) for path in paths for pattern in ignores):
            continue
        filtered.append(paper)
    return filtered


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        raise ValueError("Embedding dimensions do not match")
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = sum(value * value for value in left) ** 0.5
    right_norm = sum(value * value for value in right) ** 0.5
    return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0


def rank_papers_by_corpus(
    papers: list[dict[str, Any]],
    paper_embeddings: list[list[float]],
    corpus: list[dict[str, Any]],
    corpus_embeddings: list[list[float]],
) -> list[dict[str, Any]]:
    if len(papers) != len(paper_embeddings) or len(corpus) != len(corpus_embeddings):
        raise ValueError("Papers and embeddings must have matching lengths")
    now = utcnow()
    weights = []
    for item in corpus:
        added = parse_iso(str(item.get("date_added") or "").replace("Z", "+00:00"))
        age_days = max(0.0, (now - added).total_seconds() / 86400) if added else 365.0
        weights.append(1.0 / (1.0 + age_days / 365.0))
    ranked = []
    for paper, embedding in zip(papers, paper_embeddings, strict=True):
        similarities = [cosine_similarity(embedding, candidate) for candidate in corpus_embeddings]
        score = sum(score * weight for score, weight in zip(similarities, weights, strict=True)) / sum(weights) if weights else 0.0
        ranked.append({**paper, "recommendation_score": round(score, 6)})
    return sorted(ranked, key=lambda item: item["recommendation_score"], reverse=True)


def zotero_api_get(
    zotero_id: str,
    zotero_key: str,
    resource: str,
    params: dict[str, Any] | None = None,
    opener: Any = None,
) -> tuple[Any, Any]:
    opener = opener or urllib.request.urlopen
    query = urlencode(params or {})
    url = f"https://api.zotero.org/users/{zotero_id}/{resource.lstrip('/')}"
    request = urllib.request.Request(
        f"{url}?{query}" if query else url,
        headers={"Zotero-API-Key": zotero_key, "Zotero-API-Version": "3"},
    )
    with opener(request, timeout=30) as response:
        return read_json_response(response), response.headers


def fetch_zotero_corpus(zotero_id: str, zotero_key: str, opener: Any = None) -> list[dict[str, Any]]:
    if not zotero_id or not zotero_key:
        raise ValueError("Zotero User ID and API Key are required")
    collections: dict[str, dict[str, Any]] = {}
    collection_start = 0
    while True:
        collections_payload, collection_headers = zotero_api_get(
            zotero_id,
            zotero_key,
            "collections",
            {"limit": 100, "start": collection_start, "format": "json"},
            opener,
        )
        if not isinstance(collections_payload, list):
            raise ValueError("Unexpected Zotero collections response")
        collections.update({
            str(entry.get("key")): entry.get("data", {})
            for entry in collections_payload if isinstance(entry, dict) and entry.get("key")
        })
        collection_start += len(collections_payload)
        collection_total = int(collection_headers.get("Total-Results", collection_start))
        if not collections_payload or collection_start >= collection_total:
            break

    def collection_path(key: str, seen: set[str] | None = None) -> str:
        seen = set(seen or ())
        if key in seen or key not in collections:
            return ""
        seen.add(key)
        item = collections[key]
        name = str(item.get("name") or "")
        parent = str(item.get("parentCollection") or "")
        prefix = collection_path(parent, seen) if parent else ""
        return "/".join(part for part in (prefix, name) if part)

    corpus = []
    start = 0
    while True:
        payload, headers = zotero_api_get(
            zotero_id,
            zotero_key,
            "items",
            {
                "itemType": "conferencePaper || journalArticle || preprint",
                "limit": 100,
                "start": start,
                "format": "json",
            },
            opener,
        )
        if not isinstance(payload, list):
            raise ValueError("Unexpected Zotero response")
        for entry in payload:
            data = entry.get("data", {}) if isinstance(entry, dict) else {}
            abstract = str(data.get("abstractNote") or "").strip()
            if not abstract:
                continue
            corpus.append({
                "title": str(data.get("title") or ""),
                "abstract": abstract,
                "date_added": str(data.get("dateAdded") or ""),
                "paths": [path for key in data.get("collections", []) if (path := collection_path(str(key)))],
            })
        start += len(payload)
        total = int(headers.get("Total-Results", start))
        if not payload or start >= total:
            break
    return corpus


def ensure_zotero_collection(
    zotero_id: str,
    zotero_key: str,
    collection_path: str,
    opener: Any = None,
) -> str | None:
    parts = [part.strip() for part in collection_path.split("/") if part.strip()]
    if parts and parts[0].lower() in {"我的文库", "my library"}:
        parts = parts[1:]
    if not parts:
        return None
    opener = opener or urllib.request.urlopen
    collections = []
    start = 0
    while True:
        payload, headers = zotero_api_get(
            zotero_id, zotero_key, "collections", {"limit": 100, "start": start, "format": "json"}, opener
        )
        if not isinstance(payload, list):
            raise ValueError("Unexpected Zotero collections response")
        collections.extend(payload)
        start += len(payload)
        if not payload or start >= int(headers.get("Total-Results", start)):
            break
    parent: str | bool = False
    for name in parts:
        existing = next((
            entry for entry in collections
            if str(entry.get("data", {}).get("name") or "") == name
            and (entry.get("data", {}).get("parentCollection") or False) == parent
        ), None)
        if existing:
            parent = str(existing["key"])
            continue
        parent_before_create = parent
        body = json.dumps([{"name": name, "parentCollection": parent_before_create}]).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.zotero.org/users/{zotero_id}/collections",
            method="POST",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Zotero-API-Key": zotero_key,
                "Zotero-API-Version": "3",
            },
        )
        with opener(request, timeout=30) as response:
            created = read_json_response(response)
        result = (created.get("successful") or {}).get("0") or {}
        key = result.get("key")
        if not key:
            raise ValueError(f"Failed to create Zotero collection: {name}")
        parent = str(key)
        collections.append({"key": parent, "data": {"name": name, "parentCollection": parent_before_create}})
    return str(parent) if parent else None


def create_embeddings(
    api_key: str,
    base_url: str,
    model: str,
    texts: list[str],
    opener: Any = None,
    batch_size: int = 64,
) -> list[list[float]]:
    if not api_key or not base_url or not model:
        raise ValueError("AI API and embedding model must be configured")
    opener = opener or urllib.request.urlopen
    embeddings = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start:start + batch_size]
        request = urllib.request.Request(
            api_url(base_url, "embeddings"),
            method="POST",
            data=json.dumps({"model": model, "input": batch}).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        with opener(request, timeout=60) as response:
            payload = read_json_response(response)
        rows = sorted(payload.get("data") or [], key=lambda row: int(row.get("index", 0)))
        if len(rows) != len(batch):
            raise ValueError("Embedding API returned an unexpected number of vectors")
        embeddings.extend([[float(value) for value in row.get("embedding", [])] for row in rows])
    return embeddings


def load_latest_papers(root_dir: Path, when: dt.date | None = None) -> list[dict[str, Any]]:
    date_text = (when or utcnow().date()).isoformat()
    candidates = sorted((root_dir / "data").glob(f"{date_text}_AI_enhanced_*.jsonl"), reverse=True)
    candidates += [root_dir / "data" / f"{date_text}.jsonl"]
    source = next((path for path in candidates if path.is_file()), None)
    if not source:
        return []
    papers = []
    with source.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                value = json.loads(line)
                if isinstance(value, dict):
                    papers.append(value)
    return papers


def paper_embedding_text(paper: dict[str, Any]) -> str:
    return "\n\n".join(str(value).strip() for value in (paper.get("title"), paper.get("summary") or paper.get("abstract")) if value)


def render_recommendation_email(papers: list[dict[str, Any]]) -> str:
    blocks = []
    for paper in papers:
        title = html.escape(str(paper.get("title") or "Untitled"))
        authors = paper.get("authors") or []
        if isinstance(authors, list):
            authors = ", ".join(str(author) for author in authors[:8])
        summary = paper.get("AI", {}).get("tldr") if isinstance(paper.get("AI"), dict) else ""
        summary = summary or paper.get("summary") or ""
        url = str(paper.get("abs") or paper.get("url") or "")
        if not url.startswith(("https://arxiv.org/", "https://www.biorxiv.org/", "https://www.medrxiv.org/")):
            url = ""
        score = float(paper.get("recommendation_score") or 0)
        link = f'<p><a href="{html.escape(url, quote=True)}">查看论文</a></p>' if url else ""
        blocks.append(
            f'<article style="margin:0 0 20px;padding:16px;border:1px solid #ddd">'
            f"<h2>{title}</h2><p>{html.escape(str(authors))}</p>"
            f"<p><strong>相关度：</strong>{score:.3f}</p><p>{html.escape(str(summary))}</p>{link}</article>"
        )
    content = "".join(blocks) or "<p>今日没有可推荐的新论文。</p>"
    return f"<!doctype html><html><body><h1>Daily arXiv · Zotero 文库推荐</h1>{content}</body></html>"


def send_zotero_recommendations(
    root_dir: Path,
    settings: dict[str, Any],
    secrets_map: dict[str, str],
    opener: Any = None,
    smtp_factory: Any = None,
) -> dict[str, Any]:
    corpus = fetch_zotero_corpus(settings["zotero_id"], secrets_map["zotero_key"], opener)
    corpus = filter_zotero_corpus(corpus, settings["zotero_include_paths"], settings["zotero_ignore_paths"])
    if not corpus:
        raise ValueError("No Zotero papers matched the configured collection paths")
    papers = load_latest_papers(root_dir)
    if papers:
        texts = [paper_embedding_text(paper) for paper in papers] + [paper_embedding_text(paper) for paper in corpus]
        vectors = create_embeddings(
            secrets_map["ai_api_key"], settings["base_url"], settings["zotero_embedding_model"], texts, opener
        )
        split_at = len(papers)
        papers = rank_papers_by_corpus(papers, vectors[:split_at], corpus, vectors[split_at:])
        papers = papers[:settings["zotero_max_papers"]]
    result = send_test_email(
        settings,
        secrets_map,
        smtp_factory=smtp_factory,
        subject="Daily arXiv · Zotero 文库推荐",
        content="今日 Zotero 文库兴趣推荐，请使用支持 HTML 的邮件客户端查看。",
        html_content=render_recommendation_email(papers),
    )
    return {**result, "recommended": len(papers), "corpus_size": len(corpus)}


def validate_schedule(value: str) -> tuple[bool, str | None]:
    raw = (value or "").strip()
    if not raw:
        return False, "Schedule is required"
    parts = raw.split()
    if len(parts) == 1:
        try:
            hour_text, minute_text = raw.split(":", 1)
            hour = int(hour_text)
            minute = int(minute_text)
        except (ValueError, TypeError):
            return False, "Schedule must be HH:MM or a 5-field cron expression"
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return True, None
        return False, "HH:MM schedule must be within 00:00-23:59"
    if len(parts) != 5:
        return False, "Cron schedule must have 5 fields"
    validators = [
        (0, 59),
        (0, 23),
        (1, 31),
        (1, 12),
        (0, 6),
    ]
    for token, (low, high) in zip(parts, validators, strict=True):
        if token == "*":
            continue
        if token.startswith("*/"):
            try:
                step = int(token[2:])
            except ValueError:
                return False, f"Invalid cron step: {token}"
            if step <= 0:
                return False, f"Invalid cron step: {token}"
            continue
        try:
            number = int(token)
        except ValueError:
            return False, f"Invalid cron token: {token}"
        if not low <= number <= high:
            return False, f"Cron token out of range: {token}"
    return True, None


def next_run_for_schedule(value: str, now: dt.datetime | None = None) -> dt.datetime | None:
    ok, _error = validate_schedule(value)
    if not ok:
        return None
    now = now or utcnow()
    raw = value.strip()
    if " " not in raw:
        hour_text, minute_text = raw.split(":", 1)
        candidate = now.replace(hour=int(hour_text), minute=int(minute_text), second=0, microsecond=0)
        if candidate <= now:
            candidate += dt.timedelta(days=1)
        return candidate

    minute_token, hour_token, day_token, month_token, weekday_token = raw.split()

    def matches(token: str, current: int) -> bool:
        if token == "*":
            return True
        if token.startswith("*/"):
            return current % int(token[2:]) == 0
        return current == int(token)

    candidate = now.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
    for _ in range(366 * 24 * 60):
        cron_weekday = (candidate.weekday() + 1) % 7
        if (
            matches(minute_token, candidate.minute)
            and matches(hour_token, candidate.hour)
            and matches(day_token, candidate.day)
            and matches(month_token, candidate.month)
            and matches(weekday_token, cron_weekday)
        ):
            return candidate
        candidate += dt.timedelta(minutes=1)
    return None


def pbkdf2_hash(password: str, salt: bytes | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return base64.b64encode(salt).decode("ascii"), base64.b64encode(digest).decode("ascii")


def pbkdf2_verify(password: str, salt_b64: str, digest_b64: str) -> bool:
    salt = base64.b64decode(salt_b64.encode("ascii"))
    _salt, candidate = pbkdf2_hash(password, salt)
    return hmac.compare_digest(candidate, digest_b64)


class AppState:
    def __init__(self, root_dir: Path | None = None, db_path: Path | None = None):
        self.root_dir = (root_dir or Path(__file__).resolve().parents[1]).resolve()
        self.db_path = db_path or self.root_dir / "data" / "server.sqlite3"
        self.data_dir = self.db_path.parent
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._db_lock = threading.RLock()
        self._job_lock = threading.Lock()
        self._stop_event = threading.Event()
        self.job_status: dict[str, Any] = {
            "running": False,
            "last_started_at": None,
            "last_finished_at": None,
            "last_exit_code": None,
            "last_stdout": "",
            "last_stderr": "",
            "last_trigger": None,
            "next_run_at": None,
        }
        self._init_db()
        self._seed_default_settings()
        self._ensure_password()
        self.refresh_next_run()
        self._scheduler_thread = threading.Thread(target=self._scheduler_loop, name="scheduler", daemon=True)
        self._scheduler_thread.start()

    def close(self) -> None:
        self._stop_event.set()
        self._scheduler_thread.join(timeout=1)

    @contextlib.contextmanager
    def db(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self.db() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS app_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    csrf_token TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    remember INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS login_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_ip TEXT NOT NULL,
                    attempted_at TEXT NOT NULL,
                    success INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS favorites (
                    paper_id TEXT PRIMARY KEY,
                    paper_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def _seed_default_settings(self) -> None:
        defaults = {
            "base_url": os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            "model": os.getenv("MODEL_NAME", os.getenv("OPENAI_MODEL", "gpt-4o-mini")),
            "schedule": os.getenv("DAILY_SCHEDULE", os.getenv("CRON_SCHEDULE", "09:00")),
            "categories": json.dumps(split_csv(os.getenv("CATEGORIES", "cs.CV,cs.CL"))),
            "keywords": json.dumps(split_csv(os.getenv("KEYWORDS", ""))),
            "authors": json.dumps(split_csv(os.getenv("AUTHORS", ""))),
            "zotero_id": os.getenv("ZOTERO_ID", ""),
            "zotero_recommendation_enabled": json.dumps(os.getenv("ZOTERO_RECOMMENDATION_ENABLED", "false").lower() == "true"),
            "zotero_embedding_model": os.getenv("ZOTERO_EMBEDDING_MODEL", "text-embedding-3-small"),
            "zotero_include_paths": json.dumps(split_csv(os.getenv("ZOTERO_INCLUDE_PATHS", ""))),
            "zotero_ignore_paths": json.dumps(split_csv(os.getenv("ZOTERO_IGNORE_PATHS", ""))),
            "zotero_max_papers": os.getenv("ZOTERO_MAX_PAPERS", "20"),
            "zotero_target_collection": os.getenv("ZOTERO_TARGET_COLLECTION", "我的文库/arxiv"),
            "paper_ai_assistant": os.getenv("PAPER_AI_ASSISTANT", "kimi"),
            "paper_ai_custom_url": os.getenv("PAPER_AI_CUSTOM_URL", ""),
            "smtp_host": os.getenv("SMTP_HOST", ""),
            "smtp_port": os.getenv("SMTP_PORT", "587"),
            "smtp_user": os.getenv("SMTP_USER", ""),
            "email_from": os.getenv("EMAIL_FROM", ""),
            "email_to": os.getenv("EMAIL_TO", ""),
            "email_enabled": json.dumps(os.getenv("EMAIL_ENABLED", "false").lower() == "true"),
        }
        secrets_map = {
            "ai_api_key": os.getenv("OPENAI_API_KEY", ""),
            "zotero_key": os.getenv("ZOTERO_KEY", ""),
            "smtp_password": os.getenv("SMTP_PASSWORD", ""),
        }
        with self.db() as conn:
            for key, value in defaults.items():
                conn.execute(
                    "INSERT OR IGNORE INTO app_meta(key, value) VALUES (?, ?)",
                    (key, value),
                )
            for key, value in secrets_map.items():
                if value:
                    conn.execute(
                        "INSERT OR IGNORE INTO app_meta(key, value) VALUES (?, ?)",
                        (key, value),
                    )

    def _ensure_password(self) -> None:
        with self.db() as conn:
            row = conn.execute(
                "SELECT value FROM app_meta WHERE key = 'password_salt'"
            ).fetchone()
            digest = conn.execute(
                "SELECT value FROM app_meta WHERE key = 'password_hash'"
            ).fetchone()
            if row and digest:
                return
            password = os.getenv("ADMIN_PASSWORD") or os.getenv("ACCESS_PASSWORD")
            if not password or len(password) < 12:
                raise RuntimeError("ADMIN_PASSWORD must contain at least 12 characters on first start")
            salt, hashed = pbkdf2_hash(password)
            conn.execute("INSERT OR REPLACE INTO app_meta(key, value) VALUES ('password_salt', ?)", (salt,))
            conn.execute("INSERT OR REPLACE INTO app_meta(key, value) VALUES ('password_hash', ?)", (hashed,))

    def client_rate_limited(self, client_ip: str) -> bool:
        threshold = utcnow() - dt.timedelta(seconds=RATE_LIMIT_WINDOW_SECONDS)
        with self.db() as conn:
            rows = conn.execute(
                """
                SELECT COUNT(*) AS failures
                FROM login_attempts
                WHERE client_ip = ? AND success = 0 AND attempted_at >= ?
                """,
                (client_ip, threshold.isoformat()),
            ).fetchone()
        return int(rows["failures"]) >= RATE_LIMIT_MAX_FAILURES

    def record_login_attempt(self, client_ip: str, success: bool) -> None:
        when = utcnow().isoformat()
        with self.db() as conn:
            conn.execute(
                "INSERT INTO login_attempts(client_ip, attempted_at, success) VALUES (?, ?, ?)",
                (client_ip, when, 1 if success else 0),
            )

    def verify_password(self, password: str) -> bool:
        with self.db() as conn:
            salt_row = conn.execute("SELECT value FROM app_meta WHERE key = 'password_salt'").fetchone()
            hash_row = conn.execute("SELECT value FROM app_meta WHERE key = 'password_hash'").fetchone()
        return bool(salt_row and hash_row and pbkdf2_verify(password, salt_row["value"], hash_row["value"]))

    def change_password(self, current_password: str, new_password: str, keep_session_id: str) -> None:
        if not self.verify_password(current_password):
            raise ValueError("当前密码错误")
        if len(new_password) < 12:
            raise ValueError("新密码至少需要 12 个字符")
        salt, hashed = pbkdf2_hash(new_password)
        with self.db() as conn:
            conn.execute("INSERT OR REPLACE INTO app_meta(key, value) VALUES ('password_salt', ?)", (salt,))
            conn.execute("INSERT OR REPLACE INTO app_meta(key, value) VALUES ('password_hash', ?)", (hashed,))
            conn.execute("DELETE FROM sessions WHERE session_id != ?", (keep_session_id,))

    def create_session(self, remember: bool) -> dict[str, Any]:
        now = utcnow()
        expires = now + dt.timedelta(days=REMEMBER_SESSION_DAYS if remember else DEFAULT_SESSION_HOURS / 24)
        session = {
            "session_id": secrets.token_urlsafe(32),
            "csrf_token": secrets.token_urlsafe(24),
            "expires_at": expires.isoformat(),
            "remember": 1 if remember else 0,
            "created_at": now.isoformat(),
            "last_seen_at": now.isoformat(),
        }
        with self.db() as conn:
            conn.execute(
                """
                INSERT INTO sessions(session_id, csrf_token, expires_at, remember, created_at, last_seen_at)
                VALUES (:session_id, :csrf_token, :expires_at, :remember, :created_at, :last_seen_at)
                """,
                session,
            )
        return session

    def get_session(self, session_id: str | None) -> dict[str, Any] | None:
        if not session_id:
            return None
        with self.db() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
            if not row:
                return None
            expires_at = parse_iso(row["expires_at"])
            if not expires_at or expires_at <= utcnow():
                conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
                return None
            conn.execute(
                "UPDATE sessions SET last_seen_at = ? WHERE session_id = ?",
                (utcnow().isoformat(), session_id),
            )
            return dict(row)

    def delete_session(self, session_id: str | None) -> None:
        if not session_id:
            return
        with self.db() as conn:
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

    def settings_payload(self) -> dict[str, Any]:
        keys = [
            "ai_api_key",
            "base_url",
            "model",
            "schedule",
            "categories",
            "keywords",
            "authors",
            "zotero_id",
            "zotero_key",
            "zotero_recommendation_enabled",
            "zotero_embedding_model",
            "zotero_include_paths",
            "zotero_ignore_paths",
            "zotero_max_papers",
            "zotero_target_collection",
            "paper_ai_assistant",
            "paper_ai_custom_url",
            "smtp_host",
            "smtp_port",
            "smtp_user",
            "smtp_password",
            "email_from",
            "email_to",
            "email_enabled",
        ]
        with self.db() as conn:
            rows = conn.execute(
                f"SELECT key, value FROM app_meta WHERE key IN ({','.join('?' for _ in keys)})",
                keys,
            ).fetchall()
        data = {row["key"]: row["value"] for row in rows}
        payload = {
            "ai_api_key_configured": bool(data.get("ai_api_key")),
            "base_url": data.get("base_url", ""),
            "model": data.get("model", ""),
            "schedule": data.get("schedule", ""),
            "categories": json.loads(data.get("categories", "[]")),
            "keywords": json.loads(data.get("keywords", "[]")),
            "authors": json.loads(data.get("authors", "[]")),
            "zotero_id": data.get("zotero_id", ""),
            "zotero_key_configured": bool(data.get("zotero_key")),
            "zotero_recommendation_enabled": json.loads(data.get("zotero_recommendation_enabled", "false")),
            "zotero_embedding_model": data.get("zotero_embedding_model", "text-embedding-3-small"),
            "zotero_include_paths": json.loads(data.get("zotero_include_paths", "[]")),
            "zotero_ignore_paths": json.loads(data.get("zotero_ignore_paths", "[]")),
            "zotero_max_papers": int(data.get("zotero_max_papers", "20")),
            "zotero_target_collection": data.get("zotero_target_collection", "我的文库/arxiv"),
            "paper_ai_assistant": data.get("paper_ai_assistant", "kimi"),
            "paper_ai_custom_url": data.get("paper_ai_custom_url", ""),
            "smtp_host": data.get("smtp_host", ""),
            "smtp_port": int(data.get("smtp_port", "587")),
            "smtp_user": data.get("smtp_user", ""),
            "smtp_password_configured": bool(data.get("smtp_password")),
            "email_from": data.get("email_from", ""),
            "email_to": data.get("email_to", ""),
            "email_enabled": json.loads(data.get("email_enabled", "false")),
        }
        return payload

    def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        schedule = str(payload.get("schedule", "")).strip()
        ok, error = validate_schedule(schedule)
        if not ok:
            raise ValueError(error or "Invalid schedule")

        categories = payload.get("categories") or []
        keywords = payload.get("keywords") or []
        authors = payload.get("authors") or []
        zotero_include_paths = payload.get("zotero_include_paths") or []
        zotero_ignore_paths = payload.get("zotero_ignore_paths") or []
        for name, values in (
            ("categories", categories), ("keywords", keywords), ("authors", authors),
            ("zotero_include_paths", zotero_include_paths), ("zotero_ignore_paths", zotero_ignore_paths),
        ):
            if not isinstance(values, list) or len(values) > 100:
                raise ValueError(f"{name} must be an array with at most 100 entries")
            if any(not isinstance(value, str) or not value.strip() or len(value) > 200 for value in values):
                raise ValueError(f"{name} contains an invalid entry")
        smtp_port = int(payload.get("smtp_port") or 587)
        if not 1 <= smtp_port <= 65535:
            raise ValueError("SMTP port must be between 1 and 65535")
        zotero_max_papers = int(payload.get("zotero_max_papers") or 20)
        if not 1 <= zotero_max_papers <= 100:
            raise ValueError("Zotero recommendation limit must be between 1 and 100")
        paper_ai_assistant = str(payload.get("paper_ai_assistant") or "kimi").strip().lower()
        if paper_ai_assistant not in {"kimi", "chatgpt", "claude", "custom"}:
            raise ValueError("Unsupported paper AI assistant")
        paper_ai_custom_url = str(payload.get("paper_ai_custom_url") or "").strip()
        if paper_ai_custom_url and urlparse(paper_ai_custom_url).scheme not in {"http", "https"}:
            raise ValueError("Custom paper AI URL must use HTTP or HTTPS")
        normalized = {
            "base_url": str(payload.get("base_url", "")).strip(),
            "model": str(payload.get("model", "")).strip(),
            "schedule": schedule,
            "categories": json.dumps(categories),
            "keywords": json.dumps(keywords),
            "authors": json.dumps(authors),
            "zotero_id": str(payload.get("zotero_id", "")).strip(),
            "zotero_recommendation_enabled": json.dumps(bool(payload.get("zotero_recommendation_enabled"))),
            "zotero_embedding_model": str(payload.get("zotero_embedding_model") or "text-embedding-3-small").strip(),
            "zotero_include_paths": json.dumps(zotero_include_paths),
            "zotero_ignore_paths": json.dumps(zotero_ignore_paths),
            "zotero_max_papers": str(zotero_max_papers),
            "zotero_target_collection": str(payload.get("zotero_target_collection") or "我的文库/arxiv").strip(),
            "paper_ai_assistant": paper_ai_assistant,
            "paper_ai_custom_url": paper_ai_custom_url,
            "smtp_host": str(payload.get("smtp_host", "")).strip(),
            "smtp_port": str(smtp_port),
            "smtp_user": str(payload.get("smtp_user", "")).strip(),
            "email_from": str(payload.get("email_from", "")).strip(),
            "email_to": str(payload.get("email_to", "")).strip(),
            "email_enabled": json.dumps(bool(payload.get("email_enabled"))),
        }
        if any(len(value) > 4096 for value in normalized.values()):
            raise ValueError("A setting value is too long")
        with self.db() as conn:
            for key, value in normalized.items():
                conn.execute(
                    "INSERT OR REPLACE INTO app_meta(key, value) VALUES (?, ?)",
                    (key, value),
                )
            for field in SETTINGS_SECRET_FIELDS:
                if field in payload:
                    candidate = str(payload.get(field, ""))
                    if candidate:
                        conn.execute(
                            "INSERT OR REPLACE INTO app_meta(key, value) VALUES (?, ?)",
                            (field, candidate),
                        )
        self.refresh_next_run()
        return self.settings_payload()

    def list_favorites(self) -> list[dict[str, Any]]:
        with self.db() as conn:
            rows = conn.execute(
                "SELECT paper_json FROM favorites ORDER BY updated_at DESC"
            ).fetchall()
        return [json.loads(row["paper_json"]) for row in rows]

    def upsert_favorite(self, paper_id: str, paper: dict[str, Any]) -> dict[str, Any]:
        with self.db() as conn:
            conn.execute(
                """
                INSERT INTO favorites(paper_id, paper_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(paper_id) DO UPDATE SET paper_json = excluded.paper_json, updated_at = excluded.updated_at
                """,
                (paper_id, json.dumps(paper), utcnow().isoformat()),
            )
        return paper

    def delete_favorite(self, paper_id: str) -> None:
        with self.db() as conn:
            conn.execute("DELETE FROM favorites WHERE paper_id = ?", (paper_id,))

    def get_secret(self, key: str) -> str:
        with self.db() as conn:
            row = conn.execute("SELECT value FROM app_meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else ""

    def refresh_next_run(self) -> None:
        schedule = self.settings_payload()["schedule"]
        self.job_status["next_run_at"] = isoformat(next_run_for_schedule(schedule))

    def job_status_payload(self) -> dict[str, Any]:
        return dict(self.job_status)

    def start_job(self, trigger: str = "manual") -> tuple[bool, dict[str, Any]]:
        with self._job_lock:
            if self.job_status["running"]:
                return False, self.job_status_payload()
            if not self.get_secret("ai_api_key"):
                raise ValueError("请先配置 OpenAI API Key")
            self.job_status["running"] = True
            self.job_status["last_started_at"] = isoformat(utcnow())
            self.job_status["last_finished_at"] = None
            self.job_status["last_exit_code"] = None
            self.job_status["last_stdout"] = ""
            self.job_status["last_stderr"] = ""
            self.job_status["last_trigger"] = trigger
        thread = threading.Thread(target=self._run_job, args=(trigger,), daemon=True)
        thread.start()
        return True, self.job_status_payload()

    def _run_job(self, trigger: str) -> None:
        script_path = self.root_dir / "run.sh"
        env = os.environ.copy()
        settings = self.settings_payload()
        env["OPENAI_API_KEY"] = self.get_secret("ai_api_key") or env.get("OPENAI_API_KEY", "")
        env["OPENAI_BASE_URL"] = settings["base_url"]
        env["MODEL_NAME"] = settings["model"]
        env["CATEGORIES"] = ",".join(settings["categories"])
        env["KEYWORDS"] = ",".join(settings["keywords"])
        env["AUTHORS"] = ",".join(settings["authors"])
        try:
            process = subprocess.run(
                ["bash", str(script_path)],
                cwd=self.root_dir,
                capture_output=True,
                text=True,
                env=env,
                timeout=60 * 60,
            )
            stdout = process.stdout[-4000:]
            stderr = process.stderr[-4000:]
            exit_code = process.returncode
        except Exception as exc:  # noqa: BLE001
            stdout = ""
            stderr = str(exc)
            exit_code = 1
        with self._job_lock:
            self.job_status["running"] = False
            self.job_status["last_finished_at"] = isoformat(utcnow())
            self.job_status["last_exit_code"] = exit_code
            self.job_status["last_stdout"] = stdout
            self.job_status["last_stderr"] = stderr
            self.job_status["last_trigger"] = trigger
        if exit_code == 0 and settings.get("email_enabled"):
            try:
                secrets_map = {
                    "ai_api_key": self.get_secret("ai_api_key"),
                    "zotero_key": self.get_secret("zotero_key"),
                    "smtp_password": self.get_secret("smtp_password"),
                }
                if settings.get("zotero_recommendation_enabled"):
                    send_zotero_recommendations(self.root_dir, settings, secrets_map)
                else:
                    send_test_email(
                        settings,
                        secrets_map,
                        subject="Daily arXiv 更新完成",
                        content="今日论文采集与 AI 增强任务已完成，请登录站点查看。",
                    )
            except (ValueError, OSError, urllib.error.URLError, json.JSONDecodeError, smtplib.SMTPException) as exc:
                with self._job_lock:
                    self.job_status["last_stderr"] = f"{self.job_status['last_stderr']}\nEmail: {exc}".strip()
        self.refresh_next_run()

    def _scheduler_loop(self) -> None:
        while not self._stop_event.wait(30):
            next_run_text = self.job_status.get("next_run_at")
            next_run = parse_iso(next_run_text) if next_run_text else None
            if next_run and next_run <= utcnow() and not self.job_status["running"]:
                try:
                    self.start_job(trigger="scheduler")
                except ValueError as exc:
                    with self._job_lock:
                        self.job_status["last_stderr"] = str(exc)
                    self.refresh_next_run()


def make_zotero_payload(paper: dict[str, Any], collection_key: str | None = None) -> dict[str, Any]:
    authors = []
    raw_authors = paper.get("authors") or []
    if isinstance(raw_authors, str):
        raw_authors = [name.strip() for name in raw_authors.split(",") if name.strip()]
    for name in raw_authors:
        parts = str(name).split()
        authors.append(
            {
                "creatorType": "author",
                "firstName": " ".join(parts[:-1]),
                "lastName": parts[-1] if parts else str(name),
            }
        )
    payload = {
        "itemType": "preprint",
        "title": paper.get("title", ""),
        "abstractNote": paper.get("summary", ""),
        "url": paper.get("url") or f"https://arxiv.org/abs/{paper.get('id', '')}",
        "date": paper.get("date", ""),
        "creators": authors,
        "tags": [{"tag": tag} for tag in paper.get("category", []) or []],
    }
    if collection_key:
        payload["collections"] = [collection_key]
    return payload


def sync_to_zotero(
    zotero_id: str,
    zotero_key: str,
    paper: dict[str, Any],
    collection_key: str | None = None,
    opener: Any = None,
) -> dict[str, Any]:
    opener = opener or urllib.request.urlopen
    body = json.dumps([make_zotero_payload(paper, collection_key)]).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.zotero.org/users/{zotero_id}/items",
        method="POST",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Zotero-API-Key": zotero_key,
            "Zotero-API-Version": "3",
        },
    )
    with opener(request, timeout=20) as response:
        payload = response.read().decode("utf-8") or "{}"
        return {"status": response.status, "body": json.loads(payload or "{}")}


def send_test_email(
    settings: dict[str, Any],
    secrets_map: dict[str, str],
    smtp_factory: Any = None,
    subject: str = "daily-arXiv-ai-enhanced test email",
    content: str = "This is a test email from daily-arXiv-ai-enhanced.",
    html_content: str | None = None,
) -> dict[str, Any]:
    if not settings.get("email_enabled"):
        raise ValueError("Email is disabled")
    if not settings.get("smtp_host") or not settings.get("email_to"):
        raise ValueError("SMTP and email recipient settings are required")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.get("email_from") or settings.get("smtp_user") or "noreply@localhost"
    message["To"] = settings["email_to"]
    message.set_content(content)
    if html_content:
        message.add_alternative(html_content, subtype="html")

    port = int(settings.get("smtp_port") or 587)
    smtp_factory = smtp_factory or (smtplib.SMTP_SSL if port == 465 else smtplib.SMTP)
    password = secrets_map.get("smtp_password", "")
    with smtp_factory(settings["smtp_host"], port, timeout=20) as smtp:
        smtp.ehlo()
        if port in {587, 25}:
            try:
                smtp.starttls()
                smtp.ehlo()
            except smtplib.SMTPException:
                pass
        if settings.get("smtp_user") and password:
            smtp.login(settings["smtp_user"], password)
        smtp.send_message(message)
    return {"sent": True, "to": settings["email_to"]}


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "daily-arxiv-http/1.0"

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def do_HEAD(self) -> None:  # noqa: N802
        path = urlparse(self.path).path or "/"
        clean_path = "/index.html" if path == "/" else path
        if clean_path not in ALLOWED_PUBLIC_PATHS and not self._get_session():
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", f"/login.html?redirect={clean_path.lstrip('/')}")
            self.end_headers()
            return
        target = (self.app_state.root_dir / clean_path.lstrip("/")).resolve()
        if not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(str(target))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(target.stat().st_size))
        self.end_headers()

    @property
    def app_state(self) -> AppState:
        return self.server.app_state  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/"):
            self._handle_api(method, path)
            return
        self._serve_static(path)

    def _json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 1_000_000:
            raise ValueError("Request body is too large")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _send_json(self, status: int, payload: dict[str, Any], headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if headers:
            for key, value in headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status: int, payload: str, content_type: str = "text/plain; charset=utf-8") -> None:
        body = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _get_session(self) -> dict[str, Any] | None:
        cookie_header = self.headers.get("Cookie")
        if not cookie_header:
            return None
        cookie = SimpleCookie()
        cookie.load(cookie_header)
        morsel = cookie.get(SESSION_COOKIE)
        return self.app_state.get_session(morsel.value if morsel else None)

    def _require_auth(self, write: bool = False) -> dict[str, Any] | None:
        session = self._get_session()
        if not session:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "Authentication required"})
            return None
        if write:
            token = self.headers.get("X-CSRF-Token", "")
            if not token or not hmac.compare_digest(token, session["csrf_token"]):
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "Invalid CSRF token"})
                return None
        return session

    def _set_session_cookie(self, session: dict[str, Any]) -> str:
        expires = parse_iso(session["expires_at"])
        max_age = max(0, int((expires - utcnow()).total_seconds())) if expires else 0
        cookie = SimpleCookie()
        cookie[SESSION_COOKIE] = session["session_id"]
        cookie[SESSION_COOKIE]["httponly"] = True
        cookie[SESSION_COOKIE]["path"] = "/"
        cookie[SESSION_COOKIE]["samesite"] = "Strict"
        cookie[SESSION_COOKIE]["max-age"] = str(max_age)
        if os.getenv("COOKIE_SECURE", "true").lower() == "true":
            cookie[SESSION_COOKIE]["secure"] = True
        return cookie.output(header="").strip()

    def _clear_session_cookie(self) -> str:
        cookie = SimpleCookie()
        cookie[SESSION_COOKIE] = ""
        cookie[SESSION_COOKIE]["httponly"] = True
        cookie[SESSION_COOKIE]["path"] = "/"
        cookie[SESSION_COOKIE]["samesite"] = "Strict"
        cookie[SESSION_COOKIE]["max-age"] = "0"
        return cookie.output(header="").strip()

    def _handle_api(self, method: str, path: str) -> None:
        if path == "/api/session" and method == "GET":
            session = self._get_session()
            payload = {"authenticated": bool(session)}
            if session:
                payload["csrfToken"] = session["csrf_token"]
            self._send_json(HTTPStatus.OK, payload)
            return

        if path == "/api/login" and method == "POST":
            data = self._json_body()
            client_ip = self.client_address[0]
            if self.app_state.client_rate_limited(client_ip):
                self._send_json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "Too many login attempts"})
                return
            success = self.app_state.verify_password(str(data.get("password", "")))
            self.app_state.record_login_attempt(client_ip, success)
            if not success:
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "Invalid credentials"})
                return
            session = self.app_state.create_session(bool(data.get("remember", True)))
            self._send_json(
                HTTPStatus.OK,
                {"authenticated": True, "csrfToken": session["csrf_token"]},
                headers={"Set-Cookie": self._set_session_cookie(session)},
            )
            return

        if path == "/api/logout" and method == "POST":
            session = self._require_auth(write=True)
            if not session:
                return
            self.app_state.delete_session(session["session_id"])
            self._send_json(
                HTTPStatus.OK,
                {"ok": True},
                headers={"Set-Cookie": self._clear_session_cookie()},
            )
            return

        if path == "/api/password" and method == "POST":
            session = self._require_auth(write=True)
            if not session:
                return
            data = self._json_body()
            try:
                self.app_state.change_password(
                    str(data.get("current_password", "")),
                    str(data.get("new_password", "")),
                    session["session_id"],
                )
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            self._send_json(HTTPStatus.OK, {"ok": True})
            return

        if path == "/api/settings":
            if method == "GET":
                if not self._require_auth():
                    return
                self._send_json(HTTPStatus.OK, self.app_state.settings_payload())
                return
            if method == "PUT":
                if not self._require_auth(write=True):
                    return
                try:
                    data = self._json_body()
                    payload = self.app_state.update_settings(data)
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, payload)
                return

        if path == "/api/ai/test" and method == "POST":
            if not self._require_auth(write=True):
                return
            data = self._json_body()
            settings = self.app_state.settings_payload()
            try:
                result = test_ai_configuration(
                    str(data.get("ai_api_key") or self.app_state.get_secret("ai_api_key")),
                    str(data.get("base_url") or settings["base_url"]).strip(),
                    str(data.get("model") or settings["model"]).strip(),
                )
            except (ValueError, OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
                return
            self._send_json(HTTPStatus.OK, result)
            return

        if path == "/api/zotero/test" and method == "POST":
            if not self._require_auth(write=True):
                return
            data = self._json_body()
            settings = self.app_state.settings_payload()
            try:
                result = test_zotero_configuration(
                    str(data.get("zotero_id") or settings["zotero_id"]).strip(),
                    str(data.get("zotero_key") or self.app_state.get_secret("zotero_key")),
                )
            except (ValueError, OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
                return
            self._send_json(HTTPStatus.OK, result)
            return

        if path == "/api/favorites" and method == "GET":
            if not self._require_auth():
                return
            self._send_json(HTTPStatus.OK, {"favorites": self.app_state.list_favorites()})
            return

        if path.startswith("/api/favorites/"):
            paper_id = unquote(path.removeprefix("/api/favorites/"))
            if method == "PUT":
                if not self._require_auth(write=True):
                    return
                payload = self._json_body()
                self._send_json(HTTPStatus.OK, {"paper": self.app_state.upsert_favorite(paper_id, payload)})
                return
            if method == "DELETE":
                if not self._require_auth(write=True):
                    return
                self.app_state.delete_favorite(paper_id)
                self._send_json(HTTPStatus.OK, {"deleted": True})
                return

        if path.startswith("/api/zotero/sync/") and method == "POST":
            if not self._require_auth(write=True):
                return
            settings = self.app_state.settings_payload()
            zotero_key = self.app_state.get_secret("zotero_key")
            if not settings["zotero_id"] or not zotero_key:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Zotero is not configured"})
                return
            try:
                collection_key = ensure_zotero_collection(
                    settings["zotero_id"], zotero_key, settings["zotero_target_collection"]
                )
                result = sync_to_zotero(
                    settings["zotero_id"], zotero_key, self._json_body(), collection_key
                )
            except (urllib.error.URLError, ValueError, json.JSONDecodeError) as exc:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
                return
            self._send_json(HTTPStatus.OK, result)
            return

        if path == "/api/email/test" and method == "POST":
            if not self._require_auth(write=True):
                return
            try:
                result = send_test_email(
                    self.app_state.settings_payload(),
                    {"smtp_password": self.app_state.get_secret("smtp_password")},
                )
            except (ValueError, OSError, smtplib.SMTPException) as exc:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
                return
            self._send_json(HTTPStatus.OK, result)
            return

        if path == "/api/jobs/run" and method == "POST":
            if not self._require_auth(write=True):
                return
            try:
                started, payload = self.app_state.start_job("manual")
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            self._send_json(HTTPStatus.OK if started else HTTPStatus.CONFLICT, payload)
            return

        if path == "/api/jobs/status" and method == "GET":
            if not self._require_auth():
                return
            self._send_json(HTTPStatus.OK, self.app_state.job_status_payload())
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def _serve_static(self, path: str) -> None:
        clean_path = "/" if path == "" else path
        if clean_path == "/":
            clean_path = "/index.html"
        target = (self.app_state.root_dir / clean_path.lstrip("/")).resolve()
        if self.app_state.root_dir not in target.parents and target != self.app_state.root_dir:
            self._send_text(HTTPStatus.NOT_FOUND, "Not found")
            return
        if clean_path not in ALLOWED_PUBLIC_PATHS and not self._get_session():
            if clean_path.endswith(".html"):
                self.send_response(HTTPStatus.FOUND)
                self.send_header("Location", f"/login.html?redirect={clean_path.lstrip('/')}")
                self.end_headers()
            else:
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "Authentication required"})
            return
        if not target.exists() or not target.is_file():
            if clean_path.startswith("/data/") or clean_path == "/assets/file-list.txt":
                upstream = "https://raw.githubusercontent.com/dw-dengwei/daily-arXiv-ai-enhanced/data" + clean_path
                try:
                    with urllib.request.urlopen(upstream, timeout=20) as response:
                        data = response.read()
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "private, max-age=300")
                    self.end_headers()
                    self.wfile.write(data)
                    return
                except urllib.error.URLError:
                    pass
            self._send_text(HTTPStatus.NOT_FOUND, "Not found")
            return
        data = target.read_bytes()
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def build_server(host: str = "0.0.0.0", port: int = 8000, root_dir: Path | None = None, db_path: Path | None = None) -> ThreadingHTTPServer:
    state = AppState(root_dir=root_dir, db_path=db_path)
    server = ThreadingHTTPServer((host, port), RequestHandler)
    server.app_state = state  # type: ignore[attr-defined]
    return server


def main() -> None:
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    server = build_server(host=host, port=port)

    def shutdown(_signum: int, _frame: Any) -> None:
        server.shutdown()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        server.serve_forever()
    finally:
        server.app_state.close()  # type: ignore[attr-defined]
        server.server_close()


if __name__ == "__main__":
    main()
