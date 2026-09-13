"""FastAPI service wrapping the RAG workflow (rag_core) for the local web UI.

Run from the rag/ directory:  uvicorn api.app:app --port 8100
(Path anchors come from rag_core, so any CWD works; single worker only — the
job registry and its worker thread live in this process.)
"""

import asyncio
import functools
import json
import os
import queue
import re
import shutil
import sys
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from qdrant_client.models import FieldCondition, Filter, MatchValue

import rag_core
from api.jobs import JobManager

STAGE_DIRS = {"inbox": rag_core.PDF_INBOX_DIR, "review": rag_core.MD_REVIEW_DIR, "docs": rag_core.DOCS_DIR}
PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
HIDDEN_SUFFIXES = rag_core.SIDECAR_SUFFIXES  # single source: rag_core (+ flatten_md_tables)
SHEET_EXTS = rag_core.SHEET_EXTS  # single source: rag_core (.xlsx/.xlsm/.csv)
CONVERTIBLE_EXTS = (".pdf", *SHEET_EXTS)  # inbox extensions a convert job may target

job_manager = JobManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    for d in STAGE_DIRS.values():
        os.makedirs(d, exist_ok=True)
    yield


app = FastAPI(title="RAG Workflow API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:3100",
        "http://127.0.0.1:3100",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- helpers ---


def check_project_id(project_id: str) -> str:
    if not PROJECT_ID_RE.fullmatch(project_id or ""):
        raise HTTPException(
            400,
            f"Invalid project id {project_id!r}: allowed characters are letters, digits, '.', '_', '-'; must not start with a dot",
        )
    return project_id


def safe_file_path(base_dir: str, project_id: str, name: str) -> Path:
    """Canonicalize a (project_id, name) pair inside base_dir; reject anything that escapes it."""
    if not name or "/" in name or "\\" in name or name in (".", "..") or name.startswith("."):
        raise HTTPException(400, f"Invalid file name: {name!r}")
    base = Path(rag_core.project_dir(base_dir, project_id)).resolve()
    target = (base / name).resolve()
    if target != base and base not in target.parents:
        raise HTTPException(400, "Path escapes the project directory")
    return target


def visible_file(path: Path) -> bool:
    return path.is_file() and not path.name.startswith(".") and not path.name.endswith(HIDDEN_SUFFIXES)


def chunk_count(project_id: str, name: str):
    try:
        resp = rag_core.qdrant.count(
            rag_core.COLLECTION_NAME,
            exact=True,
            count_filter=Filter(must=[
                FieldCondition(key="project_id", match=MatchValue(value=project_id)),
                FieldCondition(key="source", match=MatchValue(value=name)),
            ]),
        )
        return resp.count
    except Exception:
        return None


def list_stage_files(stage: str, project_id: str):
    folder = Path(rag_core.project_dir(STAGE_DIRS[stage], project_id))
    if not folder.is_dir():
        return []
    return sorted((p for p in folder.iterdir() if visible_file(p)), key=lambda p: p.name)


# --- health ---


@app.get("/api/health")
def health():
    try:
        rag_core.qdrant.get_collections()
        qdrant_status = "ok"
    except Exception as e:
        qdrant_status = f"down: {e}"
    try:
        req = urllib.request.Request(f"{rag_core.OLLAMA_HOST}/api/tags")
        with urllib.request.urlopen(req, timeout=3):
            ollama_status = "ok"
    except (urllib.error.URLError, OSError) as e:
        ollama_status = f"down: {e}"
    return {"qdrant": qdrant_status, "ollama": ollama_status}


# --- projects ---


class ProjectCreate(BaseModel):
    project_id: str


@app.get("/api/projects")
def list_projects():
    projects = []
    for pid in rag_core.list_projects():
        counts = {stage: len(list_stage_files(stage, pid)) for stage in STAGE_DIRS}
        projects.append({"project_id": pid, "counts": counts})
    return {"projects": projects}


@app.post("/api/projects", status_code=201)
def create_project(body: ProjectCreate):
    pid = check_project_id(body.project_id)
    if pid == "global":
        raise HTTPException(400, "'global' is a virtual scope (root-level files); it cannot be created or deleted")
    if pid in rag_core.list_projects():
        raise HTTPException(409, f"Project already exists: {pid}")
    for base in STAGE_DIRS.values():
        os.makedirs(rag_core.project_dir(base, pid), exist_ok=True)
    return {"project_id": pid}


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str):
    pid = check_project_id(project_id)
    if pid == "global":
        raise HTTPException(400, "The global scope cannot be deleted; delete its files individually")
    if pid not in rag_core.list_projects():
        raise HTTPException(404, f"Project not found: {pid}")
    # Delete the vector points first: if Qdrant is unreachable the folders survive and the
    # delete can simply be retried (the project stays visible); the reverse order would
    # orphan the points with no UI path left to remove them.
    rag_core.delete_points(project_id=pid)
    for base in STAGE_DIRS.values():
        folder = Path(rag_core.project_dir(base, pid))
        if folder.is_dir():
            shutil.rmtree(folder)
    return {"deleted": pid}


# --- documents ---


@app.get("/api/projects/{project_id}/documents")
def project_documents(project_id: str):
    pid = check_project_id(project_id)
    items = []
    for stage in STAGE_DIRS:
        for path in list_stage_files(stage, pid):
            ext = os.path.splitext(path.name)[1].lower()
            kind = {"pdf": "pdf", "md": "md", "txt": "txt"}.get(ext.lstrip("."), "sheet" if ext in SHEET_EXTS else "other")
            st = path.stat()
            item = {
                "name": path.name,
                "stage": stage,
                "kind": kind,
                "size": st.st_size,
                "mtime": st.st_mtime,
            }
            if stage == "inbox" and kind in ("pdf", "sheet"):
                # mirrors convert_project_pdf/convert_project_sheet's finalized-md skip rule, so the
                # UI can explain up front why clicking Convert on this file would be a no-op
                item["already_approved"] = rag_core.finalized_md_path(pid, path.name) is not None
            if stage == "review":
                sidecar = path.with_name(path.name + ".progress")
                if sidecar.exists():
                    done_pages = None
                    try:
                        done_pages = int(sidecar.read_text(encoding="utf-8").strip().split(",")[0])
                    except (ValueError, OSError):
                        pass
                    total_pages = None
                    pdf = Path(rag_core.project_dir(rag_core.PDF_INBOX_DIR, pid)) / (os.path.splitext(path.name)[0] + ".pdf")
                    if done_pages is not None and pdf.exists():
                        try:
                            total_pages = rag_core.get_pdf_page_count(str(pdf))
                        except Exception:
                            pass
                    item["convert_progress"] = {"done_pages": done_pages, "total_pages": total_pages}
            if stage == "docs" and kind in ("md", "txt"):
                item["chunk_count"] = chunk_count(pid, path.name)
            items.append(item)
    return {"project_id": pid, "documents": items}


@app.post("/api/projects/{project_id}/upload")
async def upload(project_id: str, files: list[UploadFile] = File(...), overwrite: int = Query(0)):
    pid = check_project_id(project_id)
    saved, errors = [], []
    for f in files:
        name = os.path.basename(f.filename or "")
        try:
            dest = _route_upload(pid, name)
        except HTTPException as e:
            errors.append({"name": name, "error": e.detail, "conflict": e.status_code == 409})
            continue
        if dest.exists() and not overwrite:
            errors.append({"name": name, "error": f"already exists in {pid}", "conflict": True})
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(await f.read())
        saved.append({"name": name, "stage": "inbox" if name.lower().endswith(CONVERTIBLE_EXTS) else "docs"})
    if errors and not saved:
        if all(e.get("conflict") for e in errors):
            raise HTTPException(409, "; ".join(e["error"] for e in errors))
        raise HTTPException(400, "; ".join(e["error"] for e in errors))
    return {"saved": saved, "errors": errors}


def _route_upload(pid: str, name: str) -> Path:
    if not name or name.startswith(".") or name.endswith(HIDDEN_SUFFIXES):
        raise HTTPException(400, f"Invalid file name: {name!r}")
    ext = os.path.splitext(name)[1].lower()
    if ext in CONVERTIBLE_EXTS:
        stage = "inbox"
    elif ext in (".md", ".txt"):
        stage = "docs"
    elif ext in (".xls", ".ods"):
        raise HTTPException(400, f"{name!r}: {ext} is not supported (old binary format); save it as .xlsx first")
    else:
        raise HTTPException(
            400, f"Unsupported file type {name!r} (allowed: .pdf, .xlsx, .xlsm, .csv, .md, .txt)"
        )
    return safe_file_path(STAGE_DIRS[stage], pid, name)


@app.delete("/api/projects/{project_id}/documents/{stage}/{name}")
def delete_document(project_id: str, stage: str, name: str):
    pid = check_project_id(project_id)
    if stage not in STAGE_DIRS:
        raise HTTPException(400, f"Invalid stage: {stage} (expected one of {sorted(STAGE_DIRS)})")
    if stage == "docs" and job_manager.blocks_document_delete(pid, name):
        raise HTTPException(409, "An ingest job is targeting this document; wait for it to finish before deleting")
    path = safe_file_path(STAGE_DIRS[stage], pid, name)
    if not path.is_file():
        raise HTTPException(404, f"File not found: {stage}/{name}")
    path.unlink()
    for suffix in HIDDEN_SUFFIXES:
        sidecar = path.with_name(name + suffix)
        if sidecar.exists():
            sidecar.unlink()
    if stage == "docs":
        rag_core.delete_points(project_id=pid, source=name)
    return {"deleted": name, "stage": stage}


# --- review editing / approval ---


class ReviewUpdate(BaseModel):
    content: str


@app.get("/api/projects/{project_id}/review/{name}")
def get_review(project_id: str, name: str):
    pid = check_project_id(project_id)
    path = safe_file_path(rag_core.MD_REVIEW_DIR, pid, name)
    if not path.is_file():
        raise HTTPException(404, f"Review file not found: {name}")
    return {"project_id": pid, "name": name, "content": path.read_text(encoding="utf-8")}


@app.put("/api/projects/{project_id}/review/{name}")
def put_review(project_id: str, name: str, body: ReviewUpdate):
    pid = check_project_id(project_id)
    if job_manager.blocks_review_edit(pid, name):
        raise HTTPException(409, "A conversion job is targeting this file; wait for it to finish before editing")
    path = safe_file_path(rag_core.MD_REVIEW_DIR, pid, name)
    if not path.is_file():
        raise HTTPException(404, f"Review file not found: {name}")
    path.write_text(body.content, encoding="utf-8")
    return {"saved": name, "size": len(body.content.encode("utf-8"))}


@app.get("/api/projects/{project_id}/docs/{name}")
def get_doc_content(project_id: str, name: str):
    pid = check_project_id(project_id)
    path = safe_file_path(rag_core.DOCS_DIR, pid, name)
    if not path.is_file():
        raise HTTPException(404, f"Document not found: {name}")
    return {"project_id": pid, "name": name, "content": path.read_text(encoding="utf-8")}


@app.post("/api/projects/{project_id}/approve/{name}")
def approve(project_id: str, name: str, overwrite: int = Query(0)):
    pid = check_project_id(project_id)
    if job_manager.blocks_review_edit(pid, name):
        raise HTTPException(409, "A conversion job is targeting this file; wait for it to finish before approving")
    src = safe_file_path(rag_core.MD_REVIEW_DIR, pid, name)
    if not src.is_file():
        raise HTTPException(404, f"Review file not found: {name}")
    dest_dir = Path(rag_core.project_dir(rag_core.DOCS_DIR, pid))
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    if dest.exists() and not overwrite:
        raise HTTPException(409, f"{name} already exists in approved docs for {pid}")
    shutil.move(str(src), str(dest))
    return {"approved": name}


# --- jobs ---


class JobCreate(BaseModel):
    type: str  # "convert" | "ingest"
    project_id: str | None = None  # omit / "all" for the whole library
    name: str | None = None  # specific file; omit for the whole project


def _check_job_name(pid: str, name: str, stage: str, extensions: tuple[str, ...]):
    """Validate a per-file job target with the same path-safety rules as the file routes
    (canonicalization + containment + extension allowlist), returning the safe absolute path."""
    path = safe_file_path(STAGE_DIRS[stage], pid, name)
    if not path.name.lower().endswith(extensions):
        raise HTTPException(400, f"Invalid job target {name!r}: expected {' or '.join(extensions)}")
    return path


@app.post("/api/jobs", status_code=201)
def create_job(body: JobCreate):
    if body.type not in ("convert", "ingest"):
        raise HTTPException(400, "type must be 'convert' or 'ingest'")
    scope_all = body.project_id in (None, "", "all")
    pid = None if scope_all else check_project_id(body.project_id)

    if body.type == "convert":
        if pid is not None and body.name:
            _check_job_name(pid, body.name, "inbox", CONVERTIBLE_EXTS)
        elif pid is None and body.name:
            raise HTTPException(400, "project_id is required when converting a specific file")
        return _submit_convert(pid, body.name).to_dict()
    if pid is not None and body.name:
        _check_job_name(pid, body.name, "docs", (".md", ".txt"))
    elif pid is None and body.name:
        raise HTTPException(400, "project_id is required when ingesting a specific file")
    return _submit_ingest(pid, body.name).to_dict()


def _convert_progress_cb(ctx):
    def cb(done, total):
        ctx.set_progress(f"{done}/{total} pages")
        ctx.log(f"Converted {done}/{total} pages")
    return cb


def _ingest_progress_cb(ctx):
    def cb(done, total):
        ctx.set_progress(f"{done}/{total} chunks")
    return cb


def _convert_inbox_file(pid, name, ctx):
    """Dispatch one inbox file to the matching converter (PDF → Docling, spreadsheet → sheet_to_md).
    Sheets convert instantly in memory, so no progress_cb (a '1/1 pages' flash would be misleading)."""
    if name.lower().endswith(SHEET_EXTS):
        return rag_core.convert_project_sheet(pid, name)
    return rag_core.convert_project_pdf(pid, name, progress_cb=_convert_progress_cb(ctx))


def _submit_convert(pid, name):
    if pid is None:
        def run(ctx):
            ctx.log("Converting all pending PDFs and spreadsheets")
            rag_core.convert_inbox(progress_cb=_convert_progress_cb(ctx))
            rag_core.convert_sheets_inbox(progress_cb=None)
            ctx.log("Conversion finished")

        return job_manager.submit("convert", run, target="convert all projects")
    if name:
        src = Path(rag_core.project_dir(rag_core.PDF_INBOX_DIR, pid)) / name
        if not src.is_file():
            raise HTTPException(404, f"Inbox file not found: {name}")

        def run(ctx):
            ctx.log(f"Converting {pid}/{name}")
            result = _convert_inbox_file(pid, name, ctx)
            ctx.log(result["message"])
            if result["status"] == "error":
                raise RuntimeError(result["message"])

        return job_manager.submit("convert", run, target=f"convert {pid}/{name}", project_id=pid, name=name)

    def run(ctx):
        folder = Path(rag_core.project_dir(rag_core.PDF_INBOX_DIR, pid))
        files = [p.name for p in folder.iterdir() if visible_file(p) and p.name.lower().endswith(CONVERTIBLE_EXTS)] if folder.is_dir() else []
        ctx.log(f"{len(files)} file(s) to convert in {pid}")
        for i, fn in enumerate(files, 1):
            ctx.log(f"[{i}/{len(files)}] Converting {pid}/{fn}")
            result = _convert_inbox_file(pid, fn, ctx)
            ctx.log(result["message"])
            if result["status"] == "error":
                raise RuntimeError(result["message"])

    return job_manager.submit("convert", run, target=f"convert {pid} (all pending)", project_id=pid)


def _submit_ingest(pid, name):
    if pid is None:
        def run(ctx):
            ctx.log("Ingesting all projects")
            rag_core.ensure_collection()  # fresh Qdrant volume: the API must bootstrap the collection itself
            rag_core.ingest_all(progress_cb=_ingest_progress_cb(ctx))
            ctx.log("Ingest finished")

        return job_manager.submit("ingest", run, target="ingest all projects")
    if name:
        doc = Path(rag_core.docs_path(pid, name))
        if not doc.is_file():
            raise HTTPException(404, f"Approved document not found: {name}")

        def run(ctx):
            ctx.log(f"Ingesting {pid}/{name}")
            rag_core.ensure_collection()
            result = rag_core.ingest_file(pid, name, progress_cb=_ingest_progress_cb(ctx))
            ctx.log(result["message"])

        return job_manager.submit("ingest", run, target=f"ingest {pid}/{name}", project_id=pid, name=name)

    def run(ctx):
        rag_core.ensure_collection()
        folder = Path(rag_core.project_dir(rag_core.DOCS_DIR, pid))
        files = [p.name for p in folder.iterdir() if visible_file(p) and p.name.lower().endswith((".md", ".txt"))] if folder.is_dir() else []
        ctx.log(f"{len(files)} document(s) to ingest in {pid}")
        for i, fn in enumerate(files, 1):
            ctx.log(f"[{i}/{len(files)}] Ingesting {pid}/{fn}")
            result = rag_core.ingest_file(pid, fn, progress_cb=_ingest_progress_cb(ctx))
            ctx.log(result["message"])

    return job_manager.submit("ingest", run, target=f"ingest {pid} (project)", project_id=pid)


@app.get("/api/jobs")
def list_jobs(active: int = Query(0)):
    return {"jobs": [j.to_dict() for j in job_manager.list(active_only=bool(active))]}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = job_manager.get(job_id)
    if job is None:
        raise HTTPException(404, f"Job not found: {job_id}")
    return job.to_dict()


@app.get("/api/events")
async def events():
    """SSE stream of job state changes: a `snapshot` of active jobs on connect,
    then one `job` event per queued/running/progress/terminal transition.

    Each connection parks one default-executor thread on a blocking q.get with
    a 15s timeout doubling as the heartbeat — fine for 1-2 local tabs. Upgrade
    path if ever needed: asyncio.Queue + loop.call_soon_threadsafe from the
    JobManager worker thread.
    """
    q = job_manager.subscribe()
    loop = asyncio.get_running_loop()

    async def gen():
        try:
            snap = {"type": "snapshot", "jobs": [j.to_dict() for j in job_manager.list(active_only=True)]}
            yield f"event: snapshot\ndata: {json.dumps(snap)}\n\n"
            while True:
                try:
                    job = await loop.run_in_executor(None, functools.partial(q.get, True, 15))
                except queue.Empty:
                    yield ": ping\n\n"
                    continue
                yield f"event: job\ndata: {json.dumps(job)}\n\n"
        finally:
            job_manager.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- search ---


class SearchRequest(BaseModel):
    query: str
    project_id: str | None = None
    target_parents: int = 3


@app.post("/api/search")
def search(body: SearchRequest):
    if not body.query.strip():
        raise HTTPException(400, "query must not be empty")
    try:
        hits = rag_core.search(body.query, project_id=body.project_id, target_parents=body.target_parents)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    except Exception as e:
        # e.g. the collection does not exist yet on a fresh Qdrant volume (no ingest has ever run)
        raise HTTPException(502, f"Search failed: {e}. If the collection is missing, run an ingest first.")
    return {"query": body.query, "project_id": body.project_id, "results": hits}
