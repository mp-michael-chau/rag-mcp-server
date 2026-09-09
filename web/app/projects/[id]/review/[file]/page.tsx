"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { api } from "@/lib/api";
import MarkdownEditor from "@/components/MarkdownEditor";
import MarkdownPreview from "@/components/MarkdownPreview";
import { useJobs } from "@/components/JobsProvider";

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
    void load();
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
          className="text-sm text-blue-600 hover:underline"
        >
          ← {projectId}
        </Link>
        <p className="mt-4 text-sm text-gray-500">{saveError ?? "Loading…"}</p>
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
            className="text-sm text-blue-600 hover:underline"
          >
            ← {projectId}
          </Link>
          <h1 className="font-mono text-base font-semibold">{fileName}</h1>
          <span className="text-xs text-gray-400">
            {dirty ? "unsaved changes" : savedAt ? `saved at ${savedAt}` : "review copy"}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setShowPreview((v) => !v)}
            disabled={!previewAvailable}
            title={previewAvailable ? "Toggle rendered preview" : "Preview disabled for very large files"}
            className="rounded-md border border-gray-300 px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-100 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {showPreview ? "Show editor" : "Preview"}
          </button>
          <button
            onClick={() => void save()}
            disabled={!dirty || saving || convertRunning}
            className="rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-40"
          >
            {saving ? "Saving…" : "Save"}
          </button>
        </div>
      </div>

      {convertRunning && (
        <p className="mb-3 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-700">
          A conversion job is targeting this file — saving is disabled until it finishes.
        </p>
      )}
      {saveError && (
        <p className="mb-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{saveError}</p>
      )}

      {showPreview && previewAvailable ? (
        <MarkdownPreview content={draft} />
      ) : (
        <MarkdownEditor value={draft} onChange={setDraft} />
      )}
    </div>
  );
}
