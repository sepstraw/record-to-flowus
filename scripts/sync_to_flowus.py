import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

HEX_MEETINGS_DEFAULT = os.path.join(os.environ.get("APPDATA", ""), "hex", "meetings")
HEX_EXPORT_DIR = os.environ.get("HEX_EXPORT_DIR", "")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _is_inside(path, base):
    try:
        real_path = os.path.realpath(path)
        real_base = os.path.realpath(base)
        return os.path.commonpath([real_path, real_base]) == real_base
    except Exception:
        return False


def _validate_meeting_id(mid):
    return isinstance(mid, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,128}", mid) is not None


def _validate_uuid(value, name):
    if not isinstance(value, str) or UUID_RE.fullmatch(value) is None:
        raise SystemExit(f"error: invalid {name}")
    return value


def _resolve_transcript_path(export_path, meeting_dir, meetings_dir):
    if isinstance(export_path, str) and export_path.strip():
        ep = os.path.normpath(export_path.strip())
        allowed = [meeting_dir, meetings_dir]
        if HEX_EXPORT_DIR:
            allowed.append(HEX_EXPORT_DIR)
        for base in allowed:
            if base and _is_inside(ep, base) and os.path.isfile(ep):
                return ep
    fallback = os.path.join(meeting_dir, "transcript.md")
    if os.path.isfile(fallback):
        return fallback
    return None


def run_flowus(args, body_path=None):
    cmd = ["flowus"] + args
    if body_path:
        cmd += ["--body", body_path]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    try:
        data = json.loads(out or err)
    except Exception:
        raise RuntimeError(f"flowus failed: {err or out}")
    return data, r.returncode, out, err


def run_flowus_ok(args, body_path=None):
    data, code, out, err = run_flowus(args, body_path)
    if code != 0 or not isinstance(data, dict) or not data.get("ok", True):
        # flowus --json usually returns {"ok":false,...} on error
        if isinstance(data, dict) and data.get("ok") is False:
            return data, False
        raise RuntimeError(f"flowus command failed (code={code}): {err or out}")
    return data, True


# FlowUs rejects uploads above 100 MB ("File size exceeds 100MB limit").
UPLOAD_LIMIT_BYTES = 100 * 1024 * 1024
UPLOAD_SAFE_BYTES = 95 * 1024 * 1024  # headroom for multipart overhead


def _transcode_audio(src, work_dir):
    """Shrink an oversized wav with ffmpeg; return ((path, bitrate)) or None."""
    ff = shutil.which("ffmpeg")
    if not ff:
        return None
    out = os.path.join(work_dir, "microphone.mp3")
    # 64k mono keeps ~2.5h under the limit; drop lower only for very long recordings.
    for bitrate in ("64k", "32k", "16k"):
        try:
            subprocess.run(
                [ff, "-y", "-loglevel", "error", "-i", src, "-ac", "1", "-b:a", bitrate, out],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3600,
            )
        except Exception:
            continue
        if os.path.exists(out):
            if os.path.getsize(out) <= UPLOAD_SAFE_BYTES:
                return out, bitrate
            os.remove(out)
    return None


def prepare_audio(wav_path, work_dir):
    """Return (path_to_upload, label), or (None, label) when audio must be skipped.

    Hex records uncompressed wav (~220 MB/hour), so anything past roughly 25
    minutes exceeds the 100 MB FlowUs upload limit. Compress with ffmpeg when it
    is available; otherwise skip the audio block and say why -- the note itself
    still has to land on the page.
    """
    try:
        size = os.path.getsize(wav_path)
    except OSError:
        return None, "（音频文件不可读）"
    mb = size / 1024 / 1024
    if size <= UPLOAD_SAFE_BYTES:
        return wav_path, f"microphone.wav（{mb:.1f} MB）"
    converted = _transcode_audio(wav_path, work_dir)
    if converted:
        path, bitrate = converted
        out_mb = os.path.getsize(path) / 1024 / 1024
        return path, f"microphone.mp3（{out_mb:.1f} MB，由 {mb:.0f} MB wav 转码，{bitrate}）"
    return None, (
        f"录音 {mb:.0f} MB，超过 FlowUs 100 MB 上传上限，本次未上传音频。"
        f"本地原始文件：{wav_path}"
    )


def load_meeting(meeting_id=None, meetings_dir=None):
    meetings_dir = os.path.normpath(meetings_dir or os.environ.get("HEX_MEETINGS_DIR") or HEX_MEETINGS_DEFAULT)
    if not os.path.isdir(meetings_dir):
        raise RuntimeError(f"meetings dir not found: {meetings_dir}")
    if meeting_id and not _validate_meeting_id(meeting_id):
        raise RuntimeError("invalid meeting id")

    dirs = [os.path.join(meetings_dir, d) for d in os.listdir(meetings_dir) if os.path.isdir(os.path.join(meetings_dir, d))]
    candidates = []
    for d in dirs:
        if not _is_inside(d, meetings_dir):
            continue
        mf = os.path.join(d, "manifest.json")
        if not os.path.isfile(mf):
            continue
        try:
            with open(mf, "r", encoding="utf-8") as f:
                m = json.load(f)
        except Exception:
            continue
        mid = m.get("id") or os.path.basename(d)
        if not _validate_meeting_id(str(mid)):
            continue
        if meeting_id and str(mid) != meeting_id:
            continue
        if not meeting_id and m.get("status") != "complete":
            continue
        candidates.append((m.get("created_at_ms") or 0, d, m))
    if not candidates:
        raise RuntimeError("no matching complete meeting found")
    candidates.sort()
    _, d, m = candidates[-1]
    mid = str(m.get("id") or os.path.basename(d))
    if not _validate_meeting_id(mid):
        raise RuntimeError("invalid meeting id")

    transcript_md = _resolve_transcript_path(m.get("export_path"), d, meetings_dir)
    if not transcript_md:
        raise RuntimeError("transcript not found")
    with open(transcript_md, "r", encoding="utf-8") as f:
        transcript = f.read()

    wav = os.path.join(d, "microphone.wav")
    if not os.path.isfile(wav):
        wav = os.path.join(d, "system.wav")
    if not os.path.isfile(wav) or not _is_inside(wav, meetings_dir):
        raise RuntimeError("audio file not found")

    return {"id": mid, "dir": d, "manifest": m, "wav": wav, "transcript_path": transcript_md, "transcript": transcript}


def rt(text, bold=False, italic=False, code=False, color="default"):
    ann = {"bold": bold, "italic": italic, "strikethrough": False, "underline": False, "code": code,
           "color": color, "background_color": "default", "text_color": color}
    return [{"type": "text", "text": {"content": text, "link": None}, "annotations": ann, "plain_text": text, "href": None}]


def para(text, bold=False):
    return {"type": "paragraph", "paragraph": {"rich_text": rt(text, bold=bold), "text_color": "default", "color": "default", "background_color": "default"}}


def bullet(items_rt):
    return [{"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": rti, "text_color": "default", "color": "default", "background_color": "default"}} for rti in items_rt]


def todo(text, checked=False):
    return {"type": "to_do", "to_do": {"rich_text": rt(text), "checked": checked, "text_color": "default", "color": "default", "background_color": "default"}}


def heading2(text, color="blue"):
    return {"type": "heading_2", "heading_2": {"rich_text": rt(text, bold=True, color=color), "is_toggleable": False, "text_color": color, "color": color, "background_color": "default"}}


def divider():
    return {"type": "divider", "divider": {}}


def callout(title, icon, bg="gray", title_color="default", title_bold=True, children=None, body_rich=None):
    rich = rt(title, bold=title_bold, color=title_color)
    if body_rich:
        rich = rich + body_rich
    c = {"type": "callout", "callout": {"rich_text": rich, "icon": {"type": "emoji", "emoji": icon},
        "background_color": bg, "text_color": "default", "color": "default"}}
    if children:
        c["children"] = children
    return c


def toggle(title, children):
    return {"type": "toggle", "toggle": {"rich_text": rt(title, bold=True), "text_color": "default", "color": "default", "background_color": "default"}, "children": children}


def column_list(cols):
    return {"type": "column_list", "column_list": {}, "children": [
        {"type": "column", "column": {"width_ratio": w}, "children": ch} for w, ch in cols
    ]}


def file_block(oss_name, size):
    return {"type": "file", "file": {"type": "file", "file": {"oss_name": oss_name, "size": size}, "caption": []}}


def chunk_paras(text, size=1800):
    parts = []
    buf = ""
    for line in text.splitlines():
        if len(buf) + len(line) + 1 > size and buf:
            parts.append(buf.rstrip())
            buf = line + "\n"
        else:
            buf += line + "\n"
    if buf.strip():
        parts.append(buf.rstrip())
    return parts or ([text[:size]] if text else [])


def parse_summary_sections(summary_text):
    out = {"basic": [], "summary": "", "points": [], "todos": []}
    sec = None
    for raw in (summary_text or "").splitlines():
        line = raw.strip()
        if line.startswith("### "):
            h = line[4:].strip()
            if "基本信息" in h: sec = "basic"
            elif "摘要" in h: sec = "summary"
            elif "要点" in h: sec = "points"
            elif "行动" in h or "待办" in h: sec = "todos"
            else: sec = None
            continue
        if not line or line.startswith("## "):
            continue
        if sec == "basic":
            out["basic"].append(line.lstrip("- ").strip())
        elif sec == "summary":
            out["summary"] += (line + " ")
        elif sec == "points":
            if line.startswith("- "): out["points"].append(line[2:].strip())
            elif line: out["points"].append(line)
        elif sec == "todos":
            if line.startswith("- "): out["todos"].append(line[2:].strip())
            elif line: out["todos"].append(line)
    out["summary"] = out["summary"].strip()
    return out


def marker_text(archive_id):
    return f"ARCHIVE_ID:{archive_id}"


def _safe_token(s):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(s or ""))[:80] or "run"


def make_tmp_dir(base_hint="vn"):
    d = os.path.join(tempfile.gettempdir(), f"{base_hint}_{_safe_token(uuid.uuid4().hex)}")
    os.makedirs(d, exist_ok=True)
    return d


def page_contains_marker(page_id, archive_id):
    if not archive_id:
        return False, None
    m = marker_text(archive_id)
    # best-effort: markdown is simpler to search than paginated raw blocks
    try:
        data, ok = run_flowus_ok(["--json", "markdown", "get", page_id])
        if not ok:
            return False, data
        md = ((data or {}).get("data") or {}).get("markdown") or ""
        if m in md:
            return True, "markdown"
    except Exception:
        pass
    # fallback: scan block children text
    try:
        cursor = None
        while True:
            args = ["--json", "block", "children", page_id, "--page-size", "100"]
            if cursor:
                args += ["--start-cursor", cursor]
            data, ok = run_flowus_ok(args)
            if not ok:
                return False, data
            payload = (data or {}).get("data") or {}
            for b in payload.get("results") or []:
                blob = json.dumps(b, ensure_ascii=False)
                if m in blob:
                    return True, "blocks"
            if not payload.get("has_more"):
                break
            cursor = payload.get("next_cursor")
            if not cursor:
                break
    except Exception as e:
        return False, {"error": str(e)}
    return False, None


def build_beautified_blocks(meeting, transcript, summary_text, oss_name, size, mindmap_url, archive_id=None, audio_label=None):
    m = meeting["manifest"]
    mid = meeting["id"]
    created_ms = m.get("created_at_ms") or 0
    dt = datetime.fromtimestamp(created_ms / 1000, tz=timezone(timedelta(hours=8))) if created_ms else None
    date_str = dt.strftime("%Y-%m-%d %H:%M（+08:00）") if dt else ""
    dur_s = int((m.get("duration_ms") or 0) / 1000)
    mm, ss = divmod(dur_s, 60)
    dur_str = f"{mm:02d}:{ss:02d}（{dur_s}s）"
    secs = parse_summary_sections(summary_text)

    meta_items = [
        rt("会议ID：", bold=True) + rt(mid, code=True),
        rt("时间：", bold=True) + rt(date_str),
        rt("时长：", bold=True) + rt(dur_str),
        rt("转写：", bold=True) + rt("本地 ASR（Hex）"),
        rt("来源：", bold=True) + rt(f"Hex 本地会议录音 · {m.get('title') or 'Meeting'}"),
    ]
    if archive_id:
        meta_items.append(rt("归档标识：", bold=True) + rt(marker_text(archive_id), code=True))
    blocks = []
    blocks.append(callout("会议速览", "📋", bg="light_blue", title_color="blue", children=bullet(meta_items)))
    blocks.append(divider())
    blocks.append(heading2("摘要与整理", color="blue"))

    basic_items = [rt(x) for x in (secs["basic"] or [f"会议 {mid}", date_str, f"时长 {dur_str}"])]
    left_children = [callout("基本信息", "🗂️", bg="gray", children=bullet(basic_items) if basic_items else [para("（无）")])]
    left_children.append(callout("摘要", "🧾", bg="light_yellow", children=[para(secs["summary"] or "（未识别到有效摘要，请参考原始转写）")]))
    point_items = [rt(p) for p in (secs["points"] or ["（未识别到明确要点，请参考原始转写）"])]
    right_children = [callout("要点", "💡", bg="light_green", children=bullet(point_items))]
    blocks.append(column_list([(1, left_children), (1, right_children)]))

    todos = secs["todos"] or ["（未识别到明确待办，可根据内容自行补充）"]
    blocks.append(callout("可行动项 / 后续计划", "✅", bg="light_red", children=[todo(t) for t in todos]))
    blocks.append(divider())

    blocks.append(heading2("原始转写", color="blue"))
    blocks.append(callout("说明：", "ℹ️", bg="gray", title_bold=True, body_rich=rt("以下为本地 ASR 原始转写（保留口语与停顿痕迹），折叠存放以保持页面清晰。")))
    t_children = [para(chunk) for chunk in chunk_paras(transcript, 1700)] or [para("（无转写内容）")]
    blocks.append(toggle(f"展开查看完整转写（{dur_str}）", t_children))
    blocks.append(divider())

    blocks.append(heading2("原始资料", color="blue"))
    if oss_name:
        audio_children = [para(audio_label or f"microphone.wav（{round((size or 0)/1024/1024, 2)} MB）"), file_block(oss_name, size or 0)]
    else:
        audio_children = [para(audio_label or "（音频未上传）")]
    audio_col = [callout("原始录音", "🎧", bg="light_blue", children=audio_children)]
    mm_children = [para(mindmap_url or "（未生成思维导图）")]
    mm_col = [callout("思维导图", "🧠", bg="light_purple", children=mm_children)]
    blocks.append(column_list([(1, audio_col), (1, mm_col)]))
    blocks.append(divider())
    blocks.append(callout("排版说明", "🎨", bg="yellow_green", children=[para("本页由自动化工作流生成，使用 FlowUs 原生块排版（Callout/Column/Toggle/To-do/文件块/思维导图）。")]))
    return blocks


def main():
    ap = argparse.ArgumentParser(description="Sync Hex meeting artifacts to FlowUs as a beautified voice note")
    ap.add_argument("--meeting-id", help="Hex meeting id (default: latest complete)")
    ap.add_argument("--meetings-dir", help="Hex meetings directory (default: %%APPDATA%%/hex/meetings)")
    ap.add_argument("--page-id", help="Append into this existing FlowUs page")
    ap.add_argument("--parent-page-id", help="Create a new child page under this page")
    ap.add_argument("--database-id", help="Create a new record/page in this database")
    ap.add_argument("--title", help="Note title (for new pages)")
    ap.add_argument("--summary-file", help="Path to summary markdown/text file")
    ap.add_argument("--mindmap-json", help="Path to FlowUs mindmap create JSON body")
    ap.add_argument("--no-mindmap", action="store_true")
    ap.add_argument("--plain", action="store_true", help="Disable beautified layout")
    ap.add_argument("--archive-id", help="Stable archive id for idempotency marker and remote verification")
    ap.add_argument("--skip-if-present", action="store_true", help="If archive marker already exists on target page, skip writes")
    ap.add_argument("--work-dir", help="Directory for temporary request payload files (unique per run recommended)")
    ap.add_argument("--reuse-upload-oss-name", help="Reuse previously uploaded audio oss_name to avoid duplicate upload")
    ap.add_argument("--reuse-upload-size", type=int, default=0, help="Size for reused upload")
    ap.add_argument("--reuse-mindmap-id", help="Reuse previously created mindmap id")
    ap.add_argument("--reuse-mindmap-url", help="Reuse previously created mindmap url")
    args = ap.parse_args()

    work_dir = args.work_dir or make_tmp_dir("vn_sync")
    os.makedirs(work_dir, exist_ok=True)

    meeting = load_meeting(args.meeting_id, args.meetings_dir)
    m = meeting["manifest"]
    created_ms = m.get("created_at_ms") or 0
    dt = datetime.fromtimestamp(created_ms / 1000, tz=timezone(timedelta(hours=8))) if created_ms else None
    date_str = dt.strftime("%Y-%m-%d %H:%M") if dt else ""
    title = args.title or f"语音笔记 {m.get('title') or 'Hex会议'} {date_str}".strip()

    if args.summary_file and os.path.isfile(args.summary_file):
        with open(args.summary_file, "r", encoding="utf-8") as f:
            summary_text = f.read().strip()
    else:
        transcript = meeting.get("transcript") or ""
        clean = re.sub(r"^#{1,6}\s+.*$", "", transcript, flags=re.M)
        clean = re.sub(r"\*\*\d{1,2}:\d{2}(?::\d{2})?\s+[^*\n]+\*\*\s*", "", clean)
        clean = re.sub(r"\s+", " ", clean).strip()
        summary_text = "## 语音笔记整理\n### 摘要\n" + (clean[:400] if clean else "（未识别到有效转写内容）") + "\n### 要点\n- （未识别到明确要点，请参考原始转写）\n### 可行动项\n- （未识别到明确待办，可根据内容自行补充）"

    page_created = False
    if args.page_id:
        page_id = _validate_uuid(args.page_id, "page-id")
        page_url = f"https://flowus.cn/{page_id}"
        print("PAGE_TARGET", page_id, page_url)
    else:
        if not args.parent_page_id and not args.database_id:
            raise SystemExit("error: provide --page-id, or --parent-page-id, or --database-id")
        icon = {"type": "emoji", "emoji": "🎙️"}
        if args.parent_page_id:
            parent_page_id = _validate_uuid(args.parent_page_id, "parent-page-id")
            page_body = {"parent": {"type": "page_id", "page_id": parent_page_id},
                         "title": [{"type": "text", "text": {"content": title}}], "icon": icon}
        else:
            database_id = _validate_uuid(args.database_id, "database-id")
            page_body = {"parent": {"type": "database_id", "database_id": database_id},
                         "title": [{"type": "text", "text": {"content": title}}], "icon": icon}
        tmp = os.path.join(work_dir, "page.json")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(page_body, f, ensure_ascii=False)
        res, ok = run_flowus_ok(["--json", "page", "create"], tmp)
        if not ok:
            print(json.dumps(res, ensure_ascii=False, indent=2)); sys.exit(1)
        page_id = res["data"]["id"]
        page_url = res["data"].get("url") or f"https://flowus.cn/{page_id}"
        page_created = True
        print("PAGE_OK", page_id, page_url)

    # Strict-ish remote idempotency check (append target known): if marker exists, skip writes
    if args.archive_id and args.skip_if_present and args.page_id:
        found, where = page_contains_marker(page_id, args.archive_id)
        if found:
            print("SKIP_ALREADY_ARCHIVED", args.archive_id, where or "")
            print(json.dumps({"ok": True, "skipped": True, "reason": "marker_present", "archive_id": args.archive_id,
                              "page_id": page_id, "page_url": page_url, "meeting_id": meeting["id"], "marker_verified": True}, ensure_ascii=False))
            return
        # inconclusive check (network/read error): proceed cautiously (no false-skip), but caller may treat uncertainty separately if needed.

    # Upload audio (or reuse from a previous partial attempt)
    audio_label = None
    if args.reuse_upload_oss_name:
        oss_name = args.reuse_upload_oss_name
        size = int(args.reuse_upload_size or 0)
        print("UPLOAD_REUSE", oss_name, size)
    else:
        upload_path, audio_label = prepare_audio(meeting["wav"], work_dir)
        if upload_path is None:
            oss_name = None
            size = 0
            print("UPLOAD_SKIPPED", audio_label)
        else:
            up, ok = run_flowus_ok(["--json", "file", "upload", "--parent-page", page_id, upload_path])
            if not ok:
                print(json.dumps(up, ensure_ascii=False, indent=2)); sys.exit(1)
            oss_name = up["data"]["oss_name"]
            size = up["data"].get("size", 0)
            print("UPLOAD_OK", oss_name, size)

    # Mindmap (optional, or reuse)
    mindmap_url = args.reuse_mindmap_url or None
    mindmap_id = args.reuse_mindmap_id or None
    if mindmap_id or mindmap_url:
        print("MINDMAP_REUSE", mindmap_id or "", mindmap_url or "")
    elif (not args.no_mindmap) and args.mindmap_json and os.path.isfile(args.mindmap_json):
        with open(args.mindmap_json, "r", encoding="utf-8") as f:
            mm_body = json.load(f)
        mm_body["parent"] = {"page_id": page_id}
        mp = os.path.join(work_dir, "mindmap.json")
        with open(mp, "w", encoding="utf-8") as f:
            json.dump(mm_body, f, ensure_ascii=False)
        mr, ok = run_flowus_ok(["--json", "mindmap", "create"], mp)
        if ok:
            md = mr["data"]
            mindmap_id = md.get("id")
            mindmap_url = md.get("url") or (f"https://flowus.cn/{mindmap_id}" if mindmap_id else None)
            print("MINDMAP_OK", mindmap_id, mindmap_url)
        else:
            print("MINDMAP_FAIL", json.dumps(mr, ensure_ascii=False))

    # Build blocks (include archive marker for idempotency verification)
    if args.plain:
        children = []
        if args.archive_id:
            children.append(para(f"归档标识：{marker_text(args.archive_id)}"))
        children += [para(f"会议 {meeting['id']} {date_str}"), para(summary_text), para((meeting.get("transcript") or "")[:3000])]
        if oss_name:
            children.append(file_block(oss_name, size))
        else:
            children.append(para(audio_label or "（音频未上传）"))
    else:
        children = build_beautified_blocks(meeting, meeting.get("transcript") or "", summary_text, oss_name, size, mindmap_url, archive_id=args.archive_id, audio_label=audio_label)

    # Append blocks
    for i in range(0, len(children), 80):
        batch = {"children": children[i:i+80]}
        bp = os.path.join(work_dir, f"blocks_{i}.json")
        with open(bp, "w", encoding="utf-8") as f:
            json.dump(batch, f, ensure_ascii=False)
        ar, ok = run_flowus_ok(["--json", "block", "append", page_id], bp)
        if not ok:
            print("APPEND_FAIL", json.dumps(ar, ensure_ascii=False))
            # cannot be sure if prior batches landed; caller should mark needs_review
            print(json.dumps({"ok": False, "uncertain": True, "archive_id": args.archive_id, "page_id": page_id,
                              "page_url": page_url, "meeting_id": meeting["id"], "step": "append", "error": ar}, ensure_ascii=False))
            sys.exit(2)
    print("APPEND_OK", len(children))

    # Post-verify marker presence when archive_id provided (best-effort)
    verified = None
    if args.archive_id:
        found, where = page_contains_marker(page_id, args.archive_id)
        verified = bool(found)
        if not found:
            print("VERIFY_WARN marker not confirmed on target page", json.dumps(where, ensure_ascii=False))

    print(json.dumps({"ok": True, "skipped": False, "archive_id": args.archive_id, "page_id": page_id, "page_url": page_url,
                      "meeting_id": meeting["id"], "mindmap_id": mindmap_id, "mindmap_url": mindmap_url,
                      "audio": {"oss_name": oss_name, "size": size}, "page_created": page_created,
                      "marker_verified": verified}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        sys.exit(1)