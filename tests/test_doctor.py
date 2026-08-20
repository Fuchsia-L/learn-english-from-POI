"""app/doctor.py 的只读体检（工单 13）。

版权红线（DESIGN §0 §6）：所有字幕、句子、词框坐标全是自造的，
不含任何真实剧集台词，也不读真实的 data/*.db（ecdict 一律用 build_ecdict --mini
的自造 100 词夹具，或者干脆指一个不存在的路径）。

覆盖：每个检查项的正例 + 反例、退出码、--strict、--json、
一个「243 词框 / 1 个空坐标」的真实规模回归夹具，以及**只读性断言**
（跑完 doctor 后 .db 文件的 sha256 与 mtime 都不变）。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_ecdict  # noqa: E402

from app import doctor as D  # noqa: E402
from app.ingest import ingest_srt, parse_srt_file  # noqa: E402

# --- 自造素材 --------------------------------------------------------------

# 六段自造台词（覆盖缩写、重复词、连字符、数字、专名）
LINES = [
    "The tall gardener went home early.",
    "Marlow says it's raining again tonight.",
    "My cousins bought two cameras yesterday.",
    "Ex-con Bramwell left Halloway Street 3 days ago.",
    "Nobody stopped the quiet blue train.",
    "She counted every window twice before dawn.",
]

# 243 词回归夹具的词池（自造，无实义组合）
POOL = [
    "quiet", "gardener", "window", "letter", "harbor", "signal", "ledger",
    "kettle", "marble", "orchard", "pebble", "ribbon", "saddle", "timber",
    "velvet", "walnut", "anchor", "beacon", "candle", "domino",
]


def ts(sec: float) -> str:
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int(round(s % 1 * 1000)) % 1000:03d}"


def srt_from(lines, step: float = 3.2, dur: float = 3.0, start: float = 1.0) -> str:
    out = []
    for i, text in enumerate(lines, 1):
        t0 = start + (i - 1) * step
        out.append(f"{i}\n{ts(t0)} --> {ts(t0 + dur)}\n{text}\n")
    return "\n".join(out)


def boxes_for(cues, idxs=None, x0: int = 100, y: int = 1000):
    """按 cue 文本造词框：每词一框，坐标落在 1920x1080 参考系内的字幕带上。"""
    out = []
    for c in cues:
        if idxs is not None and c.idx not in idxs:
            continue
        words, x = [], x0
        for w in c.text.split():
            width = 12 * len(w)
            words.append({"w": w, "x": x, "y": y, "width": width, "height": 34})
            x += width + 8
        out.append(
            {"idx": c.idx, "start": c.t_start, "end": c.t_end, "text": c.text, "words": words}
        )
    return out


def make_db(
    tmp_path: Path,
    lines=None,
    boxed_idx=None,
    video: Path | None = None,
    mutate_boxes=None,
    name: str = "poi.db",
    season_ep: str = "s01e01",
    db: Path | None = None,
    **srt_kw,
) -> Path:
    """造一个体检用的库：srt → ingest → 可选词框回填。返回 db 路径。"""
    lines = LINES if lines is None else lines
    srt = tmp_path / f"{season_ep}.srt"
    srt.write_text(srt_from(lines, **srt_kw), encoding="utf-8")
    cues = parse_srt_file(srt)
    boxes_path = None
    if boxed_idx is not None:
        entries = boxes_for(cues, boxed_idx)
        if mutate_boxes is not None:
            mutate_boxes(entries)
        boxes_path = tmp_path / f"{season_ep}.boxes.json"
        boxes_path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    db = db or (tmp_path / name)
    ingest_srt(
        db_path=db,
        srt_path=srt,
        title="Fixture Show",
        season_ep=season_ep,
        video_path=str(video) if video else None,
        boxes_path=boxes_path,
    )
    return db


def mini_ecdict(tmp_path: Path) -> Path:
    p = tmp_path / "ecdict_mini.db"
    if not p.exists():
        build_ecdict.build_mini(p)
    return p


def run(db: Path, tmp_path: Path, **kw) -> D.Report:
    """默认关掉 ffprobe（大多数用例不需要真视频），ecdict 指向 mini 夹具。"""
    kw.setdefault("use_ffprobe", False)
    kw.setdefault("ecdict_path", mini_ecdict(tmp_path))
    return D.run_doctor(db_path=db, **kw)


def messages(report: D.Report, level: str | None = None) -> str:
    return "\n".join(f.message for f in report.findings if level is None or f.level == level)


def levels(report: D.Report) -> dict[str, int]:
    return report.counts()


def raw_conn(db: Path) -> sqlite3.Connection:
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    return c


def seg_stats(report: D.Report, i: int = 0) -> dict:
    return report.data["contents"][i]


# --- 0. 常量口径 -----------------------------------------------------------


def test_word_re_matches_ingest_tokenizer():
    """doctor 的分词正则必须与 ingest 逐字一致，否则对齐检查会自造假警报。"""
    from app import ingest

    assert D.WORD_RE.pattern == ingest._TOKEN_RE.pattern


# --- 1. 库本身 -------------------------------------------------------------


def test_missing_db_is_fail_and_nonzero_exit(tmp_path: Path, capsys):
    rc = D.main(["--db", str(tmp_path / "nope.db"), "--no-ffprobe"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "✗" in out and "数据库不存在" in out
    assert "verdict:" in out


def test_db_without_tables_is_fail(tmp_path: Path):
    empty = tmp_path / "empty.db"
    sqlite3.connect(str(empty)).close()
    report = run(empty, tmp_path)
    assert report.verdict == D.FAIL
    assert "缺" in messages(report, D.FAIL)
    assert report.data["schema"]["tables_missing"]


def test_old_user_version_warns(tmp_path: Path):
    db = make_db(tmp_path, boxed_idx=[1, 2, 3, 4, 5, 6])
    c = raw_conn(db)
    c.execute("PRAGMA user_version = 1")
    c.commit()
    c.close()
    report = run(db, tmp_path)
    assert "user_version=1" in messages(report, D.WARN)
    assert report.verdict == D.WARN


def test_healthy_db_has_no_failures(tmp_path: Path):
    video = tmp_path / "ep.mp4"
    video.write_bytes(b"x" * 4096)  # 只测存在性，ffprobe 关着
    db = make_db(tmp_path, boxed_idx=[1, 2, 3, 4, 5, 6], video=video)
    report = run(db, tmp_path)
    assert levels(report)[D.FAIL] == 0, messages(report, D.FAIL)
    assert "视频存在" in messages(report)
    assert seg_stats(report)["boxes"]["coverage"] == 1.0


# --- 2. 视频 / ffprobe -----------------------------------------------------


def test_missing_video_file_is_fail(tmp_path: Path):
    db = make_db(tmp_path, boxed_idx=[1], video=tmp_path / "gone.mp4")
    report = run(db, tmp_path)
    assert report.verdict == D.FAIL
    assert "video_path 失效" in messages(report, D.FAIL)


def test_no_video_path_is_warn_only(tmp_path: Path):
    db = make_db(tmp_path, boxed_idx=[1, 2, 3, 4, 5, 6])
    report = run(db, tmp_path)
    assert levels(report)[D.FAIL] == 0
    assert "未登记 video_path" in messages(report, D.WARN)


def test_empty_video_file_is_fail(tmp_path: Path):
    video = tmp_path / "zero.mp4"
    video.write_bytes(b"")
    db = make_db(tmp_path, video=video, boxed_idx=[1])
    report = run(db, tmp_path)
    assert "视频文件是空的" in messages(report, D.FAIL)


def test_dead_srt_path_is_warn(tmp_path: Path):
    db = make_db(tmp_path, boxed_idx=[1])
    (tmp_path / "s01e01.srt").unlink()
    video = tmp_path / "ep.mp4"
    video.write_bytes(b"x" * 16)
    c = raw_conn(db)
    c.execute("UPDATE Content SET video_path = ?", (str(video),))
    c.commit()
    c.close()
    report = run(db, tmp_path)
    assert "srt_path 失效" in messages(report, D.WARN)


def test_no_ffprobe_flag_reports_skip(tmp_path: Path):
    video = tmp_path / "ep.mp4"
    video.write_bytes(b"x" * 16)
    db = make_db(tmp_path, video=video, boxed_idx=[1])
    report = run(db, tmp_path)
    assert "跳过 ffprobe" in messages(report, D.WARN)
    assert seg_stats(report)["media"]["duration"] is None


def test_probe_video_degrades_when_ffprobe_absent(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(D.shutil, "which", lambda _: None)
    info = D.probe_video(tmp_path / "whatever.mp4")
    assert info["ok"] is False and info["skipped"] is True


HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="需要 ffmpeg/ffprobe 造夹具视频")


def synth_video(path: Path, seconds: float, audio: bool = True) -> Path:
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
           "-i", f"testsrc2=s=320x180:r=5:d={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono", "-t", str(seconds),
                "-c:a", "aac", "-shortest"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, check=True)
    return path


@needs_ffmpeg
def test_ffprobe_reads_streams_and_duration(tmp_path: Path):
    video = synth_video(tmp_path / "ok.mp4", 30)
    db = make_db(tmp_path, boxed_idx=[1, 2, 3, 4, 5, 6], video=video)
    report = run(db, tmp_path, use_ffprobe=True)
    msg = messages(report)
    assert "视频轨 h264 320x180" in msg and "音频轨 aac" in msg
    assert levels(report)[D.FAIL] == 0, messages(report, D.FAIL)
    assert 29 < seg_stats(report)["media"]["duration"] < 31


@needs_ffmpeg
def test_ffprobe_flags_missing_audio_track(tmp_path: Path):
    video = synth_video(tmp_path / "silent.mp4", 25, audio=False)
    db = make_db(tmp_path, boxed_idx=[1], video=video)
    report = run(db, tmp_path, use_ffprobe=True)
    assert "没有音频轨" in messages(report, D.WARN)


@needs_ffmpeg
def test_ffprobe_on_garbage_file_is_fail(tmp_path: Path):
    video = tmp_path / "garbage.mp4"
    video.write_bytes(b"not a video at all" * 100)
    db = make_db(tmp_path, boxed_idx=[1], video=video)
    report = run(db, tmp_path, use_ffprobe=True)
    assert "ffprobe 读不出媒体信息" in messages(report, D.FAIL)
    assert report.exit_code() == 1


@needs_ffmpeg
def test_segments_past_video_duration_is_fail(tmp_path: Path):
    """字幕排到 20s，视频只有 5s —— 典型的「srt 与视频不是同一版」。"""
    video = synth_video(tmp_path / "short.mp4", 5)
    db = make_db(tmp_path, boxed_idx=[1], video=video)
    report = run(db, tmp_path, use_ffprobe=True)
    assert "超出视频时长" in messages(report, D.FAIL)


# --- 3. 时间轴 -------------------------------------------------------------


def test_empty_content_has_no_segments_is_fail(tmp_path: Path):
    db = make_db(tmp_path, boxed_idx=[1])
    c = raw_conn(db)
    c.execute("DELETE FROM Segment")
    c.commit()
    c.close()
    report = run(db, tmp_path)
    assert "一个字幕段都没有" in messages(report, D.FAIL)


def test_negative_and_inverted_timestamps_are_fail(tmp_path: Path):
    db = make_db(tmp_path, boxed_idx=[1])
    c = raw_conn(db)
    c.execute("UPDATE Segment SET t_start = -2.0 WHERE idx = 1")
    c.execute("UPDATE Segment SET t_end = t_start WHERE idx = 3")
    c.commit()
    c.close()
    report = run(db, tmp_path)
    msg = messages(report, D.FAIL)
    assert "t_start < 0" in msg and "t_end <= t_start" in msg
    tl = seg_stats(report)["timeline"]
    assert tl["negative_idx"] == [1] and tl["inverted_idx"] == [3]


def test_adjacent_overlap_is_warn_not_fail(tmp_path: Path):
    db = make_db(tmp_path, boxed_idx=[1])
    c = raw_conn(db)
    c.execute("UPDATE Segment SET t_end = t_end + 1.0 WHERE idx = 2")
    c.commit()
    c.close()
    report = run(db, tmp_path)
    assert "相邻段重叠" in messages(report, D.WARN)
    assert seg_stats(report)["timeline"]["overlap_pairs"] == [[2, 3]]


def test_clean_timeline_reports_ok(tmp_path: Path):
    db = make_db(tmp_path, boxed_idx=[1, 2, 3, 4, 5, 6])
    report = run(db, tmp_path)
    assert "时间轴自洽" in messages(report, D.OK)


# --- 4. 词框 ---------------------------------------------------------------


def test_no_boxes_at_all_is_warn(tmp_path: Path):
    db = make_db(tmp_path)
    report = run(db, tmp_path)
    assert "没有任何词框" in messages(report, D.WARN)
    assert seg_stats(report)["boxes"]["coverage"] == 0.0


def test_partial_box_coverage_is_warn(tmp_path: Path):
    db = make_db(tmp_path, boxed_idx=[1, 2, 3])
    report = run(db, tmp_path)
    assert "词框覆盖 3/6 段（50%）" in messages(report, D.WARN)


def test_high_null_coord_ratio_is_warn(tmp_path: Path):
    def blank_them(entries):
        for e in entries:
            for w in e["words"]:
                w.update(x=None, y=None, width=None, height=None)

    db = make_db(tmp_path, boxed_idx=[1, 2], mutate_boxes=blank_them)
    report = run(db, tmp_path)
    assert "空坐标（OCR 丢框）" in messages(report, D.WARN)
    assert levels(report)[D.FAIL] == 0


def test_negative_and_out_of_range_coords_are_warn(tmp_path: Path):
    def bend(entries):
        entries[0]["words"][0].update(x=-5)
        entries[0]["words"][1].update(x=1900, width=200)  # 1900+200 > 1920
        entries[0]["words"][2].update(y=1070, height=50)  # 1070+50 > 1080

    db = make_db(tmp_path, boxed_idx=[1, 2], mutate_boxes=bend)
    report = run(db, tmp_path)
    msg = messages(report, D.WARN)
    assert "坐标为负" in msg and f"超出 {D.REF_W}x{D.REF_H} 参考系" in msg
    b = seg_stats(report)["boxes"]
    assert b["negative_coords"] == 1 and b["out_of_range_coords"] == 2
    assert levels(report)[D.FAIL] == 0


def test_malformed_box_entry_is_fail(tmp_path: Path):
    def wreck(entries):
        entries[0]["words"][0] = {"x": 1, "y": 2}  # 缺 w
        entries[0]["words"][1].update(x="left")  # 坐标不是数字

    db = make_db(tmp_path, boxed_idx=[1], mutate_boxes=wreck)
    report = run(db, tmp_path)
    assert "词框结构非法" in messages(report, D.FAIL)
    assert seg_stats(report)["boxes"]["malformed_entries"] == 2


def test_unparsable_word_boxes_json_is_fail(tmp_path: Path):
    db = make_db(tmp_path, boxed_idx=[1, 2])
    c = raw_conn(db)
    c.execute("UPDATE Segment SET word_boxes_json = '{not json' WHERE idx = 1")
    c.commit()
    c.close()
    report = run(db, tmp_path)
    assert "word_boxes_json 解析不了" in messages(report, D.FAIL)
    assert seg_stats(report)["boxes"]["unparsable_idx"] == [1]


# --- 5. tokens 与 word_boxes 对齐 ------------------------------------------


def test_aligned_tokens_and_boxes_report_ok(tmp_path: Path):
    db = make_db(tmp_path, boxed_idx=[1, 2, 3, 4, 5, 6])
    report = run(db, tmp_path)
    assert "逐词对齐" in messages(report, D.OK)
    tk = seg_stats(report)["tokens"]
    assert tk["count_mismatch"] == [] and tk["text_mismatch"] == []


def test_box_count_mismatch_is_warn(tmp_path: Path):
    def drop_one(entries):
        entries[0]["words"].pop()

    db = make_db(tmp_path, boxed_idx=[1, 2, 3, 4, 5, 6], mutate_boxes=drop_one)
    report = run(db, tmp_path)
    msg = messages(report, D.WARN)
    assert "数量不一致" in msg and "文本有出入" in msg
    assert seg_stats(report)["tokens"]["count_mismatch"][0]["idx"] == 1
    assert levels(report)[D.FAIL] == 0


def test_wrong_episode_boxes_is_fail(tmp_path: Path):
    """多数段都对不上文本 → boxes 根本不是这一集的，必须 ✗。"""

    def scramble(entries):
        for e in entries:
            for w in e["words"]:
                w["w"] = "zzz"

    db = make_db(tmp_path, boxed_idx=[1, 2, 3, 4, 5, 6], mutate_boxes=scramble)
    report = run(db, tmp_path)
    assert "很可能不是这一集的产物" in messages(report, D.FAIL)
    assert report.exit_code() == 1


def test_punctuation_and_digits_do_not_break_alignment(tmp_path: Path):
    """OCR 的 w 带标点、纯数字词自成一框：按 ingest 口径展开后仍应对齐。"""
    db = make_db(tmp_path, lines=["Ex-con Bramwell left Halloway Street 3 days ago."],
                 boxed_idx=[1])
    report = run(db, tmp_path)
    tk = seg_stats(report)["tokens"]
    # "3" 有框但不是 token —— 这不算数量差异，否则任何带数字的句子都要误报
    assert tk["text_mismatch"] == [] and tk["count_mismatch"] == []
    assert seg_stats(report)["boxes"]["total_boxes"] == 8  # 原始框数含 "3"
    assert tk["total_tokens"] == 7
    assert "逐词对齐" in messages(report, D.OK)


def test_missing_tokens_json_is_warn(tmp_path: Path):
    db = make_db(tmp_path, boxed_idx=[1, 2])
    c = raw_conn(db)
    c.execute("UPDATE Segment SET tokens_json = NULL WHERE idx = 4")
    c.commit()
    c.close()
    report = run(db, tmp_path)
    assert "没有 tokens_json" in messages(report, D.WARN)
    assert seg_stats(report)["tokens"]["segments_without_tokens"] == [4]


def test_unparsable_tokens_json_is_fail(tmp_path: Path):
    db = make_db(tmp_path, boxed_idx=[1])
    c = raw_conn(db)
    c.execute("UPDATE Segment SET tokens_json = 'oops' WHERE idx = 2")
    c.commit()
    c.close()
    report = run(db, tmp_path)
    assert "tokens_json 解析不了" in messages(report, D.FAIL)


# --- 6. ECDICT -------------------------------------------------------------


def test_ecdict_present_is_ok(tmp_path: Path):
    db = make_db(tmp_path, boxed_idx=[1])
    report = run(db, tmp_path)
    msg = messages(report, D.OK)
    assert "词典可用" in msg and "抽样查询 5/5 命中" in msg
    assert report.data["ecdict"]["entries"] == 100


def test_missing_ecdict_is_warn(tmp_path: Path):
    db = make_db(tmp_path, boxed_idx=[1])
    report = run(db, tmp_path, ecdict_path=tmp_path / "no_such_ecdict.db")
    assert "词典不存在" in messages(report, D.WARN)
    assert levels(report)[D.FAIL] == 0


def test_empty_ecdict_is_fail(tmp_path: Path):
    empty = tmp_path / "ecdict_empty.db"
    c = sqlite3.connect(str(empty))
    c.execute("CREATE TABLE ecdict (id INTEGER PRIMARY KEY, word TEXT, word_lower TEXT)")
    c.commit()
    c.close()
    db = make_db(tmp_path, boxed_idx=[1])
    report = run(db, tmp_path, ecdict_path=empty)
    assert "词典是空的" in messages(report, D.FAIL)


def test_broken_ecdict_file_is_fail(tmp_path: Path):
    broken = tmp_path / "ecdict_broken.db"
    broken.write_bytes(b"definitely not sqlite")
    db = make_db(tmp_path, boxed_idx=[1])
    report = run(db, tmp_path, ecdict_path=broken)
    assert "词典打不开" in messages(report, D.FAIL)


def test_ecdict_with_broken_index_is_fail(tmp_path: Path):
    """有行但查不出来（word_lower 全空）：抽样查询必须把这事抓出来。"""
    p = mini_ecdict(tmp_path)
    copy = tmp_path / "ecdict_nolower.db"
    copy.write_bytes(p.read_bytes())
    c = sqlite3.connect(str(copy))
    c.execute("UPDATE ecdict SET word_lower = ''")
    c.commit()
    c.close()
    db = make_db(tmp_path, boxed_idx=[1])
    report = run(db, tmp_path, ecdict_path=copy)
    assert "抽样查询" in messages(report, D.FAIL)


# --- 7. 汇总 / 孤儿外键 ----------------------------------------------------


def test_summary_counts_rows(tmp_path: Path):
    db = make_db(tmp_path, boxed_idx=[1])
    c = raw_conn(db)
    lex = c.execute("SELECT id FROM Lexeme LIMIT 1").fetchone()["id"]
    seg = c.execute("SELECT id FROM Segment LIMIT 1").fetchone()["id"]
    c.execute("INSERT INTO VocabEntry (lexeme_id, added_at) VALUES (?, '2026-01-01')", (lex,))
    ve = c.execute("SELECT id FROM VocabEntry").fetchone()["id"]
    c.execute(
        "INSERT INTO Encounter (vocab_entry_id, segment_id, surface, added_at) "
        "VALUES (?,?,'quiet','2026-01-01')",
        (ve, seg),
    )
    c.execute("INSERT INTO AnnotationJob (lexeme_id, created_at) VALUES (?, '2026-01-01')", (lex,))
    c.execute(
        "INSERT INTO Mnemonic (lexeme_id, kind, payload_json) VALUES (?, 'story', '{}')", (lex,)
    )
    c.commit()
    c.close()
    report = run(db, tmp_path)
    counts = report.data["counts"]
    assert counts["VocabEntry"] == 1 and counts["Encounter"] == 1
    assert counts["AnnotationJob"] == 1 and counts["Mnemonic"] == 1
    assert "外键自洽" in messages(report, D.OK)


def test_orphan_foreign_keys_are_fail(tmp_path: Path):
    db = make_db(tmp_path, boxed_idx=[1])
    c = raw_conn(db)
    c.execute("PRAGMA foreign_keys = OFF")
    lex = c.execute("SELECT id FROM Lexeme LIMIT 1").fetchone()["id"]
    c.execute("INSERT INTO VocabEntry (lexeme_id, added_at) VALUES (?, '2026-01-01')", (lex,))
    ve = c.execute("SELECT id FROM VocabEntry").fetchone()["id"]
    c.execute(
        "INSERT INTO Encounter (vocab_entry_id, segment_id, surface, added_at) "
        "VALUES (?, 999999, 'ghost', '2026-01-01')",
        (ve,),
    )
    c.execute(
        "INSERT INTO AnnotationJob (lexeme_id, created_at) VALUES (999999, '2026-01-01')"
    )
    c.commit()
    c.close()
    report = run(db, tmp_path)
    assert "孤儿外键" in messages(report, D.FAIL)
    orphans = report.data["orphans"]["by_link"]
    assert orphans["Encounter.segment_id->Segment"] == 1
    assert orphans["AnnotationJob.lexeme_id->Lexeme"] == 1


def test_web_encounter_without_context_is_warn(tmp_path: Path):
    db = make_db(tmp_path, boxed_idx=[1])
    c = raw_conn(db)
    lex = c.execute("SELECT id FROM Lexeme LIMIT 1").fetchone()["id"]
    c.execute("INSERT INTO VocabEntry (lexeme_id, added_at) VALUES (?, '2026-01-01')", (lex,))
    ve = c.execute("SELECT id FROM VocabEntry").fetchone()["id"]
    c.execute(
        "INSERT INTO Encounter (vocab_entry_id, segment_id, surface, added_at, source_kind) "
        "VALUES (?, NULL, 'quiet', '2026-01-01', 'web')",
        (ve,),
    )
    c.commit()
    c.close()
    report = run(db, tmp_path)
    assert "没有 context_json" in messages(report, D.WARN)


# --- 8. 单集过滤 / 多集 ----------------------------------------------------


def test_content_id_filters_to_one_episode(tmp_path: Path):
    db = make_db(tmp_path, boxed_idx=[1, 2, 3, 4, 5, 6])
    make_db(tmp_path, lines=LINES[:2], season_ep="s01e02", db=db)
    all_report = run(db, tmp_path)
    assert len(all_report.data["contents"]) == 2

    one = run(db, tmp_path, content_id=2)
    assert [c["content_id"] for c in one.data["contents"]] == [2]
    assert one.data["contents"][0]["segments"] == 2


def test_unknown_content_id_is_fail(tmp_path: Path):
    db = make_db(tmp_path, boxed_idx=[1])
    report = run(db, tmp_path, content_id=4242)
    assert "content_id=4242 不存在" in messages(report, D.FAIL)
    assert report.exit_code() == 1


def test_empty_library_is_warn(tmp_path: Path):
    db = make_db(tmp_path, boxed_idx=[1])
    c = raw_conn(db)
    c.execute("DELETE FROM Segment")  # 手工连接不开外键，段得自己清，否则成孤儿
    c.execute("DELETE FROM Content")
    c.commit()
    c.close()
    report = run(db, tmp_path)
    assert "一集内容都没有" in messages(report, D.WARN)
    assert levels(report)[D.FAIL] == 0


# --- 9. 回归夹具：243 词框，其中 1 个空坐标 --------------------------------

REG_SEGMENTS = 37
REG_WORDS = 243


def regression_lines() -> list[str]:
    """37 段、总计 243 个词（模拟一段 2 分钟片的 OCR 产物规模）。"""
    base, extra = divmod(REG_WORDS, REG_SEGMENTS)  # 6 词打底，前 21 段各多 1 词
    lines, k = [], 0
    for i in range(REG_SEGMENTS):
        n = base + (1 if i < extra else 0)
        words = [POOL[(k + j) % len(POOL)] for j in range(n)]
        k += n
        lines.append(" ".join(words).capitalize() + ".")
    assert sum(len(s.rstrip(".").split()) for s in lines) == REG_WORDS
    return lines


@pytest.fixture()
def regression_db(tmp_path: Path) -> Path:
    """37 段全部有框、243 个词框、其中第 5 段第 2 个词丢框（x=null）。"""

    def drop_one_box(entries):
        w = entries[4]["words"][1]
        w.update(x=None, y=None, width=None, height=None)

    return make_db(
        tmp_path,
        lines=regression_lines(),
        boxed_idx=list(range(1, REG_SEGMENTS + 1)),
        mutate_boxes=drop_one_box,
        name="regression.db",
    )


def test_regression_243_boxes_one_null_coord(regression_db: Path, tmp_path: Path):
    report = run(regression_db, tmp_path)
    stats = seg_stats(report)

    assert stats["segments"] == REG_SEGMENTS
    assert stats["boxes"]["segments_with_boxes"] == REG_SEGMENTS
    assert stats["boxes"]["coverage"] == 1.0
    assert stats["boxes"]["total_boxes"] == REG_WORDS
    assert stats["boxes"]["null_coords"] == 1
    assert stats["boxes"]["negative_coords"] == 0
    assert stats["boxes"]["out_of_range_coords"] == 0
    assert stats["tokens"]["total_tokens"] == REG_WORDS
    assert stats["tokens"]["count_mismatch"] == []
    assert stats["tokens"]["text_mismatch"] == []

    # 丢一个框不该把整集判死
    assert report.verdict != D.FAIL, messages(report, D.FAIL)
    assert levels(report)[D.FAIL] == 0
    assert report.exit_code() == 0

    msg = messages(report)
    assert f"词框覆盖 {REG_SEGMENTS}/{REG_SEGMENTS} 段（100%）" in msg
    assert f"共 {REG_WORDS} 个词框" in msg
    assert f"空坐标 1/{REG_WORDS}" in msg


def test_regression_console_and_json_agree(regression_db: Path, tmp_path: Path, capsys):
    ec = str(mini_ecdict(tmp_path))
    rc_text = D.main(["--db", str(regression_db), "--ecdict", ec, "--no-ffprobe"])
    text = capsys.readouterr().out
    rc_json = D.main(["--db", str(regression_db), "--ecdict", ec, "--no-ffprobe", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert rc_text == rc_json == 0
    assert "✗" not in text
    assert payload["verdict"] in (D.OK, D.WARN)
    assert payload["counts"]["fail"] == 0
    boxes = payload["data"]["contents"][0]["boxes"]
    assert boxes["total_boxes"] == REG_WORDS and boxes["null_coords"] == 1


# --- 10. 退出码 / 输出格式 -------------------------------------------------


def test_exit_code_zero_when_only_warnings(tmp_path: Path, capsys):
    db = make_db(tmp_path, boxed_idx=[1, 2])  # 覆盖率不足 → ⚠
    ec = str(mini_ecdict(tmp_path))
    assert D.main(["--db", str(db), "--ecdict", ec, "--no-ffprobe"]) == 0
    out = capsys.readouterr().out
    assert "⚠" in out and "✗" not in out


def test_strict_turns_warnings_into_nonzero_exit(tmp_path: Path, capsys):
    db = make_db(tmp_path, boxed_idx=[1, 2])
    ec = str(mini_ecdict(tmp_path))
    assert D.main(["--db", str(db), "--ecdict", ec, "--no-ffprobe", "--strict"]) == 1
    assert "--strict 下按失败处理" in capsys.readouterr().out


def test_strict_does_not_change_clean_run(tmp_path: Path, capsys):
    video = tmp_path / "ep.mp4"
    video.write_bytes(b"x" * 32)
    db = make_db(tmp_path, boxed_idx=[1, 2, 3, 4, 5, 6], video=video)
    # --no-ffprobe 会产生一条 ⚠，所以这里只断言非 strict 通过、strict 因该 ⚠ 非零
    ec = str(mini_ecdict(tmp_path))
    assert D.main(["--db", str(db), "--ecdict", ec, "--no-ffprobe"]) == 0
    assert D.main(["--db", str(db), "--ecdict", ec, "--no-ffprobe", "--strict"]) == 1
    capsys.readouterr()


def test_failures_exit_nonzero_via_cli(tmp_path: Path, capsys):
    db = make_db(tmp_path, boxed_idx=[1], video=tmp_path / "gone.mp4")
    ec = str(mini_ecdict(tmp_path))
    rc = D.main(["--db", str(db), "--ecdict", ec, "--no-ffprobe"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "✗ 体检不通过" in out


def test_json_report_is_machine_readable(tmp_path: Path, capsys):
    db = make_db(tmp_path, boxed_idx=[1, 2, 3, 4, 5, 6])
    ec = str(mini_ecdict(tmp_path))
    rc = D.main(["--db", str(db), "--ecdict", ec, "--no-ffprobe", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == payload["exit_code"]
    assert set(payload) == {"verdict", "exit_code", "counts", "findings", "data"}
    assert set(payload["counts"]) == {D.OK, D.WARN, D.FAIL}
    assert {f["level"] for f in payload["findings"]} <= {D.OK, D.WARN, D.FAIL}
    assert all({"level", "section", "message"} <= set(f) for f in payload["findings"])
    data = payload["data"]
    assert data["schema"]["user_version"] == 2
    assert data["counts"]["Segment"] == 6
    assert data["contents"][0]["title"] == "Fixture Show"


def test_console_output_is_sectioned(tmp_path: Path, capsys):
    db = make_db(tmp_path, boxed_idx=[1])
    ec = str(mini_ecdict(tmp_path))
    D.main(["--db", str(db), "--ecdict", ec, "--no-ffprobe"])
    out = capsys.readouterr().out
    for header in ("== db ==", "== schema ==", "== ecdict ==", "== 汇总 =="):
        assert header in out
    assert "== Fixture Show s01e01 (content_id=1) ==" in out
    assert out.rstrip().splitlines()[-1].startswith("verdict:")


# --- 11. 只读性 ------------------------------------------------------------


def digest(p: Path) -> tuple[str, int, int]:
    b = p.read_bytes()
    st = p.stat()
    return hashlib.sha256(b).hexdigest(), st.st_size, st.st_mtime_ns


def test_doctor_never_writes_the_database(tmp_path: Path, capsys):
    """跑完体检，.db 主文件的 sha256 / 大小 / mtime 一律不变。"""
    video = tmp_path / "ep.mp4"
    video.write_bytes(b"x" * 128)
    db = make_db(tmp_path, boxed_idx=[1, 2, 3], video=video)
    ecdict = mini_ecdict(tmp_path)
    before_db, before_dict = digest(db), digest(ecdict)

    D.main(["--db", str(db), "--ecdict", str(ecdict), "--no-ffprobe"])
    D.main(["--db", str(db), "--ecdict", str(ecdict), "--no-ffprobe", "--json"])
    D.main(["--db", str(db), "--ecdict", str(ecdict), "--no-ffprobe", "--content-id", "1"])
    capsys.readouterr()

    assert digest(db) == before_db
    assert digest(ecdict) == before_dict


def test_doctor_connection_rejects_writes(tmp_path: Path):
    """连接本身就是 mode=ro：哪怕将来有人手滑写了 UPDATE，sqlite 会当场拒绝。"""
    db = make_db(tmp_path, boxed_idx=[1])
    conn = D.open_readonly(db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE Segment SET text_en = 'x'")
    finally:
        conn.close()


def test_doctor_does_not_create_a_missing_database(tmp_path: Path, capsys):
    """库不存在时只报错，绝不顺手 init_db 建一个空库出来。"""
    missing = tmp_path / "ghost.db"
    rc = D.main(["--db", str(missing), "--no-ffprobe"])
    capsys.readouterr()
    assert rc == 1
    assert not missing.exists()


# --- 12. v1 老库：只报告，不迁移，不 traceback（工单 17-3） -----------------
# 下面这份 DDL 是**真的 v1**：从 git 412db51（工单 9 时期，SCHEMA_VERSION = 1）
# 里逐字取出来的建表脚本 —— Encounter 那时还是 segment_id NOT NULL、没有
# source_kind / context_json。用户手上跑了半年的 data/poi-ocr.db 就长这样。
V1_SCHEMA = """
-- 1. 一集（或一个片段）的媒体 + 字幕来源
CREATE TABLE IF NOT EXISTS Content (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    season_ep   TEXT NOT NULL,
    video_path  TEXT,
    srt_path    TEXT,
    UNIQUE (title, season_ep)
);

-- 2. 字幕段。tokens_json 供 GET /segments 直接吐给前端；
--    word_boxes_json 由 extract_hardsub.py 的词级包围盒回填（§4 热区）。
CREATE TABLE IF NOT EXISTS Segment (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    content_id      INTEGER NOT NULL REFERENCES Content(id) ON DELETE CASCADE,
    idx             INTEGER NOT NULL,
    t_start         REAL NOT NULL,
    t_end           REAL NOT NULL,
    text_en         TEXT NOT NULL,
    tokens_json     TEXT,
    word_boxes_json TEXT,
    UNIQUE (content_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_segment_content_time
    ON Segment (content_id, t_start);

-- 3. 客观词典条目缓存（来自 ECDICT），与用户行为无关
CREATE TABLE IF NOT EXISTS Lexeme (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lemma       TEXT NOT NULL UNIQUE,
    pos         TEXT,
    ipa         TEXT,
    dict_gloss  TEXT
);

-- 4. surface -> lexeme 映射；surface 统一小写
CREATE TABLE IF NOT EXISTS WordForm (
    surface     TEXT PRIMARY KEY,
    lexeme_id   INTEGER NOT NULL REFERENCES Lexeme(id) ON DELETE CASCADE,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_wordform_lexeme ON WordForm (lexeme_id);

-- 5. 用户收藏了什么
CREATE TABLE IF NOT EXISTS VocabEntry (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lexeme_id   INTEGER NOT NULL UNIQUE REFERENCES Lexeme(id) ON DELETE CASCADE,
    added_at    TEXT NOT NULL,
    note        TEXT
);

-- 6. 每次真实语境下的相遇
CREATE TABLE IF NOT EXISTS Encounter (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    vocab_entry_id  INTEGER NOT NULL REFERENCES VocabEntry(id) ON DELETE CASCADE,
    segment_id      INTEGER NOT NULL REFERENCES Segment(id) ON DELETE CASCADE,
    surface         TEXT NOT NULL,
    added_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_encounter_vocab ON Encounter (vocab_entry_id);

-- 7. 异步助记任务队列（与收藏解耦）
CREATE TABLE IF NOT EXISTS AnnotationJob (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lexeme_id   INTEGER NOT NULL REFERENCES Lexeme(id) ON DELETE CASCADE,
    status      TEXT NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'running', 'done', 'failed')),
    priority    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    done_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_pick ON AnnotationJob (status, priority DESC, id);
CREATE INDEX IF NOT EXISTS idx_job_lexeme ON AnnotationJob (lexeme_id);

-- 8. 助记卡（按 lexeme 缓存 + 版本化）
CREATE TABLE IF NOT EXISTS Mnemonic (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    lexeme_id       INTEGER NOT NULL REFERENCES Lexeme(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    provider        TEXT,
    version         INTEGER NOT NULL DEFAULT 1,
    edited_by_user  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (lexeme_id, kind, version)
);

-- 9. 复习记录（M1 才写，表先建好）
CREATE TABLE IF NOT EXISTS Review (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    vocab_entry_id  INTEGER NOT NULL REFERENCES VocabEntry(id) ON DELETE CASCADE,
    at              TEXT NOT NULL,
    result          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_entry ON Review (vocab_entry_id, at);
"""

V1_USER_VERSION = 1


def make_v1_db(tmp_path: Path, name: str = "poi_v1.db", rows: bool = True) -> Path:
    """按真实 v1 DDL 建一个老库，塞点数据（含一条 Encounter）。绝不调用 init_db。"""
    db = tmp_path / name
    conn = sqlite3.connect(str(db))
    conn.executescript(V1_SCHEMA)
    conn.execute(f"PRAGMA user_version = {V1_USER_VERSION}")
    if rows:
        conn.execute(
            "INSERT INTO Content (id, title, season_ep, video_path, srt_path) "
            "VALUES (1, 'Fixture Show', 's01e01', NULL, NULL)"
        )
        conn.execute(
            "INSERT INTO Segment (id, content_id, idx, t_start, t_end, text_en, tokens_json) "
            "VALUES (1, 1, 1, 1.0, 4.0, ?, ?)",
            (
                LINES[0],
                json.dumps(
                    [{"surface": "the", "lemma": "the", "char_start": 0, "char_end": 3}]
                ),
            ),
        )
        conn.execute("INSERT INTO Lexeme (id, lemma) VALUES (1, 'gardener')")
        conn.execute(
            "INSERT INTO VocabEntry (id, lexeme_id, added_at) VALUES (1, 1, '2026-08-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO Encounter (id, vocab_entry_id, segment_id, surface, added_at) "
            "VALUES (1, 1, 1, 'gardener', '2026-08-01T00:00:00+00:00')"
        )
    conn.commit()
    conn.close()
    return db


def test_v1_db_has_no_source_kind_column(tmp_path: Path):
    """夹具自检：这确实是一个 v1 库（不是 init_db 建的 v2）。"""
    db = make_v1_db(tmp_path)
    conn = raw_conn(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(Encounter)")}
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert "source_kind" not in cols and "context_json" not in cols
    assert ver == V1_USER_VERSION and V1_USER_VERSION != D.SCHEMA_VERSION


def test_v1_db_is_reported_not_crashed(tmp_path: Path):
    """真实 v1 库：给出人话报告 + ✗，绝不 traceback（以前这里是 no such column）。"""
    db = make_v1_db(tmp_path)
    report = run(db, tmp_path)          # 抛异常的话这一行就炸了 —— 那正是回归点
    msg = messages(report)
    assert "老库" in msg and "Encounter" in msg
    assert "source_kind" in msg and "context_json" in msg
    assert "只读" in msg and "不会替你迁移" in msg
    assert "app.db import init_db" in msg or "uvicorn" in msg   # 怎么迁写清楚了
    assert report.verdict == D.FAIL
    assert report.data["schema"]["needs_migration"] is True
    assert report.data["schema"]["encounter_missing"] == ["source_kind", "context_json"]


def test_v1_db_exits_nonzero(tmp_path: Path, capsys):
    """`python -m app.doctor --db 老库` 退出码非零，输出里没有 Traceback。"""
    db = make_v1_db(tmp_path)
    rc = D.main(["--db", str(db), "--no-ffprobe", "--ecdict", str(mini_ecdict(tmp_path))])
    out = capsys.readouterr().out
    assert rc == 1
    assert "Traceback" not in out and "no such column" not in out
    assert "老库" in out


def test_v1_db_skips_v2_dependent_summary(tmp_path: Path):
    """依赖 v2 列的那条汇总检查被跳过并说明了 —— 不是偷偷不查。"""
    db = make_v1_db(tmp_path)
    report = run(db, tmp_path)
    skipped = [f for f in report.findings if f.data.get("skipped") == "encounter_context_json"]
    assert len(skipped) == 1 and skipped[0].level == D.WARN
    assert "跳过" in skipped[0].message
    # 与 Encounter 无关的检查照常出结果（行数、外键、时间轴）
    assert report.data["counts"]["Encounter"] == 1
    assert "orphans" in report.data


def test_v1_db_is_not_migrated_by_doctor(tmp_path: Path, capsys):
    """只读硬约束：体检完，库还是 v1，字节都没变。"""
    db = make_v1_db(tmp_path)
    before = digest(db)
    D.main(["--db", str(db), "--no-ffprobe", "--ecdict", str(mini_ecdict(tmp_path))])
    capsys.readouterr()
    conn = raw_conn(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(Encounter)")}
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert "source_kind" not in cols          # 没被顺手迁走
    assert ver == V1_USER_VERSION
    assert digest(db) == before               # 主文件逐字节不变（含 mtime）


def test_v2_db_with_stale_user_version_is_only_a_warning(tmp_path: Path):
    """表结构已经是 v2、只是版本号没盖上：⚠ 而不是 ✗（还能用）。"""
    db = make_db(tmp_path, boxed_idx={1})
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()
    report = run(db, tmp_path)
    assert report.data["schema"]["needs_migration"] is False
    warn = messages(report, D.WARN)
    assert "user_version=1" in warn and "版本号没盖上" in warn
    assert report.verdict == D.WARN
