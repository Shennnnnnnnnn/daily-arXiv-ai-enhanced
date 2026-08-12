from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from server.app import (
    build_server,
    create_embeddings,
    fetch_zotero_corpus,
    filter_zotero_corpus,
    ensure_zotero_collection,
    load_latest_papers,
    make_zotero_payload,
    rank_papers_by_corpus,
    send_test_email,
    test_ai_configuration,
    test_zotero_configuration,
    validate_schedule,
)


class FakeResponse:
    def __init__(self, payload, status=200, headers=None):
        self.payload = payload
        self.status = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class FakeSMTP:
    sent_messages = []
    logins = []

    def __init__(self, host, port, timeout=20):
        self.host = host
        self.port = port

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def ehlo(self):
        return None

    def starttls(self):
        return None

    def login(self, user, password):
        self.logins.append((user, password))

    def send_message(self, message):
        self.sent_messages.append(message)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.password_patch = mock.patch.dict(os.environ, {"ADMIN_PASSWORD": "correct horse battery staple", "COOKIE_SECURE": "false"})
        self.password_patch.start()
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        (self.root / "js").mkdir()
        (self.root / "assets").mkdir()
        (self.root / "data").mkdir()
        (self.root / "index.html").write_text("<html>index</html>", encoding="utf-8")
        (self.root / "login.html").write_text("<html>login</html>", encoding="utf-8")
        (self.root / "js" / "api.js").write_text("console.log('api')", encoding="utf-8")
        (self.root / "js" / "auth.js").write_text("console.log('auth')", encoding="utf-8")
        (self.root / "assets" / "logo2-removebg-preview.png").write_bytes(b"png")
        (self.root / "run.sh").write_text("#!/bin/bash\necho run-ok\n", encoding="utf-8")
        os.chmod(self.root / "run.sh", 0o755)
        self.server = build_server("127.0.0.1", 0, root_dir=self.root, db_path=self.root / "data" / "test.sqlite3")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server.app_state.close()  # type: ignore[attr-defined]
        self.thread.join(timeout=2)
        self.tempdir.cleanup()
        self.password_patch.stop()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = None if body is None else json.dumps(body)
        request_headers = {"Content-Type": "application/json"}
        if headers:
            request_headers.update(headers)
        conn.request(method, path, body=payload, headers=request_headers)
        response = conn.getresponse()
        raw = response.read().decode("utf-8")
        content_type = response.getheader("Content-Type", "")
        parsed = json.loads(raw) if raw and "application/json" in content_type else raw
        response_headers = dict(response.getheaders())
        conn.close()
        return response.status, parsed, response_headers

    def login(self):
        status, payload, headers = self.request("POST", "/api/login", {"password": "correct horse battery staple", "remember": True})
        self.assertEqual(status, 200)
        return headers["Set-Cookie"], payload["csrfToken"]

    def test_auth_settings_favorites_and_jobs(self):
        status, payload, _headers = self.request("GET", "/api/session")
        self.assertEqual(status, 200)
        self.assertFalse(payload["authenticated"])

        cookie, csrf = self.login()
        auth_headers = {"Cookie": cookie}
        write_headers = {"Cookie": cookie, "X-CSRF-Token": csrf}

        status, payload, _headers = self.request("GET", "/api/settings", headers=auth_headers)
        self.assertEqual(status, 200)
        self.assertIn("schedule", payload)

        update = {
            "ai_api_key": "sk-test-secret",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o-mini",
            "schedule": "08:15",
            "categories": ["cs.AI"],
            "keywords": ["llm"],
            "authors": ["Ada Lovelace"],
            "zotero_id": "123",
            "zotero_key": "zkey",
            "zotero_target_collection": "我的文库/arxiv",
            "zotero_recommendation_enabled": True,
            "zotero_embedding_model": "text-embedding-3-small",
            "zotero_include_paths": ["Research/**"],
            "zotero_ignore_paths": ["Research/Archive/**"],
            "zotero_max_papers": 12,
            "paper_ai_assistant": "claude",
            "paper_ai_custom_url": "",
            "smtp_host": "smtp.example.com",
            "smtp_port": 587,
            "smtp_user": "bot@example.com",
            "smtp_password": "pw-secret",
            "email_from": "bot@example.com",
            "email_to": "reader@example.com",
            "email_enabled": True,
        }
        status, payload, _headers = self.request("PUT", "/api/settings", update, headers=write_headers)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ai_api_key_configured"])
        self.assertNotIn("ai_api_key", payload)
        self.assertNotIn("zotero_key", payload)
        self.assertNotIn("smtp_password", payload)
        self.assertEqual(payload["categories"], ["cs.AI"])
        self.assertTrue(payload["zotero_recommendation_enabled"])
        self.assertEqual(payload["zotero_include_paths"], ["Research/**"])
        self.assertEqual(payload["zotero_max_papers"], 12)
        self.assertEqual(payload["zotero_target_collection"], "我的文库/arxiv")
        self.assertEqual(payload["paper_ai_assistant"], "claude")

        paper = {"id": "paper/1", "title": "Paper", "authors": ["Ada Lovelace"], "category": ["cs.AI"], "summary": "S"}
        status, payload, _headers = self.request("PUT", "/api/favorites/paper%2F1", paper, headers=write_headers)
        self.assertEqual(status, 200)
        self.assertEqual(payload["paper"]["title"], "Paper")

        status, payload, _headers = self.request("GET", "/api/favorites", headers=auth_headers)
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["favorites"]), 1)

        with mock.patch.object(self.server.app_state, "start_job", return_value=(True, {"running": True, "last_trigger": "manual"})):  # type: ignore[attr-defined]
            status, payload, _headers = self.request("POST", "/api/jobs/run", {}, headers=write_headers)
        self.assertEqual(status, 200)
        self.assertTrue(payload["running"])

        status, payload, _headers = self.request("DELETE", "/api/favorites/paper%2F1", headers=write_headers)
        self.assertEqual(status, 200)
        self.assertTrue(payload["deleted"])

        status, payload, _headers = self.request(
            "POST", "/api/password",
            {"current_password": "correct horse battery staple", "new_password": "a different secure password"},
            headers=write_headers,
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(self.server.app_state.verify_password("a different secure password"))  # type: ignore[attr-defined]

        status, payload, _headers = self.request("POST", "/api/logout", {}, headers=write_headers)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_static_requires_auth(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/index.html")
        response = conn.getresponse()
        self.assertEqual(response.status, 302)
        self.assertIn("/login.html", response.getheader("Location"))
        response.read()
        conn.close()

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/login.html")
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        response.read()
        conn.close()

    def test_schedule_validation_and_helpers(self):
        self.assertEqual(validate_schedule("09:00"), (True, None))
        self.assertEqual(validate_schedule("*/15 * * * *"), (True, None))
        self.assertFalse(validate_schedule("99:00")[0])
        payload = make_zotero_payload({"title": "T", "authors": ["Ada Lovelace"], "category": ["cs.AI"]}, "COLLECTION")
        self.assertEqual(payload["title"], "T")
        self.assertEqual(payload["creators"][0]["lastName"], "Lovelace")
        self.assertEqual(payload["collections"], ["COLLECTION"])

        with self.assertRaises(ValueError):
            self.server.app_state.update_settings({"schedule": "09:00", "categories": "cs.AI"})  # type: ignore[attr-defined]
        with self.assertRaises(ValueError):
            self.server.app_state.update_settings({"schedule": "09:00", "smtp_port": 70000})  # type: ignore[attr-defined]
        with self.assertRaises(ValueError):
            self.server.app_state.update_settings({"schedule": "09:00", "zotero_max_papers": 101})  # type: ignore[attr-defined]
        with self.assertRaises(ValueError):
            self.server.app_state.update_settings({"schedule": "09:00", "paper_ai_assistant": "unknown"})  # type: ignore[attr-defined]
        with self.assertRaises(ValueError):
            self.server.app_state.update_settings({  # type: ignore[attr-defined]
                "schedule": "09:00", "paper_ai_assistant": "custom", "paper_ai_custom_url": "javascript:alert(1)"
            })

    def test_ai_and_zotero_configuration_helpers(self):
        requests = []

        def ai_opener(request, timeout=20):
            requests.append(request)
            return FakeResponse({"model": "provider-model", "choices": [{"message": {"content": "OK"}}]})

        result = test_ai_configuration("sk-secret", "https://llm.example/v1", "test-model", opener=ai_opener)
        self.assertEqual(result, {"ok": True, "model": "provider-model", "response": "OK"})
        self.assertEqual(requests[0].full_url, "https://llm.example/v1/chat/completions")
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer sk-secret")

        def zotero_opener(request, timeout=20):
            requests.append(request)
            return FakeResponse([{"data": {"title": "Library paper"}}], headers={"Total-Results": "42"})

        result = test_zotero_configuration("123", "z-secret", opener=zotero_opener)
        self.assertEqual(result["item_count"], 42)
        self.assertEqual(result["sample_title"], "Library paper")
        self.assertNotIn("z-secret", json.dumps(result))

    def test_zotero_target_collection_is_created_by_path(self):
        calls = []

        def opener(request, timeout=30):
            method = request.get_method()
            calls.append((method, request.full_url, request.data))
            if method == "GET":
                return FakeResponse([
                    {"key": "ROOT", "data": {"name": "Research", "parentCollection": False}}
                ], headers={"Total-Results": "1"})
            body = json.loads(request.data)
            self.assertEqual(body, [{"name": "arxiv", "parentCollection": "ROOT"}])
            return FakeResponse({"successful": {"0": {"key": "ARXIV"}}})

        key = ensure_zotero_collection("123", "secret", "我的文库/Research/arxiv", opener=opener)
        self.assertEqual(key, "ARXIV")
        self.assertEqual([call[0] for call in calls], ["GET", "POST"])

    def test_configuration_test_api_uses_unsaved_or_stored_secrets(self):
        cookie, csrf = self.login()
        write_headers = {"Cookie": cookie, "X-CSRF-Token": csrf}
        self.server.app_state.update_settings({  # type: ignore[attr-defined]
            "schedule": "09:00", "categories": [], "keywords": [], "authors": [],
            "ai_api_key": "stored-ai", "base_url": "https://stored.example/v1", "model": "stored-model",
            "zotero_id": "stored-id", "zotero_key": "stored-zotero",
        })
        with mock.patch("server.app.test_ai_configuration", return_value={"ok": True, "model": "draft-model", "response": "OK"}) as ai_test:
            status, payload, _headers = self.request("POST", "/api/ai/test", {
                "ai_api_key": "", "base_url": "https://draft.example/v1", "model": "draft-model"
            }, headers=write_headers)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        ai_test.assert_called_once_with("stored-ai", "https://draft.example/v1", "draft-model")

        with mock.patch("server.app.test_zotero_configuration", return_value={"ok": True, "item_count": 2}) as zotero_test:
            status, payload, _headers = self.request("POST", "/api/zotero/test", {
                "zotero_id": "draft-id", "zotero_key": ""
            }, headers=write_headers)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        zotero_test.assert_called_once_with("draft-id", "stored-zotero")

    def test_zotero_corpus_filter_and_ranking(self):
        corpus = [
            {"title": "Recent AI", "abstract": "models", "paths": ["Research/AI"], "date_added": "2026-08-01T00:00:00Z"},
            {"title": "Archived", "abstract": "old", "paths": ["Research/Archive/Old"], "date_added": "2020-01-01T00:00:00Z"},
            {"title": "Other", "abstract": "other", "paths": ["Personal"], "date_added": "2026-08-01T00:00:00Z"},
        ]
        filtered = filter_zotero_corpus(corpus, ["Research/**"], ["Research/Archive/**"])
        self.assertEqual([paper["title"] for paper in filtered], ["Recent AI"])

        papers = [{"title": "Relevant"}, {"title": "Unrelated"}]
        ranked = rank_papers_by_corpus(papers, [[1.0, 0.0], [0.0, 1.0]], corpus[:1], [[1.0, 0.0]])
        self.assertEqual(ranked[0]["title"], "Relevant")
        self.assertGreater(ranked[0]["recommendation_score"], ranked[1]["recommendation_score"])

    def test_zotero_corpus_fetch_embeddings_and_latest_data(self):
        def zotero_opener(request, timeout=30):
            if "/collections" in request.full_url:
                return FakeResponse([
                    {"key": "ROOT", "data": {"name": "Research", "parentCollection": False}},
                    {"key": "AI", "data": {"name": "AI", "parentCollection": "ROOT"}},
                ], headers={"Total-Results": "2"})
            return FakeResponse([
                {"data": {"title": "Library", "abstractNote": "Abstract", "dateAdded": "2026-08-01T00:00:00Z", "collections": ["AI"]}},
                {"data": {"title": "No abstract", "abstractNote": "", "collections": []}},
            ], headers={"Total-Results": "2"})

        corpus = fetch_zotero_corpus("123", "secret", opener=zotero_opener)
        self.assertEqual(corpus[0]["paths"], ["Research/AI"])
        self.assertEqual(len(corpus), 1)

        def embedding_opener(request, timeout=60):
            body = json.loads(request.data)
            return FakeResponse({"data": [
                {"index": index, "embedding": [float(index + 1), 0.5]}
                for index, _text in enumerate(body["input"])
            ]})

        vectors = create_embeddings("sk", "https://llm.example/v1/", "embedding-model", ["one", "two"], opener=embedding_opener)
        self.assertEqual(vectors, [[1.0, 0.5], [2.0, 0.5]])

        data_file = self.root / "data" / f"{time.strftime('%Y-%m-%d', time.gmtime())}_AI_enhanced_Chinese.jsonl"
        data_file.write_text(json.dumps({"title": "Today", "summary": "New"}) + "\n", encoding="utf-8")
        self.assertEqual(load_latest_papers(self.root)[0]["title"], "Today")

    def test_zotero_collection_pagination_preserves_paths(self):
        starts = []

        def opener(request, timeout=30):
            if "/collections" in request.full_url:
                start = int(request.full_url.split("start=")[1].split("&")[0])
                starts.append(start)
                if start == 0:
                    return FakeResponse([
                        {"key": f"C{index}", "data": {"name": f"Collection {index}", "parentCollection": False}}
                        for index in range(100)
                    ], headers={"Total-Results": "101"})
                return FakeResponse([
                    {"key": "LAST", "data": {"name": "Last Collection", "parentCollection": False}}
                ], headers={"Total-Results": "101"})
            return FakeResponse([
                {"data": {"title": "Last paper", "abstractNote": "Abstract", "dateAdded": "2026-08-01T00:00:00Z", "collections": ["LAST"]}}
            ], headers={"Total-Results": "1"})

        corpus = fetch_zotero_corpus("123", "secret", opener=opener)
        self.assertEqual(starts, [0, 100])
        self.assertEqual(corpus[0]["paths"], ["Last Collection"])

    def test_send_email_helper(self):
        FakeSMTP.sent_messages = []
        settings = {
            "email_enabled": True,
            "smtp_host": "smtp.example.com",
            "smtp_port": 587,
            "smtp_user": "bot@example.com",
            "email_from": "bot@example.com",
            "email_to": "reader@example.com",
        }
        result = send_test_email(settings, {"smtp_password": "pw"}, smtp_factory=FakeSMTP)
        self.assertTrue(result["sent"])
        self.assertEqual(len(FakeSMTP.sent_messages), 1)

        ssl_settings = {**settings, "smtp_port": 465}
        with mock.patch("server.app.smtplib.SMTP_SSL", FakeSMTP):
            result = send_test_email(ssl_settings, {"smtp_password": "pw"})
        self.assertTrue(result["sent"])
        self.assertEqual(len(FakeSMTP.sent_messages), 2)


if __name__ == "__main__":
    unittest.main()
