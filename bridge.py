import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# local helper (scripts/archive_store.py)
BASE_DIR_IMPORT = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR_IMPORT not in sys.path:
    sys.path.insert(0, BASE_DIR_IMPORT)
try:
    from scripts.archive_store import ArchiveStore, make_archive_id, default_db_path
except Exception:
    ArchiveStore = None
    make_archive_id = None
    default_db_path = None
try:
    from scripts.summarizer import generate_summary
except Exception:
    generate_summary = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAX_BODY_SIZE = 8 * 1024
READ_TIMEOUT = 10
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def load_config():
    cfg = {}
    for name in ("config.json", "config.local.json"):
        p = os.path.join(BASE_DIR, name)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data is None:
                data = {}
            if not isinstance(data, dict):
                raise ValueError("top-level JSON value must be an object")
            cfg.update(data)
        except Exception as e:
            print(f"ERROR: failed to load config file '{p}': {e}", file=sys.stderr)
            print("Fix: correct the JSON syntax/structure in the file above, or run `python scripts/setup.py` to rebuild config.local.json.", file=sys.stderr)
            print("Tip: run `python scripts/doctor.py` for a full diagnostic.", file=sys.stderr)
            raise SystemExit(2)
    return cfg


def detect_hex_exe():
    appdata = os.environ.get("APPDATA", "")
    versions_dir = os.path.join(appdata, "hex", "versions")
    if os.path.isdir(versions_dir):
        try:
            vers = sorted([d for d in os.listdir(versions_dir) if os.path.isdir(os.path.join(versions_dir, d))], reverse=True)
            for v in vers:
                exe = os.path.join(versions_dir, v, "hex.exe")
                if os.path.isfile(exe):
                    return exe
        except Exception:
            pass
    return "hex"


CFG = load_config()
HEX_EXE = os.environ.get("HEX_EXE") or CFG.get("hex_exe") or detect_hex_exe()
HEX_MEETINGS = os.environ.get("HEX_MEETINGS_DIR") or CFG.get("hex_meetings_dir") or os.path.join(os.environ.get("APPDATA", ""), "hex", "meetings")
HEX_EXPORT_DIR = os.environ.get("HEX_EXPORT_DIR") or CFG.get("hex_export_dir") or ""
SYNC_SCRIPT = os.environ.get("SYNC_SCRIPT") or CFG.get("sync_script") or os.path.join(BASE_DIR, "scripts", "sync_to_flowus.py")
if not os.path.isabs(SYNC_SCRIPT):
    SYNC_SCRIPT = os.path.join(BASE_DIR, SYNC_SCRIPT)
HOST = "127.0.0.1"
PORT = int(os.environ.get("BRIDGE_PORT") or CFG.get("bridge_port") or 27123)
BRIDGE_TOKEN = str(os.environ.get("BRIDGE_TOKEN") or CFG.get("bridge_token") or "").strip()
ALLOWED_ORIGIN = str(os.environ.get("BRIDGE_ALLOWED_ORIGIN") or CFG.get("bridge_allowed_origin") or "").strip()
EXPECTED_HOST = f"127.0.0.1:{PORT}"
LEDGER_DB = os.environ.get("ARCHIVE_LEDGER_DB") or CFG.get("archive_ledger_db") or (default_db_path() if default_db_path else "")
LEDGER = ArchiveStore(LEDGER_DB) if (ArchiveStore and LEDGER_DB) else None
ARCHIVE_ARTIFACTS_DIR = os.environ.get("ARCHIVE_ARTIFACTS_DIR") or CFG.get("archive_artifacts_dir") or os.path.join(os.environ.get("APPDATA", "") or os.path.expanduser("~"), "hex-flowus-voice-note", "artifacts")
SUMMARY_CFG = {
    "summary_mode": CFG.get("summary_mode") or "rules",
    "ollama_url": CFG.get("ollama_url"),
    "ollama_model": CFG.get("ollama_model"),
    "ollama_timeout": CFG.get("ollama_timeout"),
}

state = {
    "status": "idle",  # idle|starting|recording|stopping|processing|done|error
    "message": "",
    "page_id": None,
    "page_url": None,
    "page_title": None,
    "meeting_id": None,
    "result": None,
    "error": None,
    "started_at": None,
}
lock = threading.Lock()


def run(cmd, timeout=120):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def hex_cmd(args, timeout=60):
    return run([HEX_EXE] + args, timeout=timeout)


def list_meetings():
    try:
        r = hex_cmd(["meetings", "list", "--json"], timeout=30)
        j = json.loads(r.stdout or r.stderr or "{}")
        return j.get("meetings", [])
    except Exception:
        return []


def latest_meeting_id():
    ms = list_meetings()
    return ms[0].get("id") if ms else None


def find_meeting_by_id(mid):
    for m in list_meetings():
        if m.get("id") == mid:
            return m
    return None


def _is_inside(path, base):
    try:
        real_path = os.path.realpath(path)
        real_base = os.path.realpath(base)
        return os.path.commonpath([real_path, real_base]) == real_base
    except Exception:
        return False


def _validate_meeting_id(mid):
    return isinstance(mid, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,128}", mid) is not None


def _resolve_transcript_path(export_path, meeting_dir):
    candidates = []
    if isinstance(export_path, str) and export_path.strip():
        ep = os.path.normpath(export_path.strip())
        allowed = [meeting_dir, HEX_MEETINGS]
        if HEX_EXPORT_DIR:
            allowed.append(HEX_EXPORT_DIR)
        for base in allowed:
            if base and _is_inside(ep, base) and os.path.isfile(ep):
                candidates.append(ep)
    fallback = os.path.join(meeting_dir, "transcript.md")
    if os.path.isfile(fallback):
        candidates.append(fallback)
    return candidates[0] if candidates else None


def load_meeting_artifacts(mid):
    if not _validate_meeting_id(mid):
        raise RuntimeError("invalid meeting id")
    d = os.path.normpath(os.path.join(HEX_MEETINGS, mid))
    if not _is_inside(d, HEX_MEETINGS):
        raise RuntimeError("invalid meeting path")
    mf = os.path.join(d, "manifest.json")
    if not os.path.isfile(mf):
        raise RuntimeError("manifest not found")
    with open(mf, "r", encoding="utf-8") as f:
        m = json.load(f)
    transcript_md = _resolve_transcript_path(m.get("export_path"), d)
    if not transcript_md:
        raise RuntimeError("transcript not found")
    with open(transcript_md, "r", encoding="utf-8") as f:
        transcript = f.read()
    wav = os.path.join(d, "microphone.wav")
    if not os.path.isfile(wav):
        wav = os.path.join(d, "system.wav")
    if not os.path.isfile(wav) or not _is_inside(wav, HEX_MEETINGS):
        raise RuntimeError("audio file not found")
    return {"id": mid, "dir": d, "manifest": m, "transcript": transcript, "wav": wav, "transcript_path": transcript_md}


def _strip_transcript_prefix(line):
    s = line.strip()
    if not s:
        return ""
    # drop markdown headings like "# Meeting – 2:23 PM" or "## Transcript"
    if re.match(r"^#{1,6}\s+", s):
        if re.match(r"^#{1,6}\s*(transcript|转写|原始转写)\b", s, re.I):
            return ""
        # meeting title heading - usually noise for summary
        if re.match(r"^#{1,6}\s*meeting\b", s, re.I):
            return ""
    # speaker/timestamp formats: "**00:00 You** text", "00:00 You  text", "**Speaker** · 00:00"
    s = re.sub(r"^\*\*\d{1,2}:\d{2}(?::\d{2})?\s+[^*\n]+\*\*\s*", "", s)
    s = re.sub(r"^\d{1,2}:\d{2}(?::\d{2})?\s+[A-Za-z0-9_\u4e00-\u9fff]+\s*[：:·\-]?\s*", "", s)
    s = re.sub(r"^\*\*[^*\n]+\*\*\s*·\s*\d{1,2}:\d{2}(?::\d{2})?\s*", "", s)
    s = re.sub(r"^\[[^\]\n]{1,20}\]\s*", "", s)
    return s.strip()


def extract_transcript_text(transcript_md):
    body = transcript_md or ""
    m = re.search(r"^##\s*Transcript\s*$", body, re.M | re.I)
    if m:
        body = body[m.end():].strip()
    cleaned, prev_key = [], ""
    for raw in body.splitlines():
        s = _strip_transcript_prefix(raw)
        if not s:
            continue
        # skip pure separators/metadata remnants
        if re.fullmatch(r"[-_=*~\s]{3,}", s):
            continue
        key = re.sub(r"\s+", "", s)
        # skip exact consecutive duplicates
        if key and key == prev_key:
            continue
        prev_key = key or prev_key
        cleaned.append(s)
    # ASR chunks are often mid-sentence fragments; merge into continuous flow for better sentence segmentation
    text = " ".join(cleaned).strip()
    text = re.sub(r"\s+", " ", text)
    return text


# Sentence-final shapes that are only grammatical when the next clause follows.
# ASR cuts at a pause and then inserts a period, so these survive as "sentences":
#   "他会进行他的一个。" / "我会发回给这个。" / "让这个智能体直接去。"
_HANGING_TAIL_RE = re.compile(
    r"(?:(?:的|地|了)(?:一|这|那|哪|几)(?:个|种|件|些|步|块|条|项)"
    r"|(?:一|这|那|哪|几)(?:个|种|件|些)"
    r"|(?:直接|就|再|马上)(?:去|来|走))$"
)


def _looks_truncated(s):
    raw = str(s or "").strip()
    t = _clean_spoken(s)
    plain = re.sub(r"[\s\W_]+", "", t, flags=re.UNICODE)
    if not t:
        return True
    # Sentence-final punctuation blinds every $-anchored rule below: ASR emits
    # "他会进行他的一个。" with a period, but the thought is still open.
    core = t.rstrip("。！？!?；;，,、 \t")
    plain_core = re.sub(r"[\s\W_]+", "", core, flags=re.UNICODE)
    if _HANGING_TAIL_RE.search(core):
        return True
    # explicit ASR-break / incomplete-thought patterns
    if re.search(r"(?:很多情况下|诸如此类的东西)$", core):
        return True
    if re.search(r"(?:整理|描|描述)$", core) and len(plain) <= 60:
        return True
    if re.search(r"(?:表现|输入|输出)$", core) and len(plain) <= 60:
        return True
    if re.search(r"(?:的|地|得|在|把|被|给|和|与|及|或|对|向|从|到|要|想|去|做|用|来)$", core) and len(plain_core) < 30:
        return True
    # very short fragment with no clear sentence completeness (but allow normal punctuated sentences)
    if len(plain) < 6 and not re.search(r"[。！？!?；;]$", raw):
        return True
    return False


def _join_fragments(buf, nxt):
    """Join an ASR-cut fragment with the clause that actually continues it."""
    prev = buf.rstrip("。！？!?；;，,、 \t")
    nxt = (nxt or "").strip()
    if not prev:
        return nxt
    # "...我会发回给这个" + "GPT，让他..." → drop the orphaned determiner
    if re.match(r"^[A-Za-z0-9]", nxt) and re.search(r"(?:这个|那个|这些|那些)$", prev):
        return re.sub(r"(?:这个|那个|这些|那些)$", "", prev) + nxt
    # "他会进行他的一个" + "提高我们的工作效率" → the measure word already binds them;
    # inserting a comma here would manufacture an unnatural pause
    if re.search(r"(?:的|地|了)?(?:一|这|那)(?:个|种|件|些)$", prev):
        return prev + nxt
    return prev + "，" + nxt


def _split_sentences(text):
    if not text:
        return []
    # split after sentence-ending punctuation; keep clauses together for analysis
    parts = re.split(r"(?<=[。！？!?；;])\s*|\n+", text)
    raw = []
    for p in parts:
        s = re.sub(r"\s+", " ", p).strip(" \t\r\n-•·")
        if not s:
            continue
        raw.append(s)
    # merge ASR-broken fragments where a segment is clearly truncated
    merged = []
    buf = ""
    for s in raw:
        cand = _join_fragments(buf, s) if buf else s
        if _looks_truncated(cand):
            buf = cand
            # avoid runaway merge
            if len(re.sub(r"\s+", "", buf)) > 120:
                merged.append(buf)
                buf = ""
            continue
        if buf:
            merged.append(cand)
            buf = ""
        else:
            merged.append(s)
    if buf:
        merged.append(buf)
    return merged if merged else ([re.sub(r"\s+", " ", text).strip()] if text.strip() else [])


_FILLER_PHRASES = [
    "我想说的是", "就是说呢", "就是说", "其实呢", "其实", "然后呢", "那么呢", "这个呢", "那个呢",
    "这样子的", "这样的一个", "这么一个", "诸如此类的东西", "之类的东西", "之类的",
]


def _clean_spoken(s):
    if not s:
        return ""
    t = re.sub(r"\s+", " ", str(s)).strip()
    # remove standalone filler tokens (keep meaning, avoid over-deleting inside words)
    t = re.sub(r"\b(?:OK|ok)\b[，,\s]*", "", t)
    t = re.sub(r"(?:^|[，,。\s])(?:嗯+|啊+|呃+|哦+|唉+|哈+|呀+|吧|嘛)(?=[，,。\s]|$)", " ", t)
    # inline fillers in Chinese speech
    t = re.sub(r"(?:\s+|[，,])(?:嗯+|啊+|呃+|哦+|唉+|哈+|呀)(?:\s+|[，,])", "，", t)
    t = re.sub(r"(?:嗯+|呃+|哦+|唉+)\s*", "", t)
    # hesitation "，就是，" survives mid-sentence and reads as a broken cut
    t = re.sub(r"[，,]\s*(?:就是|然后|那么)[，,]\s*", "，", t)
    t = re.sub(r"(?<=[\u4e00-\u9fff，,\s])(哈|呢)(?=[，,\s。！？!?])", "", t)
    t = re.sub(r"([，,])\s*([，,])+", "，", t)
    for p in _FILLER_PHRASES:
        t = t.replace(p, "")
    # fix common ASR stutters/duplications
    t = re.sub(r"(不出)说不出", r"\1", t)
    t = re.sub(r"(说不)说不", r"\1", t)
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"([，,])\s*([，,])+", "，", t)
    t = re.sub(r"[，,]\s*([。！？!?])", r"\1", t)
    t = t.strip(" \t\r\n-•·，,；;：:")
    # drop leading dangling conjunctions/punctuation fragments (iterate for chains)
    for _ in range(4):
        nt = re.sub(r"^(?:所以呢|然后呢|那么呢|所以|然后|那么|但是|不过|而且|并且|或者|因为|如果|就是|再一个就是|再一个|还有就是|还有|以及|或者是说|对|好|行|来|去|的实验)[，,、\s]*", "", t)
        nt = re.sub(r"^(?:的|了|呢|啊|吧|呀|吗|哈)(?=[，,、\s]|$)", "", nt)
        nt = nt.strip(" \t\r\n-•·，,；;：:")
        if nt == t:
            break
        t = nt
    # remove trailing dangling particles that often mark broken ASR cuts
    t = re.sub(r"(?:[，,\s]*(?:啊|呢|嘛|吧|呀|哈))+(?=[。！？!?]|$)", "", t)
    t = re.sub(r"(?:[，,\s]+(?:啊|呢|嘛|吧|呀|哈))+$", "", t).strip(" \t\r\n-•·，,；;：:")
    t = re.sub(r"\s+", " ", t).strip(" \t\r\n-•·，,；;：:")
    return t


def _sentence_key(s):
    t = _clean_spoken(s)
    t = re.sub(r"[\s`*_~#>\[\]\(\)（）【】\"'“”‘’、，,。.!！？?；;：:·\-—]", "", t)
    return t[:40]


def _is_complete_enough(s):
    t = _clean_spoken(s).strip()
    plain = re.sub(r"[\s\W_]+", "", t, flags=re.UNICODE)
    if len(plain) < 8:
        return False
    # A sentence still ending in a hanging structure is a fragment, never a standalone point,
    # however long it is ("完了之后记录之后呢，他会进行他的一个。").
    if _looks_truncated(s):
        return False
    # reject common broken starts from ASR chunking / clause fragments
    if re.match(r"^(?:的|了|着|过|得|把|被|是说明|是因为|学\s*school|school\b|等他|再一个|说孩子|咱们现在这个页面)", t):
        return False
    # likely fragment if it starts lowercase-mid idea without subject context in Chinese speech patterns
    if len(plain) < 22 and re.match(r"^(?:咱们|这个页面|正在进行)", t):
        return False
    # trailing incompleteness (ends mid-clause or with dangling particles in context)
    if re.search(r"(?:很多情况下|诸如此类的东西|说的顺畅)$", t):
        return False
    if re.search(r"(?:整理|描|表现|输入|输出|转写|总结|描述)$", t) and len(plain) < 60:
        # often truncated at chunk boundary in ASR
        return False
    if re.search(r"(?:的|地|得|了|着|在|把|被|给|和|与|及|或|对|向|从|到|是|有|会|能|要|想|去|做|让|用|跟|比|为|啊|呢|嘛|吧|呀)$", t):
        return False
    # reject clearly malformed / duplicated fragments
    if re.search(r"(不出说不出|说不说不)", t):
        return False
    # very short after cleaning is usually fragmentary (but allow clean punctuated statements)
    if len(plain) < 12 and not re.search(r"[。！？!?；;]$", str(s).strip()):
        return False
    # must contain some substantive content cue
    if not re.search(r"(是|有|能|会|要|想|可以|需要|希望|记录|整理|使用|帮助|提高|做|说|提供|上传|遇到|导致|意识到|觉得|认为|解决|支持|存|传|面对|具备|困难|问题|目的|因为|为了|工具|插件|语音|学习|孩子|模型|讨论|要点|内容|确认|测试|方案|流程|显示)", t):
        return False
    return True


def _format_point(s, limit=88):
    t = _clean_spoken(s)
    if not t:
        t = re.sub(r"\s+", " ", str(s or "")).strip()
    t = t.strip(" \t\r\n-•·，,；;：:")
    if not t:
        return ""
    # try to keep a complete sentence/clause: cut at nearest boundary before limit
    if len(t) > limit:
        window = t[:limit]
        cut = -1
        cut_sep = ""
        for sep in ["。", "！", "？", "；"]:
            idx = window.rfind(sep)
            if idx >= max(18, limit - 30):
                cut = idx
                cut_sep = sep
                break
        if cut <= 0:
            for sep in ["，", ",", " "]:
                idx = window.rfind(sep)
                if idx >= max(24, limit - 24):
                    cut = idx
                    cut_sep = sep
                    break
        if cut > 0:
            t = window[:cut].rstrip(" \t\r\n-•·，,；;：:")
        else:
            t = window.rstrip(" \t\r\n-•·，,；;：:")
        if t and not t.endswith(("。", "！", "？", "…")):
            # only claim a full stop when we actually cut on sentence punctuation;
            # a clause-level cut must stay visibly open instead of faking completeness
            t += "。" if cut_sep in ("。", "！", "？", "；") else "…"
    else:
        # ensure it reads like a complete point
        if not t.endswith(("。", "！", "？")):
            t += "。"
    t = re.sub(r"[。！？]{2,}。?$", "。", t)
    return t.strip()


def _select_representative_sentences(text, limit=6):
    # pick diverse, information-bearing sentences from across the transcript (not just the first few)
    sents = _split_sentences(text)
    scored = []
    for idx, s in enumerate(sents):
        cleaned = _clean_spoken(s)
        plain = re.sub(r"[\s\W_]+", "", cleaned, flags=re.UNICODE)
        if len(plain) < 10:
            continue
        score = min(len(plain), 80)
        if re.search(r"(因为|所以|为了|希望|想要|可以|能够|需要|问题|困难|方法|工具|目的|记录|整理|总结|提高|帮助|使用|插件|语音|转写|归档|学习|孩子|模型|家长)", cleaned):
            score += 12
        if _is_complete_enough(cleaned):
            score += 8
        else:
            score -= 10
        # penalize obvious fragments
        if re.search(r"(的|啊|呢|吧)$", cleaned) or re.match(r"^(是说明|是因为|学\b)", cleaned):
            score -= 12
        scored.append((idx, s, cleaned, score))
    if not scored:
        return []
    # keep only reasonably good candidates
    scored = [x for x in scored if x[3] > 0] or sorted(scored, key=lambda x: -x[3])[:limit]
    n = len(scored)
    picks = []
    if n <= limit:
        candidates = sorted(scored, key=lambda x: x[0])
    else:
        third = max(1, n // 3)
        buckets = [scored[:third], scored[third:2*third], scored[2*third:]]
        candidates = []
        for b in buckets:
            if b:
                candidates.extend(sorted(b, key=lambda x: -x[3])[:2])
        for item in sorted(scored, key=lambda x: (-x[3], x[0])):
            if item not in candidates:
                candidates.append(item)
        candidates = sorted(candidates[: limit * 4], key=lambda x: x[0])
    used = set()
    for idx, s, cleaned, _ in candidates:
        k = _sentence_key(s)
        if not k or k in used:
            continue
        if any(k[:18] and k[:18] == u[:18] for u in used):
            continue
        used.add(k)
        picks.append((idx, s, cleaned))
        if len(picks) >= limit:
            break
    picks.sort(key=lambda x: x[0])
    return [p[1] for p in picks]


def _pick_points(text, limit=5):
    # heuristic "analysis" baseline: select representative points across the talk and rewrite as readable points
    reps = _select_representative_sentences(text, limit=max(limit + 3, 8))
    points, seen = [], set()
    source = reps or _split_sentences(text)
    for s in source:
        cleaned = _clean_spoken(s)
        if not _is_complete_enough(cleaned):
            continue
        # avoid clause fragments that read broken as standalone points
        if re.match(r"^(?:是说明|是因为|为了说明)", cleaned):
            continue
        plain_c = re.sub(r"[\s\W_]+", "", cleaned, flags=re.UNICODE)
        if len(plain_c) < 10:
            continue
        key = _sentence_key(s)
        if not key or key in seen:
            continue
        # skip near-duplicates
        if any(key[:18] and key[:18] == u[:18] for u in seen):
            continue
        seen.add(key)
        p = _format_point(s, limit=88)
        if p:
            points.append(p)
        if len(points) >= limit:
            break
    # fallback: if too few, relax completeness constraint but keep basic readability
    if len(points) < min(3, limit):
        for s in _split_sentences(text):
            cleaned = _clean_spoken(s)
            if re.match(r"^(?:是说明|是因为|学\s*school|咱们现在这个页面)", cleaned):
                continue
            key = _sentence_key(s)
            if not key or key in seen:
                continue
            plain = re.sub(r"[\s\W_]+", "", cleaned, flags=re.UNICODE)
            if len(plain) < 14:
                continue
            if re.search(r"(?:很多情况下|诸如此类的东西)$", cleaned):
                continue
            if re.search(r"(?:整理|描|表现|输入|输出|记录|转写|总结|描述)$", cleaned) and len(plain) < 60:
                continue
            if re.search(r"(?:的|地|得|了|着|在|把|被|给|和|与|及|或|对|向|从|到|是|有|会|能|要|想|去|做|说|让|用|跟|比|为|啊|呢|嘛|吧|呀)$", cleaned):
                continue
            seen.add(key)
            p = _format_point(s, limit=88)
            if p:
                points.append(p)
            if len(points) >= limit:
                break
    return points


# Explicit next-step / hand-off intent. Spoken Chinese states todos without keywords:
#   "等一下…我会发回给 GPT，让他跟我继续进行这个项目的审计"
_NEXT_STEP_RE = re.compile(
    r"(?:等一下|接下来|下一步|回头|稍后|后面|结束后)[^。]{0,12}?(?:我会|我们要|我需要|就|再|要|需要|把)"
    r"|(?:让|请|叫)(?:他|她|它|你|我|大家)[^。]{0,10}?(?:去|来|继续|帮|做|处理|跟进|看)"
    r"|(?:继续|接着)(?:进行|做|跟进|观察|审计)"
)


def _extract_action_items(text):
    if not text:
        return []
    # conservative cues: explicit assignment/reminder/next-step intent; prefer under-extraction over false positives
    strong_cues = (
        "待办", "TODO", "todo", "下一步要", "下一步需要", "需要你", "需要我", "需要我们", "请你", "请大家",
        "记得", "务必", "尽快", "别忘", "麻烦", "负责", "跟进一下", "安排一下", "去确认", "去修复", "去处理",
    )
    weak_cues = ("需要", "确认", "修复", "处理", "提交", "发送", "补充", "联系", "安排", "跟进")
    todos, seen = [], set()
    for s in _split_sentences(text):
        has_strong = any(k in s for k in strong_cues)
        has_weak = any(k in s for k in weak_cues)
        # Spoken next-step intent often carries no keyword at all:
        # "等一下…我会发回给这个 GPT，让他跟我继续进行这个项目的审计"
        m_next = _NEXT_STEP_RE.search(s)
        if not (has_strong or has_weak or m_next):
            continue
        # weak cues are easy to false-positive in discussion; require action-oriented phrasing
        if not has_strong:
            if not (m_next or re.search(r"(需要|请|记得|务必|尽快|负责).{0,8}(做|改|修|补|发|提|联|确|处|查|写|测|安|跟)", s)):
                continue
        # avoid meta/system phrases and pure narration
        if "幂等" in s and "meeting id" in s.lower():
            continue
        if re.search(r"(已经|已|刚刚|之前).{0,6}(完成|处理|修复|确认)", s):
            continue
        body = s.strip()
        if m_next and m_next.start() > 0:
            # drop the narration lead-in so the item starts at the actionable part
            body = body[m_next.start():]
        item = body.strip(" 。！？!?；;")
        if len(item) < 6:
            continue
        if len(item) > 60:
            # cut at a clause boundary, never mid-clause: a clipped half-sentence is unreadable
            cuts = [item.rfind(c, 0, 60) for c in "，,；;。"]
            cut = max(cuts)
            item = (item[:cut] if cut >= 24 else item[:58]).rstrip("，,；;。 ") + "…"
        key = re.sub(r"\s+", "", item)
        if not key or key in seen:
            continue
        seen.add(key)
        todos.append(item)
        if len(todos) >= 5:
            break
    return todos


def make_auto_summary(meeting):
    transcript = meeting.get("transcript") or ""
    text = extract_transcript_text(transcript)
    m = meeting.get("manifest") or {}
    dur_s = int((m.get("duration_ms") or 0) / 1000)
    mm, ss = divmod(dur_s, 60)
    created_ms = m.get("created_at_ms") or 0
    dt = datetime.fromtimestamp(created_ms / 1000, tz=timezone(timedelta(hours=8))) if created_ms else None
    date_str = dt.strftime("%Y-%m-%d %H:%M（+08:00）") if dt else ""
    points = _pick_points(text, limit=5)
    if text:
        # heuristic baseline (not LLM understanding): synthesize a compact overview from selected points
        parts = []
        for p in points[:4]:
            c = re.sub(r"[。！？!?…]+$", "", p).strip()
            c = _clean_spoken(c)
            if c and len(re.sub(r"[\s\W_]+", "", c, flags=re.UNICODE)) >= 10:
                parts.append(c)
            if len(parts) >= 3:
                break
        if len(parts) < 2:
            reps = _select_representative_sentences(text, limit=5)
            for s in (reps or _split_sentences(text)):
                c = re.sub(r"[。！？!?…]+$", "", _format_point(s, limit=56)).strip()
                c = _clean_spoken(c)
                if not c:
                    continue
                if re.search(r"(?:很多情况下)$", c) or len(re.sub(r"[\s\W_]+", "", c, flags=re.UNICODE)) < 10:
                    continue
                parts.append(c)
                if len(parts) >= 3:
                    break
        deduped, seen_k = [], set()
        for p in parts:
            k = re.sub(r"[\s\W_]+", "", p, flags=re.UNICODE)[:24]
            if not k or k in seen_k:
                continue
            seen_k.add(k)
            deduped.append(p)
        core = "；".join(deduped[:3]).strip("； ")
        if core:
            summary_para = f"本次语音主要围绕：{core}。"
            if len(text) > 180:
                summary_para += "（启发式整理，详见要点与原始转写）"
        else:
            norm = _clean_spoken(text)
            summary_para = (norm[:200] + "…") if len(norm) > 200 else (norm or "（未识别到有效转写内容，请查看原始转写/录音）")
        if len(summary_para) > 320:
            summary_para = summary_para[:318].rstrip() + "…"
    else:
        summary_para = "（未识别到有效转写内容，请查看原始转写/录音）"
    todos = _extract_action_items(text)
    md = [
        "## 语音笔记整理（自动生成）",
        "### 基本信息",
        f"- 会议ID：{meeting.get('id', '')}",
        f"- 时间：{date_str}",
        f"- 时长：{mm:02d}:{ss:02d}",
        "- 转写：Hex 本地 ASR",
        "### 摘要",
        summary_para,
        "### 要点",
    ]
    for p in (points or ["（未识别到明确要点，请参考原始转写）"]):
        md.append(f"- {p}")
    md.append("### 可行动项")
    if todos:
        for t in todos:
            md.append(f"- {t}")
    else:
        md.append("- （未从转写中识别到明确待办，可根据内容自行补充）")
    return "\n".join(md), points


def rt_node(text):
    return [{"type": "text", "text": {"content": text, "link": None},
             "annotations": {"bold": False, "italic": False, "strikethrough": False, "underline": False,
                             "code": False, "color": "default", "background_color": "default", "text_color": "default"},
             "plain_text": text, "href": None}]


def make_auto_mindmap(meeting, points):
    m = meeting.get("manifest") or {}
    dur_s = int((m.get("duration_ms") or 0) / 1000)
    mm, ss = divmod(dur_s, 60)
    title_text = f"语音笔记导图 {meeting.get('id', '')}（{mm:02d}:{ss:02d}）"
    point_children = [{"rich_text": rt_node(p[:40])} for p in (points[:6] or ["（未识别到明确要点）"])]
    return {
        "title": rt_node(title_text),
        "settings": {"layout": "both", "line_style": "curved", "branch_color": "gray", "sibling_node_alignment": True},
        "nodes": [
            {"rich_text": rt_node("基本信息"), "children": [
                {"rich_text": rt_node(f"会议ID: {meeting.get('id', '')}")},
                {"rich_text": rt_node(f"时长: {mm:02d}:{ss:02d}")},
                {"rich_text": rt_node("来源: Hex 本地录音+ASR")},
            ]},
            {"rich_text": rt_node("核心要点"), "children": point_children},
            {"rich_text": rt_node("后续"), "children": [
                {"rich_text": rt_node("复核原始转写与录音")},
                {"rich_text": rt_node("按需补充待办与结论")},
            ]},
        ]
    }


def wait_for_meeting_complete(mid, timeout_s=360):
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        info = find_meeting_by_id(mid)
        if info and info.get("status") == "complete" and not info.get("in_progress"):
            mf = os.path.join(HEX_MEETINGS, mid, "manifest.json")
            if os.path.isfile(mf):
                try:
                    with open(mf, "r", encoding="utf-8") as f:
                        m = json.load(f)
                    if m.get("status") == "complete":
                        return m
                except Exception:
                    pass
        time.sleep(2)
    raise RuntimeError("timeout waiting for meeting completion/transcript")


def detect_new_meeting(before_ids, timeout_s=20):
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        for m in list_meetings():
            if m.get("id") not in before_ids:
                return m.get("id")
        time.sleep(1)
    return None


def _job_dir(archive_id):
    # Windows 禁止在目录名中使用 ':'（archive_id 形如 vnarc::append::<mid>::<page>），
    # 旧白名单 [A-Za-z0-9._:-] 放行了 ':'，导致 os.makedirs 抛
    # [WinError 123] 且每次归档立即失败。这里只保留跨平台安全字符。
    raw = str(archive_id or "job")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", raw)
    safe = re.sub(r"\.{2,}", ".", safe).strip("._-")[:120] or "job"
    # 清洗可能让不同 archive_id 落到同名目录；尾缀短哈希保证一一对应
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    d = os.path.join(ARCHIVE_ARTIFACTS_DIR, safe + "-" + digest)
    os.makedirs(d, exist_ok=True)
    return d


def _write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def _read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _prepare_job_artifacts(meeting_id, archive_id, page_id, page_url=None, page_title=None, force_regen=False):
    meeting = load_meeting_artifacts(meeting_id)
    job_dir = _job_dir(archive_id)
    meta_path = os.path.join(job_dir, "job.json")
    sum_path = os.path.join(job_dir, "summary.md")
    mm_path = os.path.join(job_dir, "mindmap.json")
    summary_meta_path = os.path.join(job_dir, "summary_meta.json")

    if (not force_regen) and os.path.isfile(sum_path) and os.path.isfile(mm_path) and os.path.isfile(meta_path):
        try:
            meta = _read_json(meta_path)
            # reuse if bound to same meeting/archive/page
            if str(meta.get("meeting_id")) == str(meeting_id) and str(meta.get("archive_id")) == str(archive_id):
                if os.path.isfile(summary_meta_path):
                    meta["summary_meta"] = _read_json(summary_meta_path)
                return meeting, job_dir, sum_path, mm_path, meta
        except Exception:
            pass

    text = extract_transcript_text(meeting.get("transcript") or "")
    if generate_summary is not None:
        sres = generate_summary(meeting, text, cfg=SUMMARY_CFG, make_auto_summary_fn=make_auto_summary)
        if sres.get("fallback"):
            sys.stderr.write("[bridge] summary fallback to rules: %s\n" % (sres.get("fallback_reason") or "unknown"))
    else:
        summary_md, pts = make_auto_summary(meeting)
        sres = {"ok": True, "mode": "rules", "summary_md": summary_md, "points": pts or [], "fallback": False}

    summary_md = sres.get("summary_md") or ""
    points = sres.get("points") or []
    # if model result lacked valid structure, fall back
    if not summary_md.strip():
        summary_md, points = make_auto_summary(meeting)
        sres = {"ok": True, "mode": "rules", "summary_md": summary_md, "points": points or [], "fallback": True, "fallback_reason": "empty_summary"}

    mm_body = make_auto_mindmap(meeting, points)
    if sres.get("fallback"):
        # 用户只看 FlowUs 页面，看不到 stderr。降级必须在页面上可见，
        # 否则"要点又是残句"会被当成 bug 而不是"模型没启动"。
        summary_md = (
            "> ⚠️ 本次摘要由**规则模式**生成（模型不可用：%s）\n"
            "> 要点为转写原句的挑选与清洗，可能仍夹带口语碎片。"
            "模型恢复后可用 `POST /retry` 带 `\"regenerate\": true` 重新生成。\n\n%s"
        ) % (str(sres.get("fallback_reason") or "unknown")[:200], summary_md)
    with open(sum_path, "w", encoding="utf-8") as f:
        f.write(summary_md)
    with open(mm_path, "w", encoding="utf-8") as f:
        json.dump(mm_body, f, ensure_ascii=False)

    meta = {
        "archive_id": archive_id,
        "meeting_id": meeting_id,
        "page_id": page_id,
        "page_url": page_url,
        "page_title": page_title,
        "created_at": time.time(),
        "summary_mode": sres.get("mode"),
        "summary_fallback": bool(sres.get("fallback")),
        "summary_fallback_reason": sres.get("fallback_reason"),
        "summary_model": sres.get("model"),
    }
    _write_json(meta_path, meta)
    _write_json(summary_meta_path, {k: sres.get(k) for k in ("mode", "fallback", "fallback_reason", "requested_mode", "model") if k in sres})
    meta["summary_meta"] = _read_json(summary_meta_path)
    return meeting, job_dir, sum_path, mm_path, meta


def _ledger_retry_context(archive_id):
    if not LEDGER or not archive_id:
        return {}
    try:
        rec = LEDGER.get(archive_id) or {}
        steps = LEDGER.get_steps(archive_id) or {}
        result = {}
        try:
            result = json.loads(rec.get("result_json") or "{}") or {}
        except Exception:
            result = {}
        ctx = {}
        audio = (steps.get("upload") or {}) if isinstance(steps.get("upload"), dict) else {}
        if not audio and isinstance(result.get("audio"), dict):
            audio = result.get("audio")
        if audio.get("oss_name"):
            ctx["reuse_upload_oss_name"] = audio.get("oss_name")
            ctx["reuse_upload_size"] = audio.get("size") or 0
        mm = (steps.get("mindmap") or {}) if isinstance(steps.get("mindmap"), dict) else {}
        if not mm.get("id") and result.get("mindmap_id"):
            mm = {"id": result.get("mindmap_id"), "url": result.get("mindmap_url")}
        if mm.get("id") or mm.get("url"):
            ctx["reuse_mindmap_id"] = mm.get("id")
            ctx["reuse_mindmap_url"] = mm.get("url")
        ctx["steps"] = steps
        ctx["record"] = rec
        return ctx
    except Exception:
        return {}


def process_and_upload(meeting_id, page_id, archive_id=None, page_url=None, page_title=None, retry=False, regenerate=False):
    if archive_id:
        meeting, job_dir, sum_path, mm_path, job_meta = _prepare_job_artifacts(
            meeting_id, archive_id, page_id, page_url=page_url, page_title=page_title, force_regen=bool(regenerate)
        )
    else:
        meeting = load_meeting_artifacts(meeting_id)
        job_dir = os.path.join(tempfile.gettempdir(), "hex-flowus-voice-note", (meeting_id or "run") + "_" + uuid.uuid4().hex[:8])
        os.makedirs(job_dir, exist_ok=True)
        sum_path = os.path.join(job_dir, f"auto_summary_{meeting_id}.md")
        mm_path = os.path.join(job_dir, f"auto_mindmap_{meeting_id}.json")
        summary_md, points = make_auto_summary(meeting)
        with open(sum_path, "w", encoding="utf-8") as f:
            f.write(summary_md)
        with open(mm_path, "w", encoding="utf-8") as f:
            json.dump(make_auto_mindmap(meeting, points), f, ensure_ascii=False)
        job_meta = {}

    # per-run isolated temp workspace for sync payloads
    run_tag = (archive_id or meeting_id or "run").replace(":", "_").replace("/", "_")
    tmp = os.path.join(job_dir, "runs", time.strftime("%Y%m%d-%H%M%S") + "_" + uuid.uuid4().hex[:6])
    os.makedirs(tmp, exist_ok=True)

    if not os.path.isfile(SYNC_SCRIPT):
        raise RuntimeError(f"sync script not found: {SYNC_SCRIPT}")

    cmd = [sys.executable, SYNC_SCRIPT, "--meeting-id", meeting_id, "--page-id", page_id,
           "--summary-file", sum_path, "--mindmap-json", mm_path, "--meetings-dir", HEX_MEETINGS,
           "--work-dir", tmp]
    if archive_id:
        cmd += ["--archive-id", archive_id, "--skip-if-present"]

    # On retry, reuse known successful side-effect artifacts when available to reduce duplicates
    if retry and archive_id:
        rctx = _ledger_retry_context(archive_id)
        if rctx.get("reuse_upload_oss_name"):
            cmd += ["--reuse-upload-oss-name", str(rctx["reuse_upload_oss_name"]), "--reuse-upload-size", str(int(rctx.get("reuse_upload_size") or 0))]
        if (not regenerate) and (rctx.get("reuse_mindmap_id") or rctx.get("reuse_mindmap_url")):
            if rctx.get("reuse_mindmap_id"):
                cmd += ["--reuse-mindmap-id", str(rctx["reuse_mindmap_id"])]
            if rctx.get("reuse_mindmap_url"):
                cmd += ["--reuse-mindmap-url", str(rctx["reuse_mindmap_url"])]

    if LEDGER and archive_id:
        LEDGER.update_steps(archive_id, {
            "prepare": {"ok": True, "job_dir": job_dir, "work_dir": tmp, "summary_mode": job_meta.get("summary_mode")},
            "context": {"page_id": page_id, "page_url": page_url, "page_title": page_title, "meeting_id": meeting_id},
            "sync_started": {"ts": time.time(), "retry": bool(retry)},
        })

    r = run(cmd, timeout=600)
    out = (r.stdout or "") + "\n" + (r.stderr or "")
    result = None
    for line in reversed(out.strip().splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                result = json.loads(line)
                break
            except Exception:
                pass

    # classify result
    if result and result.get("skipped") and result.get("reason") == "marker_present":
        if LEDGER and archive_id:
            LEDGER.update_steps(archive_id, {"remote_marker": {"present": True, "where": result.get("marker_verified") or True}, "sync": {"skipped": True}})
        return {"ok": True, "skipped": True, "archive_id": archive_id, "result": result}

    if r.returncode == 0 and result and result.get("ok"):
        if LEDGER and archive_id:
            LEDGER.update_steps(archive_id, {
                "upload": result.get("audio"),
                "mindmap": {"id": result.get("mindmap_id"), "url": result.get("mindmap_url")},
                "append": {"ok": True},
                "verify": {"marker_verified": result.get("marker_verified")},
                "sync": {"ok": True, "retry": bool(retry)},
            })
        return result

    # uncertain cases: append failed mid-way or process interrupted after partial writes
    uncertain = bool(result and (result.get("uncertain") or result.get("step") == "append"))
    if LEDGER and archive_id:
        step_patch = {"sync": {"ok": False, "uncertain": uncertain, "returncode": r.returncode, "tail": out[-1500:], "retry": bool(retry)}}
        # capture partial outputs if present for future retry reuse
        if result and isinstance(result.get("audio"), dict):
            step_patch["upload"] = result.get("audio")
        if result and (result.get("mindmap_id") or result.get("mindmap_url")):
            step_patch["mindmap"] = {"id": result.get("mindmap_id"), "url": result.get("mindmap_url")}
        LEDGER.update_steps(archive_id, step_patch)
    err = RuntimeError(out[-2000:] or "sync failed")
    setattr(err, "uncertain", uncertain)
    setattr(err, "result", result)
    raise err


def worker_start(page_id, page_title, page_url):
    try:
        with lock:
            state.update({"status": "recording", "message": "recording started", "page_id": page_id,
                          "page_title": page_title, "page_url": page_url, "error": None, "result": None})
        before = {m.get("id") for m in list_meetings()}
        r = hex_cmd(["meetings", "record", "--json"], timeout=30)
        mid = None
        try:
            j = json.loads(r.stdout or "{}")
            mid = (j.get("meeting") or {}).get("id") or j.get("id") or j.get("meeting_id")
        except Exception:
            pass
        if not mid:
            mid = detect_new_meeting(before, timeout_s=20)
        if not mid:
            for m in list_meetings():
                if m.get("in_progress"):
                    mid = m.get("id"); break
        if not mid or not _validate_meeting_id(mid):
            raise RuntimeError("cannot determine meeting id")
        with lock:
            state["meeting_id"] = mid
            state["message"] = "recording in progress"
    except Exception as e:
        with lock:
            state.update({"status": "error", "error": str(e), "message": "start failed"})


def worker_stop_and_process():
    archive_id = None
    claim_worker = None
    try:
        with lock:
            mid = state.get("meeting_id")
            page_id = state.get("page_id")
            page_url = state.get("page_url")
            page_title = state.get("page_title")
            state.update({"status": "stopping", "message": "stopping recording"})
        hex_cmd(["meetings", "stop", "--json"], timeout=60)
        if not mid:
            for m in list_meetings():
                if m.get("in_progress"):
                    mid = m.get("id"); break
            if not mid:
                mid = latest_meeting_id()
        if not _validate_meeting_id(mid or ""):
            raise RuntimeError("no meeting to stop/process")
        with lock:
            state.update({"meeting_id": mid, "status": "processing", "message": "waiting for transcript"})
        wait_for_meeting_complete(mid, timeout_s=360)

        if not page_id:
            raise RuntimeError("missing target page_id")
        if not _valid_page_id(str(page_id)):
            raise RuntimeError("invalid target page_id")

        # idempotency: archive identity per meeting+target page (append mode)
        if make_archive_id:
            archive_id = make_archive_id(mid, page_id, mode="append")

        # strict local dedupe by meeting+page: if any done record exists for same pair, skip
        if LEDGER and archive_id:
            existing = LEDGER.find_by_meeting_page(mid, page_id)
            if existing and existing.get("status") == "done" and existing.get("archive_id") != archive_id:
                # different archive_id should not happen for same pair, but be safe
                with lock:
                    state.update({"status": "done", "message": "already archived (skipped)", "archive_id": existing.get("archive_id"),
                                  "result": {"ok": True, "skipped": True, "reason": "local_done", "archive_id": existing.get("archive_id")}})
                return

            claim = LEDGER.claim(archive_id, mid, target_page_id=page_id)
            if not claim.get("ok"):
                reason = claim.get("reason")
                rec = claim.get("record") or {}
                if reason == "already_done":
                    with lock:
                        state.update({"status": "done", "message": "already archived (skipped)", "archive_id": archive_id,
                                      "result": {"ok": True, "skipped": True, "reason": reason, "archive_id": archive_id}})
                    return
                if reason == "needs_review":
                    with lock:
                        state.update({"status": "error", "message": "needs review", "archive_id": archive_id,
                                      "error": "archive needs review before retry (possible prior partial write)"})
                    return
                if reason == "already_running":
                    with lock:
                        state.update({"status": "error", "error": "same archive is already running", "message": "busy", "archive_id": archive_id})
                    return
                with lock:
                    state.update({"status": "error", "error": f"archive claim rejected: {reason}", "message": "busy", "archive_id": archive_id})
                return
            claim_worker = claim.get("worker_id")

        with lock:
            state["message"] = "uploading to FlowUs"
            if archive_id:
                state["archive_id"] = archive_id

        result = process_and_upload(mid, page_id, archive_id=archive_id, page_url=page_url, page_title=page_title, retry=False)
        with lock:
            state.update({"status": "done", "message": "uploaded", "result": result})
        if LEDGER and archive_id:
            LEDGER.mark_done(archive_id, result=result)
    except Exception as e:
        uncertain = bool(getattr(e, "uncertain", False))
        if LEDGER and archive_id:
            if uncertain:
                LEDGER.mark_needs_review(archive_id, str(e), result=getattr(e, "result", None))
            else:
                LEDGER.mark_error(archive_id, str(e))
        with lock:
            if uncertain:
                state.update({"status": "error", "error": "write result uncertain; manual review required before retry: " + str(e),
                              "message": "needs review", "archive_id": archive_id})
            else:
                state.update({"status": "error", "error": str(e), "message": "processing failed", "archive_id": archive_id})


def worker_retry_archive(archive_id=None, meeting_id=None, page_id=None, regenerate=False):
    try:
        with lock:
            state.update({"status": "processing", "message": "retrying archive", "error": None, "result": None})

        if not LEDGER:
            raise RuntimeError("archive ledger unavailable")

        rec = None
        if archive_id:
            rec = LEDGER.get(archive_id)
        elif meeting_id and page_id:
            rec = LEDGER.find_by_meeting_page(meeting_id, page_id)
            if rec:
                archive_id = rec.get("archive_id")
        else:
            raise RuntimeError("archive_id or (meeting_id,page_id) required")

        if not rec:
            raise RuntimeError("archive record not found")
        archive_id = rec.get("archive_id") or archive_id
        meeting_id = rec.get("meeting_id") or meeting_id
        page_id = rec.get("target_page_id") or page_id
        if not meeting_id or not page_id or not archive_id:
            raise RuntimeError("archive record missing meeting/page id")

        steps = LEDGER.get_steps(archive_id) or {}
        ctx = steps.get("context") if isinstance(steps.get("context"), dict) else {}
        page_url = ctx.get("page_url") or state.get("page_url")
        page_title = ctx.get("page_title") or state.get("page_title")

        # for explicit retry, allow transition from needs_review/error
        if rec.get("status") == "needs_review":
            LEDGER.mark_retry_pending(archive_id)

        claim = LEDGER.claim(archive_id, meeting_id, target_page_id=page_id, allow_review_retry=True)
        if not claim.get("ok"):
            reason = claim.get("reason")
            if reason == "already_done":
                with lock:
                    state.update({"status": "done", "message": "already archived (skipped)", "archive_id": archive_id,
                                  "result": {"ok": True, "skipped": True, "reason": "already_done", "archive_id": archive_id}})
                return
            raise RuntimeError(f"cannot retry archive: {reason}")

        with lock:
            state.update({"meeting_id": meeting_id, "page_id": page_id, "archive_id": archive_id, "message": "uploading to FlowUs (retry)"})

        result = process_and_upload(meeting_id, page_id, archive_id=archive_id, page_url=page_url, page_title=page_title, retry=True, regenerate=bool(regenerate))
        with lock:
            state.update({"status": "done", "message": "uploaded", "result": result})
        LEDGER.mark_done(archive_id, result=result)
    except Exception as e:
        uncertain = bool(getattr(e, "uncertain", False))
        try:
            if LEDGER and (archive_id or (meeting_id and page_id)):
                aid = archive_id or (make_archive_id(meeting_id, page_id, mode="append") if make_archive_id and meeting_id and page_id else None)
                if aid:
                    if uncertain:
                        LEDGER.mark_needs_review(aid, str(e), result=getattr(e, "result", None))
                    else:
                        LEDGER.mark_error(aid, str(e))
        except Exception:
            pass
        with lock:
            if uncertain:
                state.update({"status": "error", "error": "write result uncertain; manual review required before retry: " + str(e),
                              "message": "needs review", "archive_id": archive_id})
            else:
                state.update({"status": "error", "error": str(e), "message": "retry failed", "archive_id": archive_id})


def _is_flowus_url(url):
    if not isinstance(url, str) or not url:
        return False
    try:
        u = urlparse(url)
    except Exception:
        return False
    if u.scheme != "https":
        return False
    host = (u.hostname or "").lower()
    return host == "flowus.cn" or host.endswith(".flowus.cn")


def _valid_page_id(pid):
    return isinstance(pid, str) and UUID_RE.fullmatch(pid) is not None


def _public_state():
    with lock:
        st = {
            "status": state.get("status"),
            "message": state.get("message") or "",
            "has_error": bool(state.get("error")),
            "page_bound": bool(state.get("page_id")),
            "can_retry": False,
        }
        if state.get("error"):
            st["error"] = str(state.get("error"))[:500]
        if state.get("archive_id"):
            st["archive_id"] = state.get("archive_id")
        if state.get("meeting_id"):
            st["meeting_id"] = state.get("meeting_id")
        if state.get("status") == "error" and state.get("archive_id") and LEDGER:
            try:
                rec = LEDGER.get(state.get("archive_id")) or {}
                st["can_retry"] = rec.get("status") in ("error", "needs_review", "pending")
                st["archive_status"] = rec.get("status")
            except Exception:
                st["can_retry"] = True
        return st


def _authorized(handler):
    auth = handler.headers.get("Authorization", "")
    expected = "Bearer " + BRIDGE_TOKEN if BRIDGE_TOKEN else ""
    if not BRIDGE_TOKEN or not auth.startswith("Bearer "):
        return False
    return hmac.compare_digest(auth, expected)


class Handler(BaseHTTPRequestHandler):
    server_version = "FlowUsVoiceBridge/1.0"

    def _cors_origin(self):
        origin = self.headers.get("Origin", "")
        if ALLOWED_ORIGIN and origin == ALLOWED_ORIGIN:
            return origin
        return None

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        origin = self._cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            vary = "Origin"
            if self.headers.get("Access-Control-Request-Private-Network"):
                vary = "Origin, Access-Control-Request-Private-Network"
            self.send_header("Vary", vary)
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
            if self.headers.get("Access-Control-Request-Private-Network") == "true":
                self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _reject(self, code, msg):
        self._send(code, {"ok": False, "error": msg})

    def _read_json(self):
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ValueError("content-type must be application/json")
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            raise ValueError("missing content length")
        try:
            length = int(raw_len)
        except Exception:
            raise ValueError("invalid content length")
        if length < 0 or length > MAX_BODY_SIZE:
            raise ValueError("request too large")
        try:
            self.connection.settimeout(READ_TIMEOUT)
        except Exception:
            pass
        raw = self.rfile.read(length) if length else b"{}"
        if len(raw) != length:
            raise ValueError("incomplete request body")
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            raise ValueError("invalid json")
        if not isinstance(data, dict):
            raise ValueError("json object required")
        return data

    def _send_preflight(self):
        origin = self._cors_origin()
        self.send_response(204)
        self.send_header("Content-Length", "0")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            vary = "Origin"
            if self.headers.get("Access-Control-Request-Private-Network"):
                vary = "Origin, Access-Control-Request-Private-Network"
            self.send_header("Vary", vary)
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
            self.send_header("Access-Control-Max-Age", "600")
            if self.headers.get("Access-Control-Request-Private-Network") == "true":
                self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()

    def _preflight(self):
        host = (self.headers.get("Host") or "").lower()
        if host != EXPECTED_HOST.lower():
            self._reject(403, "invalid host")
            return False
        origin = self.headers.get("Origin")
        if ALLOWED_ORIGIN:
            # Chrome 157 extension service-worker fetches carry NO Origin header.
            # A request without Origin is not a web page origin; the Bearer token
            # remains mandatory, so allow it. Only a mismatching Origin is rejected.
            if origin and origin != ALLOWED_ORIGIN:
                self._reject(403, "forbidden origin")
                return False
        elif origin:
            # No allowed origin configured: reject cross-origin browser requests by default.
            self._reject(403, "origin not allowed")
            return False
        return True

    def _authorize(self):
        if not BRIDGE_TOKEN:
            self._reject(503, "bridge token not configured")
            return False
        if not _authorized(self):
            self._reject(401, "unauthorized")
            return False
        return True

    def do_OPTIONS(self):
        if not self._preflight():
            return
        self._send_preflight()

    def do_GET(self):
        if not self._preflight():
            return
        if not self._authorize():
            return
        path = urlparse(self.path).path
        if path == "/state":
            self._send(200, {"ok": True, "state": _public_state()})
        elif path == "/ping":
            self._send(200, {"ok": True, "status": "ok"})
        else:
            self._reject(404, "not found")

    def do_POST(self):
        if not self._preflight():
            return
        if not self._authorize():
            return
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
        except ValueError as e:
            self._reject(400, str(e))
            return
        except Exception:
            self._reject(400, "bad request")
            return

        if path == "/start":
            with lock:
                if state.get("status") in ("recording", "stopping", "processing", "starting"):
                    self._send(409, {"ok": False, "error": "busy", "state": _public_state()})
                    return
            page_id = payload.get("pageId") or payload.get("page_id")
            page_title = str(payload.get("title") or "")[:300]
            page_url = str(payload.get("url") or "")[:1000]
            if not _valid_page_id(page_id):
                self._reject(400, "invalid page id")
                return
            if page_url and not _is_flowus_url(page_url):
                self._reject(400, "invalid page url")
                return
            with lock:
                state.update({"started_at": time.time(), "status": "starting", "message": "starting", "result": None, "error": None,
                              "page_id": page_id, "page_title": page_title, "page_url": page_url})
            threading.Thread(target=worker_start, args=(page_id, page_title, page_url), daemon=True).start()
            self._send(200, {"ok": True})

        elif path == "/stop":
            with lock:
                if state.get("status") not in ("recording", "starting"):
                    self._send(409, {"ok": False, "error": "not recording", "state": _public_state()})
                    return
            threading.Thread(target=worker_stop_and_process, daemon=True).start()
            self._send(200, {"ok": True})

        elif path == "/retry":
            with lock:
                if state.get("status") in ("recording", "stopping", "processing", "starting"):
                    self._send(409, {"ok": False, "error": "busy", "state": _public_state()})
                    return
            archive_id = payload.get("archive_id") or payload.get("archiveId") or state.get("archive_id")
            meeting_id = payload.get("meeting_id") or payload.get("meetingId") or state.get("meeting_id")
            page_id = payload.get("page_id") or payload.get("pageId") or state.get("page_id")
            raw_regen = payload.get("regenerate")
            regenerate = raw_regen is True or str(raw_regen).strip().lower() in ("1", "true", "yes", "on")
            # if nothing specified, pick latest retryable from ledger
            if not archive_id and not (meeting_id and page_id) and LEDGER:
                try:
                    items = LEDGER.list_retryable(limit=1)
                    if items:
                        archive_id = items[0].get("archive_id")
                except Exception:
                    pass
            if not archive_id and not (meeting_id and page_id):
                self._send(400, {"ok": False, "error": "archive_id or (meeting_id,page_id) required (no retryable archive found)"})
                return
            threading.Thread(target=worker_retry_archive, args=(archive_id, meeting_id, page_id, regenerate), daemon=True).start()
            self._send(200, {"ok": True, "regenerate": regenerate})

        elif path == "/archives":
            if not LEDGER:
                self._send(200, {"ok": True, "archives": []})
                return
            try:
                items = LEDGER.list_retryable(limit=20)
                safe = []
                for r in items:
                    safe.append({
                        "archive_id": r.get("archive_id"),
                        "meeting_id": r.get("meeting_id"),
                        "target_page_id": r.get("target_page_id"),
                        "status": r.get("status"),
                        "updated_at": r.get("updated_at"),
                        "error": (r.get("error") or "")[:300],
                    })
                self._send(200, {"ok": True, "archives": safe})
            except Exception as e:
                sys.stderr.write("[bridge] /archives failed: %r\n" % (e,))
                self._send(500, {"ok": False, "error": "internal error"})

        elif path == "/reset":
            with lock:
                if state.get("status") in ("recording", "stopping", "processing", "starting"):
                    self._send(409, {"ok": False, "error": "busy"})
                    return
                state.update({"status": "idle", "message": "", "error": None, "result": None, "meeting_id": None, "archive_id": None})
            self._send(200, {"ok": True})
        else:
            self._reject(404, "not found")

    def log_message(self, fmt, *args):
        sys.stderr.write("[bridge] " + (fmt % args) + "\n")


def _print_startup_help(msg, fix=None):
    print(f"ERROR: {msg}", file=sys.stderr)
    if fix:
        print(f"Fix: {fix}", file=sys.stderr)
    print("Tip: run `python scripts/doctor.py` for diagnostics, or `python scripts/setup.py` to configure.", file=sys.stderr)


# ---- autostart (keep the bridge running after login) ----

AUTOSTART_NAME = "record2flowus-bridge.vbs"


def _autostart_enabled():
    raw = os.environ.get("BRIDGE_AUTOSTART")
    if raw is None:
        raw = CFG.get("autostart", True)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "disabled")


def autostart_dir():
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")


def autostart_path():
    return os.path.join(autostart_dir(), AUTOSTART_NAME)


def _autostart_vbs():
    py = sys.executable or "python"
    if py.lower().endswith("python.exe"):
        candidate = py[: -len("python.exe")] + "pythonw.exe"
        if os.path.isfile(candidate):
            py = candidate
    bridge_py = os.path.join(BASE_DIR, "bridge.py")
    return (
        "' Generated by bridge.py on start-up so the bridge is ready after login.\r\n"
        "' Delete this file (or set \"autostart\": false) to disable.\r\n"
        'Set WshShell = CreateObject("WScript.Shell")\r\n'
        f'WshShell.CurrentDirectory = "{BASE_DIR}"\r\n'
        'WshShell.Run """' + py + '"" ""' + bridge_py + '""", 0, False\r\n'
    )


def ensure_autostart():
    """Install/refresh the Start-up shortcut. Idempotent; never fatal.

    Returns "present" | "installed" | "error" | None (disabled / not Windows).
    """
    if _autostart_enabled() is False or os.name != "nt":
        return None
    try:
        path = autostart_path()
        content = _autostart_vbs()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
                    if f.read() == content:
                        return "present"
            except Exception:
                pass
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        return "installed"
    except Exception as e:
        sys.stderr.write("[bridge] autostart setup skipped: %r\n" % (e,))
        return "error"


def _ollama_exe_candidates():
    cands = []
    cfg_exe = str(CFG.get("ollama_exe") or "").strip()
    if cfg_exe:
        cands.append(cfg_exe)
    found = shutil.which("ollama")
    if found:
        cands.append(found)
    local = os.environ.get("LOCALAPPDATA") or ""
    if local:
        cands.append(os.path.join(local, "Programs", "Ollama", "ollama.exe"))
    cands.append(os.path.join(os.environ.get("ProgramFiles") or r"C:\Program Files", "Ollama", "ollama.exe"))
    return cands


def _summary_uses_ollama():
    mode = str(SUMMARY_CFG.get("summary_mode") or "rules").strip().lower()
    return mode in ("ollama", "llm", "model")


def ensure_ollama(wait_sec=12.0):
    """Make sure the local Ollama server is answering when summary_mode needs it.

    Ollama is a separate process that can be closed (or die with a crashed shell);
    when it is down the summary silently degrades to rules, which is exactly the
    "要点还是残句" symptom users report. Start it detached if it is installed.

    Returns "running" | "started" | "missing" | "failed" | None (not needed).
    """
    if not _summary_uses_ollama():
        return None
    try:
        from scripts.summarizer import _preflight_ollama
    except Exception:
        return None
    base = str(SUMMARY_CFG.get("ollama_url") or os.environ.get("OLLAMA_URL") or "http://127.0.0.1:11434").rstrip("/")
    try:
        _preflight_ollama(base)
        return "running"
    except Exception:
        pass
    exe = next((p for p in _ollama_exe_candidates() if p and os.path.isfile(p)), None)
    if not exe:
        sys.stderr.write("[bridge] ollama not installed; summary will fall back to rules\n")
        return "missing"
    try:
        flags = 0
        if os.name == "nt":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
        subprocess.Popen(
            [exe, "serve"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
        )
    except Exception as e:
        sys.stderr.write("[bridge] failed to start ollama: %r\n" % (e,))
        return "failed"
    deadline = time.time() + max(1.0, float(wait_sec))
    while time.time() < deadline:
        try:
            _preflight_ollama(base)
            sys.stderr.write("[bridge] started ollama serve (%s)\n" % exe)
            return "started"
        except Exception:
            time.sleep(0.5)
    sys.stderr.write("[bridge] ollama did not answer within %.0fs; summary will fall back to rules\n" % wait_sec)
    return "failed"


def _ensure_stdio():
    """Give headless launches (pythonw, e.g. the autostart VBS) real streams.

    pythonw has no console, so sys.stdout/sys.stderr are None. BaseHTTPRequestHandler
    writes one access-log line per request to sys.stderr; writing to None raises
    inside the handler and the client sees the connection reset while the port stays
    bound -- the bridge looks alive but answers nothing. Point both at a log file.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    stream = None
    try:
        log_dir = os.path.join(os.environ.get("APPDATA") or BASE_DIR, "hex-flowus-voice-note")
        os.makedirs(log_dir, exist_ok=True)
        stream = open(os.path.join(log_dir, "bridge.log"), "a", encoding="utf-8", buffering=1)
    except Exception:
        try:
            stream = open(os.devnull, "w")
        except Exception:
            return
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


if __name__ == "__main__":
    _ensure_stdio()
    if "--check" in sys.argv:
        # lightweight config/startup checks without serving
        start_ok = True
        browser_ok = True
        origin_re = re.compile(r"^chrome-extension://[a-p]{32}$")

        if not BRIDGE_TOKEN:
            print("[FAIL] bridge.start: bridge_token is not configured.", file=sys.stderr)
            print("       fix: Run `python scripts/setup.py` to generate a token, save it in config.local.json, and set the SAME token in the extension Options.", file=sys.stderr)
            start_ok = False
            browser_ok = False
        elif len(BRIDGE_TOKEN) < 32:
            print("[FAIL] bridge.start: bridge_token is too short (must be >= 32 characters).", file=sys.stderr)
            print("       fix: Generate a new high-entropy token via `python scripts/setup.py` and update extension Options.", file=sys.stderr)
            start_ok = False
            browser_ok = False
        else:
            print(f"[PASS] bridge.start: 配置可启动（已通过 token/基础配置检查，监听地址 http://{HOST}:{PORT}）。")

        if start_ok:
            if not ALLOWED_ORIGIN:
                print("[FAIL] bridge.browser_connect: bridge_allowed_origin 未配置，浏览器扩展无法连接（带 Origin 的请求会被拒绝）。", file=sys.stderr)
                print("       fix: 在 chrome://extensions 复制扩展 ID，设置 bridge_allowed_origin=chrome-extension://<ID> 到 config.local.json，并重启 bridge。", file=sys.stderr)
                browser_ok = False
            elif not origin_re.fullmatch(ALLOWED_ORIGIN):
                print(f"[FAIL] bridge.browser_connect: bridge_allowed_origin 格式无效：{ALLOWED_ORIGIN}", file=sys.stderr)
                print("       fix: 使用 chrome-extension://<32个小写字母a-p>，并与已加载扩展 ID 一致；修改后重启 bridge。", file=sys.stderr)
                browser_ok = False
            else:
                print(f"[PASS] bridge.browser_connect: 浏览器扩展可连接的桥接侧前提已满足（Origin 已配置且格式有效）。请确保扩展选项中的 token/端口与配置一致。")

        print("[INFO] bridge.e2e: 端到端归档可用性请运行 `python scripts/doctor.py`（检查 Hex、FlowUs CLI 认证、扩展文件等）。")

        mode = str(SUMMARY_CFG.get("summary_mode") or "rules").strip().lower()
        if mode in ("ollama", "llm", "model"):
            model = str(SUMMARY_CFG.get("ollama_model") or os.environ.get("OLLAMA_MODEL") or "qwen2.5:3b")
            url = str(SUMMARY_CFG.get("ollama_url") or os.environ.get("OLLAMA_URL") or "http://127.0.0.1:11434").rstrip("/")
            try:
                from scripts.summarizer import _preflight_ollama
                _preflight_ollama(url)
                print(f"[PASS] bridge.summary: summary_mode=ollama，Ollama 可达（{url}），模型 {model}。")
            except Exception as e:
                print(f"[FAIL] bridge.summary: summary_mode=ollama 但 Ollama 不可用：{e}", file=sys.stderr)
                print("       fix: 运行 `ollama serve` 并 `ollama pull %s`；或把 summary_mode 改回 rules。" % model, file=sys.stderr)
                print("       note: `python bridge.py` 正常启动时会尝试自动拉起本机 ollama serve；`--check` 不会启动任何进程。", file=sys.stderr)
        else:
            print("[INFO] bridge.summary: summary_mode=rules（启发式挑句+清洗，不改写句子；如需通顺书面语请切 ollama）。")

        if os.name == "nt":
            if not _autostart_enabled():
                print("[INFO] bridge.autostart: 已显式关闭（config autostart=false 或 BRIDGE_AUTOSTART=0）。")
            elif os.path.isfile(autostart_path()):
                print(f"[PASS] bridge.autostart: 开机自启已就绪（{autostart_path()}）。")
            else:
                print("[INFO] bridge.autostart: 尚未安装；成功启动一次 bridge.py 会自动创建开机自启项。")

        if not start_ok:
            print("Tip: run `python scripts/doctor.py` for diagnostics, or `python scripts/setup.py` to configure.", file=sys.stderr)
            sys.exit(2)
        if not browser_ok:
            print("Tip: bridge 可启动，但浏览器扩展尚不可连接；修复上述问题后重启 bridge 并重测。", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)

    if HOST != "127.0.0.1":
        raise RuntimeError("Bridge must bind to 127.0.0.1")
    if not BRIDGE_TOKEN:
        _print_startup_help(
            "bridge_token is not configured.",
            "Run `python scripts/setup.py` to generate a token, save it in config.local.json, and set the SAME token in the extension Options page.",
        )
        sys.exit(2)
    if len(BRIDGE_TOKEN) < 32:
        _print_startup_help(
            "bridge_token is too short (must be >= 32 characters).",
            "Generate a new high-entropy token via `python scripts/setup.py`, update config.local.json, and update the extension Options.",
        )
        sys.exit(2)
    if not ALLOWED_ORIGIN:
        print("WARNING: bridge_allowed_origin is not configured; browser requests with Origin will be rejected.", file=sys.stderr)
        print("Fix: open chrome://extensions, copy the extension ID, set bridge_allowed_origin=chrome-extension://<ID> in config.local.json, then restart bridge.", file=sys.stderr)
    elif not re.fullmatch(r"chrome-extension://[a-p]{32}", ALLOWED_ORIGIN):
        print(f"WARNING: bridge_allowed_origin format looks invalid: {ALLOWED_ORIGIN}", file=sys.stderr)
        print("Fix: use chrome-extension://<32 lowercase letters a-p> matching your loaded extension ID.", file=sys.stderr)

    print(f"Bridge listening on http://{HOST}:{PORT}")
    print(f"HEX_EXE={HEX_EXE}")
    print(f"SYNC_SCRIPT={SYNC_SCRIPT}")
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        if getattr(e, "errno", None) in (98, 10048) or "address already in use" in str(e).lower() or "only one usage" in str(e).lower():
            _print_startup_help(
                f"port {PORT} is already in use on 127.0.0.1.",
                "Stop the process using this port, or change bridge_port in config.local.json (and set the SAME port in extension Options), then start again.",
            )
            sys.exit(2)
        _print_startup_help(f"failed to start server: {e}", "Check host/port permissions and configuration, then retry.")
        sys.exit(2)

    # Server is actually serving: keep it ready after every login by default.
    _autostart_state = ensure_autostart()
    if _autostart_state == "installed":
        print(f"[bridge] autostart installed: {autostart_path()}")
    elif _autostart_state == "present":
        print(f"[bridge] autostart already present: {autostart_path()}")
    elif _autostart_state is None:
        print("[bridge] autostart disabled (config autostart=false or BRIDGE_AUTOSTART=0)")

    # summary_mode=ollama depends on a local Ollama server that is NOT part of
    # this process; if it is down every archive silently degrades to rules.
    _ollama_state = ensure_ollama()
    if _ollama_state == "started":
        print("[bridge] ollama serve started (was not running)")
    elif _ollama_state == "missing":
        print("[bridge] ollama not installed; summaries will fall back to rules")
    elif _ollama_state == "failed":
        print("[bridge] ollama unreachable; summaries will fall back to rules")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            server.server_close()
        except Exception:
            pass
