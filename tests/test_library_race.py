"""同名并发导入的仲裁（工单 17-2）。

验的是这一条：两个同名 (title, season_ep) 的导入同时冲进入库阶段时，
**必定一成一败**，败的那个把自己的 uuid 媒体目录清干净——库里不会出现
"后者覆盖前者的 Content、前者的媒体目录成了没人引用的孤儿"。

本文件不需要 ffmpeg：ffprobe 那一层用假的 MediaInfo 顶掉（并发竞态与媒体
校验无关），所以它在任何机器上都真跑，不会被 skip 掉当作通过。

素材同样自造（conftest 的 FIXTURE_SRT），不含任何真实台词。
"""

from __future__ import annotations

import sqlite3
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import library as lib  # noqa: E402
from app.db import Database, init_db  # noqa: E402
from app.ingest import ContentExists, ingest_srt, upsert_content  # noqa: E402
from tests.conftest import FIXTURE_SRT  # noqa: E402

TITLE = "Fixture Show"
SEASON_EP = "s01e01"
BARRIER_TIMEOUT = 30.0


# --- create_only 本身（单测，不起线程） ------------------------------------


def test_upsert_content_create_only_refuses_to_overwrite(conn: sqlite3.Connection):
    """create_only=True：同一集第二次进来直接抛，绝不改已有行。"""
    first = upsert_content(conn, TITLE, SEASON_EP, "/media/a.mp4", "/srt/a.srt", True)
    with pytest.raises(ContentExists) as ei:
        upsert_content(conn, TITLE, SEASON_EP, "/media/b.mp4", "/srt/b.srt", True)
    assert (ei.value.title, ei.value.season_ep) == (TITLE, SEASON_EP)

    rows = conn.execute("SELECT * FROM Content").fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == first
    assert rows[0]["video_path"] == "/media/a.mp4"  # 第二次的路径没写进去


def test_upsert_content_default_stays_idempotent_for_cli(conn: sqlite3.Connection):
    """默认（CLI 的 python -m app.ingest）照旧幂等覆盖 —— 重跑一集是日常操作。"""
    first = upsert_content(conn, TITLE, SEASON_EP, "/media/a.mp4", "/srt/a.srt")
    again = upsert_content(conn, TITLE, SEASON_EP, "/media/b.mp4", "/srt/b.srt")
    assert first == again
    row = conn.execute("SELECT * FROM Content").fetchone()
    assert row["video_path"] == "/media/b.mp4"  # 覆盖是 CLI 想要的行为


def test_ingest_srt_create_only_leaves_nothing_behind(tmp_path: Path):
    """create_only 撞车时整个事务回滚：段数不变、库里没有半条新内容。"""
    db = tmp_path / "poi.db"
    srt = tmp_path / "ep.srt"
    srt.write_text(FIXTURE_SRT, encoding="utf-8")
    stats = ingest_srt(
        db_path=db, srt_path=srt, title=TITLE, season_ep=SEASON_EP,
        video_path="/media/a.mp4", create_only=True,
    )
    with pytest.raises(ContentExists):
        ingest_srt(
            db_path=db, srt_path=srt, title=TITLE, season_ep=SEASON_EP,
            video_path="/media/b.mp4", create_only=True,
        )
    conn = init_db(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM Content").fetchone()[0] == 1
        row = conn.execute("SELECT * FROM Content").fetchone()
        assert row["id"] == stats["content_id"] and row["video_path"] == "/media/a.mp4"
        n = conn.execute(
            "SELECT COUNT(*) FROM Segment WHERE content_id = ?", (row["id"],)
        ).fetchone()[0]
        assert n == stats["segments"]
    finally:
        conn.close()


# --- 并发：barrier 把两个 job 钉在同一瞬间冲入库 ---------------------------


def fake_probe(path, timeout: float = 120.0) -> lib.MediaInfo:
    """假 ffprobe：这组用例验的是并发仲裁，不验媒体校验（所以不依赖系统 ffmpeg）。"""
    return lib.MediaInfo(
        path=Path(path), has_video=True, has_audio=True, duration=2.0,
        vcodec="h264", acodec="aac",
    )


def make_work_dir(root: Path, srt_text: str = FIXTURE_SRT) -> tuple[Path, Path, Path]:
    """造一个 uuid 目录 + 里头的假媒体和自造 srt（同 POST /import 落盘后的样子）。"""
    work = lib.new_work_dir(root)
    video = work / "ep.mp4"
    video.write_bytes(b"\x00" * 2048)  # 内容无所谓：probe 是假的
    srt = work / "ep.srt"
    srt.write_text(srt_text, encoding="utf-8")
    return work, video, srt


def test_concurrent_same_episode_import_one_wins_one_fails(tmp_path: Path, monkeypatch):
    """两个同名导入同时冲入库：一成一败，败者目录清空，零孤儿。"""
    monkeypatch.setattr(lib, "probe", fake_probe)

    db = tmp_path / "poi.db"
    init_db(db).close()
    database = Database(db)
    root = tmp_path / "library"
    root.mkdir()

    # 两个线程在真正写 Content 之前对齐，尽可能撞在同一瞬间
    barrier = threading.Barrier(2, timeout=BARRIER_TIMEOUT)
    real_ingest = lib.ingest_srt

    def barrier_ingest(**kw):
        barrier.wait()
        return real_ingest(**kw)

    monkeypatch.setattr(lib, "ingest_srt", barrier_ingest)

    jobs = []
    for _ in range(2):
        work, video, srt = make_work_dir(root)
        job = lib.ImportJob(
            id=work.name, title=TITLE, season_ep=SEASON_EP, dir=work
        )
        jobs.append((job, video, srt))

    def run(job, video, srt):
        lib.run_import(
            job,
            conn_factory=database.conn,
            db_path=db,
            video_path=video,
            srt_path=srt,
        )

    threads = [
        threading.Thread(target=run, args=(j, v, s), daemon=True) for j, v, s in jobs
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=BARRIER_TIMEOUT + 30)
        assert not t.is_alive(), "导入线程卡死了"

    done = [j for j, _v, _s in jobs if j.stage == lib.STAGE_DONE]
    failed = [j for j, _v, _s in jobs if j.stage == lib.STAGE_ERROR]
    assert len(done) == 1 and len(failed) == 1, [
        (j.stage, j.error) for j, _v, _s in jobs
    ]
    # 「撞车」这三个字只出现在 INSERT 撞唯一索引的那条路径上——
    # 两个线程都是在 barrier 之前做的预检 SELECT，所以仲裁必定发生在唯一索引上，
    # 不是那次"友好提示"用的预检。
    assert "撞车" in (failed[0].error or ""), failed[0].error
    assert "已经在库里" in (failed[0].error or "") and "不覆盖" in (failed[0].error or "")

    conn = database.conn()
    rows = conn.execute("SELECT * FROM Content").fetchall()
    assert len(rows) == 1, "同名的两条 Content 不该同时存在"
    assert rows[0]["id"] == done[0].content_id
    # 库里指着的媒体，就是赢家那个 uuid 目录里的那份
    assert Path(rows[0]["video_path"]).parent == done[0].dir

    # 磁盘：只剩赢家的 uuid 目录，输家的连目录带媒体一起被清掉（零孤儿）
    dirs = sorted(p for p in root.iterdir() if p.is_dir())
    assert dirs == [done[0].dir]
    assert not failed[0].dir.exists()
    # 反过来再确认一遍：磁盘上每个目录都被库里的 Content 引用着
    referenced = {Path(r["video_path"]).parent for r in rows}
    assert set(dirs) == referenced

    database.close_all()


def test_concurrent_different_episodes_both_succeed(tmp_path: Path, monkeypatch):
    """对照组：不同集数的并发导入互不干扰，两个都成、两个目录都留着。"""
    monkeypatch.setattr(lib, "probe", fake_probe)

    db = tmp_path / "poi.db"
    init_db(db).close()
    database = Database(db)
    root = tmp_path / "library"
    root.mkdir()

    barrier = threading.Barrier(2, timeout=BARRIER_TIMEOUT)
    real_ingest = lib.ingest_srt

    def barrier_ingest(**kw):
        barrier.wait()
        return real_ingest(**kw)

    monkeypatch.setattr(lib, "ingest_srt", barrier_ingest)

    jobs = []
    for season_ep in ("s01e01", "s01e02"):
        work, video, srt = make_work_dir(root)
        jobs.append(
            (
                lib.ImportJob(id=work.name, title=TITLE, season_ep=season_ep, dir=work),
                video,
                srt,
            )
        )

    threads = [
        threading.Thread(
            target=lambda j=j, v=v, s=s: lib.run_import(
                j, conn_factory=database.conn, db_path=db, video_path=v, srt_path=s
            ),
            daemon=True,
        )
        for j, v, s in jobs
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=BARRIER_TIMEOUT + 30)
        assert not t.is_alive()

    assert [j.stage for j, _v, _s in jobs] == [lib.STAGE_DONE, lib.STAGE_DONE], [
        j.error for j, _v, _s in jobs
    ]
    conn = database.conn()
    rows = conn.execute("SELECT season_ep FROM Content ORDER BY season_ep").fetchall()
    assert [r["season_ep"] for r in rows] == ["s01e01", "s01e02"]
    assert len(sorted(p for p in root.iterdir() if p.is_dir())) == 2

    database.close_all()
