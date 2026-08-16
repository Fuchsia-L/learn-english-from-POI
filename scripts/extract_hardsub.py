#!/usr/bin/env python3
"""Extract English hardsubs from a video into an .srt, fully offline.

Validated on Person of Interest (bilibili licensed 1080p, burned-in bilingual
subs: Chinese line above, English line below, white text with dark outline).
Word-level accuracy on a 2-min test clip: ~99.7% (37/37 cues correct after
cleanup; all 5 non-subtitle light-bleed segments correctly rejected).

Pipeline (all local, zero API cost):
  1. ffmpeg samples the English-line crop band at --fps
  2. consecutive frames are merged into segments via mask IoU (same cue
     persists across frames; a change of cue drops IoU below threshold)
  3. tesseract OCRs one representative frame per segment (TSV output: text and
     word-level bounding boxes in one pass)
  4. deterministic cleanup fixes known tesseract quirks (| -> I, "Is" -> "is")
     and a junk filter drops non-subtitle segments (e.g. bright surveillance
     shots bleeding into the crop band)
  5. cues are written as .srt with segment start/end timestamps
  6. optional --boxes-json writes per-cue word boxes mapped back to the original
     video coordinate system, for the player's transparent click hotspots
     (DESIGN.md §4)

Dependencies: ffmpeg, tesseract-ocr (eng traineddata), python: pillow, numpy,
wordfreq.  Install: pip install pillow numpy wordfreq

Usage:
  python extract_hardsub.py episode.mp4 -o episode.en.srt
  python extract_hardsub.py episode.mp4 --crop 1920:54:0:1026 --fps 3
  python extract_hardsub.py episode.mp4 -o ep.srt --boxes-json ep.boxes.json

The default --crop targets 1920x1080 bilibili POI hardsubs. For other sources,
grab a frame with a subtitle visible and adjust W:H:X:Y until the band contains
exactly the English line.
"""
import argparse
import csv
import difflib
import glob
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

import numpy as np
from PIL import Image
from wordfreq import zipf_frequency

WHITE_THRESHOLD = 200   # pixel value above which we treat a pixel as glyph
MIN_TEXT_PX = 400       # fewer white pixels than this = no subtitle in frame
IOU_SAME_CUE = 0.5      # mask IoU above this = same subtitle as previous frame
UPSCALE = 2             # ocr_frame upscales the strip before tesseract
MIN_WORD_CONF = 20.0    # tesseract word confidence below this = drop the box
MERGE_MAX_AREA_RATIO = 3.0   # merged box this much bigger than its parts = bogus


# ---------------------------------------------------------------- sampling

def sample_strips(video, workdir, fps, crop):
    strip_dir = os.path.join(workdir, "strips")
    os.makedirs(strip_dir, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-i", video,
         "-vf", f"fps={fps},crop={crop}",
         os.path.join(strip_dir, "f%06d.png"), "-y"],
        check=True)
    return sorted(glob.glob(os.path.join(strip_dir, "f*.png")))


# ------------------------------------------------------------------- dedup

def _mask(path):
    return np.asarray(Image.open(path).convert("L"), dtype=np.uint8) > WHITE_THRESHOLD


def _iou(m1, m2):
    union = np.logical_or(m1, m2).sum()
    return 1.0 if union == 0 else np.logical_and(m1, m2).sum() / union


def dedup(files, fps):
    """Merge consecutive frames showing the same cue -> list of segments."""
    segs, cur, prev_mask = [], None, None
    for i, f in enumerate(files):
        m = _mask(f)
        px = int(m.sum())
        if px < MIN_TEXT_PX:
            if cur:
                segs.append(cur)
            cur, prev_mask = None, None
            continue
        if cur is None or prev_mask is None or _iou(prev_mask, m) < IOU_SAME_CUE:
            if cur:
                segs.append(cur)
            cur = {"start_i": i, "end_i": i, "best_i": i, "best_px": px}
        else:
            cur["end_i"] = i
            if px > cur["best_px"]:
                cur.update(best_px=px, best_i=i)
        prev_mask = m
    if cur:
        segs.append(cur)
    return [{
        "start": s["start_i"] / fps,
        "end": (s["end_i"] + 1) / fps,
        "png": files[s["best_i"]],
        "n_frames": s["end_i"] - s["start_i"] + 1,
    } for s in segs]


# --------------------------------------------------------------------- ocr

def parse_crop(crop):
    """ffmpeg crop 'W:H:X:Y' -> (w, h, x, y) ints.  X/Y default to 0.

    Only plain integer fields are supported (ffmpeg also accepts expressions);
    anything else raises ValueError, since word boxes need a known offset.
    """
    parts = str(crop).split(":")
    if len(parts) not in (2, 4):
        raise ValueError(f"unsupported crop {crop!r}: expected W:H or W:H:X:Y")
    try:
        vals = [int(p) for p in parts]
    except ValueError:
        raise ValueError(f"unsupported crop {crop!r}: non-integer field") from None
    if len(vals) == 2:
        vals += [0, 0]
    w, h, x, y = vals
    if w <= 0 or h <= 0 or x < 0 or y < 0:
        raise ValueError(f"unsupported crop {crop!r}: bad geometry")
    return w, h, x, y


def parse_tsv(tsv_text, crop_box=None):
    """tesseract TSV -> (words, boxes) in *original video* coordinates.

    words[i] is the raw OCR token, boxes[i] its box dict or None when the box
    is untrustworthy (low confidence, degenerate, or outside the crop band):
    drop the box, keep the word.  crop_box is (w, h, x, y) from parse_crop;
    None means "no geometry known" -> every box is dropped.
    """
    rows = csv.DictReader(io.StringIO(tsv_text), delimiter="\t", quoting=csv.QUOTE_NONE)
    words, boxes = [], []
    for row in rows:
        if row.get("level") != "5":                      # level 5 = word
            continue
        text = (row.get("text") or "").strip()
        if not text:
            continue
        words.append(text)
        boxes.append(_box_from_row(row, crop_box))
    return words, boxes


def _box_from_row(row, crop_box):
    if crop_box is None:
        return None
    try:
        left, top = int(row["left"]), int(row["top"])
        w, h = int(row["width"]), int(row["height"])
        conf = float(row["conf"])
    except (KeyError, TypeError, ValueError):
        return None
    if conf < MIN_WORD_CONF or w <= 0 or h <= 0:
        return None
    crop_w, crop_h, crop_x, crop_y = crop_box
    # tesseract coordinates are in the upscaled crop strip
    if left < 0 or top < 0:
        return None
    if left + w > crop_w * UPSCALE or top + h > crop_h * UPSCALE:
        return None
    x0, x1 = round(left / UPSCALE), round((left + w) / UPSCALE)
    y0, y1 = round(top / UPSCALE), round((top + h) / UPSCALE)
    if x1 <= x0 or y1 <= y0:
        return None
    return {"x": x0 + crop_x, "y": y0 + crop_y, "width": x1 - x0, "height": y1 - y0}


def ocr_frame(png, crop_box=None):
    """Binarize (black text on white), upscale 2x, tesseract single-line TSV.

    Returns (text, boxes): text is the whitespace-joined OCR tokens (identical
    to what `--psm 7` plain text output yields), boxes is the parallel list of
    per-token box dicts / None, in original video coordinates.
    """
    a = np.asarray(Image.open(png).convert("L"), dtype=np.uint8)
    bw = Image.fromarray(np.where(a > WHITE_THRESHOLD, 0, 255).astype(np.uint8))
    bw = bw.resize((bw.width * UPSCALE, bw.height * UPSCALE), Image.LANCZOS)
    tmp = png + ".bw.png"
    bw.save(tmp)
    r = subprocess.run(
        ["tesseract", tmp, "stdout", "-l", "eng", "--psm", "7", "tsv"],
        capture_output=True, text=True)
    os.remove(tmp)
    words, boxes = parse_tsv(r.stdout, crop_box)
    return " ".join(words), boxes


# ----------------------------------------------------------------- cleanup

def clean(text):
    t = re.sub(r"(?<![\w])\|(?![\w])", "I", text)   # standalone | -> I
    t = re.sub(r"\bI I\b", "I", t)                  # doubled I from '| |'
    t = re.sub(r"([a-z][,.]? )Is\b", r"\1is", t)    # 'is' mid-sentence quirk
    return re.sub(r"\s+", " ", t).strip()


def _union(boxes):
    """Union of merged word boxes, or None if the pieces are too far apart.

    Merges come from clean() (e.g. '| |' -> 'I').  When one of the pieces is
    light-bleed noise at the far edge of the band, the union would be a huge
    hotspot swallowing its neighbours -> better no hotspot than a wrong one.
    """
    bs = [b for b in boxes if b]
    if not bs:
        return None
    x0 = min(b["x"] for b in bs)
    y0 = min(b["y"] for b in bs)
    x1 = max(b["x"] + b["width"] for b in bs)
    y1 = max(b["y"] + b["height"] for b in bs)
    if len(bs) > 1:
        parts = sum(b["width"] * b["height"] for b in bs)
        if (x1 - x0) * (y1 - y0) > MERGE_MAX_AREA_RATIO * max(1, parts):
            return None
    return {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}


def _split_groups(items, n_groups):
    """Split a list into n_groups contiguous, as-even-as-possible chunks."""
    if n_groups <= 0:
        return []
    total, out, start = len(items), [], 0
    for g in range(n_groups):
        end = (g + 1) * total // n_groups
        out.append(items[start:end])
        start = end
    return out


def align_boxes(raw_words, clean_words, raw_boxes):
    """Map boxes of the raw OCR tokens onto the cleaned tokens.

    clean() only rewrites characters (| -> I, Is -> is), merges tokens
    ("I I" -> "I") or drops them; it never reorders.  A token-level diff is
    therefore enough: equal/1:1 runs keep their box, merges get the union of
    the merged boxes, dropped tokens lose their box, and any cleaned token
    with no raw counterpart gets None (no hotspot).
    """
    out = [None] * len(clean_words)
    if len(raw_boxes) != len(raw_words):   # desync -> no hotspots, never crash
        return out
    sm = difflib.SequenceMatcher(a=list(raw_words), b=list(clean_words), autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                out[j1 + k] = raw_boxes[i1 + k]
        elif tag == "replace":
            groups = _split_groups(raw_boxes[i1:i2], j2 - j1)
            for k, g in enumerate(groups):
                out[j1 + k] = _union(g)
        elif tag == "insert":
            pass          # cleaned-in token with no OCR source -> no box
        # tag == "delete": raw tokens vanished, their boxes go with them
    return out


def words_with_boxes(raw_text, raw_boxes, cleaned_text):
    """-> [{w, x, y, width, height}] for one cue; x is None when there's no box."""
    raw_words = raw_text.split()
    clean_words = cleaned_text.split()
    boxes = align_boxes(raw_words, clean_words, raw_boxes)
    out = []
    for w, b in zip(clean_words, boxes):
        if b is None:
            out.append({"w": w, "x": None, "y": None, "width": None, "height": None})
        else:
            out.append({"w": w, **b})
    return out


def is_junk(text, n_frames):
    """Reject OCR noise from non-subtitle brightness (city lights, HUD shots)."""
    if not text:
        return True
    words = re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", text)
    if not words:
        return True
    known = [w for w in words if zipf_frequency(w.lower(), "en") > 2.5]
    if len(known) / len(words) < 0.5:
        return True
    avg_len = sum(map(len, words)) / len(words)
    nonspace = text.replace(" ", "")
    sym = sum(not (c.isalnum() or c in "'\",.?!-…“”") for c in nonspace)
    sym_ratio = sym / max(1, len(nonspace))
    if n_frames <= 2 and (avg_len < 2.6 or sym_ratio > 0.15):
        return True
    if len(words) <= 1 and (avg_len < 3 or sym_ratio > 0.15):
        return True
    return False


# --------------------------------------------------------------------- srt

def fmt_ts(t):
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int(round((t % 1) * 1000)):03d}"


def write_srt(cues, path):
    with open(path, "w", encoding="utf-8") as f:
        for n, c in enumerate(cues, 1):
            f.write(f"{n}\n{fmt_ts(c['start'])} --> {fmt_ts(c['end'])}\n{c['text']}\n\n")


# ------------------------------------------------------------------- boxes

def boxes_payload(cues):
    """Word boxes for the kept cues; idx matches the 1-based .srt cue number."""
    return [{
        "idx": n,
        "start": round(c["start"], 3),
        "end": round(c["end"], 3),
        "text": c["text"],
        "words": c.get("words", []),
    } for n, c in enumerate(cues, 1)]


def write_boxes_json(cues, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(boxes_payload(cues), f, ensure_ascii=False, indent=1)


# -------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("video")
    ap.add_argument("-o", "--out", default=None, help="output .srt (default: <video>.en.srt)")
    ap.add_argument("--fps", type=float, default=3.0, help="sampling rate (default 3)")
    ap.add_argument("--crop", default="1920:54:0:1026",
                    help="ffmpeg crop W:H:X:Y of the English sub line")
    ap.add_argument("--dump-json", default=None,
                    help="also dump segments (incl. dropped) as JSON for review")
    ap.add_argument("--boxes-json", default=None,
                    help="also write word-level boxes (player hotspots) as JSON")
    args = ap.parse_args()

    out = args.out or os.path.splitext(args.video)[0] + ".en.srt"
    crop_box = parse_crop(args.crop) if args.boxes_json else None
    workdir = tempfile.mkdtemp(prefix="hardsub_")
    try:
        files = sample_strips(args.video, workdir, args.fps, args.crop)
        print(f"sampled {len(files)} frames", file=sys.stderr)
        segs = dedup(files, args.fps)
        print(f"merged into {len(segs)} candidate segments", file=sys.stderr)

        kept, dropped = [], []
        for s in segs:
            raw_text, raw_boxes = ocr_frame(s["png"], crop_box)
            s["text"] = clean(raw_text)
            if args.boxes_json:
                s["words"] = words_with_boxes(raw_text, raw_boxes, s["text"])
            (dropped if is_junk(s["text"], s["n_frames"]) else kept).append(s)
        for s in dropped:
            print(f"  dropped {s['start']:7.2f}s {s['text']!r}", file=sys.stderr)

        write_srt(kept, out)
        print(f"wrote {out}: {len(kept)} cues ({len(dropped)} junk dropped)")

        if args.boxes_json:
            write_boxes_json(kept, args.boxes_json)
            n_words = sum(len(c["words"]) for c in kept)
            n_boxed = sum(1 for c in kept for w in c["words"] if w["x"] is not None)
            print(f"wrote {args.boxes_json}: {n_words} words, "
                  f"{n_boxed} boxed, {n_words - n_boxed} without box")

        if args.dump_json:
            for s in segs:
                s.pop("png", None)
            json.dump({"kept": kept, "dropped": dropped},
                      open(args.dump_json, "w"), ensure_ascii=False, indent=1)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
