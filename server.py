#!/usr/bin/env python3
"""Image2PPT 本地 Web 服务。

一条命令启动：

    pnpm start          # 等价于 python3 server.py

然后打开浏览器访问 http://127.0.0.1:8123 ，把图片拖进去即可批量转成一个 PPT。
转换完成后点“保存 PPTX 到…”自选保存位置。只用 Python 标准库，不新增依赖。
"""
from __future__ import annotations

import json
import mimetypes
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import uuid
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
UPLOAD_ROOT = ROOT / ".web_uploads"
WORK_ROOT = ROOT / ".web_work"
PREFS_FILE = ROOT / ".prefs.json"
JOBS_FILE = ROOT / ".web_jobs.json"

PORT = int(os.environ.get("PORT", "8123"))
MAX_BODY = 300 * 1024 * 1024  # 单次上传上限 300MB

SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
SUPPORTED_EXTS = SUPPORTED_IMAGE_EXTS | {".pdf"}

jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


# ---------- prefs（选项记忆：刷新页面、重启后端都不丢） ----------

DEFAULT_PREFS: dict = {
    "deck_name": "",
    "ocr_token": "",
    "detect_tables": False,
    "text_only": False,
    "last_dir": "",   # 上次保存 PPT 的目录，作为下次另存为的起点
}


def load_prefs() -> dict:
    try:
        data = json.loads(PREFS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(DEFAULT_PREFS)
    prefs = dict(DEFAULT_PREFS)
    for key in prefs:
        if key not in data:
            continue
        if key in ("deck_name", "ocr_token", "last_dir"):
            prefs[key] = str(data[key])
        else:
            prefs[key] = bool(data[key])
    return prefs


def store_prefs(prefs: dict) -> None:
    clean = {k: prefs.get(k, DEFAULT_PREFS[k]) for k in DEFAULT_PREFS}
    try:
        PREFS_FILE.write_text(
            json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


PREFS = load_prefs()


# ---------- job persistence（重启后端后最近任务仍在） ----------

def _serialize_job(job: dict) -> dict:
    return {
        "id": job["id"],
        "name": job["name"],
        "status": job["status"],
        "created": job["created"],
        "files": job["files"],
        "options": job.get("options", {}),
        "src_path": str(job.get("src_path", "")),
        "work_path": str(job.get("work_path", "")),
        "pptx_name": job.get("pptx_name"),
        "pptx_path": job.get("pptx_path"),
        "saved_path": job.get("saved_path"),
        "saved_count": job.get("saved_count", 0),
        "error": job.get("error"),
    }


def persist_jobs() -> None:
    with jobs_lock:
        data = [_serialize_job(j) for j in sorted(
            jobs.values(), key=lambda j: j.get("created", ""))]
    try:
        JOBS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    except OSError:
        pass


def load_jobs() -> None:
    """启动时恢复任务列表；中断的任务标为失败，图片与日志仍在磁盘上。"""
    try:
        data = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(data, list):
        return
    with jobs_lock:
        for item in data:
            jid = str(item.get("id") or "")
            if not jid:
                continue
            status = str(item.get("status") or "error")
            error = item.get("error")
            if status in ("queued", "running"):
                status = "error"
                error = "服务重启，任务已中断"
            job = {
                "id": jid,
                "name": str(item.get("name") or jid),
                "status": status,
                "created": str(item.get("created") or ""),
                "files": list(item.get("files") or []),
                "options": dict(item.get("options") or {}),
                "src_path": str(item.get("src_path") or ""),
                "work_path": str(item.get("work_path") or ""),
                "pptx_name": item.get("pptx_name"),
                "pptx_path": item.get("pptx_path"),
                "saved_path": item.get("saved_path"),
                "saved_count": int(item.get("saved_count") or 0),
                "error": error,
                "log": [],
            }
            log_file = Path(job["work_path"]) / "job.log"
            if log_file.is_file():
                try:
                    lines = log_file.read_text(
                        encoding="utf-8", errors="replace").splitlines()
                    job["log"] = lines[-300:]
                except OSError:
                    pass
            jobs[jid] = job


# ---------- 系统对话框 ----------

def _applescript_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def pick_save_path(default_name: str, start_dir: str = "") -> str | None:
    """弹出本机“另存为”对话框，返回用户选择的完整路径；取消返回 None。"""
    start = Path(start_dir).expanduser() if start_dir else Path.home()
    if not start.is_dir():
        start = Path.home()
    system = platform.system()

    if system == "Darwin":
        script = (
            'set theFile to choose file name with prompt "保存 PPT" '
            f'default name "{_applescript_escape(default_name)}" '
            f'default location POSIX file "{_applescript_escape(str(start))}"\n'
            "return POSIX path of theFile"
        )
        try:
            proc = subprocess.run(["osascript", "-e", script],
                                  capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"无法弹出系统保存框：{exc}")
        if proc.returncode != 0:
            if "User canceled" in (proc.stderr or ""):
                return None
            raise RuntimeError(f"系统保存框失败：{(proc.stderr or '').strip()}")
        return proc.stdout.strip() or None

    if system.startswith("Win"):
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$d = New-Object System.Windows.Forms.SaveFileDialog;"
            f"$d.FileName = '{default_name}';"
            f"$d.InitialDirectory = '{start}';"
            "$d.Filter = 'PowerPoint|*.pptx';"
            "if ($d.ShowDialog() -eq 'OK') { Write-Output $d.FileName }"
        )
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"无法弹出系统保存框：{exc}")
        return proc.stdout.strip() or None

    if shutil.which("zenity"):
        try:
            proc = subprocess.run(
                ["zenity", "--file-selection", "--save", "--confirm-overwrite",
                 "--title=保存 PPT", f"--filename={start}/{default_name}"],
                capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"无法弹出系统保存框：{exc}")
        if proc.returncode != 0:
            return None
        return proc.stdout.strip() or None

    raise RuntimeError("当前系统没有可用的“另存为”对话框，请用页面上的直接下载")


def reveal_path(target: str) -> dict:
    """在本机文件管理器中打开目录，或定位到具体文件。"""
    path = Path(target).expanduser()
    is_file = path.is_file()
    base = path.parent if is_file else path
    if not base.is_dir():
        raise RuntimeError("目录不存在，可能已被移动或删除")
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)] if is_file else ["open", str(base)])
        elif sys.platform.startswith("win"):
            subprocess.Popen(["explorer", f"/select,{path}"] if is_file else ["explorer", str(base)])
        else:
            subprocess.Popen(["xdg-open", str(base)])
    except OSError as exc:
        raise RuntimeError(f"无法打开文件夹：{exc}")
    return {"opened": str(base), "file": path.name if is_file else ""}


# ---------- progress ----------

# 流水线日志里的阶段标记 → (累计百分比, 中文说明)。
# 百分比是按各阶段实测耗时估的大概权重，只为展示总体进度。
PROGRESS_STAGES: list[tuple[str, int, str]] = [
    (r"submitting \d+ page", 3, "正在提交云端识别"),
    (r"remote OCR done", 30, "文字识别完成"),
    (r"=== Stage 2:", 32, "识别结果分层"),
    (r"=== Stage 3:", 35, "装饰元素过滤"),
    (r"=== Stage 4:", 37, "复核图渲染"),
    (r"=== Summary ===", 39, "识别阶段完成"),
    (r"=== 1/5", 42, "页面还原（最耗时，请耐心等待）"),
    (r"=== 2/5", 70, "合并页面布局"),
    (r"=== 2b/5", 73, "文字分类"),
    (r"=== 3/5", 77, "生成 PPTX"),
    (r"=== 3b/5", 82, "字号校准"),
    (r"=== 3c/5", 86, "位置校准"),
    (r"=== 4/5", 92, "质量检查"),
    (r"=== 5/5", 95, "预览渲染"),
    (r"build_deck total", 99, "收尾"),
]

_PAGE_LINE_RE = re.compile(r"-> \d+ items|^page \d+:")


def compute_progress(job: dict) -> dict:
    """从任务日志推算总体进度，返回 {percent, label}。"""
    status = job["status"]
    if status == "queued":
        return {"percent": 1, "label": "排队等待"}
    if status == "done":
        return {"percent": 100, "label": "转换完成"}
    log = job.get("log", [])
    stage_idx = -1
    base, label, hits = 2, "准备中", 0
    total = len(job.get("files", [])) or 1
    for line in log:
        matched = False
        for i, (pat, pct, lab) in enumerate(PROGRESS_STAGES):
            if re.search(pat, line):
                if i >= stage_idx:
                    stage_idx, base, label, hits = i, pct, lab, 0
                matched = True
                break
        if not matched and stage_idx >= 0 and _PAGE_LINE_RE.search(line):
            hits += 1
    percent = float(base)
    if stage_idx >= 0 and stage_idx + 1 < len(PROGRESS_STAGES):
        nxt = PROGRESS_STAGES[stage_idx + 1][1]
        percent = base + (nxt - base) * min(0.9, hits / max(1, total))
    if status == "error":
        return {"percent": int(percent), "label": "转换失败"}
    return {"percent": int(percent), "label": label}


# ---------- helpers ----------

def slugify(value: str) -> str:
    text = re.sub(r"[^\w.-]+", "_", value.strip(), flags=re.UNICODE)
    return text.strip("._-") or "deck"


def job_summary(job: dict) -> dict:
    return {
        "id": job["id"],
        "name": job["name"],
        "status": job["status"],
        "created": job["created"],
        "files": job["files"],
        "pptx_name": job.get("pptx_name"),
        "error": job.get("error"),
        "saved": bool(job.get("saved_path")),
        "saved_count": job.get("saved_count", 0),
    }


def job_append_log(job: dict, line: str) -> None:
    with jobs_lock:
        job["log"].append(line)
        if len(job["log"]) > 2000:
            del job["log"][: len(job["log"]) - 2000]


def parse_multipart(body: bytes, boundary: bytes) -> list[tuple[dict, bytes]]:
    """极简 multipart 解析，返回 [(headers, content)]，保持原始顺序。"""
    parts: list[tuple[dict, bytes]] = []
    for chunk in body.split(b"--" + boundary)[1:]:
        if chunk.startswith(b"--"):
            break
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        if chunk.endswith(b"\r\n"):
            chunk = chunk[:-2]
        head, sep, content = chunk.partition(b"\r\n\r\n")
        if not sep:
            continue
        headers: dict[str, str] = {}
        for line in head.decode("latin-1").splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()
        parts.append((headers, content))
    return parts


def parse_disposition(value: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in value.split(";"):
        item = item.strip()
        if "=" in item:
            key, val = item.split("=", 1)
            out[key.strip().lower()] = val.strip().strip('"')
        else:
            out["type"] = item.lower()
    return out


# ---------- conversion worker ----------

def run_job(job_id: str) -> None:
    job = jobs[job_id]
    work_path = Path(str(job["work_path"]))
    log_path = work_path / "job.log"
    try:
        with jobs_lock:
            job["status"] = "running"
        persist_jobs()
        cmd = [
            sys.executable, str(ROOT / "scripts" / "convert.py"),
            "--source", str(job["src_path"]),
            "--work-dir", str(work_path),
        ]
        if job["options"].get("detect_tables"):
            cmd.append("--detect-tables")
        if job["options"].get("skip_render"):
            cmd.append("--skip-render")
        if job["options"].get("mode", "full") != "full":
            cmd += ["--mode", job["options"]["mode"]]
        env = os.environ.copy()
        # A Windows pipe defaults to a legacy code page (GBK on zh-CN) that
        # cannot encode the pipeline's emoji, which used to kill the child.
        # Force UTF-8 out of the child and decode the pipe as UTF-8 here.
        env.setdefault("PYTHONIOENCODING", "utf-8")
        if job["options"].get("ocr_token"):
            env["PADDLE_OCR_TOKEN"] = job["options"]["ocr_token"]

        job_append_log(job, "$ " + " ".join(cmd))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as lf:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                encoding="utf-8", errors="replace",
                cwd=str(ROOT), env=env, bufsize=1,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                lf.write(line)
                lf.flush()
                job_append_log(job, line.rstrip("\n"))
            rc = proc.wait()

        if rc != 0:
            raise RuntimeError(f"convert.py 退出码 {rc}，详见上方日志")

        pptx = work_path / "slides.pptx"
        if not pptx.is_file():
            raise RuntimeError("转换结束但未生成 slides.pptx，详见上方日志")

        with jobs_lock:
            job["status"] = "done"
            job["pptx_name"] = f"{slugify(job['name'])}.pptx"
            job["pptx_path"] = str(pptx)
        job_append_log(job, "完成，点页面上的“保存 PPTX”选择保存位置")
        persist_jobs()
    except Exception as exc:  # noqa: BLE001
        with jobs_lock:
            job["status"] = "error"
            job["error"] = str(exc)
        job_append_log(job, f"失败：{exc}")
        persist_jobs()


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "Image2PPT/1.0"

    def log_message(self, *args: object) -> None:  # 安静一点，只打关键行
        sys.stderr.write(" ".join(str(a) for a in args) + "\n")

    def _json(self, obj: object, status: int = 200) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _text(self, text: str, status: int = 200) -> None:
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(length or 1).decode("utf-8"))
        except ValueError:
            return {}

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            self._serve_file(WEB_DIR / "index.html")
        elif path == "/api/prefs":
            self._json(dict(PREFS))
        elif path == "/api/jobs":
            with jobs_lock:
                items = [job_summary(j) for j in sorted(
                    jobs.values(), key=lambda j: j["created"], reverse=True)[:20]]
            self._json({"jobs": items})
        elif path.startswith("/api/jobs/"):
            job_id = path[len("/api/jobs/"):]
            with jobs_lock:
                job = jobs.get(job_id)
                snapshot = None if job is None else {
                    **job_summary(job),
                    "log": job["log"],
                    "progress": compute_progress(job),
                    "saved_path": job.get("saved_path"),
                }
            if snapshot is None:
                self._json({"error": "任务不存在"}, 404)
            else:
                self._json(snapshot)
        elif path.startswith("/api/download/"):
            job_id = path[len("/api/download/"):]
            with jobs_lock:
                job = jobs.get(job_id)
                pptx_path = job.get("pptx_path") if job else None
                pptx_name = job.get("pptx_name", "slides.pptx") if job else "slides.pptx"
            if not pptx_path or not Path(pptx_path).is_file():
                self._json({"error": "文件不存在或任务尚未完成"}, 404)
                return
            raw = Path(pptx_path).read_bytes()
            self.send_response(200)
            self.send_header(
                "Content-Type",
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            )
            safe = pptx_name.encode("utf-8")
            self.send_header(
                "Content-Disposition",
                "attachment; filename*=UTF-8''" + "".join(
                    f"%{b:02X}" for b in safe),
            )
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        else:
            self._text("Not Found", 404)

    def do_POST(self) -> None:  # noqa: N802
        global PREFS
        parsed = urlparse(self.path)

        if parsed.path == "/api/prefs":
            data = self._read_json()
            prefs = dict(PREFS)
            for key in ("deck_name", "ocr_token", "last_dir"):
                if key in data:
                    prefs[key] = str(data[key])
            for key in ("detect_tables", "text_only"):
                if key in data:
                    prefs[key] = bool(data[key])
            PREFS = prefs
            store_prefs(prefs)
            self._json(dict(PREFS))
            return

        if parsed.path == "/api/save-as":
            self._handle_save_as()
            return

        if parsed.path == "/api/reveal":
            data = self._read_json()
            job_id = str(data.get("job_id", ""))
            kind = str(data.get("kind", "ppt"))
            with jobs_lock:
                job = jobs.get(job_id)
                saved_path = job.get("saved_path") if job else None
                work_path = job.get("work_path") if job else None
                src_path = job.get("src_path") if job else None
            if job is None:
                self._json({"error": "任务不存在"}, 404)
                return
            if kind == "src":
                target = src_path
            else:
                target = saved_path or str(Path(str(work_path)) / "slides.pptx")
            if not target or not Path(str(target)).exists():
                self._json({"error": "目录不存在，可能已被移动或删除"}, 404)
                return
            try:
                self._json(reveal_path(str(target)))
            except RuntimeError as exc:
                self._json({"error": str(exc)}, 500)
            return

        if parsed.path == "/api/convert":
            self._handle_convert()
            return
        self._text("Not Found", 404)

    def _handle_save_as(self) -> None:
        """弹出系统另存为对话框，把 PPT 复制到用户选择的位置。"""
        global PREFS
        data = self._read_json()
        job_id = str(data.get("job_id", ""))
        with jobs_lock:
            job = jobs.get(job_id)
            pptx_path = job.get("pptx_path") if job else None
            pptx_name = (job or {}).get("pptx_name") or "slides.pptx"
        if job is None or not pptx_path or not Path(pptx_path).is_file():
            self._json({"error": "PPT 尚未生成，无法保存"}, 404)
            return
        try:
            picked = pick_save_path(pptx_name, str(PREFS.get("last_dir") or ""))
        except RuntimeError as exc:
            self._json({"error": str(exc)}, 500)
            return
        if not picked:
            self._json({"canceled": True})
            return
        target = Path(picked).expanduser()
        if target.suffix.lower() != ".pptx":
            target = target.with_suffix(".pptx")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(pptx_path, target)
        except OSError as exc:
            self._json({"error": f"保存失败：{exc}"}, 500)
            return
        with jobs_lock:
            job["saved_path"] = str(target)
            job["saved_count"] = int(job.get("saved_count") or 0) + 1
        PREFS = {**PREFS, "last_dir": str(target.parent)}
        store_prefs(PREFS)
        persist_jobs()
        self._json({"saved_path": str(target), "folder": str(target.parent)})

    def _serve_file(self, path: Path) -> None:
        if not path.is_file():
            self._text("Not Found", 404)
            return
        raw = path.read_bytes()
        ctype, _ = mimetypes.guess_type(str(path))
        self.send_response(200)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _handle_convert(self) -> None:
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype or "boundary=" not in ctype:
            self._json({"error": "需要 multipart/form-data 上传"}, 400)
            return
        boundary = ctype.split("boundary=")[1].strip().strip('"').encode("latin-1")
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            self._json({"error": "上传内容为空或超过 300MB 上限"}, 400)
            return
        body = self.rfile.read(length)

        files: list[tuple[str, bytes]] = []
        fields: dict[str, str] = {}
        for headers, content in parse_multipart(body, boundary):
            disp = parse_disposition(headers.get("content-disposition", ""))
            name = disp.get("name", "")
            if disp.get("filename"):
                files.append((disp["filename"], content))
            else:
                fields[name] = content.decode("utf-8", "replace")

        if not files:
            self._json({"error": "请至少上传一张图片"}, 400)
            return
        exts = {Path(f).suffix.lower() for f, _ in files}
        if not exts <= SUPPORTED_EXTS:
            bad = sorted(exts - SUPPORTED_EXTS)
            self._json({"error": f"不支持的文件类型：{', '.join(bad)}（仅支持 PNG / JPG / WebP / BMP / TIFF / PDF）"}, 400)
            return
        if ".pdf" in exts and len(files) > 1:
            self._json({"error": "PDF 一次只能传一个文件，多图请用图片格式"}, 400)
            return

        job_id = uuid.uuid4().hex[:12]
        upload_dir = UPLOAD_ROOT / job_id
        work_dir = WORK_ROOT / job_id
        upload_dir.mkdir(parents=True, exist_ok=True)
        work_dir.mkdir(parents=True, exist_ok=True)

        # 按上传顺序编号为 page_01…，保证 PPT 页序就是拖入顺序；
        # 原图长期保留在后端，任务列表可随时打开图片文件夹。
        saved_names: list[str] = []
        if ".pdf" in exts:
            fname, content = files[0]
            safe = slugify(Path(fname).stem) + ".pdf"
            (upload_dir / safe).write_bytes(content)
            saved_names.append(safe)
        else:
            for i, (fname, content) in enumerate(files, 1):
                ext = Path(fname).suffix.lower()
                safe = f"page_{i:02d}{ext}"
                (upload_dir / safe).write_bytes(content)
                saved_names.append(f"{fname} → {safe}")

        options = {
            "ocr_token": fields.get("ocr_token", "").strip(),
            "detect_tables": fields.get("detect_tables") == "on",
            "skip_render": fields.get("skip_render") == "on",
            "mode": fields.get("mode") if fields.get("mode") in ("full", "text-only") else "full",
        }
        job = {
            "id": job_id,
            "name": fields.get("name", "").strip() or f"deck_{datetime.now():%Y%m%d_%H%M}",
            "status": "queued",
            "created": datetime.now().isoformat(timespec="seconds"),
            "files": saved_names,
            "log": [],
            "options": options,
            "src_path": str(upload_dir),
            "work_path": str(work_dir),
            "pptx_name": None,
            "pptx_path": None,
            "saved_path": None,
            "saved_count": 0,
            "error": None,
        }
        with jobs_lock:
            jobs[job_id] = job
        persist_jobs()
        threading.Thread(target=run_job, args=(job_id,), daemon=True).start()
        self._json({"job_id": job_id})


def main() -> None:
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    load_jobs()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"Image2PPT Web 已启动：{url}")
    print("拖图转换；完成后点“保存 PPTX 到…”自选保存位置，任务列表可打开文件夹。")
    print("按 Ctrl+C 停止服务。")
    if os.environ.get("NO_BROWSER") != "1":
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
