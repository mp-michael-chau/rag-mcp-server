export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

async function throwApiError(res: Response): Promise<never> {
  let detail = `${res.status} ${res.statusText}`;
  try {
    const body = await res.json();
    if (typeof body.detail === "string") detail = body.detail;
    else if (body.detail) detail = JSON.stringify(body.detail);
  } catch {
    // keep statusText
  }
  throw new ApiError(detail, res.status);
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    await throwApiError(res);
  }
  return res.json() as Promise<T>;
}

// SSE stream of job state changes. Direct to FastAPI (bypasses the Next.js
// /api proxy, which may buffer streaming responses); the backend's CORS
// middleware allows this origin and EventSource sends a simple GET (no preflight).
export const EVENTS_URL = "http://127.0.0.1:8100/api/events";

// --- types mirroring the FastAPI schemas ---

export type Stage = "inbox" | "review" | "docs";

export interface ProjectSummary {
  project_id: string;
  counts: Record<Stage, number>;
}

export interface DocumentItem {
  name: string;
  stage: Stage;
  kind: "pdf" | "md" | "txt" | "sheet" | "other";
  size: number;
  mtime: number;
  /** inbox PDFs/sheets only: an approved .md already exists in docs, so convert would be skipped */
  already_approved?: boolean;
  convert_progress?: { done_pages: number | null; total_pages: number | null };
  chunk_count?: number | null;
}

export interface Job {
  id: string;
  type: "convert" | "ingest";
  target: string;
  project_id: string | null;
  name: string | null;
  status: "queued" | "running" | "done" | "error";
  progress: string;
  log: string[];
  error: string | null;
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
}

export interface SearchHit {
  score: number;
  project_id: string;
  source: string;
  headers: string;
  text: string;
}

export async function uploadFiles(
  projectId: string,
  files: FileList | File[],
  overwrite = false,
): Promise<{ saved: { name: string; stage: string }[]; errors: { name: string; error: string; conflict?: boolean }[] }> {
  const form = new FormData();
  for (const f of Array.from(files)) form.append("files", f);
  const res = await fetch(
    `/api/projects/${encodeURIComponent(projectId)}/upload${overwrite ? "?overwrite=1" : ""}`,
    { method: "POST", body: form },
  );
  if (!res.ok) {
    await throwApiError(res);
  }
  return res.json();
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}
