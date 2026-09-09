"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { api } from "@/lib/api";
import MarkdownEditor from "@/components/MarkdownEditor";
import MarkdownPreview from "@/components/MarkdownPreview";
import { useJobs } from "@/components/JobsProvider";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";

const PREVIEW_MAX_CHARS = 500_000; // react-markdown is slow on multi-MB specs; preview on demand only

export default function ReviewEditorPage() {
  const params = useParams<{ id: string; file: string }>();
  const projectId = decodeURIComponent(params.id);
  const fileName = decodeURIComponent(params.file);
  const [content, setContent] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<string | null>(null);
  const [showPreview, setShowPreview] = useState(false);
  const { isTargeted } = useJobs();

  const load = useCallback(async () => {
    try {
      const data = await api<{ content: string }>(
        `/api/projects/${encodeURIComponent(projectId)}/review/${encodeURIComponent(fileName)}`,
      );
      setContent(data.content);
      setDraft(data.content);
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : String(e));
    }
  }, [projectId, fileName]);

  useEffect(() => {
    queueMicrotask(() => void load());
  }, [load]);

  const dirty = content !== null && draft !== content;

  const save = useCallback(async () => {
    setSaving(true);
    setSaveError(null);
    try {
      await api(`/api/projects/${encodeURIComponent(projectId)}/review/${encodeURIComponent(fileName)}`, {
        method: "PUT",
        body: JSON.stringify({ content: draft }),
      });
      setContent(draft);
      setSavedAt(new Date().toLocaleTimeString());
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }, [projectId, fileName, draft]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "s") {
        e.preventDefault();
        if (dirty && !saving) void save();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [dirty, saving, save]);

  // includes project-wide and library-wide (convert all) jobs via isTargeted's scope rules
  const convertRunning = isTargeted("convert", projectId, fileName);

  if (content === null) {
    return (
      <div>
        <Link
          href={`/projects/${encodeURIComponent(projectId)}`}
          className="text-sm text-primary hover:underline"
        >
          ← {projectId}
        </Link>
        <p className="mt-4 text-sm text-muted-foreground">{saveError ?? "Loading…"}</p>
      </div>
    );
  }

  const previewAvailable = draft.length <= PREVIEW_MAX_CHARS;

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <Link
            href={`/projects/${encodeURIComponent(projectId)}`}
            className="text-sm text-primary hover:underline"
          >
            ← {projectId}
          </Link>
          <h1 className="font-mono text-base font-semibold">{fileName}</h1>
          <span className="text-xs text-muted-foreground">
            {dirty ? "unsaved changes" : savedAt ? `saved at ${savedAt}` : "review copy"}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            onClick={() => setShowPreview((v) => !v)}
            disabled={!previewAvailable}
            title={previewAvailable ? "Toggle rendered preview" : "Preview disabled for very large files"}
          >
            {showPreview ? "Show editor" : "Preview"}
          </Button>
          <Button onClick={() => void save()} disabled={!dirty || saving || convertRunning}>
            {saving ? "Saving…" : "Save"}
          </Button>
        </div>
      </div>

      {convertRunning && (
        <Alert className="mb-3 border-amber-300 bg-amber-50 text-amber-800 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-300">
          <AlertDescription className="text-amber-700 dark:text-amber-300">
            A conversion job is targeting this file — saving is disabled until it finishes.
          </AlertDescription>
        </Alert>
      )}
      {saveError && (
        <Alert variant="destructive" className="mb-3">
          <AlertDescription>{saveError}</AlertDescription>
        </Alert>
      )}

      {showPreview && previewAvailable ? (
        <MarkdownPreview content={draft} />
      ) : (
        <MarkdownEditor value={draft} onChange={setDraft} />
      )}
    </div>
  );
}
