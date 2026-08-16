"""端到端入库：Segment/tokens_json/WordForm/Lexeme + 幂等。"""

from __future__ import annotations

import json

from app.ingest import ingest_srt, main


def counts(conn):
    return {
        t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
        for t in ("Content", "Segment", "Lexeme", "WordForm")
    }


def test_ingest_writes_content_and_segments(db_path, srt_file, conn):
    stats = ingest_srt(db_path, srt_file, "Fixture Show", "s01e01", "/v/ep1.mp4", conn=conn)
    assert stats["segments"] == 5
    row = conn.execute("SELECT * FROM Content").fetchone()
    assert row["title"] == "Fixture Show"
    assert row["season_ep"] == "s01e01"
    assert row["video_path"] == "/v/ep1.mp4"
    assert row["srt_path"] == str(srt_file)

    segs = conn.execute("SELECT * FROM Segment ORDER BY idx").fetchall()
    assert [s["idx"] for s in segs] == [1, 2, 3, 4, 5]
    assert segs[0]["t_start"] == 1.0 and segs[0]["t_end"] == 3.5
    assert segs[0]["content_id"] == row["id"]


def test_tokens_json_shape_supports_segments_api(db_path, srt_file, conn):
    ingest_srt(db_path, srt_file, "Fixture Show", "s01e01", conn=conn)
    seg = conn.execute("SELECT * FROM Segment WHERE idx = 2").fetchone()
    tokens = json.loads(seg["tokens_json"])
    assert tokens, "tokens_json 不能为空"
    for tok in tokens:
        assert set(tok) == {"surface", "lemma", "char_start", "char_end"}
        assert tok["surface"] == tok["surface"].lower()
        assert seg["text_en"][tok["char_start"] : tok["char_end"]].lower() == tok["surface"]
    assert "it's" in {t["surface"] for t in tokens}
    assert "don't" in {t["surface"] for t in tokens}


def test_wordform_maps_surface_to_lemma(db_path, srt_file, conn):
    ingest_srt(db_path, srt_file, "Fixture Show", "s01e01", conn=conn)
    rows = conn.execute(
        "SELECT w.surface, l.lemma FROM WordForm w JOIN Lexeme l ON l.id = w.lexeme_id"
    ).fetchall()
    mapping = {r["surface"]: r["lemma"] for r in rows}
    assert mapping["went"] == "go"
    assert mapping["cousins"] == "cousin"
    assert mapping["cameras"] == "camera"
    assert mapping["stopped"] == "stop"
    # 专名照收不误
    assert "marlow" in mapping and "bramwell" in mapping
    assert all(s == s.lower() for s in mapping)
    assert all(l == l.lower() for l in mapping.values())


def test_repeated_ingest_is_idempotent(db_path, srt_file, conn):
    first = ingest_srt(db_path, srt_file, "Fixture Show", "s01e01", "/v/ep1.mp4", conn=conn)
    before = counts(conn)
    seg_ids_before = [r["id"] for r in conn.execute("SELECT id FROM Segment ORDER BY idx")]

    second = ingest_srt(db_path, srt_file, "Fixture Show", "s01e01", "/v/ep1.mp4", conn=conn)
    after = counts(conn)

    assert first == second
    assert before == after
    assert [r["id"] for r in conn.execute("SELECT id FROM Segment ORDER BY idx")] == seg_ids_before


def test_reingest_shorter_srt_drops_stale_segments(db_path, srt_file, srt_file_shorter, conn):
    ingest_srt(db_path, srt_file, "Fixture Show", "s01e01", conn=conn)
    ingest_srt(db_path, srt_file_shorter, "Fixture Show", "s01e01", conn=conn)
    segs = conn.execute("SELECT idx, text_en FROM Segment ORDER BY idx").fetchall()
    assert [s["idx"] for s in segs] == [1, 2]
    assert segs[1]["text_en"] == "A quiet morning in the empty office."
    assert conn.execute("SELECT COUNT(*) c FROM Content").fetchone()["c"] == 1
    # 旧词条留在词表里（词典缓存不随字幕收缩而删）
    assert conn.execute(
        "SELECT COUNT(*) c FROM WordForm WHERE surface = 'cousins'"
    ).fetchone()["c"] == 1


def test_two_episodes_share_lexemes(db_path, srt_file, srt_file_shorter, conn):
    ingest_srt(db_path, srt_file, "Fixture Show", "s01e01", conn=conn)
    n_lex = conn.execute("SELECT COUNT(*) c FROM Lexeme").fetchone()["c"]
    ingest_srt(db_path, srt_file_shorter, "Fixture Show", "s01e02", conn=conn)
    assert conn.execute("SELECT COUNT(*) c FROM Content").fetchone()["c"] == 2
    # 'the'/'went'/'home' 等复用旧 lexeme，不重复建
    lemmas = [r["lemma"] for r in conn.execute("SELECT lemma FROM Lexeme")]
    assert len(lemmas) == len(set(lemmas))
    assert conn.execute("SELECT COUNT(*) c FROM Lexeme").fetchone()["c"] > n_lex


def test_every_segment_token_surface_has_wordform(db_path, srt_file, conn):
    ingest_srt(db_path, srt_file, "Fixture Show", "s01e01", conn=conn)
    known = {r["surface"] for r in conn.execute("SELECT surface FROM WordForm")}
    for seg in conn.execute("SELECT tokens_json FROM Segment"):
        for tok in json.loads(seg["tokens_json"]):
            assert tok["surface"] in known


def test_stats_are_consistent(db_path, srt_file, conn):
    stats = ingest_srt(db_path, srt_file, "Fixture Show", "s01e01", conn=conn)
    total = sum(
        len(json.loads(r["tokens_json"]))
        for r in conn.execute("SELECT tokens_json FROM Segment")
    )
    assert stats["tokens"] == total
    assert stats["unique_lemmas"] <= stats["unique_surfaces"] <= stats["tokens"]
    assert stats["unique_surfaces"] == conn.execute(
        "SELECT COUNT(*) c FROM WordForm"
    ).fetchone()["c"]


def test_cli_main(db_path, srt_file, capsys):
    rc = main(
        [
            str(srt_file),
            "--title",
            "Fixture Show",
            "--season-ep",
            "s01e01",
            "--video",
            "/v/ep1.mp4",
            "--db",
            str(db_path),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "segments        : 5" in out
    from app.db import get_conn

    c = get_conn(db_path)
    assert c.execute("SELECT COUNT(*) c FROM Segment").fetchone()["c"] == 5
    c.close()
