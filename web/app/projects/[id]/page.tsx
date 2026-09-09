"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { api, ApiError, formatBytes, uploadFiles, type DocumentItem } from "@/lib/api";
import { useJobs } from "@/components/JobsProvider";

function UploadButton({ projectId, onDone }: { projectId: string; onDone: () => void }) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);

  const handleFiles = async (files: FileList) => {
    if (!files.length) return;
    setBusy(true);
    try {
      let res = await uploadFiles(projectId, files);
      if (res.errors.some((e) => e.conflict)) {
        const names = res.errors.filter((e) => e.conflict).map((e) => e.name).join(", ");
        if (window.confirm(`Already exists: ${names}\nOverwrite?`)) {
          res = await uploadFiles(projectId, files, true);
        }
      }
      const errs = res.errors.filter((e) => !e.conflict || res.saved.length > 0);
      if (errs.length) window.alert(errs.map((e) => `${e.name}: ${e.error}`).join("\n"));
      onDone();
    } catch (e) {
      window.alert(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
      if (inputRef.current) inputRef.current.value = "";
    }
  };

  return (
    <>
      <input
        ref={inputRef}
        type="file"
        multiple
        accept=".pdf,.md,.txt"
        className="hidden"
        onChange={(e) => e.target.files && void handleFiles(e.target.files)}
      />
      <button
        onClick={() => inputRef.current?.click()}
        disabled={busy}
        className="rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-40"
      >
        {busy ? "Uploading…" : "Upload files"}
      </button>
    </>
  );
}

function ProgressBar({ text }: { text: string }) {
  const m = text.match(/^(\d+)\/(\d+)/);
  const pct = m && Number(m[2]) ? Math.min(100, Math.round((Number(m[1]) / Number(m[2])) * 100)) : null;
  return (
    <div className="w-40">
      <div className="h-1.5 w-full overflow-hidden rounded bg-gray-200">
        <div
          className={`h-full rounded bg-blue-500 transition-all ${pct === null ? "w-full animate-pulse" : ""}`}
          style={pct !== null ? { width: `${pct}%` } : undefined}
        />
      </div>
      <div className="mt-0.5 text-right text-xs text-gray-500">{text}</div>
    </div>
  );
}

function DocName({ item }: { item: DocumentItem }) {
  return (
    <span className="font-mono text-sm" title={item.name}>
      {item.name}
    </span>
  );
}

function DeleteButton({ onClick, label = "delete" }: { onClick: () => void; label?: string }) {
  return (
    <button onClick={onClick} className="rounded px-1.5 py-0.5 text-xs text-red-500 hover:bg-red-50">
      {label}
    </button>
  );
}

export default function ProjectPage() {
  const params = useParams<{ id: string }>();
  const projectId = decodeURIComponent(params.id);
  const [documents, setDocuments] = useState<DocumentItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { active, isTargeted, refresh } = useJobs();

  const load = useCallback(async () => {
    try {
      const data = await api<{ documents: DocumentItem[] }>(
        `/api/projects/${encodeURIComponent(projectId)}/documents`,
      );
      setDocuments(data.documents);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [projectId]);

  useEffect(() => {
    void load();
    const t = setInterval(() => void load(), 3000);
    return () => clearInterval(t);
  }, [load]);

  // refresh doc list right after jobs finish
  useEffect(() => {
    if (active.length === 0) void load();
  }, [active.length, load]);

  const enqueue = async (body: object) => {
    try {
      await api("/api/jobs", { method: "POST", body: JSON.stringify(body) });
      refresh();
    } catch (e) {
      window.alert(e instanceof Error ? e.message : String(e));
    }
  };

  const call = async (path: string, init?: RequestInit) => {
    try {
      await api(path, init);
      void load();
    } catch (e) {
      window.alert(e instanceof Error ? e.message : String(e));
    }
  };

  const approve = async (name: string, andIngest: boolean) => {
    const base = `/api/projects/${encodeURIComponent(projectId)}/approve/${encodeURIComponent(name)}`;
    try {
      try {
        await api(base, { method: "POST" });
      } catch (e) {
        if (e instanceof ApiError && e.status === 409) {
          if (!window.confirm(`${name} already exists in approved docs. Overwrite?`)) return;
          await api(`${base}?overwrite=1`, { method: "POST" });
        } else {
          throw e;
        }
      }
      if (andIngest) await enqueue({ type: "ingest", project_id: projectId, name });
      void load();
    } catch (e) {
      window.alert(e instanceof Error ? e.message : String(e));
    }
  };

  const deleteDoc = async (item: DocumentItem) => {
    const extra = item.stage === "docs" ? " and its vector points" : "";
    if (!window.confirm(`Delete ${item.name}${extra}?`)) return;
    await call(`/api/projects/${encodeURIComponent(projectId)}/documents/${item.stage}/${encodeURIComponent(item.name)}`, {
      method: "DELETE",
    });
  };

  if (documents === null) {
    return (
      <div>
        <Link href="/" className="text-sm text-blue-600 hover:underline">
          ← Projects
        </Link>
        {error ? (
          <p className="mt-4 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p>
        ) : (
          <p className="mt-4 text-sm text-gray-500">Loading…</p>
        )}
      </div>
    );
  }

  const inbox = documents.filter((d) => d.stage === "inbox");
  const review = documents.filter((d) => d.stage === "review");
  const docs = documents.filter((d) => d.stage === "docs");
  const projBusy = (type: "convert" | "ingest") =>
    active.some((j) => j.type === type && j.project_id === projectId && j.name === null) ||
    active.some((j) => j.type === type && j.project_id === null);
  const fileBusy = (type: "convert" | "ingest", name: string) => isTargeted(type, projectId, name);

  return (
    <div>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <Link href="/" className="text-sm text-blue-600 hover:underline">
            ← Projects
          </Link>
          <h1 className="font-mono text-xl font-semibold">{projectId}</h1>
        </div>
        <div className="flex items-center gap-2">
          <UploadButton projectId={projectId} onDone={() => void load()} />
          {inbox.some((d) => d.kind === "pdf") && (
            <button
              onClick={() => void enqueue({ type: "convert", project_id: projectId })}
              disabled={projBusy("convert")}
              className="rounded-md border border-gray-300 px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-100 disabled:opacity-40"
            >
              Convert all pending
            </button>
          )}
          <button
            onClick={() => void enqueue({ type: "ingest", project_id: projectId })}
            disabled={projBusy("ingest")}
            className="rounded-md border border-gray-300 px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-100 disabled:opacity-40"
          >
            Ingest project
          </button>
        </div>
      </div>

      {error && (
        <p className="mb-4 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p>
      )}

      {/* Inbox */}
      <section className="mb-6">
        <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-gray-500">
          Inbox PDFs ({inbox.length})
        </h2>
        <div className="overflow-hidden rounded-lg border border-gray-200 bg-white">
          {inbox.length === 0 ? (
            <p className="px-4 py-3 text-sm text-gray-400">No PDFs waiting. Upload <code>.pdf</code> files to convert.</p>
          ) : (
            <table className="w-full text-left">
              <tbody>
                {inbox.map((d) => {
                  const job = active.find(
                    (j) => j.type === "convert" && j.project_id === projectId && (j.name === d.name || j.name === null),
                  );
                  return (
                    <tr key={d.name} className="border-b border-gray-100 last:border-0">
                      <td className="px-4 py-2">
                        <DocName item={d} />
                        <span className="ml-2 text-xs text-gray-400">{formatBytes(d.size)}</span>
                      </td>
                      <td className="w-48 px-4 py-2">
                        {job?.progress ? (
                          <ProgressBar text={job.progress} />
                        ) : (
                          <span className="text-xs text-gray-400">{job ? job.status : ""}</span>
                        )}
                      </td>
                      <td className="w-32 px-4 py-2 text-right whitespace-nowrap">
                        <button
                          onClick={() => void enqueue({ type: "convert", project_id: projectId, name: d.name })}
                          disabled={fileBusy("convert", d.name)}
                          className="rounded bg-blue-600 px-2 py-1 text-xs font-medium text-white hover:bg-blue-700 disabled:opacity-40"
                        >
                          Convert
                        </button>
                        <DeleteButton onClick={() => void deleteDoc(d)} />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      </section>

      {/* Review */}
      <section className="mb-6">
        <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-gray-500">
          Review Markdown ({review.length})
        </h2>
        <div className="overflow-hidden rounded-lg border border-gray-200 bg-white">
          {review.length === 0 ? (
            <p className="px-4 py-3 text-sm text-gray-400">
              Nothing awaiting review. Converted Markdown lands here for editing before approval.
            </p>
          ) : (
            <table className="w-full text-left">
              <tbody>
                {review.map((d) => (
                  <tr key={d.name} className="border-b border-gray-100 last:border-0">
                    <td className="px-4 py-2">
                      <Link
                        href={`/projects/${encodeURIComponent(projectId)}/review/${encodeURIComponent(d.name)}`}
                        className="font-mono text-sm text-blue-600 hover:underline"
                      >
                        {d.name}
                      </Link>
                      <span className="ml-2 text-xs text-gray-400">{formatBytes(d.size)}</span>
                      {d.convert_progress && (
                        <span className="ml-2 rounded bg-amber-100 px-1.5 py-0.5 text-xs text-amber-700">
                          interrupted — {d.convert_progress.done_pages ?? "?"}
                          {d.convert_progress.total_pages ? `/${d.convert_progress.total_pages}` : ""} pages done
                        </span>
                      )}
                    </td>
                    <td className="w-64 px-4 py-2 text-right whitespace-nowrap">
                      <button
                        onClick={() => void approve(d.name, false)}
                        disabled={fileBusy("convert", d.name)}
                        title={fileBusy("convert", d.name) ? "Wait for the conversion to finish" : undefined}
                        className="rounded border border-gray-300 px-2 py-1 text-xs font-medium text-gray-700 hover:bg-gray-100 disabled:cursor-not-allowed disabled:opacity-40"
                      >
                        Approve
                      </button>
                      <button
                        onClick={() => void approve(d.name, true)}
                        disabled={fileBusy("convert", d.name)}
                        title={fileBusy("convert", d.name) ? "Wait for the conversion to finish" : undefined}
                        className="ml-1 rounded bg-green-600 px-2 py-1 text-xs font-medium text-white hover:bg-green-700 disabled:cursor-not-allowed disabled:opacity-40"
                      >
                        Approve &amp; Ingest
                      </button>
                      <DeleteButton onClick={() => void deleteDoc(d)} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </section>

      {/* Approved docs */}
      <section>
        <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-gray-500">
          Approved documents ({docs.length})
        </h2>
        <div className="overflow-hidden rounded-lg border border-gray-200 bg-white">
          {docs.length === 0 ? (
            <p className="px-4 py-3 text-sm text-gray-400">
              No approved documents yet. Upload <code>.md</code>/<code>.txt</code> directly or approve reviewed files.
            </p>
          ) : (
            <table className="w-full text-left">
              <tbody>
                {docs.map((d) => {
                  const job = active.find(
                    (j) => j.type === "ingest" && j.project_id === projectId && (j.name === d.name || j.name === null),
                  );
                  return (
                    <tr key={d.name} className="border-b border-gray-100 last:border-0">
                      <td className="px-4 py-2">
                        <Link
                          href={`/projects/${encodeURIComponent(projectId)}/docs/${encodeURIComponent(d.name)}`}
                          className="font-mono text-sm text-blue-600 hover:underline"
                        >
                          {d.name}
                        </Link>
                        <span className="ml-2 text-xs text-gray-400">{formatBytes(d.size)}</span>
                      </td>
                      <td className="w-48 px-4 py-2">
                        {job?.progress ? (
                          <ProgressBar text={job.progress} />
                        ) : d.chunk_count == null ? (
                          <span className="text-xs text-gray-400">chunks unknown</span>
                        ) : d.chunk_count === 0 ? (
                          <span className="text-xs text-amber-600">not ingested</span>
                        ) : (
                          <span className="text-xs text-green-700">{d.chunk_count} chunks</span>
                        )}
                      </td>
                      <td className="w-40 px-4 py-2 text-right whitespace-nowrap">
                        <button
                          onClick={() => void enqueue({ type: "ingest", project_id: projectId, name: d.name })}
                          disabled={fileBusy("ingest", d.name)}
                          className="rounded bg-blue-600 px-2 py-1 text-xs font-medium text-white hover:bg-blue-700 disabled:opacity-40"
                        >
                          {d.chunk_count ? "Re-ingest" : "Ingest"}
                        </button>
                        <DeleteButton onClick={() => void deleteDoc(d)} />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      </section>
    </div>
  );
}
