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


# --- 词框回填 --boxes-json（DESIGN §4 热区） --------------------------------


def fake_boxes(entries):
    """自造 extract_hardsub --boxes-json 产物：[(idx, text, [(w, x)…])]。

    坐标随手编，只要求形状与 README 契约一致（视频原始帧像素，x 可为 null）。
    """
    out = []
    for idx, text, words in entries:
        out.append(
            {
                "idx": idx,
                "start": float(idx),
                "end": float(idx) + 1.0,
                "text": text,
                "words": [
                    {"w": w, "x": x, "y": 1030, "width": 20 * len(w), "height": 40}
                    if x is not None
                    else {"w": w, "x": None, "y": None, "width": None, "height": None}
                    for w, x in words
                ],
            }
        )
    return out


def boxes_file(tmp_path, payload, name="ep.boxes.json"):
    p = tmp_path / name
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


def seg_boxes(conn, idx):
    row = conn.execute(
        "SELECT word_boxes_json FROM Segment WHERE idx = ?", (idx,)
    ).fetchone()
    return None if row["word_boxes_json"] is None else json.loads(row["word_boxes_json"])


def test_boxes_json_backfills_words_verbatim(tmp_path, db_path, srt_file, conn):
    payload = fake_boxes(
        [
            (1, "The tall gardener went home early.", [("The", 100), ("tall", 180)]),
            (3, "My cousins bought two cameras; the cameras were cheap.", [("My", 90)]),
        ]
    )
    stats = ingest_srt(
        db_path,
        srt_file,
        "Fixture Show",
        "s01e01",
        conn=conn,
        boxes_path=boxes_file(tmp_path, payload),
    )
    assert stats["boxes_applied"] == 2
    assert stats["boxes_missing_idx"] == []
    # words 数组原样入库（键名、顺序、坐标都不动）
    assert seg_boxes(conn, 1) == payload[0]["words"]
    assert seg_boxes(conn, 3) == payload[1]["words"]
    # 没给框的段保持 NULL → 前端走自渲染退路
    assert seg_boxes(conn, 2) is None
    assert stats["segments_without_boxes"] == [2, 4, 5]


def test_boxes_json_keeps_null_x_words(tmp_path, db_path, srt_file, conn):
    payload = fake_boxes([(1, "The tall gardener went home early.",
                          [("The", 100), ("tall", None), ("gardener", 260)])])
    ingest_srt(db_path, srt_file, "Fixture Show", "s01e01", conn=conn,
               boxes_path=boxes_file(tmp_path, payload))
    words = seg_boxes(conn, 1)
    assert [w["w"] for w in words] == ["The", "tall", "gardener"]
    assert words[1]["x"] is None  # 丢框不丢词


def test_boxes_json_is_idempotent(tmp_path, db_path, srt_file, conn):
    path = boxes_file(tmp_path, fake_boxes(
        [(1, "The tall gardener went home early.", [("The", 100)])]))
    first = ingest_srt(db_path, srt_file, "Fixture Show", "s01e01", conn=conn,
                       boxes_path=path)
    snap = [dict(r) for r in conn.execute("SELECT * FROM Segment ORDER BY idx")]
    second = ingest_srt(db_path, srt_file, "Fixture Show", "s01e01", conn=conn,
                        boxes_path=path)
    assert first == second
    assert [dict(r) for r in conn.execute("SELECT * FROM Segment ORDER BY idx")] == snap


def test_boxes_json_unknown_idx_warns_without_aborting(tmp_path, db_path, srt_file, conn, capsys):
    payload = fake_boxes(
        [
            (99, "Ghost cue that no segment matches.", [("Ghost", 10)]),
            (2, "Marlow says it's raining again,\nand I don't believe her at all.",
             [("Marlow", 120)]),
        ]
    )
    stats = ingest_srt(db_path, srt_file, "Fixture Show", "s01e01", conn=conn,
                       boxes_path=boxes_file(tmp_path, payload))
    assert stats["boxes_applied"] == 1
    assert stats["boxes_missing_idx"] == [99]
    assert seg_boxes(conn, 2) == payload[1]["words"]
    err = capsys.readouterr().err
    assert "idx=99" in err and "警告" in err


def test_boxes_json_bad_entries_are_skipped(tmp_path, db_path, srt_file, conn, capsys):
    payload = [
        "not a dict",
        {"idx": "abc", "words": []},
        {"idx": 4, "words": "nope"},
        {"idx": 5, "text": "\"Stop!\" she shouted -- nobody stopped.", "words": []},
    ]
    stats = ingest_srt(db_path, srt_file, "Fixture Show", "s01e01", conn=conn,
                       boxes_path=boxes_file(tmp_path, payload))
    assert stats["boxes_skipped"] == 3
    assert stats["boxes_applied"] == 1
    assert seg_boxes(conn, 5) == []       # 空 words 也算回填（前端据此走退路）
    assert seg_boxes(conn, 4) is None
    assert "警告" in capsys.readouterr().err


def test_boxes_json_text_mismatch_warns_but_applies(tmp_path, db_path, srt_file, conn, capsys):
    payload = fake_boxes([(1, "Totally different sentence here.", [("Totally", 100)])])
    stats = ingest_srt(db_path, srt_file, "Fixture Show", "s01e01", conn=conn,
                       boxes_path=boxes_file(tmp_path, payload))
    assert stats["boxes_text_mismatch_idx"] == [1]
    assert seg_boxes(conn, 1) == payload[0]["words"]
    assert "text_en 不一致" in capsys.readouterr().err


def test_reingest_without_boxes_keeps_existing(tmp_path, db_path, srt_file, conn):
    payload = fake_boxes([(1, "The tall gardener went home early.", [("The", 100)])])
    ingest_srt(db_path, srt_file, "Fixture Show", "s01e01", conn=conn,
               boxes_path=boxes_file(tmp_path, payload))
    ingest_srt(db_path, srt_file, "Fixture Show", "s01e01", conn=conn)
    assert seg_boxes(conn, 1) == payload[0]["words"]


def test_boxes_json_rejects_non_list_toplevel(tmp_path, db_path, srt_file, conn):
    import pytest

    path = boxes_file(tmp_path, {"idx": 1})
    with pytest.raises(ValueError):
        ingest_srt(db_path, srt_file, "Fixture Show", "s01e01", conn=conn,
                   boxes_path=path)


def test_cli_main_with_boxes(tmp_path, db_path, srt_file, capsys):
    path = boxes_file(tmp_path, fake_boxes(
        [(1, "The tall gardener went home early.", [("The", 100), ("tall", 180)])]))
    rc = main([str(srt_file), "--title", "Fixture Show", "--season-ep", "s01e01",
               "--db", str(db_path), "--boxes-json", str(path)])
    assert rc == 0
    assert "word boxes      : 1 段回填" in capsys.readouterr().out
    from app.db import get_conn

    c = get_conn(db_path)
    words = json.loads(
        c.execute("SELECT word_boxes_json FROM Segment WHERE idx = 1").fetchone()[0]
    )
    assert [w["w"] for w in words] == ["The", "tall"]
    c.close()
