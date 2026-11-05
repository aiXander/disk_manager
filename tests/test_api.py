"""HTTP API tests (FastAPI TestClient). Everything lives under tmp_path; deletion is permanent-mode on tmp files only."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

import backend.scanner
from backend.main import create_app

API = "/api/v1"


@pytest.fixture()
def app_paths(tmp_path):
    return tmp_path / "scans", tmp_path / "config.json"


@pytest.fixture()
def client(app_paths):
    scans_dir, config_path = app_paths
    app = create_app(scans_dir=scans_dir, config_path=config_path, frontend_dir=None)
    with TestClient(app, base_url="http://localhost:8000") as c:
        yield c


@pytest.fixture()
def data_dir(tmp_path):
    root = tmp_path / "data"
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "big.bin").write_bytes(b"x" * 50_000)
    (root / "a" / "b" / "note.txt").write_text("hello\n" * 2000)
    (root / "pic.jpg").write_bytes(b"\xff\xd8\x00\x10" + bytes(range(256)) * 80)
    (root / "song.mp3").write_bytes(b"z" * 10_000)
    return root


def read_sse(client, scan_id) -> list[tuple[str, dict]]:
    events = []
    kind = None
    with client.stream("GET", f"{API}/scans/{scan_id}/stream") as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        for line in r.iter_lines():
            if line.startswith("event: "):
                kind = line[len("event: "):]
            elif line.startswith("data: "):
                events.append((kind, json.loads(line[len("data: "):])))
    return events


def scan(client, root) -> str:
    r = client.post(f"{API}/scans", json={"roots": [str(root)]})
    assert r.status_code == 200, r.text
    scan_id = r.json()["scan_id"]
    events = read_sse(client, scan_id)
    assert events[-1][0] == "complete", events[-1]
    return scan_id


def node(client, scan_id, path):
    return client.get(f"{API}/scans/{scan_id}/preview/meta", params={"path": str(path)})


# ---------------------------------------------------------------------------
# Scan lifecycle
# ---------------------------------------------------------------------------


def test_scan_start_stream_complete(client, data_dir):
    r = client.post(f"{API}/scans", json={"roots": [str(data_dir)]})
    scan_id = r.json()["scan_id"]
    events = read_sse(client, scan_id)
    kinds = [k for k, _ in events]
    assert kinds[-1] == "complete" and kinds.count("complete") == 1
    assert set(kinds[:-1]) <= {"progress"}
    assert events[-1][1]["files_scanned"] == 4

    info = client.get(f"{API}/scans/{scan_id}").json()
    assert info["status"] == "complete"
    assert info["roots"] == [str(data_dir.resolve())]
    assert client.get(f"{API}/config").json()["last_scan_paths"] == [str(data_dir)]

    tree = client.get(f"{API}/scans/{scan_id}/tree").json()
    assert tree["path"] == str(data_dir.resolve())
    assert tree["size"] > 0


def test_reconnect_after_completion_gets_terminal_event_without_rescanning(client, data_dir):
    scan_id = scan(client, data_dir)
    again = read_sse(client, scan_id)
    assert [k for k, _ in again] == ["complete"]
    assert len(client.get(f"{API}/scans").json()) == 1


def test_scans_list_newest_first(client, data_dir):
    first = scan(client, data_dir)
    second = scan(client, data_dir)
    ids = [s["scan_id"] for s in client.get(f"{API}/scans").json()]
    assert ids == [second, first]


@pytest.fixture()
def slow_scanner(monkeypatch):
    """Replace the scanner with one that runs until cancelled."""

    async def fake_scan_roots(scan_id, roots, cancel_event=None, **_):
        n = 0
        while not cancel_event.is_set():
            n += 1
            yield _event(scan_id, n, "running")
            await asyncio.sleep(0.01)
        yield _event(scan_id, n, "cancelled")

    monkeypatch.setattr(backend.scanner, "scan_roots", fake_scan_roots)


def _event(scan_id, n, status):
    return {
        "scan_id": scan_id, "files_scanned": n, "dirs_scanned": 0, "current_path": "/x",
        "bytes_seen": 0, "status": status, "error": None, "phase": "walking", "elapsed_s": 0.0, "errors": 0,
    }


def test_cancel(client, data_dir, slow_scanner):
    scan_id = client.post(f"{API}/scans", json={"roots": [str(data_dir)]}).json()["scan_id"]
    assert client.get(f"{API}/scans/{scan_id}").json()["status"] == "running"
    assert client.post(f"{API}/scans/{scan_id}/cancel").json() == {"cancelled": scan_id}
    events = read_sse(client, scan_id)
    assert events[-1][0] == "cancelled"
    assert client.post(f"{API}/scans/{scan_id}/cancel").status_code == 409
    assert client.post(f"{API}/scans/nope/cancel").status_code == 404


def test_running_scan_is_not_pruned_or_deletable(client, data_dir, slow_scanner):
    client.patch(f"{API}/config", json={"max_scan_sessions": 1})
    running = client.post(f"{API}/scans", json={"roots": [str(data_dir)]}).json()["scan_id"]
    assert client.delete(f"{API}/scans/{running}").status_code == 409
    other = client.post(f"{API}/scans", json={"roots": [str(data_dir)]}).json()["scan_id"]
    ids = {s["scan_id"] for s in client.get(f"{API}/scans").json()}
    assert ids == {running, other}
    for sid in ids:
        client.post(f"{API}/scans/{sid}/cancel")
        read_sse(client, sid)


@pytest.mark.parametrize(
    "roots, msg",
    [([], "at least one"), (["/definitely/not/here"], "does not exist"), (None, "Not a folder")],
)
def test_roots_validation(client, data_dir, roots, msg):
    if roots is None:
        roots = [str(data_dir / "pic.jpg")]
    r = client.post(f"{API}/scans", json={"roots": roots})
    assert r.status_code == 400
    assert msg in r.json()["detail"]


def test_prune_removes_db_wal_and_shm(client, data_dir, app_paths):
    scans_dir, _ = app_paths
    client.patch(f"{API}/config", json={"max_scan_sessions": 2})
    first = scan(client, data_dir)
    for suffix in (".db-wal", ".db-shm"):
        (scans_dir / f"{first}{suffix}").touch()
    scan(client, data_dir)
    scan(client, data_dir)
    assert len(client.get(f"{API}/scans").json()) == 2
    assert not list(scans_dir.glob(f"{first}*"))


def test_stale_running_scan_marked_interrupted(app_paths):
    scans_dir, config_path = app_paths
    scans_dir.mkdir(parents=True)
    from backend import db

    conn = db.init_db(scans_dir / "scan_20200101_000000_abcdef.db")
    conn.execute(
        "INSERT INTO scan_meta (scan_id, started_at, roots, max_depth, status) VALUES (?, 1, '/x', 5, 'running')",
        ("scan_20200101_000000_abcdef",),
    )
    conn.commit()
    conn.close()
    app = create_app(scans_dir=scans_dir, config_path=config_path, frontend_dir=None)
    with TestClient(app, base_url="http://localhost") as c:
        [info] = c.get(f"{API}/scans").json()
        assert info["status"] == "interrupted"
        events = read_sse(c, info["scan_id"])
        assert [k for k, _ in events] == ["error"]


# ---------------------------------------------------------------------------
# Data endpoints
# ---------------------------------------------------------------------------


def test_files_summary_categories(client, data_dir):
    scan_id = scan(client, data_dir)
    root = str(data_dir.resolve())
    r = client.get(f"{API}/scans/{scan_id}/files", params={"is_dir": "false", "sort": "size_desc"}).json()
    assert r["total"] == 4
    assert r["items"][0]["name"] == "big.bin"
    assert r["total_size"] == sum(i["size"] for i in r["items"])
    assert {"apparent_size", "file_count", "collapsed", "category"} <= set(r["items"][0])

    r = client.get(f"{API}/scans/{scan_id}/files", params={"under": f"{root}/a", "is_dir": "false"}).json()
    assert {i["name"] for i in r["items"]} == {"big.bin", "note.txt"}

    r = client.get(f"{API}/scans/{scan_id}/files", params={"categories": "image,audio"}).json()
    assert {i["category"] for i in r["items"]} == {"image", "audio"}

    assert client.get(f"{API}/scans/{scan_id}/files", params={"sort": "bogus"}).status_code == 422

    s = client.get(f"{API}/scans/{scan_id}/summary").json()
    assert s["total_files"] == 4
    assert {c["category"] for c in s["by_category"]} == {"image", "audio", "document", "data"}

    cats = client.get(f"{API}/categories").json()
    assert "image" in cats["categories"]


def test_preview_meta_and_text(client, data_dir):
    scan_id = scan(client, data_dir)
    note = data_dir.resolve() / "a" / "b" / "note.txt"
    meta = node(client, scan_id, note).json()
    assert meta["exists"] is True and meta["category"] == "document"
    assert node(client, scan_id, "/etc/passwd").status_code == 404

    text = client.get(f"{API}/scans/{scan_id}/preview/text", params={"path": str(note)})
    assert text.status_code == 200 and text.json()["text"].startswith("hello")
    binary = client.get(f"{API}/scans/{scan_id}/preview/text", params={"path": str(data_dir.resolve() / "pic.jpg")})
    assert binary.status_code == 415

    thumb = client.get(f"{API}/scans/{scan_id}/preview/thumbnail", params={"path": str(note)})
    assert thumb.status_code == 200 and thumb.headers["content-type"].startswith("image/")

    r = client.post(f"{API}/scans/{scan_id}/preview/open", json={"path": "/etc/passwd"})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Staging & deletion
# ---------------------------------------------------------------------------


def test_staging_add_remove_total_without_double_count(client, data_dir):
    scan_id = scan(client, data_dir)
    root = data_dir.resolve()
    a_size = node(client, scan_id, root / "a").json()["size"]
    big_size = node(client, scan_id, root / "a" / "big.bin").json()["size"]

    base = f"{API}/scans/{scan_id}/staging"
    state = client.post(f"{base}/add", json={"paths": [str(root / "a"), str(root / "a" / "big.bin"), "/not/in/scan"]}).json()
    assert state["item_count"] == 2 and state["dir_count"] == 1 and state["file_count"] == 1
    assert state["total_size"] == a_size

    state = client.post(f"{base}/remove", json={"paths": [str(root / "a")]}).json()
    assert state["item_count"] == 1 and state["total_size"] == big_size
    assert client.get(base).json() == state
    assert client.delete(base).json()["item_count"] == 0


def test_execute_permanent_updates_tree(client, data_dir):
    scan_id = scan(client, data_dir)
    root = data_dir.resolve()
    before = client.get(f"{API}/scans/{scan_id}/tree", params={"depth": 1}).json()["size"]
    sizes = {p: node(client, scan_id, p).json()["size"] for p in (root / "a" / "b", root / "pic.jpg")}

    base = f"{API}/scans/{scan_id}/staging"
    client.post(f"{base}/add", json={"paths": [str(p) for p in sizes] + [str(root / "a" / "b" / "note.txt")]})
    result = client.post(f"{base}/execute", json={"permanent": True}).json()

    assert sorted(result["deleted"]) == sorted(str(p) for p in sizes)
    assert result["failed"] == []
    assert result["bytes_freed"] == sum(sizes.values())
    assert not (root / "a" / "b").exists() and not (root / "pic.jpg").exists()
    assert (root / "a" / "big.bin").exists() and (root / "song.mp3").exists()

    after = client.get(f"{API}/scans/{scan_id}/tree", params={"depth": 1}).json()["size"]
    assert after == before - result["bytes_freed"]
    assert node(client, scan_id, root / "a" / "b" / "note.txt").status_code == 404
    assert client.get(base).json()["item_count"] == 0


def test_execute_reports_vanished_path_and_drops_it(client, data_dir):
    scan_id = scan(client, data_dir)
    song = data_dir.resolve() / "song.mp3"
    base = f"{API}/scans/{scan_id}/staging"
    client.post(f"{base}/add", json={"paths": [str(song)]})
    song.unlink()
    result = client.post(f"{base}/execute", json={"permanent": True}).json()
    assert result["deleted"] == [] and result["bytes_freed"] == 0
    assert result["failed"] == [{"path": str(song), "error": "no longer exists on disk"}]
    assert node(client, scan_id, song).status_code == 404
    assert client.get(base).json()["item_count"] == 0
    assert client.post(f"{base}/execute", json={"permanent": True}).status_code == 400


def test_delete_scan(client, data_dir, app_paths):
    scans_dir, _ = app_paths
    scan_id = scan(client, data_dir)
    assert client.delete(f"{API}/scans/{scan_id}").json() == {"deleted": scan_id}
    assert not list(scans_dir.glob(f"{scan_id}*"))
    assert client.get(f"{API}/scans/{scan_id}").status_code == 404


# ---------------------------------------------------------------------------
# Security & config
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8000", "localhost.evil.example", "192.168.1.5:8000"])
def test_foreign_host_header_rejected(client, host):
    r = client.get(f"{API}/config", headers={"host": host})
    assert r.status_code == 403


@pytest.mark.parametrize("host", ["localhost", "localhost:1234", "127.0.0.1:8000", "[::1]:8000"])
def test_local_host_header_allowed(client, host):
    assert client.get(f"{API}/config", headers={"host": host}).status_code == 200


def test_cross_origin_mutation_rejected(client):
    body = {"page_size": 50}
    assert client.patch(f"{API}/config", json=body, headers={"origin": "https://evil.example"}).status_code == 403
    assert client.patch(f"{API}/config", json=body, headers={"origin": "null"}).status_code == 403
    assert client.patch(f"{API}/config", json=body, headers={"origin": "http://127.0.0.1:5173"}).status_code == 200


def test_config_validation(client):
    assert client.patch(f"{API}/config", json={"bogus": 1}).status_code == 422
    assert client.patch(f"{API}/config", json={"page_size": 0}).status_code == 422
    r = client.patch(f"{API}/config", json={"page_size": 42})
    assert r.status_code == 200 and r.json()["page_size"] == 42



def test_frontend_served_with_no_cache(app_paths, tmp_path):
    scans_dir, config_path = app_paths
    front = tmp_path / "front"
    front.mkdir()
    (front / "index.html").write_text("<!doctype html><title>x</title>")
    (front / "app.js").write_text("export {};")
    app = create_app(scans_dir=scans_dir, config_path=config_path, frontend_dir=front)
    with TestClient(app, base_url="http://localhost") as c:
        for url in ("/", "/app.js"):
            r = c.get(url)
            assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
