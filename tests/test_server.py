"""app/server.py 全端点冒烟 + 边界（DESIGN §3 §6 server 行）。

夹具一律自造：srt 复用 conftest 的 FIXTURE_SRT（自造句子），词典用
build_ecdict 的 mini 夹具（100 词自造条目），媒体用 os.urandom 生成的假文件。
本文件不发任何网络请求。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_ecdict  # noqa: E402

from app.ingest import ingest_srt  # noqa: E402
from app.server import create_app, parse_range  # noqa: E402
from tests.conftest import FIXTURE_SRT  # noqa: E402

MEDIA_SIZE = 1024 * 1024  # 1MB 假媒体


# --- 夹具 ------------------------------------------------------------------


@pytest.fixture()
def media_file(tmp_path: Path) -> Path:
    p = tmp_path / "fake_episode.mp4"
    p.write_bytes(os.urandom(MEDIA_SIZE))
    return p


@pytest.fixture()
def ecdict_path(tmp_path: Path) -> Path:
    p = tmp_path / "ecdict_mini.db"
    build_ecdict.build_mini(p)
    return p


@pytest.fixture()
def env(tmp_path: Path, media_file: Path, ecdict_path: Path) -> dict:
    """建库 + ingest 一集自造字幕，返回上下文。"""
    srt = tmp_path / "fixture.srt"
    srt.write_text(FIXTURE_SRT, encoding="utf-8")
    db = tmp_path / "poi.db"
    stats = ingest_srt(
        db_path=db,
        srt_path=srt,
        title="Test Show",
        season_ep="s01e01",
        video_path=str(media_file),
    )
    return {
        "db": db,
        "ecdict": ecdict_path,
        "media": media_file,
        "content_id": stats["content_id"],
    }


@pytest.fixture()
def client(env: dict):
    app = create_app(db_path=env["db"], ecdict_path=env["ecdict"])
    with TestClient(app) as c:
        c.env = env  # type: ignore[attr-defined]
        yield c


def first_segment_id(client: TestClient, content_id: int) -> int:
    segs = client.get("/segments", params={"content_id": content_id}).json()["segments"]
    return segs[0]["id"]


# --- 配置 ------------------------------------------------------------------


def test_env_vars_are_honored(tmp_path, monkeypatch):
    monkeypatch.setenv("POI_DB", str(tmp_path / "from_env.db"))
    monkeypatch.setenv("POI_ECDICT", str(tmp_path / "dict_from_env.db"))
    app = create_app()
    assert app.state.db_path == tmp_path / "from_env.db"
    assert app.state.ecdict_path == tmp_path / "dict_from_env.db"
    # 建表推迟到首个请求：import/建 app 不该造文件
    assert not (tmp_path / "from_env.db").exists()
    with TestClient(app) as c:
        assert c.get("/episodes").json() == {"episodes": []}
    assert (tmp_path / "from_env.db").exists()


def test_explicit_args_beat_env(tmp_path, monkeypatch):
    monkeypatch.setenv("POI_DB", str(tmp_path / "env.db"))
    app = create_app(db_path=tmp_path / "explicit.db")
    assert app.state.db_path == tmp_path / "explicit.db"


# --- / 与静态托管 ----------------------------------------------------------


def test_root_redirects_to_player(client: TestClient):
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (307, 302)
    assert r.headers["location"] == "/static/player.html"


def test_static_mount_exists_even_without_player_file(client: TestClient):
    # player.html 尚未落地：挂载点在，返回 404 而不是 500/路由缺失
    r = client.get("/static/definitely-missing.html")
    assert r.status_code == 404


# --- /episodes -------------------------------------------------------------


def test_episodes_lists_ingested_content(client: TestClient):
    body = client.get("/episodes").json()
    assert len(body["episodes"]) == 1
    ep = body["episodes"][0]
    assert ep["title"] == "Test Show"
    assert ep["season_ep"] == "s01e01"
    assert ep["segments"] == 5
    assert ep["duration"] == pytest.approx(18.0)
    assert ep["has_video"] is True
    assert ep["media_url"] == f"/media/{ep['id']}"


def test_episodes_marks_missing_video(client: TestClient, env: dict):
    env["media"].unlink()
    assert client.get("/episodes").json()["episodes"][0]["has_video"] is False


# --- /media（Range） -------------------------------------------------------


def test_media_full_stream_without_range(client: TestClient, env: dict):
    r = client.get(f"/media/{env['content_id']}")
    assert r.status_code == 200
    assert r.headers["accept-ranges"] == "bytes"
    assert int(r.headers["content-length"]) == MEDIA_SIZE
    assert r.content == env["media"].read_bytes()


def test_media_middle_range(client: TestClient, env: dict):
    raw = env["media"].read_bytes()
    r = client.get(f"/media/{env['content_id']}", headers={"Range": "bytes=1000-1999"})
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes 1000-1999/{MEDIA_SIZE}"
    assert int(r.headers["content-length"]) == 1000
    assert r.content == raw[1000:2000]


def test_media_open_ended_range(client: TestClient, env: dict):
    raw = env["media"].read_bytes()
    start = MEDIA_SIZE - 4096
    r = client.get(f"/media/{env['content_id']}", headers={"Range": f"bytes={start}-"})
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes {start}-{MEDIA_SIZE - 1}/{MEDIA_SIZE}"
    assert r.content == raw[start:]


def test_media_suffix_range(client: TestClient, env: dict):
    raw = env["media"].read_bytes()
    r = client.get(f"/media/{env['content_id']}", headers={"Range": "bytes=-512"})
    assert r.status_code == 206
    assert r.content == raw[-512:]
    assert r.headers["content-range"] == f"bytes {MEDIA_SIZE - 512}-{MEDIA_SIZE - 1}/{MEDIA_SIZE}"


def test_media_range_end_beyond_eof_is_clamped(client: TestClient, env: dict):
    raw = env["media"].read_bytes()
    start = MEDIA_SIZE - 100
    r = client.get(
        f"/media/{env['content_id']}", headers={"Range": f"bytes={start}-99999999"}
    )
    assert r.status_code == 206
    assert r.content == raw[start:]


def test_media_range_out_of_bounds_416(client: TestClient, env: dict):
    r = client.get(
        f"/media/{env['content_id']}", headers={"Range": f"bytes={MEDIA_SIZE}-"}
    )
    assert r.status_code == 416
    assert r.headers["content-range"] == f"bytes */{MEDIA_SIZE}"
    assert r.content == b""


def test_media_multi_range_serves_first_part(client: TestClient, env: dict):
    raw = env["media"].read_bytes()
    r = client.get(
        f"/media/{env['content_id']}", headers={"Range": "bytes=0-99,200-299"}
    )
    assert r.status_code == 206
    assert r.content == raw[:100]


def test_media_garbage_range_falls_back_to_full(client: TestClient, env: dict):
    r = client.get(f"/media/{env['content_id']}", headers={"Range": "pages=1-2"})
    assert r.status_code == 200
    assert len(r.content) == MEDIA_SIZE


def test_media_head_reports_size_without_body(client: TestClient, env: dict):
    r = client.head(f"/media/{env['content_id']}")
    assert r.status_code == 200
    assert int(r.headers["content-length"]) == MEDIA_SIZE
    assert r.headers["accept-ranges"] == "bytes"
    assert r.content == b""


def test_media_404_unknown_content(client: TestClient):
    assert client.get("/media/999").status_code == 404


def test_media_404_when_file_gone(client: TestClient, env: dict):
    env["media"].unlink()
    assert client.get(f"/media/{env['content_id']}").status_code == 404


def test_media_404_when_no_video_path(client: TestClient, env: dict):
    conn = sqlite3.connect(str(env["db"]))
    conn.execute("UPDATE Content SET video_path = NULL")
    conn.commit()
    conn.close()
    assert client.get(f"/media/{env['content_id']}").status_code == 404


@pytest.mark.parametrize(
    "header,size,expect",
    [
        (None, 100, ("full", None)),
        ("", 100, ("full", None)),
        ("bytes=0-", 100, ("partial", (0, 99))),
        ("bytes=10-20", 100, ("partial", (10, 20))),
        ("bytes=-10", 100, ("partial", (90, 99))),
        ("bytes=-500", 100, ("partial", (0, 99))),  # suffix 超过文件大小 → 全量
        ("bytes=99-", 100, ("partial", (99, 99))),
        ("bytes=100-", 100, ("unsatisfiable", None)),
        ("bytes=-0", 100, ("unsatisfiable", None)),
        ("bytes=0-", 0, ("unsatisfiable", None)),  # 空文件
        ("bytes=20-10", 100, ("full", None)),  # 无效区间 → 忽略 Range
        ("bytes=abc", 100, ("full", None)),
        ("items=0-10", 100, ("full", None)),
    ],
)
def test_parse_range_table(header, size, expect):
    kind, spec = parse_range(header, size)
    assert kind == expect[0]
    if expect[1] is None:
        assert spec is None
    else:
        assert (spec.start, spec.end) == expect[1]


# --- /segments -------------------------------------------------------------


def test_segments_shape(client: TestClient, env: dict):
    body = client.get("/segments", params={"content_id": env["content_id"]}).json()
    assert body["title"] == "Test Show"
    segs = body["segments"]
    assert [s["idx"] for s in segs] == [1, 2, 3, 4, 5]
    first = segs[0]
    assert first["t_start"] == pytest.approx(1.0)
    assert first["text_en"] == "The tall gardener went home early."
    surfaces = [t["surface"] for t in first["tokens"]]
    assert surfaces == ["the", "tall", "gardener", "went", "home", "early"]
    assert all({"surface", "lemma", "char_start", "char_end"} <= set(t) for t in first["tokens"])
    assert [t["lemma"] for t in first["tokens"]][3] == "go"
    assert first["word_boxes"] is None  # OCR 词框尚未回填
    # 同句重复词各自成 token
    dup = [s for s in segs if "cameras" in s["text_en"]][0]
    assert [t["surface"] for t in dup["tokens"]].count("cameras") == 2


def test_segments_404_unknown_content(client: TestClient):
    assert client.get("/segments", params={"content_id": 999}).status_code == 404


def test_segments_requires_content_id(client: TestClient):
    assert client.get("/segments").status_code == 422


# --- /lookup ---------------------------------------------------------------


def test_lookup_known_word_hits_ecdict(client: TestClient, env: dict):
    seg = first_segment_id(client, env["content_id"])
    r = client.get("/lookup", params={"surface": "home", "segment_id": seg})
    assert r.status_code == 200
    body = r.json()
    assert body["surface"] == "home"
    assert body["lemma"] == "home"
    assert body["in_dict"] is True
    assert body["collected"] is False
    assert body["ipa"] == "həʊm"
    assert "家" in body["dict_gloss"]
    assert body["pos"] == "n"
    assert body["sentence"] == "The tall gardener went home early."


def test_lookup_is_case_insensitive(client: TestClient):
    a = client.get("/lookup", params={"surface": "Home"}).json()
    b = client.get("/lookup", params={"surface": "  HOME "}).json()
    assert a["surface"] == b["surface"] == "home"
    assert a["dict_gloss"] == b["dict_gloss"]


def test_lookup_inflected_surface_maps_to_lemma(client: TestClient):
    # 'cousins' 由 ingest 建了 WordForm → Lexeme('cousin')，mini 词典只收 'cousin'
    body = client.get("/lookup", params={"surface": "cousins"}).json()
    assert body["lemma"] == "cousin"
    assert body["in_dict"] is True
    assert "堂表" in body["dict_gloss"]


def test_lookup_contraction(client: TestClient):
    # 缩写词：ingest 把 it's 整体存成 token，词元归到 it（mini 词典没收 it）
    body = client.get("/lookup", params={"surface": "it's"}).json()
    assert body["surface"] == "it's"
    assert body["lemma"] == "it"
    assert body["in_dict"] is False
    assert body["lexeme_id"] is not None  # 词元行由 ingest 建好了
    # don't → do，mini 词典收了 do
    body = client.get("/lookup", params={"surface": "DON'T"}).json()
    assert body["lemma"] == "do"
    assert body["in_dict"] is True
    assert body["ipa"] == "duː"


def test_lookup_proper_noun_not_in_dict(client: TestClient):
    body = client.get("/lookup", params={"surface": "Halloway"}).json()
    assert body["lemma"] == "halloway"
    assert body["in_dict"] is False
    assert body["dict_gloss"] is None
    assert body["ipa"] is None
    assert body["lexeme_id"] is not None  # 专名不排除，ingest 已建词元


def test_lookup_word_outside_this_episode(client: TestClient):
    """没进过 ingest 的词：无 Lexeme 行，但词典命中照样给释义，且不写库。"""
    body = client.get("/lookup", params={"surface": "stakeout"}).json()
    assert body["lexeme_id"] is None
    assert body["in_dict"] is True
    assert body["ipa"] == "ˈsteɪkaʊt"
    assert body["collected"] is False
    conn = sqlite3.connect(str(client.env["db"]))  # type: ignore[attr-defined]
    assert conn.execute(
        "SELECT COUNT(*) FROM Lexeme WHERE lemma='stakeout'"
    ).fetchone()[0] == 0
    conn.close()


def test_lookup_backfills_lexeme_cache(env: dict):
    """本用例要删词典文件，所以不用 client 夹具：TestClient 必须先退出。

    Windows 上没关掉的 sqlite 连接会锁住 ecdict.db，unlink 直接 PermissionError
    （工单 8c-3）。退出 with 块触发 lifespan 关闭 → EcdictStore.close_all()。
    """
    app = create_app(db_path=env["db"], ecdict_path=env["ecdict"])
    conn = sqlite3.connect(str(env["db"]))
    conn.row_factory = sqlite3.Row
    before = conn.execute("SELECT * FROM Lexeme WHERE lemma='home'").fetchone()
    assert before["ipa"] is None and before["dict_gloss"] is None  # ingest 只建骨架

    with TestClient(app) as c:
        c.get("/lookup", params={"surface": "home"})

    after = conn.execute("SELECT * FROM Lexeme WHERE lemma='home'").fetchone()
    assert after["ipa"] == "həʊm"
    assert after["dict_gloss"] and after["pos"] == "n"
    conn.close()

    # 缓存生效：词典文件消失后仍然能查到，且 in_dict 保持 true
    env["ecdict"].unlink()
    with TestClient(app) as c:
        body = c.get("/lookup", params={"surface": "home"}).json()
    assert body["in_dict"] is True
    assert body["ipa"] == "həʊm"


def test_lookup_degrades_without_ecdict(tmp_path, env: dict):
    app = create_app(db_path=env["db"], ecdict_path=tmp_path / "no_such_dict.db")
    with TestClient(app) as c:
        r = c.get("/lookup", params={"surface": "home"})
    assert r.status_code == 200
    body = r.json()
    assert body["in_dict"] is False
    assert body["dict_gloss"] is None
    assert body["lemma"] == "home"


def test_lookup_degrades_on_corrupt_ecdict(tmp_path, env: dict):
    bad = tmp_path / "corrupt.db"
    bad.write_bytes(b"this is not a sqlite file" * 100)
    app = create_app(db_path=env["db"], ecdict_path=bad)
    with TestClient(app) as c:
        body = c.get("/lookup", params={"surface": "home"}).json()
    assert body["in_dict"] is False


def test_lookup_bad_input(client: TestClient, env: dict):
    assert client.get("/lookup").status_code == 422
    assert client.get("/lookup", params={"surface": ""}).status_code == 422
    assert client.get("/lookup", params={"surface": "..."}).status_code == 400
    assert (
        client.get("/lookup", params={"surface": "home", "segment_id": 9999}).status_code
        == 404
    )


def test_lookup_p50_under_50ms(client: TestClient, env: dict):
    """DESIGN §3 性能红线：本地 P50 < 50ms（纯 SQLite）。"""
    words = ["home", "cousins", "gardener", "don't", "halloway", "raining", "believe"]
    seg = first_segment_id(client, env["content_id"])
    for w in words:  # 预热（首次查会回填缓存）
        client.get("/lookup", params={"surface": w, "segment_id": seg})
    samples = []
    for i in range(60):
        w = words[i % len(words)]
        t0 = time.perf_counter()
        r = client.get("/lookup", params={"surface": w, "segment_id": seg})
        samples.append((time.perf_counter() - t0) * 1000)
        assert r.status_code == 200
    samples.sort()
    p50 = samples[len(samples) // 2]
    assert p50 < 50, f"/lookup P50={p50:.1f}ms 超过 50ms 红线"


# --- /collect --------------------------------------------------------------


def test_collect_creates_entry_encounter_and_job(client: TestClient, env: dict):
    seg = first_segment_id(client, env["content_id"])
    r = client.post("/collect", json={"surface": "gardener", "segment_id": seg})
    assert r.status_code == 200
    body = r.json()
    assert body["created"] is True
    assert body["collected"] is True
    assert body["job_created"] is True
    assert body["encounters"] == 1

    conn = sqlite3.connect(str(env["db"]))
    conn.row_factory = sqlite3.Row
    job = conn.execute("SELECT * FROM AnnotationJob").fetchall()
    assert len(job) == 1
    assert job[0]["status"] == "queued"
    assert job[0]["priority"] == 10  # 收藏 = 高优先级插队
    assert conn.execute("SELECT COUNT(*) FROM Encounter").fetchone()[0] == 1
    conn.close()

    assert client.get("/lookup", params={"surface": "gardener"}).json()["collected"] is True


def test_collect_backfills_dict_fields(client: TestClient, env: dict):
    seg = first_segment_id(client, env["content_id"])
    body = client.post("/collect", json={"surface": "home", "segment_id": seg}).json()
    assert body["in_dict"] is True
    assert body["ipa"] == "həʊm"


def test_collect_is_idempotent_and_only_adds_encounters(client: TestClient, env: dict):
    segs = client.get("/segments", params={"content_id": env["content_id"]}).json()[
        "segments"
    ]
    seg1, seg2 = segs[0]["id"], segs[2]["id"]
    first = client.post("/collect", json={"surface": "home", "segment_id": seg1}).json()
    second = client.post("/collect", json={"surface": "home", "segment_id": seg2}).json()
    third = client.post("/collect", json={"surface": "home", "segment_id": seg1}).json()

    assert second["created"] is False and third["created"] is False
    assert second["job_created"] is False and third["job_created"] is False
    assert first["vocab_entry_id"] == second["vocab_entry_id"] == third["vocab_entry_id"]
    assert first["job_id"] == second["job_id"]
    assert third["encounters"] == 3

    conn = sqlite3.connect(str(env["db"]))
    assert conn.execute("SELECT COUNT(*) FROM VocabEntry").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM AnnotationJob").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM Encounter").fetchone()[0] == 3
    conn.close()


def test_collect_different_surfaces_same_lexeme_share_entry(client: TestClient, env: dict):
    segs = client.get("/segments", params={"content_id": env["content_id"]}).json()[
        "segments"
    ]
    a = client.post("/collect", json={"surface": "cameras", "segment_id": segs[2]["id"]}).json()
    b = client.post("/collect", json={"surface": "Camera", "segment_id": segs[2]["id"]}).json()
    assert a["lemma"] == b["lemma"] == "camera"
    assert a["vocab_entry_id"] == b["vocab_entry_id"]
    assert b["created"] is False


def test_collect_creates_lexeme_for_unseen_word(client: TestClient, env: dict):
    seg = first_segment_id(client, env["content_id"])
    body = client.post("/collect", json={"surface": "Stakeout", "segment_id": seg}).json()
    assert body["lemma"] == "stakeout"
    assert body["created"] is True
    assert body["in_dict"] is True
    conn = sqlite3.connect(str(env["db"]))
    assert conn.execute(
        "SELECT COUNT(*) FROM WordForm WHERE surface='stakeout'"
    ).fetchone()[0] == 1
    conn.close()


def test_collect_requeues_after_failed_job(client: TestClient, env: dict):
    seg = first_segment_id(client, env["content_id"])
    first = client.post("/collect", json={"surface": "home", "segment_id": seg}).json()
    conn = sqlite3.connect(str(env["db"]))
    conn.execute("UPDATE AnnotationJob SET status='failed' WHERE id=?", (first["job_id"],))
    conn.commit()
    second = client.post("/collect", json={"surface": "home", "segment_id": seg}).json()
    assert second["job_created"] is True
    assert conn.execute(
        "SELECT COUNT(*) FROM AnnotationJob WHERE status='queued'"
    ).fetchone()[0] == 1
    conn.close()


def test_collect_bumps_priority_of_preheat_job(client: TestClient, env: dict):
    """预热任务（低优先级）已在队列时，收藏把它提到高优先级而不是重复建。"""
    conn = sqlite3.connect(str(env["db"]))
    conn.row_factory = sqlite3.Row
    lex = conn.execute("SELECT id FROM Lexeme WHERE lemma='home'").fetchone()["id"]
    conn.execute(
        "INSERT INTO AnnotationJob (lexeme_id, status, priority, created_at) "
        "VALUES (?, 'queued', 0, '2026-01-01T00:00:00+00:00')",
        (lex,),
    )
    conn.commit()
    seg = first_segment_id(client, env["content_id"])
    body = client.post("/collect", json={"surface": "home", "segment_id": seg}).json()
    assert body["job_created"] is False
    rows = conn.execute("SELECT * FROM AnnotationJob").fetchall()
    assert len(rows) == 1 and rows[0]["priority"] == 10
    conn.close()


def test_collect_bad_input(client: TestClient, env: dict):
    seg = first_segment_id(client, env["content_id"])
    assert client.post("/collect", json={"surface": "home"}).status_code == 422
    assert client.post("/collect", json={"segment_id": seg}).status_code == 422
    assert (
        client.post("/collect", json={"surface": "home", "segment_id": 9999}).status_code
        == 404
    )
    assert (
        client.post("/collect", json={"surface": "!!!", "segment_id": seg}).status_code
        == 400
    )


# --- /vocab ----------------------------------------------------------------


def test_vocab_empty(client: TestClient):
    assert client.get("/vocab").json() == {"count": 0, "vocab": []}


def test_vocab_structure_with_encounters(client: TestClient, env: dict):
    segs = client.get("/segments", params={"content_id": env["content_id"]}).json()[
        "segments"
    ]
    client.post("/collect", json={"surface": "home", "segment_id": segs[0]["id"]})
    client.post("/collect", json={"surface": "home", "segment_id": segs[1]["id"]})
    client.post("/collect", json={"surface": "cameras", "segment_id": segs[2]["id"]})

    body = client.get("/vocab").json()
    assert body["count"] == 2
    by_lemma = {v["lemma"]: v for v in body["vocab"]}
    assert set(by_lemma) == {"home", "camera"}

    home = by_lemma["home"]
    assert home["ipa"] == "həʊm" and home["dict_gloss"]
    assert home["mnemonic_status"] == "queued"
    assert home["has_mnemonic"] is False
    assert home["encounter_count"] == 2
    enc = home["encounters"][0]
    assert enc["surface"] == "home"
    assert enc["sentence"] == segs[0]["text_en"]
    assert enc["t_start"] == segs[0]["t_start"]
    assert enc["season_ep"] == "s01e01"
    assert enc["title"] == "Test Show"
    assert home["encounters"][1]["segment_id"] == segs[1]["id"]

    cam = by_lemma["camera"]
    assert cam["encounter_count"] == 1
    assert cam["encounters"][0]["surface"] == "cameras"


# --- /mnemonic -------------------------------------------------------------


def test_mnemonic_reports_job_status_when_pending(client: TestClient, env: dict):
    seg = first_segment_id(client, env["content_id"])
    lexeme_id = client.post(
        "/collect", json={"surface": "home", "segment_id": seg}
    ).json()["lexeme_id"]
    body = client.get("/mnemonic", params={"lexeme_id": lexeme_id}).json()
    assert body["status"] == "queued"
    assert body["mnemonics"] == []
    assert body["job"]["priority"] == 10
    assert body["lemma"] == "home"


def test_mnemonic_returns_payload_when_done(client: TestClient, env: dict):
    seg = first_segment_id(client, env["content_id"])
    collected = client.post(
        "/collect", json={"surface": "home", "segment_id": seg}
    ).json()
    lexeme_id = collected["lexeme_id"]
    payload = (
        '{"context_gloss":"(自造样例) 回到住处","hooks":[{"type":"morph",'
        '"text":"ho + me","label":"拆分助记，未经词源核验"}]}'
    )
    conn = sqlite3.connect(str(env["db"]))
    conn.execute(
        "INSERT INTO Mnemonic (lexeme_id, kind, payload_json, provider, version) "
        "VALUES (?,'card',?,'test',1)",
        (lexeme_id, '{"context_gloss":"旧版"}'),
    )
    conn.execute(
        "INSERT INTO Mnemonic (lexeme_id, kind, payload_json, provider, version) "
        "VALUES (?,'card',?,'test',2)",
        (lexeme_id, payload),
    )
    conn.execute(
        "UPDATE AnnotationJob SET status='done', done_at='2026-01-01T00:00:00+00:00' "
        "WHERE id=?",
        (collected["job_id"],),
    )
    conn.commit()
    conn.close()

    body = client.get("/mnemonic", params={"lexeme_id": lexeme_id}).json()
    assert body["status"] == "done"
    assert len(body["mnemonics"]) == 1  # 每个 kind 只吐最新版
    card = body["mnemonics"][0]
    assert card["version"] == 2
    assert card["payload"]["hooks"][0]["type"] == "morph"
    assert card["edited_by_user"] is False

    assert client.get("/vocab").json()["vocab"][0]["has_mnemonic"] is True


def test_mnemonic_status_none_without_job(client: TestClient, env: dict):
    conn = sqlite3.connect(str(env["db"]))
    conn.row_factory = sqlite3.Row
    lexeme_id = conn.execute("SELECT id FROM Lexeme WHERE lemma='home'").fetchone()["id"]
    conn.close()
    body = client.get("/mnemonic", params={"lexeme_id": lexeme_id}).json()
    assert body["status"] == "none" and body["job"] is None


def test_mnemonic_404_unknown_lexeme(client: TestClient):
    assert client.get("/mnemonic", params={"lexeme_id": 99999}).status_code == 404


# --- 无网络依赖 ------------------------------------------------------------


def test_no_socket_usage(client: TestClient, env: dict, monkeypatch):
    """DESIGN §6：server 无网络依赖。把 socket.socket 打成地雷再跑一遍全流程。"""
    import socket

    class Boom(socket.socket):
        def __init__(self, *a, **kw):
            raise AssertionError("server 不应发起任何网络调用")

    monkeypatch.setattr(socket, "socket", Boom)
    seg = first_segment_id(client, env["content_id"])
    assert client.get("/episodes").status_code == 200
    assert client.get("/lookup", params={"surface": "home"}).status_code == 200
    assert client.post("/collect", json={"surface": "home", "segment_id": seg}).status_code == 200
    assert client.get("/vocab").status_code == 200
    assert client.get(f"/media/{env['content_id']}", headers={"Range": "bytes=0-10"}).status_code == 206


# --- lifespan 关连接（工单 6-4） --------------------------------------------


def test_lifespan_closes_every_thread_connection(env: dict):
    """TestClient 退出后：两个库的所有连接（含线程池里那些）必须已经关掉。

    Windows 上没关的连接会锁住 .db 文件，用户删不掉也重建不了词典。
    Linux 删得掉，所以这里直接验"连接已关"这个因，顺带验文件能删（果）。
    """
    app = create_app(db_path=env["db"], ecdict_path=env["ecdict"])
    db, ecdict = app.state.db, app.state.ecdict
    with TestClient(app) as c:
        seg = first_segment_id(c, env["content_id"])
        assert c.get("/lookup", params={"surface": "cousins"}).status_code == 200
        assert c.post(
            "/collect", json={"surface": "cousins", "segment_id": seg}
        ).status_code == 200
        # 端点跑在线程池线程里：这些连接不是主线程开的，正是老实现关不掉的那些
        alive = list(db._conns) + list(ecdict._conns)
        assert len(alive) >= 2, "至少该有 poi.db + ecdict.db 各一条连接"

    for conn in alive:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
    assert db._conns == [] and ecdict._conns == []

    # 文件（含 WAL / SHM 残留）都能删掉
    for p in (env["db"], env["ecdict"]):
        for suffix in ("", "-wal", "-shm"):
            f = Path(str(p) + suffix)
            if f.exists():
                f.unlink()
    assert not env["db"].exists() and not env["ecdict"].exists()


def test_store_reopens_after_close_all(env: dict):
    """close_all 之后对象还能继续用（重开新连接），不是一次性的。"""
    app = create_app(db_path=env["db"], ecdict_path=env["ecdict"])
    with TestClient(app) as c:
        assert c.get("/episodes").status_code == 200
    with TestClient(app) as c:  # 第二次进出：连接重开，端点照常
        assert c.get("/episodes").status_code == 200
        assert c.get("/lookup", params={"surface": "cousins"}).json()["in_dict"] is True


def test_close_all_is_idempotent(env: dict):
    app = create_app(db_path=env["db"], ecdict_path=env["ecdict"])
    db = app.state.db
    with TestClient(app) as c:
        c.get("/episodes")
    assert db.close_all() == 0  # 已经关干净了，再关一次不炸也不重复计数


def test_annotate_worker_closes_ecdict_too(env: dict):
    """worker 的 EcdictStore 也归 worker.close() 管（同一个锁文件问题）。"""
    from app.annotate import AnnotateWorker
    from app.providers.fake import FakeProvider

    w = AnnotateWorker(
        db_path=env["db"], provider=FakeProvider(), ecdict_path=env["ecdict"],
        log=lambda _m: None,
    )
    w.conn.execute("SELECT 1")
    w.ecdict.lookup("cousin")
    conns = [w._conn, *w.ecdict._conns]
    w.close()
    for conn in conns:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


# --- POST /collect/web（浏览器划词插件，工单 11） --------------------------

WEB_SENTENCE = "The tired gardener began a stakeout near the greenhouse door."
WEB_URL = "https://example.invalid/notes/gardening"
WEB_TITLE = "Gardening notes // example"
EXT_ORIGIN = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
FF_ORIGIN = "moz-extension://11111111-2222-3333-4444-555555555555"


def collect_web(client: TestClient, surface: str, **over) -> dict:
    payload = {
        "surface": surface,
        "sentence": WEB_SENTENCE,
        "url": WEB_URL,
        "title": WEB_TITLE,
    }
    payload.update(over)
    r = client.post("/collect/web", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def test_collect_web_full_chain(client: TestClient, env: dict):
    body = collect_web(client, "Stakeouts")
    assert body["created"] is True and body["collected"] is True
    assert body["lemma"] == "stakeout" and body["surface"] == "stakeouts"
    assert body["job_created"] is True and body["encounters"] == 1
    assert body["source_kind"] == "web"
    assert body["sentence"] == WEB_SENTENCE and body["url"] == WEB_URL

    conn = sqlite3.connect(str(env["db"]))
    conn.row_factory = sqlite3.Row
    enc = conn.execute("SELECT * FROM Encounter").fetchall()
    assert len(enc) == 1
    assert enc[0]["segment_id"] is None
    assert enc[0]["source_kind"] == "web"
    ctx = json.loads(enc[0]["context_json"])
    assert ctx == {"url": WEB_URL, "title": WEB_TITLE, "sentence": WEB_SENTENCE}
    # 词典没收录也建骨架 Lexeme + WordForm 映射（与 /collect 同口径）
    assert conn.execute(
        "SELECT id FROM Lexeme WHERE lemma='stakeout'"
    ).fetchone() is not None
    assert conn.execute(
        "SELECT lexeme_id FROM WordForm WHERE surface='stakeouts'"
    ).fetchone() is not None
    job = conn.execute("SELECT * FROM AnnotationJob").fetchall()
    assert len(job) == 1 and job[0]["status"] == "queued"
    assert job[0]["priority"] == 10  # 收藏永远高优先级插队
    conn.close()


def test_collect_web_repeat_only_adds_encounter(client: TestClient, env: dict):
    first = collect_web(client, "stakeout")
    second = collect_web(client, "Stakeouts", sentence="A second stakeout, same word.")
    assert second["created"] is False
    assert second["vocab_entry_id"] == first["vocab_entry_id"]
    assert second["encounters"] == 2
    assert second["job_created"] is False

    conn = sqlite3.connect(str(env["db"]))
    assert conn.execute("SELECT COUNT(*) FROM VocabEntry").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM Encounter").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM AnnotationJob").fetchone()[0] == 1
    conn.close()


def test_collect_web_shares_entry_with_subtitle_collect(client: TestClient, env: dict):
    """同一个词，一次从剧里收、一次从网页收 → 同一个生词条目，两条相遇。"""
    seg = first_segment_id(client, env["content_id"])
    a = client.post("/collect", json={"surface": "home", "segment_id": seg}).json()
    b = collect_web(client, "homes", sentence="He went to two homes today.")
    assert b["vocab_entry_id"] == a["vocab_entry_id"]
    assert b["encounters"] == 2
    assert b["in_dict"] is True and b["ipa"] == "həʊm"  # 词典字段照样回填


def test_collect_web_without_sentence_or_url(client: TestClient, env: dict):
    """页面刁钻、句子/标题都没截到时也得收得下（只是语境为空）。"""
    r = client.post("/collect/web", json={"surface": "gardener"})
    assert r.status_code == 200
    conn = sqlite3.connect(str(env["db"]))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM Encounter").fetchone()
    assert json.loads(row["context_json"] or "{}") == {
        "url": None, "title": None, "sentence": None
    }
    conn.close()


def test_collect_web_bad_input(client: TestClient):
    assert client.post("/collect/web", json={}).status_code == 422
    assert client.post("/collect/web", json={"surface": ""}).status_code == 422
    assert client.post("/collect/web", json={"surface": "!!!"}).status_code == 400


def test_lookup_without_segment_reports_encounter_count(client: TestClient):
    """插件开卡时就要显示「✓ 已收 · N 次相遇」，所以 /lookup 得给出次数。"""
    before = client.get("/lookup", params={"surface": "stakeout"}).json()
    assert before["collected"] is False and before["encounters"] == 0
    assert before["segment_id"] is None and before["sentence"] is None
    collect_web(client, "stakeout")
    collect_web(client, "stakeouts")
    after = client.get("/lookup", params={"surface": "Stakeout"}).json()
    assert after["collected"] is True and after["encounters"] == 2


def test_vocab_mixes_web_and_subtitle_encounters(client: TestClient, env: dict):
    segs = client.get("/segments", params={"content_id": env["content_id"]}).json()[
        "segments"
    ]
    client.post("/collect", json={"surface": "home", "segment_id": segs[0]["id"]})
    collect_web(client, "homes", sentence="Two homes burned down.")
    collect_web(client, "stakeout")

    body = client.get("/vocab").json()
    by_lemma = {v["lemma"]: v for v in body["vocab"]}
    assert set(by_lemma) == {"home", "stakeout"}

    home = by_lemma["home"]
    assert home["encounter_count"] == 2
    sub, web = home["encounters"]
    assert sub["source_kind"] == "segment"
    assert sub["sentence"] == segs[0]["text_en"] and sub["season_ep"] == "s01e01"
    assert sub["content_id"] == env["content_id"] and sub["url"] is None

    assert web["source_kind"] == "web"
    assert web["sentence"] == "Two homes burned down."
    assert web["title"] == WEB_TITLE and web["url"] == WEB_URL
    # 网页来源没有时间轴 → 播放器不画「去这句」
    assert web["segment_id"] is None
    assert web["content_id"] is None and web["t_start"] is None

    # 纯网页来源的词也能正常展开（LEFT JOIN 不能把它吃掉）
    only_web = by_lemma["stakeout"]["encounters"][0]
    assert only_web["source_kind"] == "web" and only_web["sentence"] == WEB_SENTENCE


def test_review_next_serves_web_encounter(client: TestClient):
    """复习卡的原句也要能来自网页收藏（review.py 与 /vocab 同一套口径）。"""
    collect_web(client, "stakeout")
    cards = client.get("/review/next").json()["cards"]
    assert len(cards) == 1
    enc = cards[0]["encounter"]
    assert enc["source_kind"] == "web"
    assert enc["sentence"] == WEB_SENTENCE
    assert enc["url"] == WEB_URL and enc["title"] == WEB_TITLE
    assert enc["segment_id"] is None and enc["t_start"] is None


# --- CORS：只给插件、只给两个端点（工单 11） ------------------------------


@pytest.mark.parametrize("origin", [EXT_ORIGIN, FF_ORIGIN])
def test_cors_allows_extension_origin_on_two_endpoints(client: TestClient, origin: str):
    r = client.get("/lookup", params={"surface": "home"}, headers={"Origin": origin})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == origin
    assert r.headers["vary"] == "Origin"

    r2 = client.post(
        "/collect/web", json={"surface": "home"}, headers={"Origin": origin}
    )
    assert r2.status_code == 200
    assert r2.headers["access-control-allow-origin"] == origin


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example",
        "http://127.0.0.1:8000",
        "null",
        "chrome-extension://",             # 没有扩展 id
        "https://chrome-extension://abcd",  # 前缀骗子
    ],
)
def test_cors_denies_non_extension_origins(client: TestClient, origin: str):
    r = client.get("/lookup", params={"surface": "home"}, headers={"Origin": origin})
    assert r.status_code == 200                      # 服务端照常应答……
    assert "access-control-allow-origin" not in r.headers  # ……但浏览器读不走


@pytest.mark.parametrize(
    "method,path,kwargs",
    [
        ("get", "/vocab", {}),
        ("get", "/episodes", {}),
        ("get", "/segments", {"params": {"content_id": 1}}),
        ("get", "/review/next", {}),
        ("get", "/mnemonic", {"params": {"lexeme_id": 1}}),
        ("post", "/collect", {"json": {"surface": "home", "segment_id": 1}}),
    ],
)
def test_cors_not_granted_to_other_endpoints(client: TestClient, method, path, kwargs):
    r = getattr(client, method)(path, headers={"Origin": EXT_ORIGIN}, **kwargs)
    assert "access-control-allow-origin" not in r.headers


def test_cors_preflight_only_for_allowed_pair(client: TestClient):
    ok = client.options(
        "/collect/web",
        headers={
            "Origin": EXT_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert ok.status_code == 204
    assert ok.headers["access-control-allow-origin"] == EXT_ORIGIN
    assert "POST" in ok.headers["access-control-allow-methods"]
    assert ok.headers["access-control-allow-headers"] == "Content-Type"

    # 无关端点 / 无关 origin 的预检不给放行头（走正常路由，405）
    bad_path = client.options(
        "/vocab",
        headers={"Origin": EXT_ORIGIN, "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in bad_path.headers
    bad_origin = client.options(
        "/collect/web",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert "access-control-allow-origin" not in bad_origin.headers


def test_no_cors_headers_without_origin(client: TestClient):
    r = client.get("/lookup", params={"surface": "home"})
    assert "access-control-allow-origin" not in r.headers
    assert "vary" not in r.headers


# --- 跨站写入拦截：POST /import 只认本机 Origin（工单 17-1） ----------------
# 背景：multipart/form-data 是 CORS 的「简单请求」，浏览器不预检、直接发。
# 「没给 CORS 响应头」只挡住了读回包，挡不住写 —— 所以 /import 自己认 Origin。

# 上传三件套：内容随便，反正合法请求也走不到解析这一步就该被拒
def _import_files() -> dict:
    return {
        "video": ("ep.mp4", b"\x00" * 4096, "video/mp4"),
        "srt": ("ep.srt", FIXTURE_SRT.encode("utf-8"), "text/plain"),
    }


def _import_form() -> dict:
    return {"title": "Test Show", "season_ep": "s09e09"}


def _library_dirs(env: dict) -> list[Path]:
    lib_root = Path(env["db"]).parent / "library"
    return sorted(p for p in lib_root.iterdir()) if lib_root.is_dir() else []


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example",
        "http://evil.example",
        "https://localhost.evil.example",   # 后缀骗子
        "http://127.0.0.1.evil.example",    # 前缀骗子
        "null",                             # sandbox iframe / file:// 的不透明 origin
        "chrome-extension://abcdefghijklmnopabcdefghijklmnop",  # 插件也不许导入
        "http://[::1]:8000@evil.example",
    ],
)
def test_import_denies_cross_site_origin(client: TestClient, monkeypatch, origin: str):
    """外站 / null / 插件 origin 一律 403，且磁盘和作业表干干净净。"""
    import app.library as lib

    def _boom(*a, **kw):  # 走到落盘就说明拦晚了
        raise AssertionError("拦截失败：上传内容已经开始落盘")

    monkeypatch.setattr(lib, "save_upload", _boom)
    monkeypatch.setattr(lib, "new_work_dir", _boom)

    r = client.post(
        "/import", data=_import_form(), files=_import_files(), headers={"Origin": origin}
    )
    assert r.status_code == 403
    assert "跨站" in r.json()["detail"]
    assert "access-control-allow-origin" not in r.headers   # 顺带：也别把回包给他
    # 没建 uuid 目录、没登记作业
    assert _library_dirs(client.env) == []                  # type: ignore[attr-defined]
    assert client.get("/import").json() == {"count": 0, "jobs": []}


def test_import_denies_cross_site_before_reading_any_body_byte():
    """直捅 ASGI：拒绝发生在 await receive() 之前 —— 一个字节都没读。

    TestClient 自己管 receive，验不了这件事；这里手搓 scope + 一个「被调用就
    炸」的 receive，直接把中间件的承诺钉死。
    """
    import asyncio
    import threading

    app = create_app(db_path="/nonexistent/never-touched.db")
    received: list[str] = []

    async def receive():
        received.append("read")
        raise AssertionError("拦截失败：中间件读了请求体")

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "path": "/import",
        "raw_path": b"/import",
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "headers": [
            (b"host", b"127.0.0.1:8000"),
            (b"origin", b"https://evil.example"),
            (b"content-type", b"multipart/form-data; boundary=x"),
            (b"content-length", b"999999999"),
        ],
        "client": ("127.0.0.1", 54321),
        "server": ("127.0.0.1", 8000),
    }
    # 单起一个线程跑：同一进程里如果有别的用例开着 playwright 的同步 API，
    # 本线程会挂着一个运行中的事件循环，asyncio.run 就没法用了。
    box: dict[str, BaseException] = {}

    def drive() -> None:
        try:
            asyncio.run(app(scope, receive, send))
        except BaseException as exc:  # noqa: BLE001 —— 原样搬回主线程再抛
            box["err"] = exc

    t = threading.Thread(target=drive)
    t.start()
    t.join(timeout=30)
    assert not t.is_alive(), "中间件卡住了"
    if "err" in box:
        raise box["err"]

    assert received == []                                   # receive() 从没被 await
    assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 403
    assert b"\xe8\xb7\xa8\xe7\xab\x99" in sent[1]["body"]   # "跨站"（UTF-8）


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://127.0.0.1",
        "https://localhost:8443",
        "http://[::1]:8000",
        "HTTP://LOCALHOST:8000",   # 大小写不敏感
    ],
)
def test_import_allows_local_page_origin(client: TestClient, origin: str):
    """播放器自己的「内容库」界面（本机 origin）照常放行 —— 过了闸交给路由校验。"""
    r = client.post(
        "/import",
        data={"title": " ", "season_ep": " "},   # 空白标题：故意让路由以 400 拒绝
        files=_import_files(),
        headers={"Origin": origin},
    )
    assert r.status_code == 400                  # 不是 403：说明闸放行了
    assert "不能为空" in r.json()["detail"]
    assert _library_dirs(client.env) == []       # type: ignore[attr-defined]


def test_import_allows_cli_without_origin(client: TestClient):
    """本机 CLI（curl / 脚本）不带 Origin：放行。浏览器发跨站请求一定带 Origin。"""
    r = client.post(
        "/import", data={"title": "Test Show", "season_ep": " "}, files=_import_files()
    )
    assert r.status_code == 400 and "不能为空" in r.json()["detail"]


def test_import_guard_does_not_block_reads(client: TestClient):
    """闸只管写：GET /import（看最近几次导入）不拦 —— 它本来就没有 CORS 许可。"""
    r = client.get("/import", headers={"Origin": "https://evil.example"})
    assert r.status_code == 200
    assert "access-control-allow-origin" not in r.headers


def test_import_duplicate_check_still_wins_for_local_origin(client: TestClient):
    """闸不改原有语义：本机 origin 的重复导入还是 409。"""
    r = client.post(
        "/import",
        data={"title": "Test Show", "season_ep": "s01e01"},   # env 夹具已经导过这集
        files=_import_files(),
        headers={"Origin": "http://localhost:8000"},
    )
    assert r.status_code == 409 and "已经导入过" in r.json()["detail"]
