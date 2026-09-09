"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { api, formatBytes } from "@/lib/api";
import MarkdownEditor from "@/components/MarkdownEditor";

const PREVIEW_MAX_CHARS = 500_000;

export default function DocViewPage() {
  const params = useParams<{ id: string; file: string }>();
  const projectId = decodeURIComponent(params.id);
  const fileName = decodeURIComponent(params.file);
  const [doc, setDoc] = useState<{ content: string } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api<{ content: string }>(`/api/projects/${encodeURIComponent(projectId)}/docs/${encodeURIComponent(fileName)}`)
      .then((d) => setDoc({ content: d.content }))
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, [projectId, fileName]);

  return (
    <div>
      <div className="mb-3 flex items-center gap-3">
        <Link
          href={`/projects/${encodeURIComponent(projectId)}`}
          className="text-sm text-blue-600 hover:underline"
        >
          ← {projectId}
        </Link>
        <h1 className="font-mono text-base font-semibold">{fileName}</h1>
        {doc && (
          <span className="text-xs text-gray-400">
            approved · {formatBytes(new Blob([doc.content]).size)} · read-only
          </span>
        )}
      </div>
      {error ? (
        <p className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p>
      ) : doc === null ? (
        <p className="text-sm text-gray-500">Loading…</p>
      ) : (
        <MarkdownEditor value={doc.content} />
      )}
      {doc && doc.content.length > PREVIEW_MAX_CHARS && (
        <p className="mt-2 text-xs text-gray-400">Very large file — rendered preview disabled.</p>
      )}
    </div>
  );
}
