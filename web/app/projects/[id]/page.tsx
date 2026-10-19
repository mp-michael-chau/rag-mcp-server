"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { api, ApiError, formatBytes, uploadFiles, type DocumentItem } from "@/lib/api";
import { useJobs } from "@/components/JobsProvider";
import { Alert, AlertDescription } from "@/components/ui/alert";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";

type UploadResult = Awaited<ReturnType<typeof uploadFiles>>;

function UploadButton({ projectId, onDone }: { projectId: string; onDone: () => void }) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [errors, setErrors] = useState<string | null>(null);
  const [conflict, setConflict] = useState<{ names: string } | null>(null);
  const pending = useRef<{ files: FileList; res: UploadResult } | null>(null);

  const settle = () => {
    setBusy(false);
    if (inputRef.current) inputRef.current.value = "";
  };

  // same filter as before: non-conflict errors always; conflict errors only when nothing else saved
  const report = (res: UploadResult) => {
    const errs = res.errors.filter((e) => !e.conflict || res.saved.length > 0);
    if (errs.length) setErrors(errs.map((e) => `${e.name}: ${e.error}`).join("\n"));
    onDone();
  };

  const handleFiles = async (files: FileList) => {
    if (!files.length) return;
    setBusy(true);
    setErrors(null);
    try {
      const res = await uploadFiles(projectId, files);
      if (res.errors.some((e) => e.conflict)) {
        const names = res.errors.filter((e) => e.conflict).map((e) => e.name).join(", ");
        pending.current = { files, res };
        setConflict({ names });
        return; // resolved via the overwrite dialog
      }
      report(res);
      settle();
    } catch (e) {
      setErrors(e instanceof Error ? e.message : String(e));
      settle();
    }
  };

  // user declined overwrite: keep the first upload result
  const declineOverwrite = () => {
    setConflict(null);
    const p = pending.current;
    if (!p) return;
    pending.current = null;
    report(p.res);
    settle();
  };

  const confirmOverwrite = async () => {
    const p = pending.current;
    if (!p) return;
    pending.current = null;
    setConflict(null);
    try {
      const res = await uploadFiles(projectId, p.files, true);
      report(res);
    } catch (e) {
      setErrors(e instanceof Error ? e.message : String(e));
    } finally {
      settle();
    }
  };

  return (
    <>
      <input
        ref={inputRef}
        type="file"
        multiple
        accept=".pdf,.md,.txt,.xlsx,.xlsm,.csv"
        className="hidden"
        onChange={(e) => e.target.files && void handleFiles(e.target.files)}
      />
      <Button onClick={() => inputRef.current?.click()} disabled={busy}>
        {busy ? "Uploading…" : "Upload files"}
      </Button>
      {errors && (
        <Alert variant="destructive" className="w-full">
          <AlertDescription className="whitespace-pre-line">{errors}</AlertDescription>
        </Alert>
      )}
      <AlertDialog
        open={conflict !== null}
        onOpenChange={(o) => {
          if (!o) declineOverwrite();
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Overwrite existing files?</AlertDialogTitle>
            <AlertDialogDescription>Already exists: {conflict?.names}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={() => void confirmOverwrite()}>Overwrite</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}

function ProgressBar({ text }: { text: string }) {
  const m = text.match(/^(\d+)\/(\d+)/);
  const pct = m && Number(m[2]) ? Math.min(100, Math.round((Number(m[1]) / Number(m[2])) * 100)) : null;
  return (
    <div className="w-40">
      {pct === null ? <Progress value={100} className="animate-pulse" /> : <Progress value={pct} />}
      <div className="mt-0.5 text-right text-xs text-muted-foreground">{text}</div>
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
    <Button
      variant="ghost"
      size="xs"
      onClick={onClick}
      className="ml-1 text-destructive hover:text-destructive"
    >
      {label}
    </Button>
  );
}

export default function ProjectPage() {
  const params = useParams<{ id: string }>();
  const projectId = decodeURIComponent(params.id);
  const [documents, setDocuments] = useState<DocumentItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionInfo, setActionInfo] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<DocumentItem | null>(null);
  const [approveOverwrite, setApproveOverwrite] = useState<string | null>(null);
  const { active, finished, isTargeted, refresh } = useJobs();

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
    queueMicrotask(() => void load());
  }, [load]);

  // reload the file lists when a job targeting this project (or the whole
  // library) reaches a terminal state; bursts collapse into one 250ms-debounced load
  const handledTerminal = useRef<Set<string>>(new Set());
  const reloadTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    const fresh = finished.filter(
      (j) => !handledTerminal.current.has(j.id) && (j.project_id === null || j.project_id === projectId),
    );
    if (fresh.length === 0) return;
    for (const j of fresh) handledTerminal.current.add(j.id);
    if (reloadTimer.current) clearTimeout(reloadTimer.current);
    reloadTimer.current = setTimeout(() => void load(), 250);
    return () => {
      if (reloadTimer.current) clearTimeout(reloadTimer.current);
    };
  }, [finished, load, projectId]);

  const enqueue = async (body: object) => {
    setActionError(null);
    setActionInfo(null);
    try {
      await api("/api/jobs", { method: "POST", body: JSON.stringify(body) });
      refresh();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    }
  };

  // Convert on an inbox file whose approved .md already exists would be a silent server-side skip
  // (convert_project_pdf/convert_project_sheet never reconverts a finalized doc), so explain it
  // instead of enqueueing.
  const convertOne = (d: DocumentItem) => {
    if (d.already_approved) {
      setActionError(null);
      const md = d.name.replace(/\.[^.]*$/, ".md");
      setActionInfo(
        `"${d.name}" is already approved — "${md}" exists in approved docs, so conversion is skipped to protect the reviewed copy. Delete that document in the Approved documents section (this also removes its vector points) first if you want to re-convert this file.`,
      );
      return;
    }
    void enqueue({ type: "convert", project_id: projectId, name: d.name });
  };

  const call = async (path: string, init?: RequestInit) => {
    setActionError(null);
    setActionInfo(null);
    try {
      await api(path, init);
      void load();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    }
  };

  const approve = async (name: string) => {
    const base = `/api/projects/${encodeURIComponent(projectId)}/approve/${encodeURIComponent(name)}`;
    setActionError(null);
    try {
      try {
        await api(base, { method: "POST" });
      } catch (e) {
        if (e instanceof ApiError && e.status === 409) {
          setApproveOverwrite(name);
          return;
        } else {
          throw e;
        }
      }
      await enqueue({ type: "ingest", project_id: projectId, name });
      void load();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    }
  };

  const confirmApproveOverwrite = async () => {
    const name = approveOverwrite;
    setApproveOverwrite(null);
    if (!name) return;
    const base = `/api/projects/${encodeURIComponent(projectId)}/approve/${encodeURIComponent(name)}`;
    try {
      await api(`${base}?overwrite=1`, { method: "POST" });
      await enqueue({ type: "ingest", project_id: projectId, name });
      void load();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    }
  };

  const confirmDelete = async () => {
    const item = deleting;
    setDeleting(null);
    if (!item) return;
    await call(`/api/projects/${encodeURIComponent(projectId)}/documents/${item.stage}/${encodeURIComponent(item.name)}`, {
      method: "DELETE",
    });
  };

  if (documents === null) {
    return (
      <div>
        <Link href="/" className="text-sm text-primary hover:underline">
          ← Projects
        </Link>
        {error ? (
          <Alert variant="destructive" className="mt-4">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        ) : (
          <p className="mt-4 text-sm text-muted-foreground">Loading…</p>
        )}
      </div>
    );
  }

  const inbox = documents.filter((d) => d.stage === "inbox");
  const review = documents.filter((d) => d.stage === "review");
  const docs = documents.filter((d) => d.stage === "docs");
  const projBusy = (type: "convert" | "ingest" | "index_pages") =>
    active.some((j) => j.type === type && j.project_id === projectId && j.name === null) ||
    active.some((j) => j.type === type && j.project_id === null);
  const fileBusy = (type: "convert" | "ingest" | "index_pages", name: string) => isTargeted(type, projectId, name);

  return (
    <div>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <Link href="/" className="text-sm text-primary hover:underline">
            ← Projects
          </Link>
          <h1 className="font-mono text-xl font-semibold">{projectId}</h1>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <UploadButton projectId={projectId} onDone={() => void load()} />
          {inbox.some((d) => d.kind === "pdf" || d.kind === "sheet") && (
            <Button
              variant="outline"
              onClick={() => void enqueue({ type: "convert", project_id: projectId })}
              disabled={projBusy("convert")}
            >
              Convert all pending
            </Button>
          )}
          <Button
            variant="outline"
            onClick={() => void enqueue({ type: "ingest", project_id: projectId })}
            disabled={projBusy("ingest")}
          >
            Ingest project
          </Button>
          {inbox.some((d) => d.kind === "pdf" && d.already_approved) && (
            <Button
              variant="outline"
              onClick={() => void enqueue({ type: "index_pages", project_id: projectId })}
              disabled={projBusy("index_pages")}
              title="Render every approved PDF's pages and embed them for page-image search (EmbeddingGemma 2)"
            >
              Index pages
            </Button>
          )}
        </div>
      </div>

      {error && (
        <Alert variant="destructive" className="mb-4">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      {actionError && (
        <Alert variant="destructive" className="mb-4">
          <AlertDescription className="whitespace-pre-line">{actionError}</AlertDescription>
        </Alert>
      )}
      {actionInfo && (
        <Alert className="mb-4">
          <AlertDescription>{actionInfo}</AlertDescription>
        </Alert>
      )}

      {/* Inbox */}
      <section className="mb-6">
        <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-muted-foreground">
          Inbox files ({inbox.length})
        </h2>
        <Card className="gap-0 py-0">
          {inbox.length === 0 ? (
            <p className="px-4 py-3 text-sm text-muted-foreground">
              No files waiting. Upload <code>.pdf</code>, <code>.xlsx</code>, <code>.xlsm</code>, or{" "}
              <code>.csv</code> files to convert.
            </p>
          ) : (
            <table className="w-full text-left">
              <tbody>
                {inbox.map((d) => {
                  const job = active.find(
                    (j) => j.type === "convert" && j.project_id === projectId && (j.name === d.name || j.name === null),
                  );
                  return (
                    <tr key={d.name} className="border-b border-muted last:border-0">
                      <td className="px-4 py-2">
                        <DocName item={d} />
                        <span className="ml-2 text-xs text-muted-foreground">{formatBytes(d.size)}</span>
                      </td>
                      <td className="w-48 px-4 py-2">
                        {job?.progress ? (
                          <ProgressBar text={job.progress} />
                        ) : (
                          <span className="text-xs text-muted-foreground">{job ? job.status : ""}</span>
                        )}
                      </td>
                      <td className="w-32 px-4 py-2 text-right whitespace-nowrap">
                        <Button
                          size="sm"
                          onClick={() => convertOne(d)}
                          disabled={fileBusy("convert", d.name)}
                          title={
                            d.already_approved ? "approved — delete docs copy to re-convert" : undefined
                          }
                          className={d.already_approved ? "opacity-50 hover:opacity-100" : undefined}
                        >
                          Convert
                        </Button>
                        <DeleteButton onClick={() => setDeleting(d)} />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </Card>
      </section>

      {/* Review */}
      <section className="mb-6">
        <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-muted-foreground">
          Review Markdown ({review.length})
        </h2>
        <Card className="gap-0 py-0">
          {review.length === 0 ? (
            <p className="px-4 py-3 text-sm text-muted-foreground">
              Nothing awaiting review. Converted Markdown lands here for editing before approval.
            </p>
          ) : (
            <table className="w-full text-left">
              <tbody>
                {review.map((d) => (
                  <tr key={d.name} className="border-b border-muted last:border-0">
                    <td className="px-4 py-2">
                      <Link
                        href={`/projects/${encodeURIComponent(projectId)}/review/${encodeURIComponent(d.name)}`}
                        className="font-mono text-sm text-primary hover:underline"
                      >
                        {d.name}
                      </Link>
                      <span className="ml-2 text-xs text-muted-foreground">{formatBytes(d.size)}</span>
                      {d.convert_progress && (
                        <Badge
                          variant="outline"
                          className="ml-2 border-amber-300 bg-amber-50 text-amber-700 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-400"
                        >
                          interrupted — {d.convert_progress.done_pages ?? "?"}
                          {d.convert_progress.total_pages ? `/${d.convert_progress.total_pages}` : ""} pages done
                        </Badge>
                      )}
                    </td>
                    <td className="w-64 px-4 py-2 text-right whitespace-nowrap">
                      <Button
                        size="sm"
                        onClick={() => void approve(d.name)}
                        disabled={fileBusy("convert", d.name)}
                        title={fileBusy("convert", d.name) ? "Wait for the conversion to finish" : "Approve and ingest this document"}
                      >
                        Approve
                      </Button>
                      <DeleteButton onClick={() => setDeleting(d)} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      </section>

      {/* Approved docs */}
      <section>
        <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-muted-foreground">
          Approved documents ({docs.length})
        </h2>
        <Card className="gap-0 py-0">
          {docs.length === 0 ? (
            <p className="px-4 py-3 text-sm text-muted-foreground">
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
                    <tr key={d.name} className="border-b border-muted last:border-0">
                      <td className="px-4 py-2">
                        <Link
                          href={`/projects/${encodeURIComponent(projectId)}/docs/${encodeURIComponent(d.name)}`}
                          className="font-mono text-sm text-primary hover:underline"
                        >
                          {d.name}
                        </Link>
                        <span className="ml-2 text-xs text-muted-foreground">{formatBytes(d.size)}</span>
                      </td>
                      <td className="w-48 px-4 py-2">
                        {job?.progress ? (
                          <ProgressBar text={job.progress} />
                        ) : d.chunk_count == null ? (
                          <span className="text-xs text-muted-foreground">chunks unknown</span>
                        ) : d.chunk_count === 0 ? (
                          <span className="text-xs text-amber-600 dark:text-amber-400">not ingested</span>
                        ) : (
                          <span className="text-xs text-green-700 dark:text-green-400">{d.chunk_count} chunks</span>
                        )}
                      </td>
                      <td className="w-40 px-4 py-2 text-right whitespace-nowrap">
                        <Button
                          size="sm"
                          onClick={() => void enqueue({ type: "ingest", project_id: projectId, name: d.name })}
                          disabled={fileBusy("ingest", d.name)}
                        >
                          {d.chunk_count ? "Re-ingest" : "Ingest"}
                        </Button>
                        <DeleteButton onClick={() => setDeleting(d)} />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </Card>
      </section>

      <AlertDialog
        open={deleting !== null}
        onOpenChange={(o) => {
          if (!o) setDeleting(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete file</AlertDialogTitle>
            <AlertDialogDescription>
              Permanently delete <span className="font-mono">{deleting?.name}</span>
              {deleting?.stage === "docs" ? " and its vector points" : ""}?
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction variant="destructive" onClick={() => void confirmDelete()}>
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog
        open={approveOverwrite !== null}
        onOpenChange={(o) => {
          if (!o) setApproveOverwrite(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Overwrite approved document?</AlertDialogTitle>
            <AlertDialogDescription>
              <span className="font-mono">{approveOverwrite}</span> already exists in approved docs.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={() => void confirmApproveOverwrite()}>Overwrite</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
