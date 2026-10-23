"""FastAPI 主服务：上传 / 分析任务 / 导出 / 页面图像"""
import os
import json
import asyncio
import uuid
import logging
import time
import shutil
import argparse
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()  # 须在 security 等读取环境变量的模块之前执行

from fastapi import FastAPI, UploadFile, HTTPException, Depends, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel

import parser
from parser import parse_pdf, build_chunks, ScannedPDFError, ParseError
from analyzer import analyze
from export import to_markdown, to_cards_csv, to_json
from qa import ask, compare, review
from anki_export import build_apkg
from security import (
    READER_API_TOKEN,
    AUTH_HEADER,
    check_token,
    validate_base_url,
    get_cors_origins,
    validate_startup,
)
from word_convert import is_word_filename, word_to_pdf, WordConvertError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# P0-5：状态持久化存储（替代内存 dict）
# ---------------------------------------------------------------------------

DEFAULT_MAX_TOTAL_GB = 2.0
DEFAULT_MAX_AGE_DAYS = 7.0
CLEANUP_INTERVAL_SECONDS = 3600  # 每小时检查一次

# P1-4：成本与页数上限
MAX_PAGES = int(os.environ.get("MAX_PAGES", "100") or "100")
MAX_TOTAL_TOKENS = int(os.environ.get("MAX_TOTAL_TOKENS", "200000") or "200000")
TOKEN_ESTIMATE_FACTOR = 1.3 / 4  # 字符数 ÷ 4 × 1.3


class StateStore:
    """用 JSON 文件做最小化持久化，替代 DOCS/JOBS/REPORTS 三个内存 dict。

    目录结构（每个 doc 一个目录）：
        data/<doc_id>/
            source.pdf
            pages/page_001.png ...
            doc.json        # 完整解析结果，用于任务恢复
            chunks.json     # chunk 元数据（P0-1 真实页码）
            report.json     # 最终报告
            meta.json       # 任务状态、last_accessed、统计
            errors.log      # 错误日志

    设计要点：
    - 每次状态变更即时写盘，报告量小，直接全量 JSON。
    - 文件级原子写：先写 .tmp，再 os.replace。
    - 启动时扫描 data/*/ 重建内存索引，kill 重启后可恢复。
    - 多 worker 友好：多个 uvicorn worker 挂载同一 data-dir 即可。
    """

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.docs: dict = {}
        self.jobs: dict = {}
        self.reports: dict = {}
        self.event_queues: dict[str, asyncio.Queue] = {}

    def publish_event(self, job_id: str, event_type: str, payload: dict):
        q = self.event_queues.get(job_id)
        if not q:
            return
        try:
            q.put_nowait({"type": event_type, **payload})
        except asyncio.QueueFull:
            # 队列满时丢弃最旧事件腾出空间，确保 done/error 等关键事件不被静默丢弃
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                q.put_nowait({"type": event_type, **payload})
            except asyncio.QueueFull:
                pass

    def doc_dir(self, doc_id: str) -> Path:
        """拒绝 .. 等非法路径。"""
        d = self.data_dir / doc_id
        try:
            d.relative_to(self.data_dir)
        except ValueError:
            raise HTTPException(400, "非法 doc_id")
        return d

    def _atomic_write_json(self, path: Path, obj: dict):
        """原子写入 JSON：tmpfile + os.replace。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(str(tmp), str(path))

    # ---------------- 持久化 API ----------------

    def save_doc(self, doc_id: str, doc: dict):
        self.docs[doc_id] = doc
        # F18：落盘前剥离每页的 layout blocks（解析后不再使用），显著减小 doc.json 体积；
        # 真实页码、章节、页码文本（page_texts）均保留，以维持重启恢复与 chunk 重建能力。
        slim = dict(doc)
        pages = slim.get("pages")
        if isinstance(pages, list):
            slim["pages"] = [{k: v for k, v in p.items() if k != "blocks"} for p in pages]
        self._atomic_write_json(self.doc_dir(doc_id) / "doc.json", slim)

    def save_chunks(self, doc_id: str, chunks: list) -> list:
        self._atomic_write_json(self.doc_dir(doc_id) / "chunks.json", chunks)
        return chunks

    def save_job(self, job_id: str, job: dict):
        """任务状态变更即时落盘到 meta.json。"""
        self.jobs[job_id] = job
        doc_id = job.get("doc_id")
        if not doc_id:
            return
        meta = self._load_meta(doc_id)
        meta["job"] = job
        meta["last_accessed"] = time.time()
        self._atomic_write_json(self.doc_dir(doc_id) / "meta.json", meta)

    def save_report(self, doc_id: str, report: dict):
        """报告生成后落盘 report.json，并更新 meta。"""
        self.reports[doc_id] = report
        self._atomic_write_json(self.doc_dir(doc_id) / "report.json", report)
        meta = self._load_meta(doc_id)
        meta["report_ready"] = True
        meta["last_accessed"] = time.time()
        self._atomic_write_json(self.doc_dir(doc_id) / "meta.json", meta)

    def touch_doc(self, doc_id: str):
        """访问文档时刷新 last_accessed。"""
        meta = self._load_meta(doc_id)
        meta["last_accessed"] = time.time()
        self._atomic_write_json(self.doc_dir(doc_id) / "meta.json", meta)

    def _load_meta(self, doc_id: str) -> dict:
        p = self.doc_dir(doc_id) / "meta.json"
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _load_json(self, path: Path) -> dict | None:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return None
        return None

    # ---------------- 启动恢复 ----------------

    def restore(self):
        """从 data/ 扫描重建 docs / jobs / reports。"""
        if not self.data_dir.exists():
            return
        for d in self.data_dir.iterdir():
            if not d.is_dir():
                continue
            doc_id = d.name

            # 恢复 doc（解析结果）
            doc = self._load_json(d / "doc.json")
            if doc is not None:
                self.docs[doc_id] = doc

            # 恢复报告
            report = self._load_json(d / "report.json")
            if report is not None:
                self.reports[doc_id] = report

            # 恢复任务状态
            meta = self._load_json(d / "meta.json") or {}
            job = meta.get("job")
            if isinstance(job, dict) and job.get("job_id"):
                # 如果重启前任务在 running，诚实标记为中断，避免僵尸任务
                if job.get("status") == "running":
                    job["status"] = "interrupted"
                    job["message"] = "后端重启，任务中断，请重新分析"
                    job["error"] = "server_restarted"
                self.jobs[job.get("job_id")] = job

    # ---------------- LRU 清理 ----------------

    def cleanup(self, max_total_gb: float = DEFAULT_MAX_TOTAL_GB,
                max_age_days: float = DEFAULT_MAX_AGE_DAYS) -> dict:
        """按总容量上限 + 单文档超龄清理最老文档；跳过有 running 任务的目录。"""
        max_total_bytes = max_total_gb * 1024 * 1024 * 1024
        max_age_seconds = max_age_days * 24 * 3600
        now = time.time()

        running_doc_ids = {
            j.get("doc_id") for j in self.jobs.values()
            if j.get("status") == "running" and j.get("doc_id")
        }

        candidates = []
        total_bytes = 0
        for d in self.data_dir.iterdir():
            if not d.is_dir():
                continue
            if d.name in running_doc_ids:
                continue
            size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            total_bytes += size
            meta = self._load_meta(d.name) or {}
            last_accessed = meta.get("last_accessed") or d.stat().st_mtime
            candidates.append({"dir": d, "last_accessed": last_accessed, "size": size})

        removed = []

        # 第一步：删除超过年龄的
        for c in candidates[:]:
            if now - c["last_accessed"] > max_age_seconds:
                shutil.rmtree(c["dir"], ignore_errors=True)
                removed.append({"doc_id": c["dir"].name, "reason": "age", "size": c["size"]})
                total_bytes -= c["size"]
                candidates.remove(c)

        # 第二步：如果总容量仍超限，按 last_accessed 删除最老的
        candidates.sort(key=lambda x: x["last_accessed"])
        while total_bytes > max_total_bytes and candidates:
            c = candidates.pop(0)
            shutil.rmtree(c["dir"], ignore_errors=True)
            removed.append({"doc_id": c["dir"].name, "reason": "lru", "size": c["size"]})
            total_bytes -= c["size"]

        # 同步内存索引：移除已经不存在的 doc/job/report
        existing = {d.name for d in self.data_dir.iterdir() if d.is_dir()}
        for doc_id in list(self.docs.keys()):
            if doc_id not in existing:
                self.docs.pop(doc_id, None)
                self.reports.pop(doc_id, None)
        for job_id, job in list(self.jobs.items()):
            if job.get("doc_id") not in existing:
                self.jobs.pop(job_id, None)

        return {
            "total_bytes_after": total_bytes,
            "removed_count": len(removed),
            "removed": removed,
        }


# ---------------------------------------------------------------------------
# 命令行参数与数据目录初始化
# ---------------------------------------------------------------------------

_arg_parser = argparse.ArgumentParser()
_arg_parser.add_argument("--data-dir", default=None, help="数据目录路径（多 worker 共享）")
_arg_parser.add_argument("--max-total-gb", type=float, default=DEFAULT_MAX_TOTAL_GB,
                         help="data/ 总容量上限（GB）")
_arg_parser.add_argument("--max-age-days", type=float, default=DEFAULT_MAX_AGE_DAYS,
                         help="单文档最大保留天数")
_cli_args, _ = _arg_parser.parse_known_args()

if _cli_args.data_dir:
    parser.DATA_DIR = Path(_cli_args.data_dir)
DATA_DIR = parser.DATA_DIR

store = StateStore(DATA_DIR)
store.restore()


# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------

app = FastAPI(title="文献速读 Agent")
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_cors_origins(),
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    allow_credentials=True,
)


# ---------- 启动检查 ----------
@app.on_event("startup")
async def _startup():
    # 显式环境判定：READER_ENV=production 时必须设置 READER_API_TOKEN，
    # 不再依赖 uvicorn 并不设置的 UVICORN_HOST 变量（F9）。
    env = os.environ.get("READER_ENV", "development")
    validate_startup(env)

    # P0-5：启动后台 LRU 清理任务
    async def cleanup_loop():
        while True:
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
            try:
                store.cleanup(max_total_gb=_cli_args.max_total_gb,
                              max_age_days=_cli_args.max_age_days)
            except Exception:
                logger.exception("LRU cleanup failed")
    asyncio.create_task(cleanup_loop())


# ---------- 统一异常响应（隐藏内部细节） ----------
@app.exception_handler(Exception)
async def _global_exception_handler(request: Request, exc: Exception):
    logger.exception("未捕获异常：%s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error": "internal_error", "message": "服务器内部错误，请查看后端日志"},
    )


@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException):
    """统一 HTTPException 响应格式，隐藏堆栈等内部信息。"""
    detail = exc.detail
    if isinstance(detail, dict) and "error" in detail and "message" in detail:
        content = detail
    elif isinstance(detail, str):
        content = {"error": "request_error", "message": detail}
    else:
        content = {"error": "request_error", "message": str(detail)}
    return JSONResponse(status_code=exc.status_code, content=content, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(request: Request, exc: RequestValidationError):
    logger.warning("请求参数校验失败：%s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=422,
        content={"error": "validation_error", "message": "请求参数格式错误"},
    )


# ===== DeepSeek 配置 =====
# 密钥必须通过环境变量 OPENAI_API_KEY 提供，不要硬编码到代码里。
# 可选环境变量：OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL
_DEEPSEEK_KEY = ""
_DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
_DEEPSEEK_MODEL = "deepseek-v4-pro"

# P1-2：API Key 优先从服务端环境变量读取，前端不再接触
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "") or _DEEPSEEK_KEY
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "") or _DEEPSEEK_BASE_URL
DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "") or _DEEPSEEK_MODEL

# ---------- 鉴权依赖 ----------
async def sse_token(request: Request, token: str = ""):
    """SSE 无法自定义请求头，允许从 query 参数传 token。"""
    header_token = request.headers.get(AUTH_HEADER)
    if not check_token(header_token or token):
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthorized", "message": "缺少或错误的访问令牌"},
            headers={"WWW-Authenticate": f"Token header={AUTH_HEADER}"},
        )

async def require_token(request: Request):
    token = request.headers.get(AUTH_HEADER)
    if not check_token(token):
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthorized", "message": "缺少或错误的访问令牌"},
            headers={"WWW-Authenticate": f"Token header={AUTH_HEADER}"},
        )


def _save_meta(doc_id: str, report: dict) -> None:
    """把质量统计（P0-2 图表来源/回验通过率等）写入 data/<doc_id>/meta.json。"""
    meta = store._load_meta(doc_id)
    meta["chart_stats"] = report.get("chart_stats", {})
    meta["critic_stats"] = report.get("critic_stats", {})
    store._atomic_write_json(store.doc_dir(doc_id) / "meta.json", meta)




# P2-3：Q&A / 对比 / 综述请求模型
class AskReq(BaseModel):
    question: str
    top_k: int = 5
    model: str = DEFAULT_MODEL
    api_key: str = ""   # 可选：每次请求覆盖服务端 env key（F14）
    base_url: str = ""  # 可选：每次请求覆盖服务端 env base_url


class CompareReq(BaseModel):
    doc_ids: list[str]
    model: str = DEFAULT_MODEL
    api_key: str = ""
    base_url: str = ""


class ReviewReq(BaseModel):
    doc_ids: list[str]
    model: str = DEFAULT_MODEL
    api_key: str = ""
    base_url: str = ""


class AnalyzeReq(BaseModel):
    doc_id: str
    # 注意：API Key 仅从服务端环境变量 OPENAI_API_KEY 读取（P1-2 前端零接触），
    # 不再接受客户端上传的 api_key。
    base_url: str = ""
    model: str = DEFAULT_MODEL


def _save_chunks(doc_id: str, doc: dict) -> list:
    """把 chunk 元数据（含真实页码区间）落盘到 data/<doc_id>/chunks.json，供核查与 Q&A 复用。"""
    chunks = doc.get("chunks") or build_chunks(doc.get("sections", []))
    store.save_chunks(doc_id, chunks)
    return chunks


# ---------------------------------------------------------------------------
# API 端点
# ---------------------------------------------------------------------------

@app.get("/healthz")
async def healthz():
    """Render / Railway 等平台健康检查用;不鉴权、无副作用。"""
    return {"status": "ok"}


@app.get("/")
async def root():
    return {"name": "文献速读 Agent", "docs": "/docs", "health": "/healthz"}


@app.post("/api/upload")
async def upload(file: UploadFile, _=Depends(require_token)):
    filename = file.filename or ""
    name_lower = filename.lower()
    if name_lower.endswith(".pdf"):
        source_kind = "pdf"
        saved_name = "source.pdf"
    elif is_word_filename(name_lower):
        source_kind = "word"
        saved_name = "source" + Path(name_lower).suffix  # source.docx / source.doc
    else:
        raise HTTPException(400, detail={"error": "invalid_file",
                                         "message": "只支持 PDF 或 Word（.docx / .doc）文件"})
    doc_id = uuid.uuid4().hex[:12]
    raw = await file.read()
    if len(raw) > 50 * 1024 * 1024:
        raise HTTPException(400, detail={"error": "file_too_large", "message": "文件超过 50MB"})
    fpath = store.doc_dir(doc_id) / saved_name
    fpath.parent.mkdir(parents=True, exist_ok=True)
    fpath.write_bytes(raw)

    # Word 先统一转 PDF，再走原有解析管线（真实页码 / 页面图像 / 图表提取均不受影响）
    pdf_path = store.doc_dir(doc_id) / "source.pdf"
    if source_kind == "word":
        try:
            await asyncio.to_thread(word_to_pdf, str(fpath), str(pdf_path))
        except WordConvertError as e:
            shutil.rmtree(store.doc_dir(doc_id), ignore_errors=True)
            return JSONResponse(
                status_code=422,
                content={"error": "word_convert_failed", "message": str(e)},
            )
        except Exception:
            logger.exception("Word 转 PDF 失败")
            shutil.rmtree(store.doc_dir(doc_id), ignore_errors=True)
            raise HTTPException(422, detail={"error": "word_convert_failed",
                                             "message": "Word 转 PDF 失败，请另存为 PDF 后再上传"})
    try:
        doc = parse_pdf(str(pdf_path), doc_id)
    except ScannedPDFError as e:
        return JSONResponse(
            status_code=422,
            content={"error": "scanned_pdf", "message": str(e)},
        )
    except ParseError as e:
        return JSONResponse(
            status_code=422,
            content={"error": "parse_failed", "message": str(e)},
        )
    except Exception:
        logger.exception("PDF 解析失败")
        raise HTTPException(422, detail={"error": "parse_failed", "message": "PDF 解析失败"})
    # F11：页数上限前移到上传阶段，避免先渲染几百张 PNG 才告诉你不行
    if doc.get("n_pages", 0) > MAX_PAGES:
        shutil.rmtree(store.doc_dir(doc_id), ignore_errors=True)
        raise HTTPException(413, detail={"error": "too_many_pages",
            "message": f"PDF 共 {doc['n_pages']} 页，超过服务端限制 {MAX_PAGES} 页，上传被拒绝"})
    doc["chunks"] = _save_chunks(doc_id, doc)
    store.save_doc(doc_id, doc)
    store._atomic_write_json(
        store.doc_dir(doc_id) / "meta.json",
        {"created": time.time(), "last_accessed": time.time(), "filename": file.filename},
    )
    return {
        "doc_id": doc_id, "title": doc["title"], "author": doc["author"],
        "n_pages": doc["n_pages"], "source_kind": source_kind,
        "sections": [{"title": s["title"], "level": s["level"],
                      "page_start": s["page_start"], "page_end": s["page_end"]}
                     for s in doc["sections"]],
    }


@app.post("/api/analyze")
async def start_analyze(req: AnalyzeReq, _=Depends(require_token)):
    # P1-2：服务端读取 API Key，前端不再上传（F22 已移除客户端 api_key 字段）
    effective_key = (OPENAI_API_KEY or "demo").strip()
    effective_base_url = (req.base_url or OPENAI_BASE_URL or "").strip()
    # P1-1：SSRF 防护，校验 base_url
    err = validate_base_url(effective_base_url)
    if err:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_base_url", "message": err},
        )

    doc = store.docs.get(req.doc_id)
    if not doc:
        raise HTTPException(404, detail={"error": "not_found", "message": "文档不存在，请重新上传"})
    if doc.get("n_pages", 0) > MAX_PAGES:
        raise HTTPException(413, detail={"error": "too_many_pages", "message": f"PDF 共 {doc['n_pages']} 页，超过服务端限制 {MAX_PAGES} 页"})
    chunks = doc.get("chunks") or []
    total_chars = sum(len(c.get("text", "")) for c in chunks)
    estimated_tokens = int(total_chars * TOKEN_ESTIMATE_FACTOR)
    if estimated_tokens > MAX_TOTAL_TOKENS:
        raise HTTPException(413, detail={"error": "too_many_tokens", "message": f"预估输入 token {estimated_tokens}，超过限制 {MAX_TOTAL_TOKENS}"})
    if not chunks and not doc.get("sections"):
        raise HTTPException(422, detail={"error": "empty_document", "message": "文档解析结果为空，无法分析"})
    job_id = uuid.uuid4().hex[:12]
    job = {"job_id": job_id, "status": "running", "progress": 0, "message": "开始分析…",
           "result": None, "error": None, "doc_id": req.doc_id}
    store.save_job(job_id, job)
    store.event_queues[job_id] = asyncio.Queue(maxsize=128)

    async def progress_cb(msg, pct):
        job["message"] = msg
        job["progress"] = pct
        store.save_job(job_id, job)

    async def event_cb(event_type, payload):
        store.publish_event(job_id, event_type, payload)

    errors_path = store.doc_dir(req.doc_id) / "errors.log"

    def error_sink(line: str):
        with errors_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    async def run():
        try:
            report = await analyze(doc, effective_key, effective_base_url, req.model, progress_cb, error_sink=error_sink, event_cb=event_cb)
            store.save_report(req.doc_id, report)
            _save_meta(req.doc_id, report)  # P0-2：图表回验统计落盘
            job.update(status="done", progress=100, result=report)
            store.save_job(job_id, job)
            store.publish_event(job_id, "done", {"job_id": job_id, "report": report})
        except Exception as e:
            logger.exception("分析任务失败")
            err_msg = str(e) if str(e) else "分析任务失败，请查看后端日志"
            job.update(status="error", error=err_msg)
            store.save_job(job_id, job)
            store.publish_event(job_id, "error", {"job_id": job_id, "error": err_msg})
        finally:
            store.event_queues.pop(job_id, None)

    asyncio.create_task(run())
    return {"job_id": job_id}




@app.post("/api/docs/{doc_id}/ask")
async def doc_ask(doc_id: str, req: AskReq, _=Depends(require_token)):
    result = await ask(doc_id, req.question,
                       req.api_key or OPENAI_API_KEY, req.base_url or OPENAI_BASE_URL,
                       req.model,
                       top_k=req.top_k)
    if "error" in result:
        raise HTTPException(400, detail={"error": result["error"], "message": result["message"]})
    store.touch_doc(doc_id)
    return result


@app.post("/api/compare")
async def docs_compare(req: CompareReq, _=Depends(require_token)):
    result = await compare(req.doc_ids, req.api_key or OPENAI_API_KEY,
                           req.base_url or OPENAI_BASE_URL, req.model)
    if "error" in result:
        raise HTTPException(400, detail={"error": result["error"], "message": result["message"]})
    for did in req.doc_ids:
        store.touch_doc(did)
    return result


@app.post("/api/review")
async def docs_review(req: ReviewReq, _=Depends(require_token)):
    result = await review(req.doc_ids, req.api_key or OPENAI_API_KEY,
                          req.base_url or OPENAI_BASE_URL, req.model)
    if "error" in result:
        raise HTTPException(400, detail={"error": result["error"], "message": result["message"]})
    for did in req.doc_ids:
        store.touch_doc(did)
    return result

@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str, _=Depends(require_token)):
    j = store.jobs.get(job_id)
    if not j:
        raise HTTPException(404, detail={"error": "not_found", "message": "任务不存在"})
    return j




@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str, token: str = "", _=Depends(sse_token)):
    """SSE 流式推送分析进度与中间结果；断开后前端可回退到轮询。"""
    q = store.event_queues.get(job_id)
    if not q:
        # 任务已结束或不存在，直接返回一条 done/error 后关闭（便于恢复）
        j = store.jobs.get(job_id)
        status = j.get("status") if j else None
        async def once():
            if status == "done" and j.get("result"):
                yield _sse_event("done", {"job_id": job_id, "report": j["result"]})
            elif status == "error":
                yield _sse_event("error", {"job_id": job_id, "error": j.get("error", "")})
            else:
                yield _sse_event("error", {"job_id": job_id, "error": "任务不存在或已过期"})
        return StreamingResponse(once(), media_type="text/event-stream")

    async def gen():
        while True:
            event = await q.get()
            yield _sse_event(event["type"], event)
            if event["type"] in ("done", "error"):
                break

    return StreamingResponse(gen(), media_type="text/event-stream")


def _sse_event(event_type: str, payload: dict):
    data = {"type": event_type, **payload}
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")

@app.get("/api/docs/{doc_id}/chunks")
async def get_chunks(doc_id: str, _=Depends(require_token)):
    p = store.doc_dir(doc_id) / "chunks.json"
    if not p.exists():
        raise HTTPException(404, detail={"error": "not_found", "message": "chunks 不存在"})
    store.touch_doc(doc_id)
    return json.loads(p.read_text(encoding="utf-8"))




# P1-4：分析前 token / 页数 / chunk 预估
class EstimateResp(BaseModel):
    n_pages: int
    n_sections: int
    n_chunks: int
    estimated_tokens: int
    skipped_sections: list[str]
    max_pages: int
    max_total_tokens: int


@app.get("/api/docs/{doc_id}/estimate")
async def estimate_cost(doc_id: str, _=Depends(require_token)):
    doc = store.docs.get(doc_id)
    if not doc:
        raise HTTPException(404, detail={"error": "not_found", "message": "文档不存在"})
    chunks = doc.get("chunks") or []
    total_chars = sum(len(c.get("text", "")) for c in chunks)
    estimated_tokens = int(total_chars * TOKEN_ESTIMATE_FACTOR)
    return EstimateResp(
        n_pages=doc.get("n_pages", 0),
        n_sections=len(doc.get("sections", [])),
        n_chunks=len(chunks),
        estimated_tokens=estimated_tokens,
        skipped_sections=doc.get("skipped_sections", []),
        max_pages=MAX_PAGES,
        max_total_tokens=MAX_TOTAL_TOKENS,
    )

@app.get("/api/pages/{doc_id}/{page}")
async def page_image(doc_id: str, page: int, _=Depends(require_token)):
    p = store.doc_dir(doc_id) / "pages" / f"{page}.png"
    if not p.exists():
        raise HTTPException(404, detail={"error": "not_found", "message": "页面不存在"})
    store.touch_doc(doc_id)
    return FileResponse(str(p), media_type="image/png")


@app.get("/api/export/{doc_id}/report.md")
async def export_md(doc_id: str, _=Depends(require_token)):
    r = store.reports.get(doc_id)
    if not r:
        raise HTTPException(404, detail={"error": "not_found", "message": "报告不存在"})
    store.touch_doc(doc_id)
    return JSONResponse({"filename": f"{r['title'][:40]}-速读报告.md",
                         "content": to_markdown(r)})


@app.get("/api/export/{doc_id}/cards.csv")
async def export_csv(doc_id: str, _=Depends(require_token)):
    r = store.reports.get(doc_id)
    if not r:
        raise HTTPException(404, detail={"error": "not_found", "message": "报告不存在"})
    store.touch_doc(doc_id)
    return JSONResponse({"filename": f"{r['title'][:40]}-知识卡片.csv",
                         "content": to_cards_csv(r)})


@app.get("/api/export/{doc_id}/report.json")
async def export_json(doc_id: str, _=Depends(require_token)):
    r = store.reports.get(doc_id)
    if not r:
        raise HTTPException(404, detail={"error": "not_found", "message": "报告不存在"})
    store.touch_doc(doc_id)
    return JSONResponse({"filename": f"{r['title'][:40]}-速读报告.json",
                         "content": to_json(r)})




@app.get("/api/export/{doc_id}/cards.apkg")
async def export_apkg(doc_id: str, _=Depends(require_token)):
    r = store.reports.get(doc_id)
    if not r:
        raise HTTPException(404, detail={"error": "not_found", "message": "报告不存在"})
    store.touch_doc(doc_id)
    out = store.doc_dir(doc_id) / "cards.apkg"
    build_apkg(r, doc_id, out)
    return FileResponse(str(out), media_type="application/octet-stream",
                        filename=f"{r['title'][:40]}-记忆卡片.apkg")

@app.get("/api/source/{doc_id}")
async def source_pdf(doc_id: str, _=Depends(require_token)):
    p = store.doc_dir(doc_id) / "source.pdf"
    if not p.exists():
        raise HTTPException(404, detail={"error": "not_found", "message": "源文件不存在"})
    store.touch_doc(doc_id)
    return FileResponse(str(p), media_type="application/pdf",
                        filename=f"{doc_id}.pdf")


@app.post("/api/admin/cleanup")
async def admin_cleanup(_=Depends(require_token)):
    """手动触发 LRU 清理（调试用）。删除类操作使用 POST 而非 GET（F21）。"""
    return store.cleanup(max_total_gb=_cli_args.max_total_gb,
                         max_age_days=_cli_args.max_age_days)


if __name__ == "__main__":
    import uvicorn
    # F20：关闭 access 日志，避免 SSE 的 token（出现在 query 中）被明文记录到日志。
    # 注意：SSE 鉴权 token 仍经由 query 传递（浏览器 EventSource 无法自定义头），
    # 生产部署建议配合反向代理将 token 从日志中剥离。
    uvicorn.run(app, host="127.0.0.1", port=8321, access_log=False)
