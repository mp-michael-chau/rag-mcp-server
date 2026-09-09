"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { api, formatBytes } from "@/lib/api";
import MarkdownEditor from "@/components/MarkdownEditor";
import { Alert, AlertDescription } from "@/components/ui/alert";

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
          className="text-sm text-primary hover:underline"
        >
          ← {projectId}
        </Link>
        <h1 className="font-mono text-base font-semibold">{fileName}</h1>
        {doc && (
          <span className="text-xs text-muted-foreground">
            approved · {formatBytes(new Blob([doc.content]).size)} · read-only
          </span>
        )}
      </div>
      {error ? (
        <Alert variant="destructive">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      ) : doc === null ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : (
        <MarkdownEditor value={doc.content} />
      )}
      {doc && doc.content.length > PREVIEW_MAX_CHARS && (
        <p className="mt-2 text-xs text-muted-foreground">Very large file — rendered preview disabled.</p>
      )}
    </div>
  );
}
