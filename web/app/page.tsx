"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { api, type ProjectSummary } from "@/lib/api";
import Modal from "@/components/Modal";
import { useJobs } from "@/components/JobsProvider";

const PROJECT_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;

function NewProjectDialog({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // trim before testing: JS `^…$` would otherwise accept a trailing newline the backend rejects
  const trimmed = name.trim();
  const valid = trimmed === name && name !== "" && PROJECT_ID_RE.test(trimmed) && trimmed !== "global";

  const create = async () => {
    setBusy(true);
    setError(null);
    try {
      await api("/api/projects", { method: "POST", body: JSON.stringify({ project_id: trimmed }) });
      onCreated();
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title="New project" onClose={onClose}>
      <label className="block text-sm font-medium text-gray-700">
        Project ID
        <input
          autoFocus
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && valid && !busy) void create();
          }}
          placeholder="e.g. clpe-cdhk"
          className="mt-1 w-full rounded-md border border-gray-300 px-3 py-2 font-mono text-sm focus:border-blue-500 focus:outline-none"
        />
      </label>
      <p className="mt-2 text-xs text-gray-500">
        Letters, digits, <code>.</code> <code>_</code> <code>-</code>; must not start with a dot. The project ID is
        baked into vector point IDs and cannot be renamed later (delete + recreate instead).
      </p>
      {error && <p className="mt-2 rounded bg-red-50 px-2 py-1 text-sm text-red-700">{error}</p>}
      <div className="mt-4 flex justify-end gap-2">
        <button onClick={onClose} className="rounded-md px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-100">
          Cancel
        </button>
        <button
          disabled={!valid || busy}
          onClick={() => void create()}
          className="rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-40"
        >
          {busy ? "Creating…" : "Create"}
        </button>
      </div>
    </Modal>
  );
}

function DeleteProjectDialog({
  projectId,
  onClose,
  onDeleted,
}: {
  projectId: string;
  onClose: () => void;
  onDeleted: () => void;
}) {
  const [typed, setTyped] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const del = async () => {
    setBusy(true);
    setError(null);
    try {
      await api(`/api/projects/${encodeURIComponent(projectId)}`, { method: "DELETE" });
      onDeleted();
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title="Delete project" onClose={onClose}>
      <p className="text-sm text-gray-700">
        This permanently deletes <span className="font-mono font-medium">{projectId}</span>:
      </p>
      <ul className="mt-2 list-disc pl-5 text-sm text-gray-700">
        <li>all files in <code>pdf_inbox/</code>, <code>md_review/</code> and <code>my_shared_docs/</code> for this project</li>
        <li>all of its vector points in Qdrant</li>
      </ul>
      <label className="mt-4 block text-sm font-medium text-gray-700">
        Type <span className="font-mono">{projectId}</span> to confirm:
        <input
          autoFocus
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          className="mt-1 w-full rounded-md border border-gray-300 px-3 py-2 font-mono text-sm focus:border-red-500 focus:outline-none"
        />
      </label>
      {error && <p className="mt-2 rounded bg-red-50 px-2 py-1 text-sm text-red-700">{error}</p>}
      <div className="mt-4 flex justify-end gap-2">
        <button onClick={onClose} className="rounded-md px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-100">
          Cancel
        </button>
        <button
          disabled={typed !== projectId || busy}
          onClick={() => void del()}
          className="rounded-md bg-red-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-700 disabled:opacity-40"
        >
          {busy ? "Deleting…" : "Delete project"}
        </button>
      </div>
    </Modal>
  );
}

export default function DashboardPage() {
  const [projects, setProjects] = useState<ProjectSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showNew, setShowNew] = useState(false);
  const [deleting, setDeleting] = useState<string | null>(null);
  const { refresh } = useJobs();

  const load = useCallback(async () => {
    try {
      const data = await api<{ projects: ProjectSummary[] }>("/api/projects");
      setProjects(data.projects);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
    const t = setInterval(() => void load(), 5000);
    return () => clearInterval(t);
  }, [load]);

  return (
    <div>
      <div className="mb-4 flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Projects</h1>
          <p className="text-sm text-gray-500">
            Upload PDFs → convert → review Markdown → approve → ingest. Root-level files belong to the virtual{" "}
            <span className="font-mono">global</span> scope.
          </p>
        </div>
        <button
          onClick={() => setShowNew(true)}
          className="rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700"
        >
          New project
        </button>
      </div>

      {error && (
        <p className="mb-4 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
          {error}
        </p>
      )}

      {projects === null ? (
        <p className="text-sm text-gray-500">Loading…</p>
      ) : (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {projects.map((p) => (
            <div
              key={p.project_id}
              className="group relative rounded-lg border border-gray-200 bg-white p-4 shadow-sm transition hover:border-blue-300 hover:shadow"
            >
              <Link href={`/projects/${encodeURIComponent(p.project_id)}`} className="block">
                <div className="flex items-center gap-2">
                  <span className="font-mono text-base font-semibold text-gray-900">{p.project_id}</span>
                  {p.project_id === "global" && (
                    <span className="rounded bg-gray-100 px-1.5 py-0.5 text-xs text-gray-500">virtual</span>
                  )}
                </div>
                <div className="mt-3 flex gap-4 text-sm text-gray-600">
                  <span>📥 {p.counts.inbox} inbox</span>
                  <span>📝 {p.counts.review} review</span>
                  <span>✅ {p.counts.docs} docs</span>
                </div>
              </Link>
              {p.project_id !== "global" && (
                <button
                  onClick={() => setDeleting(p.project_id)}
                  className="absolute right-3 top-3 hidden rounded px-1.5 py-0.5 text-xs text-red-500 hover:bg-red-50 group-hover:block"
                >
                  delete
                </button>
              )}
            </div>
          ))}
        </div>
      )}

      {showNew && (
        <NewProjectDialog
          onClose={() => setShowNew(false)}
          onCreated={() => {
            void load();
            refresh();
          }}
        />
      )}
      {deleting && (
        <DeleteProjectDialog projectId={deleting} onClose={() => setDeleting(null)} onDeleted={() => void load()} />
      )}
    </div>
  );
}
