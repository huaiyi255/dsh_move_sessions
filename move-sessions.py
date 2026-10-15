#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DSH 会话跨工作区搬迁工具（交互式 · 一键移动）
================================================

用途
----
把「某个工作区」里的「任意几个会话」搬到「另一个工作区」，全交互：

    列出工作区 → 选源 → 勾选会话（支持 1,3-5）→ 选目标 → 确认/预演 → 一键搬迁

背景（为什么必须这么搬）
------------------------
DSH 没有任何跨工作区移动会话的 API / RPC / 插件。会话属于哪个工作区是**派生**出来的：

    WorkspaceEntity.get sessionIds() {
      return this.record.sessionIds.filter(id => this.host.sessionPath(id) === this.record.path);
    }

`sessionPath` 来自会话日志 header 里的 cwd（经 fs.realpath 规范化）。所以只有
「日志 header 的 cwd == 工作区 path」时它才算成员 —— 光改 workspace.json 是没用的。
而且 assertStoredIdentity 要求「日志的物理路径必须与 header.cwd 推出的路径一致」，
所以会话目录也得跟着挪。于是「搬迁」= 4 件事一起做：

    1) 改写日志 header（第 0 帧）里的 cwd              → 让它归属新工作区
    2) 会话目录从 sessions/--<旧路径>--/ 挪到 --<新路径>--/  → 否则日志被判 corrupt
    3) 改 session_projcache 的 record.identity.cwd     → 投影缓存的归属
    4) 改 storages/workspace.json 的 sessionIds 数组    → 列表里立刻归位

前置条件
--------
* **必须完全退出 DSH**（关窗口 + 右键托盘图标退出）。原因：
  - workspace.json 是「内存状态整体重写」的，DSH 运行期间你改完会被它覆盖；
  - 活跃会话被挪走后，它下一次追加会在旧路径重建日志，DSH 启动时会报
    `duplicate JSONL session id "..." appears in multiple project directories`。
  脚本用 tasklist 检测，检不到会让你自己确认。
* 需要 `zstandard` 模块（`pip install zstandard`）；没有则自动退回调用 Node 的 zlib。

安全措施
--------
* 全量备份到脚本目录 `backup-YYYYmmdd-HHMMSS/`（workspace.json + projcache + 会话目录）。
* 任何一步出错自动回滚（从备份整体还原），退出码 1。
* 强烈建议先跑 `--dry-run` 看一遍，或交互时按 D。

用法
----
    python move-sessions.py                    # 全交互
    python move-sessions.py --list             # 只看有哪些工作区 / 会话
    python move-sessions.py --src D:\\script\\ds --dst D:\\script\\ds_vul \\
                            --sessions session-6b0686cf,session-f5832f07 --dry-run
    python move-sessions.py --help
"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

VERSION = "1.1"
ZSTD_MAGIC = 0xFD2FB528
LOG_RE = re.compile(r"^session\.v(\d+)\.jsonl(\.zstd)?$")
INSERT_AT_FRONT = True      # 与 WorkspaceEntity.attachSession 一致：新会话插到最前面
HEAD_READ = 8192            # 取第 0 帧只需文件开头这些字节（header 独占第 0 帧）

if not sys.stdout.isatty():
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass


# ────────────────────────────── 输出小工具 ──────────────────────────────

def out(*a):
    print(*a, flush=True)


def ok(msg):
    out("  [OK] " + msg)


def warn(msg):
    out("  [!!] " + msg)


def step(msg):
    out("  ... " + msg)


def hr(ch="─", n=68):
    out(ch * n)


def _wide(c):
    o = ord(c)
    return 2 if (0x1100 <= o <= 0x115F or 0x2E80 <= o <= 0xA4CF or 0xAC00 <= o <= 0xD7A3
                 or 0xF900 <= o <= 0xFAFF or 0xFE30 <= o <= 0xFE6F or 0xFF00 <= o <= 0xFF60
                 or 0xFFE0 <= o <= 0xFFE6 or 0x1F300 <= o <= 0x1FAFF) else 1


def pad(s, width):
    s = s or ""
    return s + " " * max(0, width - sum(_wide(c) for c in s))


def fmt_size(n):
    x = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if x < 1024 or u == "GB":
            return ("%d B" % n) if u == "B" else ("%.2f %s" % (x, u))
        x /= 1024


def fmt_time(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") \
        + "%03dZ" % (datetime.datetime.now().microsecond // 1000)


# ────────────────────────────── 路径规范化 ──────────────────────────────

def canon(path):
    """复现 DSH 的 realpathNormalize：绝对化 + 解析符号链接。"""
    try:
        return os.path.realpath(os.path.abspath(path))
    except Exception:
        return os.path.abspath(path)


def same_path(a, b):
    if a is None or b is None:
        return False
    return os.path.normcase(canon(a)) == os.path.normcase(canon(b))


def project_key(cwd):
    """逐字移植 DSH 的 projectKey()：分隔符连续的一段折叠成一个 '-'。"""
    if len(cwd) == 0:
        raise ValueError("cannot encode an empty project path")
    readable = ""
    separator_run = False
    for ch in cwd:
        if ch in "/\\:":
            if not separator_run:
                readable += "-"
            separator_run = True
        elif ch != "~" and re.match(r"^[A-Za-z0-9._-]$", ch):
            readable += ch
            separator_run = False
        else:
            readable += "~" + format(ord(ch), "X").rjust(4, "0")
            separator_run = False
    return "--" + (re.sub(r"^-+", "", readable) or "root")[:251] + "--"


def project_dir(root, cwd):
    return os.path.join(root, project_key(cwd) if cwd is not None else "_no-cwd")


# ────────────────────────────── zstd 帧层 ──────────────────────────────

def scan_zstd_frames(buf):
    """扫出串联 zstd 帧的边界，返回 (frames, torn_offset)。"""
    frames = []
    off = 0
    while off < len(buf):
        start = off
        if off + 4 > len(buf) or int.from_bytes(buf[off:off + 4], "little") != ZSTD_MAGIC:
            return frames, start
        off += 4
        d = buf[off]
        off += 1
        if d & 8:
            raise ValueError("corrupt Zstandard session log: reserved frame-header bit at byte %d" % (off - 1))
        fcs, single, ck, dictf = d >> 6, (d >> 5) & 1, (d >> 2) & 1, d & 3
        content_size_bytes = (1 if single else 0) if fcs == 0 else (1 << fcs)
        off += (0 if single else 1) + dictf + content_size_bytes
        while True:
            if off + 3 > len(buf):
                return frames, start
            bh = int.from_bytes(buf[off:off + 3], "little")
            off += 3
            last, btype, bsize = bh & 1, (bh >> 1) & 3, bh >> 3
            if btype == 3:
                raise ValueError("corrupt Zstandard session log: reserved block type at byte %d" % (off - 3))
            off += 1 if btype == 1 else bsize
            if last:
                break
        if ck:
            off += 4
        if off > len(buf):
            return frames, start
        frames.append((start, off))
    return frames, None


NODE_HELPER = r"""
const z = require('zlib');
const mode = process.argv[2];
const chunks = [];
process.stdin.on('data', (c) => chunks.push(c));
process.stdin.on('end', () => {
  const data = Buffer.from(Buffer.concat(chunks).toString('utf8').trim(), 'base64');
  try {
    const out = mode === 'd'
      ? z.zstdDecompressSync(data)
      : z.zstdCompressSync(data, { params: { [z.constants.ZSTD_c_checksumFlag]: 1 } });
    process.stdout.write(out.toString('base64'));
  } catch (e) {
    process.stderr.write(String((e && e.message) || e));
    process.exit(3);
  }
});
"""


def find_node():
    cands = []
    runtimes = os.path.join(os.path.expanduser("~"), ".dsh", "dsh-runtimes")
    if os.path.isdir(runtimes):
        for name in sorted(os.listdir(runtimes), reverse=True):
            cands.append(os.path.join(runtimes, name, "dependencies", "node", "bin", "node.exe"))
    cands.append(shutil.which("node"))
    cands.append(os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "nodejs", "node.exe"))
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


class Codec(object):
    """帧编解码：优先 Python 的 zstandard 模块，缺了退回 Node 的 node:zlib。"""

    def __init__(self, force=None):
        self.kind = None
        self.zstd = None
        self.node = None
        self.helper = None
        if force in (None, "zstandard"):
            try:
                import zstandard
                self.zstd = zstandard
                self._c = zstandard.ZstdCompressor(write_checksum=True, write_content_size=True, level=3)
                self.kind = "Python zstandard %s" % zstandard.__version__
            except ImportError:
                if force == "zstandard":
                    raise SystemExit("找不到 zstandard 模块：请 `pip install zstandard`，或用 --codec node。")
        if self.kind is None:
            self.node = find_node()
            if not self.node:
                raise SystemExit("既没有 zstandard 模块，也找不到 node.exe。请先 `pip install zstandard`。")
            fd, self.helper = tempfile.mkstemp(suffix=".js", prefix="dsh-zstd-")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(NODE_HELPER)
            self.kind = "Node 兜底 %s" % self.node

    def decompress(self, frame):
        if self.zstd is not None:
            return self.zstd.ZstdDecompressor().decompress(frame, max_output_size=1 << 27)
        return self._node("d", frame)

    def compress(self, plain):
        if self.zstd is not None:
            return self._c.compress(plain)
        return self._node("c", plain)

    def _node(self, mode, payload):
        r = subprocess.run([self.node, self.helper, mode], input=base64.b64encode(payload),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if r.returncode != 0:
            raise RuntimeError("Node 帧编解码失败：%s" % r.stderr.decode("utf-8", "replace")[:400])
        return base64.b64decode(r.stdout)

    def cleanup(self):
        if self.helper and os.path.exists(self.helper):
            try:
                os.remove(self.helper)
            except OSError:
                pass


# ────────────────────────────── JSON 原排版复刻 ──────────────────────────────

_FORMATS = [(None, None), (None, (",", ":")), (2, None), (4, None), (1, None), ("\t", None)]


def detect_format(raw, obj):
    """识别原文件的排版，返回 (indent, separators, tail, crlf)；识别不出返回 None。"""
    for indent, seps in _FORMATS:
        kw = {"ensure_ascii": False, "indent": indent}
        if seps:
            kw["separators"] = seps
        try:
            s = json.dumps(obj, **kw)
        except Exception:
            continue
        for tail in ("", "\n", "\r\n"):
            if s + tail == raw:
                return (indent, seps, tail, False)
            if (s + "\n").replace("\n", "\r\n") == raw:
                return (indent, seps, "\n", True)
    return None


def render(obj, fmt, fallback_indent=2):
    """按 detect_format 得到的排版把对象写回字符串。"""
    if fmt is None:
        return json.dumps(obj, ensure_ascii=False, indent=fallback_indent) + "\n"
    indent, seps, tail, crlf = fmt
    kw = {"ensure_ascii": False, "indent": indent}
    if seps:
        kw["separators"] = seps
    s = json.dumps(obj, **kw)
    if crlf:
        s = s.replace("\n", "\r\n")
    return s + tail


def atomic_write_bytes(path, data):
    tmp = path + ".dsh-move.tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def atomic_write_text(path, text):
    atomic_write_bytes(path, text.encode("utf-8"))


# ────────────────────────────── 数据模型 ──────────────────────────────

class SessionRef(object):
    __slots__ = ("sid", "proj", "dir", "log", "version", "size", "mtime", "cwd", "title",
                 "in_record", "depth")

    def __init__(self, sid, proj, d, log, version, size, mtime, cwd, depth=0):
        self.sid = sid
        self.proj = proj
        self.dir = d
        self.log = log
        self.version = version
        self.size = size
        self.mtime = mtime
        self.cwd = cwd
        self.title = None
        self.in_record = False
        self.depth = depth or 0

    @property
    def is_subagent(self):
        return self.depth > 0


class Tool(object):
    def __init__(self, home, codec):
        self.home = home
        self.sessions_root = os.path.join(home, "sessions")
        self.ws_path = os.path.join(home, "storages", "workspace.json")
        self.pc_dir = os.path.join(home, "storages", "session_projcache", "sessions")
        self.codec = codec
        self._titles = {}

    def precheck(self):
        for p, what in ((self.home, "DSH_HOME"), (self.sessions_root, "sessions 目录"),
                        (self.ws_path, "storages\\workspace.json")):
            if not os.path.exists(p):
                raise SystemExit("找不到%s：%s" % (what, p))

    # ---- 只读第 0 帧 ----
    def read_header(self, log_path):
        with open(log_path, "rb") as fh:
            head = fh.read(HEAD_READ)
            frames, _ = scan_zstd_frames(head)
            if not frames:
                fh.seek(0)
                head = fh.read()
                frames, torn = scan_zstd_frames(head)
                if not frames:
                    raise ValueError("读不出第 0 帧（torn=%s）：%s" % (torn, log_path))
            a, b = frames[0]
            if b > len(head):
                fh.seek(a)
                buf = fh.read(b - a)
            else:
                buf = head[a:b]
        plain = self.codec.decompress(buf)
        return json.loads(plain.rstrip(b"\n").decode("utf-8")), plain

    def title_of(self, sid):
        if sid in self._titles:
            return self._titles[sid]
        title = None
        p = os.path.join(self.pc_dir, sid + ".json")
        if os.path.exists(p):
            try:
                rows = json.load(open(p, encoding="utf-8")).get("record", {}).get("rows", {})
                t = rows.get("title")
                if isinstance(t, dict):
                    title = t.get("val")
                if not title:
                    first = rows.get("titleInput", {}).get("val", {}).get("first", {})
                    txt = first.get("text") if isinstance(first, dict) else None
                    if txt:
                        title = txt.strip().replace("\n", " ")[:32]
            except Exception:
                title = None
        self._titles[sid] = title
        return title

    def build_index(self):
        index = {}
        if not os.path.isdir(self.sessions_root):
            return index
        for proj in sorted(os.listdir(self.sessions_root)):
            pdir = os.path.join(self.sessions_root, proj)
            if not os.path.isdir(pdir):
                continue
            for entry in sorted(os.listdir(pdir)):
                sdir = os.path.join(pdir, entry)
                if not os.path.isdir(sdir):
                    continue
                best = None
                for f in os.listdir(sdir):
                    m = LOG_RE.match(f)
                    if not m:
                        continue
                    if m.group(2) is None:
                        warn("跳过未压缩日志（本机 DSH 用的是 zstd 编码）：%s" % os.path.join(sdir, f))
                        continue
                    ver = int(m.group(1))
                    if best is None or ver > best[0]:
                        best = (ver, os.path.join(sdir, f))
                if best is None:
                    continue
                ver, log = best
                try:
                    hdr, _ = self.read_header(log)
                except Exception as e:
                    warn("读不出 header，跳过 %s：%s" % (log, e))
                    continue
                sid = hdr.get("id") or entry
                ref = SessionRef(sid, proj, sdir, log, ver, os.path.getsize(log),
                                 os.path.getmtime(log), hdr.get("cwd"), hdr.get("delegationDepth"))
                ref.title = self.title_of(sid)
                index.setdefault(sid, []).append(ref)
        return index

    # ---- workspace.json ----
    def load_ws(self):
        self.ws_raw = open(self.ws_path, encoding="utf-8").read()
        self.ws = json.loads(self.ws_raw)
        self.ws_fmt = detect_format(self.ws_raw, self.ws)
        if self.ws_fmt is None:
            warn("workspace.json 的排版没识别出来，写回时用 2 空格缩进（只影响排版）")
        self.records = self.ws.setdefault("tables", {}).setdefault("workspaces", {})
        self.order = self.ws.setdefault("global", {}).setdefault("workspaceIds", [])
        return self.ws

    def save_ws(self):
        atomic_write_text(self.ws_path, render(self.ws, self.ws_fmt))

    def find_record_by_path(self, path):
        for wid, rec in self.records.items():
            if same_path(rec.get("path"), path):
                return wid, rec
        return None, None

    def default_title(self, path):
        """与 DSH 的 defaultWorkspaceTitle 一致：最后一段，取不到就用根。"""
        return os.path.basename(path.rstrip("\\/")) or os.path.splitdrive(path)[0] + "\\"

    def register_workspace(self, path):
        """按 DSH createCanonical 的写法新建工作区记录：新 id 前插到 durable order 最前面。"""
        p = canon(path)
        wid, _ = self.find_record_by_path(p)
        if wid is not None:
            return None, None
        wid = str(uuid.uuid4())
        now = now_iso()
        rec = {"path": p, "title": self.default_title(p), "sessionIds": [],
               "createdAt": now, "updatedAt": now}
        self.records[wid] = rec
        self.order.insert(0, wid)
        return wid, rec

    # ---- 分组：按 header.cwd 归到工作区（与 DSH 的派生规则一致）----
    def build_groups(self, index):
        groups = []
        ids = list(self.order) + [w for w in self.records if w not in self.order]
        claimed = set()
        for wid in ids:
            rec = self.records.get(wid)
            if rec is None:
                continue
            path = rec.get("path")
            in_rec = list(rec.get("sessionIds", []))
            in_set = set(in_rec)
            refs = []
            for sid, lst in index.items():
                for r in lst:
                    if same_path(r.cwd, path):
                        r.in_record = sid in in_set
                        refs.append(r)
                        claimed.add(sid)
            refs.sort(key=lambda r: (in_rec.index(r.sid) if r.sid in in_set else 9999, -r.mtime))
            groups.append({"id": wid, "path": path, "title": rec.get("title") or path,
                           "record": rec, "refs": refs})
        orphans = {}
        for sid, lst in index.items():
            if sid in claimed:
                continue
            for r in lst:
                orphans.setdefault(r.cwd, []).append(r)
        for cwd, refs in sorted(orphans.items(), key=lambda kv: str(kv[0])):
            groups.append({"id": None, "path": cwd,
                           "title": "(未分组) " + (cwd or "无 cwd"), "record": None, "refs": refs})
        return groups


# ────────────────────────────── 交互 UI ──────────────────────────────

def ask(prompt):
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        out("")
        raise SystemExit("已取消。")


def show_groups(groups, mark=None):
    for i, g in enumerate(groups, 1):
        tail = "   ← 源" if mark == i else ""
        if g["id"] is None:
            tail += "   (没有工作区记录)"
        out("  [%2d] %s %s   %d 个会话%s" % (i, pad(g["title"][:16], 34), g["path"], len(g["refs"]), tail))


def show_sessions(refs):
    for i, r in enumerate(refs, 1):
        flags = ""
        if r.is_subagent:
            flags += "  [子代理]"
        elif not r.in_record:
            flags += "  [未记账]"
        out("  [%2d] %s %s  %9s  %s%s"
            % (i, pad((r.title or "(无标题)")[:24], 50), r.sid, fmt_size(r.size), fmt_time(r.mtime), flags))


def parse_numbers(text, n):
    t = text.strip().lower()
    if t in ("a", "all", "*"):
        return list(range(n))
    if not t:
        return []
    picked = []
    for part in re.split(r"[,\s，、]+", t):
        if not part:
            continue
        m = re.match(r"^(\d+)\s*-\s*(\d+)$", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > b:
                a, b = b, a
            rng = range(a, b + 1)
        elif part.isdigit():
            rng = [int(part)]
        else:
            return None
        for v in rng:
            if v < 1 or v > n:
                return None
            if v - 1 not in picked:
                picked.append(v - 1)
    return picked


def resolve_group_arg(groups, s):
    if s is None:
        return None
    if str(s).isdigit() and 1 <= int(s) <= len(groups):
        return groups[int(s) - 1]
    exact = [g for g in groups if same_path(g["path"], s)]
    if exact:
        return exact[0]
    loose = [g for g in groups if str(s).lower() in (g["path"] or "").lower()
             or str(s).lower() in (g["title"] or "").lower()]
    if len(loose) == 1:
        return loose[0]
    if len(loose) > 1:
        raise SystemExit("'%s' 匹配到多个工作区，请用完整路径：%s" % (s, "  ".join(g["path"] for g in loose)))
    raise SystemExit("找不到工作区 '%s'。" % s)


def yes_no(prompt, default=True):
    s = ask(prompt).strip().lower()
    if not s:
        return default
    if s in ("y", "yes", "是", "好"):
        return True
    if s in ("n", "no", "否", "不"):
        return False
    return default


def ask_mode():
    """第一次交互时问清楚要做什么。回车 = 1（保持老用法）。"""
    out("")
    out("要做什么？")
    out("  [1] 把会话搬到另一个工作区（默认，回车即可）")
    out("  [2] 把会话搬到某个文件夹")
    out("  [3] 把整个工作区文件夹搬到另一个文件夹")
    out("  [q] 退出")
    while True:
        s = ask("  > ").strip().lower()
        if s in ("q", "quit", "exit"):
            out("已取消。")
            return None
        if s in ("", "1"):
            return "session"
        if s == "2":
            return "session-dir"
        if s == "3":
            return "workspace"
        out("  请输入 1 / 2 / 3（或 q 退出）")


def make_dir_dst(t, s, register=True):
    """把文件夹路径做成目标描述。是否真的新建记录由执行器第 4 步决定（这样才好回滚）。"""
    p = canon(os.path.expanduser(str(s).strip().strip('"')))
    wid, rec = t.find_record_by_path(p)
    if rec is not None:
        return {"id": wid, "path": rec.get("path") or p, "title": rec.get("title") or p,
                "record": rec, "_new": False}
    return {"id": None, "path": p, "title": t.default_title(p), "record": None,
            "_new": True, "_no_register": not register}


def ask_dir_dst(t):
    while True:
        s = ask("\n目标文件夹（完整路径，如 D:\\work\\proj；q=退出）：").strip().strip('"')
        if s.lower() in ("q", "quit", "exit"):
            return None
        if not s:
            out("  不能为空。")
            continue
        p = canon(os.path.expanduser(s))
        if os.path.exists(p) and not os.path.isdir(p):
            out("  %s 是个文件，不是文件夹。" % p)
            continue
        wid, rec = t.find_record_by_path(p)
        if rec is not None:
            ok("这个文件夹已经是工作区「%s」了，就搬到它里面。" % (rec.get("title") or p))
            return {"id": wid, "path": rec.get("path") or p, "title": rec.get("title") or p,
                    "record": rec, "_new": False}
        if not os.path.isdir(p):
            out("  文件夹 %s 还不存在。" % p)
            if not yes_no("  要新建它吗？[Y/n] ", True):
                continue
        if yes_no("  它还不是工作区，要把它登记为新工作区吗？[Y/n] ", True):
            return {"id": None, "path": p, "title": t.default_title(p), "record": None, "_new": True}
        warn("不登记：这些会话不会显示在任何一个工作区分组里（只能在 DSH 的会话总列表里找到）。")
        return {"id": None, "path": p, "title": t.default_title(p), "record": None,
                "_new": True, "_no_register": True}


# ────────────────────────────── 执行器 ──────────────────────────────

class Mover(object):
    def __init__(self, tool, args):
        self.t = tool
        self.args = args
        self.backup_dir = None
        self.moved = []
        self.moved_project = None      # (旧路径, 新路径)：整体搬迁工作区文件夹时记下来，供回滚改名还原

    # ---- 备份 ----
    def backup(self, refs):
        base = self.args.backup_dir or os.path.dirname(os.path.abspath(__file__))
        self.backup_dir = os.path.join(base, "backup-" + time.strftime("%Y%m%d-%H%M%S"))
        # 同一秒内连跑两次会撞名；撞名时绝不能复用旧目录，否则回滚会还原成上一次的快照。
        n = 2
        while os.path.exists(self.backup_dir):
            self.backup_dir = os.path.join(base, "backup-" + time.strftime("%Y%m%d-%H%M%S") + "-%d" % n)
            n += 1
        os.makedirs(self.backup_dir, exist_ok=False)
        shutil.copy2(self.t.ws_path, os.path.join(self.backup_dir, "workspace.json"))
        pc = os.path.join(self.backup_dir, "projcache")
        os.makedirs(pc, exist_ok=True)
        for r in refs:
            src = os.path.join(self.t.pc_dir, r.sid + ".json")
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(pc, r.sid + ".json"))
        sd = os.path.join(self.backup_dir, "sessions")
        for r in refs:
            shutil.copytree(r.dir, os.path.join(sd, r.sid))
        ok("备份完成：%s" % self.backup_dir)

    # ---- 目标文件夹不存在时建出来（--no-register 也要建，不然会话的 cwd 是个不存在的目录）----
    def ensure_folder(self, dst, dry):
        if os.path.isdir(dst["path"]):
            return None
        if dry:
            return "文件夹不存在，正式执行时会创建：%s" % dst["path"]
        os.makedirs(dst["path"], exist_ok=True)
        return "文件夹不存在，已创建：%s" % dst["path"]

    # ---- 目标文件夹不是工作区时，按 DSH createCanonical 的方式登记一个 ----
    def register_dst(self, dst, dry):
        if not dst.get("_new"):
            return None
        dst["_new"] = False
        if dst.get("_no_register"):
            return None                     # 只把文件夹建出来（ensure_folder 已做），不建记录
        p = dst["path"]
        msg = []
        if not dry and not os.path.isdir(p):
            os.makedirs(p, exist_ok=True)
            msg.append("文件夹不存在，已创建")
        wid, rec = self.t.register_workspace(p)
        if wid is None:
            dst["_new"] = False
            dst["id"], dst["record"] = self.t.find_record_by_path(p)
            return "文件夹其实已经有工作区记录，直接使用"
        dst["id"], dst["record"], dst["_new"] = wid, rec, False
        if not dry:
            self.t.save_ws()
        msg.append("新建工作区记录 %s（标题=%s，已放到列表最前）" % (wid, rec["title"]))
        return "；".join(msg)

    # ---- 改名工作区文件夹（整体搬迁用）----
    def rename_dir(self, old, new):
        try:
            os.replace(old, new)
        except OSError as e:
            if getattr(e, "errno", None) == 18:      # EXDEV：跨盘，改成「复制 + 删除」
                shutil.move(old, new)
            else:
                raise
        self.moved_project = (old, new)
        return "%s\n         → %s" % (old, new)

    # ---- 1) 改写日志 header ----
    def rewrite_log(self, r, new_cwd, dry):
        data = open(r.log, "rb").read()
        frames, torn = scan_zstd_frames(data)
        if torn is not None or not frames:
            raise RuntimeError("日志帧结构异常（torn=%s）：%s" % (torn, r.log))
        a, b = frames[0]
        plain = self.t.codec.decompress(data[a:b])
        if plain.count(b"\n") != 1 or not plain.endswith(b"\n"):
            raise RuntimeError("第 0 帧不是「一行 header + 换行」：%s" % r.log)
        hdr = json.loads(plain.decode("utf-8"))
        if hdr.get("id") != r.sid:
            raise RuntimeError("header id 与目录不符：%s vs %s" % (hdr.get("id"), r.sid))
        old_cwd = hdr.get("cwd")
        if not same_path(old_cwd, r.cwd):
            raise RuntimeError("header.cwd=%r 与索引里的 %r 不一致" % (old_cwd, r.cwd))
        needle = ('"cwd":"%s"' % json.dumps(old_cwd, ensure_ascii=False)[1:-1]).encode("utf-8")
        repl = ('"cwd":"%s"' % json.dumps(new_cwd, ensure_ascii=False)[1:-1]).encode("utf-8")
        if plain.count(needle) != 1:
            raise RuntimeError("第 0 帧里定位不到唯一的 cwd 字段：%s" % r.log)
        new_plain = plain.replace(needle, repl)
        if json.loads(new_plain.decode("utf-8")).get("cwd") != new_cwd:
            raise RuntimeError("改写后的 header 校验失败：%s" % r.log)
        new_f0 = self.t.codec.compress(new_plain)
        f1, t1 = scan_zstd_frames(new_f0)
        if t1 is not None or len(f1) != 1:
            raise RuntimeError("重压后的第 0 帧不是单帧")
        rebuilt = new_f0 + data[b:]
        f2, t2 = scan_zstd_frames(rebuilt)
        if t2 is not None or len(f2) != len(frames):
            raise RuntimeError("重写后帧数变了：%d → %d" % (len(frames), len(f2)))
        if not dry:
            atomic_write_bytes(r.log, rebuilt)
        return "第 0 帧 %d → %d 字节，其余 %d 帧逐字节原样" % (b - a, len(new_f0), len(frames) - 1)

    # ---- 2) 投影缓存 ----
    def rewrite_projcache(self, r, new_cwd, dry):
        p = os.path.join(self.t.pc_dir, r.sid + ".json")
        if not os.path.exists(p):
            return "跳过：没有投影缓存文件", False
        raw = open(p, encoding="utf-8").read()
        j = json.loads(raw)
        ident = j.get("record", {}).get("identity")
        if not isinstance(ident, dict) or "cwd" not in ident:
            return "跳过：投影缓存里没有 record.identity.cwd", False
        if same_path(ident["cwd"], new_cwd):
            return "跳过：identity.cwd 已经是目标", False
        fmt = detect_format(raw, j)
        ident["cwd"] = new_cwd
        if not dry:
            atomic_write_text(p, render(j, fmt))
        return 'identity.cwd → "%s"' % new_cwd, True

    # ---- 3) workspace.json ----
    def rewrite_ws(self, refs, dst, dry):
        ids = [r.sid for r in refs]
        src_rec = dst.get("_src_record")
        order = []
        if src_rec is not None:
            for sid in src_rec.get("sessionIds", []):
                if sid in ids and sid not in order:
                    order.append(sid)
        for sid in ids:
            if sid not in order:
                order.append(sid)
        removed = 0
        for wid, rec in self.t.records.items():
            before = list(rec.get("sessionIds", []))
            after = [s for s in before if s not in ids]
            if len(after) != len(before):
                removed += len(before) - len(after)
                rec["sessionIds"] = after
        rec = dst.get("record")
        fresh = []
        if rec is not None:
            fresh = [s for s in order if s not in rec.get("sessionIds", [])]
            rec["sessionIds"] = (fresh + list(rec.get("sessionIds", []))) if INSERT_AT_FRONT \
                else (list(rec.get("sessionIds", [])) + fresh)
            rec["updatedAt"] = now_iso()
        if src_rec is not None and src_rec is not rec:
            src_rec["updatedAt"] = now_iso()
        if not dry:
            self.t.save_ws()
        if rec is None:
            return ("从各工作区移除 %d 个；目标文件夹没有工作区记录（未登记），"
                    "这些会话不会归入任何工作区" % removed)
        return "从各工作区移除 %d 个，向「%s」新增 %d 个" % (removed, dst["title"], len(fresh))

    # ---- 4) 挪目录 ----
    def move_dir(self, r, dst_path, dry):
        dest_proj = project_dir(self.t.sessions_root, dst_path)
        dest = os.path.join(dest_proj, os.path.basename(r.dir))
        if os.path.exists(dest):
            raise RuntimeError("目标目录已存在：%s" % dest)
        if not dry:
            os.makedirs(dest_proj, exist_ok=True)
            os.replace(r.dir, dest)
        self.moved.append((r.dir, dest))
        return "%s\n         → %s" % (r.dir, dest)

    # ---- 回滚 ----
    def rollback(self, refs):
        out("")
        hr("=")
        out("  出错了，正在回滚 ……")
        hr("=")
        root = os.path.realpath(self.t.sessions_root)
        for old, new in reversed(self.moved):
            try:
                real_new = os.path.realpath(new)
                if os.path.exists(real_new) and os.path.dirname(os.path.dirname(real_new)) == root:
                    shutil.rmtree(real_new)
            except Exception as e:
                warn("清理新目录失败 %s：%s" % (new, e))
        if self.backup_dir:
            try:
                shutil.copy2(os.path.join(self.backup_dir, "workspace.json"), self.t.ws_path)
                for r in refs:
                    src = os.path.join(self.backup_dir, "projcache", r.sid + ".json")
                    if os.path.exists(src):
                        shutil.copy2(src, os.path.join(self.t.pc_dir, r.sid + ".json"))
                    bdir = os.path.join(self.backup_dir, "sessions", r.sid)
                    if os.path.isdir(bdir):
                        if os.path.exists(r.dir):
                            shutil.rmtree(r.dir)
                        shutil.copytree(bdir, r.dir)
                ok("已从备份整体还原。")
            except Exception as e:
                out("  [XX] 自动回滚失败：%s" % e)
                out("       请手工从备份目录还原：%s" % self.backup_dir)
        if self.moved_project is not None:
            old, new = self.moved_project
            try:
                if os.path.exists(new) and not os.path.exists(old):
                    os.makedirs(os.path.dirname(old), exist_ok=True)
                    self.rename_dir(new, old)
                    self.moved_project = None
                    ok("工作区文件夹已改名还原：%s" % old)
                else:
                    warn("工作区文件夹没能自动还原，请手工把 %s 改回 %s" % (new, old))
            except Exception as e:
                out("  [XX] 工作区文件夹还原失败：%s" % e)
                out("       请手工把 %s 改回 %s" % (new, old))
        out("  备份目录保留在：%s" % self.backup_dir)

    # ---- 总控 ----
    def run(self, refs, dst, dry):
        t = self.t
        try:
            out("")
            out("第 1 步 · 备份")
            if dry:
                step("预演模式：不写备份")
            else:
                self.backup(refs)

            out("")
            out("第 2 步 · 改写会话日志 header 的 cwd（只重写第 0 帧）")
            for i, r in enumerate(refs, 1):
                out("  [%d] %s：%s" % (i, r.title or r.sid, self.rewrite_log(r, dst["path"], dry)))

            out("")
            out("第 3 步 · 同步投影缓存 session_projcache 的 identity.cwd")
            for i, r in enumerate(refs, 1):
                msg, _ = self.rewrite_projcache(r, dst["path"], dry)
                out("  [%d] %s：%s" % (i, r.sid, msg))

            out("")
            out("第 4 步 · 改 workspace.json 的 sessionIds")
            mk = self.ensure_folder(dst, dry)
            if mk:
                out("      [目标文件夹] " + mk)
            reg = self.register_dst(dst, dry)
            if reg:
                out("      [登记目标文件夹] " + reg)
            out("      " + self.rewrite_ws(refs, dst, dry))

            out("")
            out("第 5 步 · 移动会话目录")
            for i, r in enumerate(refs, 1):
                out("  [%d] %s" % (i, self.move_dir(r, dst["path"], dry)))

            out("")
            out("第 6 步 · 复核")
            if dry:
                step("预演模式：不写盘，跳过复核")
            else:
                self.verify(refs, dst)
            return True
        except Exception as e:
            if self.backup_dir:
                self.rollback(refs)
            else:
                out("")
                out("  [XX] %s" % e)
                step("出错发生在写盘之前，没有修改任何文件。")
            return False

    # ---- 总控（整体搬迁：工作区文件夹本身也换地方）----
    def run_workspace(self, src, new_path, refs, dry):
        t = self.t
        old_path = src["path"]
        rec = src["record"]
        try:
            out("")
            out("第 1 步 · 备份（工作区文件夹本身只改名、不复制，改名可原路回滚）")
            if dry:
                step("预演模式：不写备份")
            else:
                self.backup(refs)

            out("")
            out("第 2 步 · 改名工作区文件夹")
            if dry:
                out("      %s\n         → %s（预演：不执行）" % (old_path, new_path))
            else:
                out("      " + self.rename_dir(old_path, new_path))

            out("")
            out("第 3 步 · 改写会话日志 header 的 cwd（只重写第 0 帧）")
            for i, r in enumerate(refs, 1):
                out("  [%d] %s：%s" % (i, r.title or r.sid, self.rewrite_log(r, new_path, dry)))

            out("")
            out("第 4 步 · 同步投影缓存 session_projcache 的 identity.cwd")
            for i, r in enumerate(refs, 1):
                msg, _ = self.rewrite_projcache(r, new_path, dry)
                out("  [%d] %s：%s" % (i, r.sid, msg))

            out("")
            out("第 5 步 · 改 workspace.json 里这条记录的 path")
            rec["path"] = new_path
            rec["updatedAt"] = now_iso()
            if not dry:
                t.save_ws()
            out("      %s：path → %s" % (src["id"] or "(无记录)", new_path))

            out("")
            out("第 6 步 · 移动会话目录")
            for i, r in enumerate(refs, 1):
                out("  [%d] %s" % (i, self.move_dir(r, new_path, dry)))

            out("")
            out("第 7 步 · 复核")
            if dry:
                step("预演模式：不写盘，跳过复核")
            else:
                self.verify(refs, {"id": src["id"], "path": new_path, "title": src["title"],
                                   "record": rec})
            return True
        except Exception as e:
            if self.backup_dir or self.moved_project is not None:
                self.rollback(refs)
            else:
                out("")
                out("  [XX] %s" % e)
                step("出错发生在写盘之前，没有修改任何文件。")
            return False

    def verify(self, refs, dst):
        t = self.t
        bad = 0
        recs = json.load(open(t.ws_path, encoding="utf-8"))["tables"]["workspaces"]
        dest_proj = project_dir(t.sessions_root, dst["path"])
        for r in refs:
            dest = os.path.join(dest_proj, os.path.basename(r.dir))
            if not os.path.exists(dest):
                warn("新位置不存在：%s" % dest)
                bad += 1
                continue
            if os.path.exists(r.dir):
                warn("旧位置还在：%s" % r.dir)
                bad += 1
            hdr, _ = t.read_header(os.path.join(dest, os.path.basename(r.log)))
            if hdr.get("id") != r.sid or not same_path(hdr.get("cwd"), dst["path"]):
                warn("header 校验失败：%s" % dest)
                bad += 1
            pc = os.path.join(t.pc_dir, r.sid + ".json")
            if os.path.exists(pc):
                ident = json.load(open(pc, encoding="utf-8"))["record"]["identity"]
                if not same_path(ident.get("cwd"), dst["path"]):
                    warn("projcache 未同步：%s" % pc)
                    bad += 1
            holders = [wid for wid, rec in recs.items() if r.sid in rec.get("sessionIds", [])]
            want = [dst["id"]] if dst.get("id") else []
            if holders != want:
                warn("workspace.json 记账异常：%s 记在 %s（期望 %s）" % (r.sid, holders, want))
                bad += 1
        if bad == 0:
            ok("全部 %d 个会话搬迁完成，复核通过。" % len(refs))
        return bad == 0


# ────────────────────────────── 入口 ──────────────────────────────

def dsh_running():
    """DSH 是否在运行：True / False / None（无法判断）。"""
    if os.name == "nt":
        try:
            r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq DeepSeek Harness.exe", "/NH"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
            if r.returncode == 0:
                return b"DeepSeek Harness" in r.stdout
        except Exception:
            pass
    try:
        import psutil
        for p in psutil.process_iter(["name"]):
            if (p.info.get("name") or "").lower().startswith("deepseek harness"):
                return True
        return False
    except Exception:
        return None


def parse_args(argv):
    ap = argparse.ArgumentParser(
        description="DSH 会话跨工作区搬迁工具（交互式）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="例：\n"
               "  python move-sessions.py --list\n"
               "  功能1（会话 → 另一个工作区）：\n"
               "    python move-sessions.py --src D:\\script\\ds --dst D:\\script\\ds_vul "
               "--sessions session-6b0686cf,session-f5832f07 --dry-run\n"
               "  功能2（会话 → 某个文件夹，不是工作区就自动登记）：\n"
               "    python move-sessions.py --src D:\\script\\ds --to-dir D:\\work\\proj "
               "--sessions session-6b0686cf -y\n"
               "  功能3（整个工作区文件夹 → 另一个文件夹）：\n"
               "    python move-sessions.py --move-workspace --src D:\\script\\ds_vul "
               "--into D:\\archive --as ds_vul_old\n")
    ap.add_argument("--dsh-home", default=os.environ.get("DSH_HOME")
                    or os.path.join(os.path.expanduser("~"), ".dsh"))
    ap.add_argument("--src", help="源工作区：序号 / 路径 / 标题（不给则交互选择）")
    ap.add_argument("--dst", help="目标工作区：序号 / 路径 / 标题（不给则交互选择）")
    ap.add_argument("--sessions", help="要移动的会话 id，逗号分隔（支持唯一前缀）；不给则交互勾选")
    ap.add_argument("--list", action="store_true", help="只列出工作区与会话，不做任何修改")
    ap.add_argument("--dry-run", action="store_true", help="预演：走完检查与所有步骤，但不写盘")
    ap.add_argument("--yes", "-y", action="store_true", help="不问确认，直接执行")
    ap.add_argument("--force", action="store_true", help="跳过「DSH 是否已退出」检测（危险）")
    ap.add_argument("--codec", choices=["zstandard", "node"], help="帧编解码实现（默认自动）")
    ap.add_argument("--backup-dir", help="备份放到哪里（默认脚本所在目录）")
    ap.add_argument("--to-dir", metavar="文件夹",
                    help="功能2：把会话搬到这个文件夹（还不是工作区时自动登记为工作区）")
    ap.add_argument("--no-register", action="store_true",
                    help="配合 --to-dir：目标文件夹不自动登记为工作区")
    ap.add_argument("--move-workspace", action="store_true",
                    help="功能3：把整个工作区文件夹搬走（配合 --src 与 --into）")
    ap.add_argument("--into", metavar="父文件夹", help="功能3：工作区文件夹要搬进哪个文件夹")
    ap.add_argument("--as", dest="as_name", metavar="新名字",
                    help="功能3：搬过去之后文件夹叫什么名字（默认保持原名）")
    return ap.parse_args(argv)


def move_whole_workspace(t, args, src, codec):
    """功能3：把整个工作区文件夹搬到另一个文件夹（工作区里所有会话一起改 cwd）。"""
    rec = src["record"]
    if rec is None:
        out("")
        out("  [XX] 「%s」没有工作区记录（它是未分组的目录），不能整体搬迁。" % src["title"])
        out("       请先在 DSH 里把这个文件夹添加为工作区。")
        return 1
    old = src["path"]
    if not os.path.isdir(old):
        out("  [XX] 工作区目录不存在：%s" % old)
        return 1
    interactive = args.into is None

    # ---- 目标父文件夹 ----
    into = args.into
    if into is None and args.yes:
        out("")
        out("  [XX] --move-workspace 需要 --into <父文件夹>（--yes 模式下不提问）。")
        return 1
    parent = None
    while parent is None:
        if into is None:
            s = ask("\n要搬进哪个文件夹（父文件夹，必须已存在；q=退出）：").strip().strip('"')
            if s.lower() in ("q", "quit", "exit"):
                return 0
            if not s:
                continue
            into = s
        p = canon(os.path.expanduser(into))
        if not os.path.isdir(p):
            out("  父文件夹不存在：%s" % p)
            if args.into:
                return 1
            into = None
            continue
        parent = p

    base = os.path.basename(old.rstrip("\\/"))
    name = args.as_name
    if name is None and interactive:
        s = ask("  搬过去以后文件夹叫什么名字（回车=保持原名 %s）：" % base).strip().strip('"')
        name = s or None
    if name is not None and (os.sep in name or "/" in name):
        out("  [XX] 新名字不能带路径分隔符：%s" % name)
        return 1
    new_path = os.path.join(parent, name or base)

    # ---- 校验 ----
    if same_path(new_path, old):
        out("")
        out("  新位置和原位置一样，没什么可搬的。")
        return 0
    try:
        if os.path.commonpath([canon(new_path), old]) == old:
            out("  [XX] 不能把工作区文件夹搬进它自己里面：%s" % new_path)
            return 1
    except ValueError:
        pass                                  # 不同盘符，比不了，无所谓
    wid_other, rec_other = t.find_record_by_path(new_path)
    if wid_other is not None and wid_other != src["id"]:
        out("  [XX] 目标路径已经是另一个工作区「%s」：%s"
            % (rec_other.get("title") or "?", new_path))
        return 1
    if os.path.exists(new_path):
        out("  [XX] 目标路径已经存在，不能覆盖：%s" % new_path)
        return 1

    refs = src["refs"]
    here = set(r.sid for r in refs)
    stranded = [s for s in rec.get("sessionIds", []) if s not in here]
    if stranded:
        warn("这条工作区记录还记着 %d 个不在该目录下的会话，它们不会跟着搬（搬完会被 DSH 过滤掉）：%s"
             % (len(stranded), ", ".join(stranded[:4])))

    # ---- 汇总确认 ----
    out("")
    hr("─")
    out("  即将执行：把整个工作区文件夹搬走")
    out("    工作区：%s  (%s)" % (src["title"], old))
    out("    新位置：%s" % new_path)
    out("    会话　：%d 个（含子代理）会跟着改 cwd" % len(refs))
    out("    注意　：文件夹本身只改名、不复制；被别的程序占用时会失败（失败会自动回滚）")
    hr("─")
    dry = args.dry_run
    if not args.yes and not dry:
        while True:
            a = ask("  [Y] 执行    [D] 预演    [N] 取消  > ").strip().lower()
            if a in ("y", "yes"):
                break
            if a in ("d", "dry", "dry-run"):
                dry = True
                break
            if a in ("n", "no", "", "q"):
                out("已取消。")
                return 0
    if dry:
        out("")
        out("  >> 预演模式：不会写入任何文件 <<")

    mv = Mover(t, args)
    good = mv.run_workspace(src, new_path, refs, dry)
    out("")
    hr("═")
    if good and dry:
        out("  预演完成：以上为将要执行的改动，没有写入任何文件。")
    elif good:
        out("  搬迁完成。工作区文件夹现在在：%s" % new_path)
        out("  备份留在：%s" % mv.backup_dir)
    else:
        out("  失败，见上面的输出。")
    hr("═")
    codec.cleanup()
    return 0 if good else 1


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    out("")
    hr("═")
    out("  DSH 会话跨工作区搬迁工具 v%s" % VERSION)
    hr("═")

    codec = Codec(args.codec)
    ok("帧编解码：%s" % codec.kind)
    ok("DSH_HOME：%s" % args.dsh_home)

    t = Tool(args.dsh_home, codec)
    t.precheck()
    index = t.build_index()
    t.load_ws()
    groups = t.build_groups(index)
    ok("发现 %d 个工作区、%d 个会话" % (len(groups), sum(len(g["refs"]) for g in groups)))

    if args.list:
        for g in groups:
            out("")
            out("== [%s] %s   %s" % (g["id"] or "-", g["title"], g["path"]))
            show_sessions(g["refs"])
        out("")
        codec.cleanup()
        return 0

    # ---- 先定模式，再按模式解释 --dst / --to-dir ----
    mode = None
    if args.move_workspace:
        mode = "workspace"
    elif args.to_dir:
        mode = "session-dir"
    if mode is None and not (args.src or args.dst or args.sessions):
        mode = ask_mode()
        if mode is None:
            codec.cleanup()
            return 0
    if mode is None:
        mode = "session"
    if mode != "session" and args.dst:
        warn("--dst 只在功能1里用；本模式忽略它。")
    if mode == "workspace" and args.to_dir:
        warn("--to-dir 是功能2的参数；功能3请用 --into。")
    ok("模式：%s" % {"session": "1) 会话 → 另一个工作区",
                     "session-dir": "2) 会话 → 某个文件夹",
                     "workspace": "3) 整个工作区文件夹 → 另一个文件夹"}[mode])

    dst_hint = None
    if mode == "session" and args.dst:
        dst_hint = resolve_group_arg(groups, args.dst)
    elif mode == "session-dir" and args.to_dir:
        dst_hint = make_dir_dst(t, args.to_dir, register=not args.no_register)
        if dst_hint.get("_no_register"):
            warn("目标文件夹不会登记为工作区。")

    # ---- 先看看要搬的会话现在在哪（只读，避免白跑一趟）----
    wanted = None
    if args.sessions:
        wanted = [x.strip() for x in args.sessions.split(",") if x.strip()]
        dg = dst_hint
        missing, already = [], []
        for w in wanted:
            hit = [r for g in groups for r in g["refs"] if r.sid == w or r.sid.startswith(w)]
            if not hit:
                missing.append(w)
            elif dg is not None and all(same_path(r.cwd, dg["path"]) for r in hit):
                already.append(w)
        if missing:
            out("")
            out("  [XX] 找不到这些会话：%s" % ", ".join(missing))
            out("       可以用 --list 看看有哪些会话（id 也可以只给开头一截）。")
            codec.cleanup()
            return 1
        if already:
            out("")
            ok("这些会话已经在「%s」里了，无需搬迁：" % (dg["title"] if dg else "目标工作区"))
            for w in already:
                out("   - %s" % w)
            wanted = [w for w in wanted if w not in already]
            if not wanted:
                out("")
                codec.cleanup()
                return 0

    running = dsh_running()
    if running is True and not args.force:
        out("")
        out("  [XX] 检测到 DeepSeek Harness 还在运行。")
        out("       请先完全退出（关窗口 + 右键托盘图标退出）再运行本脚本；")
        out("       确实要冒险可以加 --force。")
        return 2
    if running is None:
        warn("无法确认 DSH 是否已退出，请自己确认「窗口 + 托盘图标」都关了。")
    else:
        ok("DSH 未在运行")

    # ---- 选源 ----
    out("")
    out("可用的工作区：")
    show_groups(groups)
    src = resolve_group_arg(groups, args.src)
    while src is None:
        s = ask("\n请选择【源】工作区编号（q=退出）：").strip()
        if s.lower() in ("q", "quit", "exit"):
            return 0
        try:
            src = resolve_group_arg(groups, s)
        except SystemExit as e:
            out("  %s" % e)
    if mode != "workspace" and not src["refs"]:
        raise SystemExit("「%s」里没有可搬迁的会话。" % src["title"])

    if mode == "workspace":
        return move_whole_workspace(t, args, src, codec)

    # ---- 选会话 ----
    out("")
    out("「%s」(%s) 里的会话：" % (src["title"], src["path"]))
    show_sessions(src["refs"])
    if args.sessions:
        refs = []
        for w in wanted:
            hit = [r for r in src["refs"] if r.sid == w] or [r for r in src["refs"] if r.sid.startswith(w)]
            if len(hit) != 1:
                out("")
                out("  [XX] --sessions 里的 '%s' 不在源工作区「%s」里（它可能属于别的工作区）。"
                    % (w, src["title"]))
                codec.cleanup()
                return 1
            if hit[0] not in refs:
                refs.append(hit[0])
    else:
        refs = None
        while refs is None:
            s = ask("\n请选择要移动的会话（可多选，如 1,3-5；a=全选；A=含子代理全选；回车=取消）：")
            if not s.strip():
                out("已取消。")
                return 0
            if s.strip() == "a":
                idx = [i for i, r in enumerate(src["refs"]) if not r.is_subagent]
                if not idx:
                    out("  没有非子代理会话，用 A 连子代理一起选。")
                    continue
            elif s.strip() == "A":
                idx = list(range(len(src["refs"])))
            else:
                idx = parse_numbers(s, len(src["refs"]))
            if idx is None:
                out("  输入看不懂，示例：1,3-5")
                continue
            if not idx:
                out("  没选中任何会话。")
                continue
            refs = [src["refs"][i] for i in idx]

    # ---- 选目标 ----
    if mode == "session-dir":
        dst = dst_hint
        if dst is None:
            out("")
            out("目标文件夹可以是一个还没有会话的空文件夹；它还不是工作区的话，脚本会替你登记。")
            dst = ask_dir_dst(t)
            if dst is None:
                out("已取消。")
                return 0
    else:
        others = [g for g in groups if not same_path(g["path"], src["path"])]
        if not others:
            raise SystemExit("没有其它工作区可搬。")
        out("")
        out("可以搬到这些工作区：")
        show_groups(others)
        out("      （想搬到还没登记为工作区的文件夹？用功能2：--to-dir <文件夹>）")
        dst = resolve_group_arg(others, args.dst)
        while dst is None:
            s = ask("\n请选择【目标】工作区编号（q=退出）：").strip()
            if s.lower() in ("q", "quit", "exit"):
                return 0
            try:
                dst = resolve_group_arg(others, s)
            except SystemExit as e:
                out("  %s" % e)
        if dst["id"] is None:
            raise SystemExit("目标必须是一个已存在的工作区记录（这个目录没有记录）；"
                             "请先在 DSH 里把它添加为工作区，或改用功能2 --to-dir。")
    if not os.path.isdir(canon(dst["path"])) and not dst.get("_new"):
        raise SystemExit("目标目录不存在：%s" % dst["path"])

    # ---- 前提校验 ----
    for r in refs:
        if len(index.get(r.sid, [])) > 1:
            out("  [XX] %s 在多个项目目录里都有日志，DSH 会报 duplicate：" % r.sid)
            for x in index[r.sid]:
                out("         %s" % x.log)
            out("       请手工删掉多余那份再跑。")
            return 3
        if r.cwd is None:
            raise SystemExit("%s 的 header 没有 cwd，DSH 无法把它归入任何工作区。" % r.sid)
        if not same_path(r.cwd, src["path"]):
            raise SystemExit("%s 的 header.cwd=%s 与源工作区 %s 不一致（列表可能过期）。"
                             % (r.sid, r.cwd, src["path"]))
    if all(same_path(r.cwd, dst["path"]) for r in refs):
        out("")
        out("  这些会话已经在「%s」里了，无需搬迁。" % dst["title"])
        return 0

    # ---- 汇总确认 ----
    src_rec = None
    for r in refs:
        _, rec = t.find_record_by_path(r.cwd)
        if rec is not None:
            src_rec = rec
            break
    dst["_src_record"] = src_rec
    out("")
    hr("─")
    out("  即将执行：")
    out("    源　：%s  (%s)" % (src["title"], src["path"]))
    out("    目标：%s  (%s)" % (dst["title"], dst["path"]))
    if dst.get("_new") and not dst.get("_no_register"):
        out("    登记：目标文件夹还不是工作区 → 会新建一条工作区记录（标题=%s）" % dst["title"])
    if dst.get("_no_register"):
        out("    注意：目标文件夹不登记为工作区，这些会话不会出现在任何工作区分组里")
    for r in refs:
        out("      · %s %s  %s" % (pad((r.title or "(无标题)")[:24], 50), r.sid, fmt_size(r.size)))
    out("    共 %d 个会话" % len(refs))
    hr("─")
    dry = args.dry_run
    if not args.yes and not dry:
        while True:
            a = ask("  [Y] 执行    [D] 预演    [N] 取消  > ").strip().lower()
            if a in ("y", "yes"):
                break
            if a in ("d", "dry", "dry-run"):
                dry = True
                break
            if a in ("n", "no", "", "q"):
                out("已取消。")
                return 0
    if dry:
        out("")
        out("  >> 预演模式：不会写入任何文件 <<")

    mv = Mover(t, args)
    good = mv.run(refs, dst, dry)
    out("")
    hr("═")
    if good and dry:
        out("  预演完成：以上为将要执行的改动，没有写入任何文件。")
    elif good:
        out("  搬迁完成。重新打开 DSH，「%s」下面就会出现这些会话。" % dst["title"])
        out("  备份留在：%s" % mv.backup_dir)
    else:
        out("  失败，见上面的输出。")
    hr("═")
    codec.cleanup()
    return 0 if good else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit("已取消。")
