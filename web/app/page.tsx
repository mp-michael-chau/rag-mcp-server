"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { api, type ProjectSummary } from "@/lib/api";
import { useJobs } from "@/components/JobsProvider";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

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
    <Dialog
      open
      onOpenChange={(o) => {
        if (!o) onClose();
      }}
    >
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>New project</DialogTitle>
          <DialogDescription>
            Letters, digits, <code>.</code> <code>_</code> <code>-</code>; must not start with a dot. The project ID is
            baked into vector point IDs and cannot be renamed later (delete + recreate instead).
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-2">
          <Label htmlFor="new-project-id">Project ID</Label>
          <Input
            id="new-project-id"
            autoFocus
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && valid && !busy) void create();
            }}
            placeholder="e.g. clpe-cdhk"
            className="font-mono"
          />
        </div>
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button disabled={!valid || busy} onClick={() => void create()}>
            {busy ? "Creating…" : "Create"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
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
    <Dialog
      open
      onOpenChange={(o) => {
        if (!o) onClose();
      }}
    >
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Delete project</DialogTitle>
          <DialogDescription>
            This permanently deletes <span className="font-mono font-medium">{projectId}</span>:
          </DialogDescription>
        </DialogHeader>
        <ul className="list-disc pl-5 text-sm text-muted-foreground">
          <li>
            all files in <code>pdf_inbox/</code>, <code>md_review/</code> and <code>my_shared_docs/</code> for this
            project
          </li>
          <li>all of its vector points in Qdrant</li>
        </ul>
        <div className="grid gap-2">
          <Label htmlFor="delete-project-confirm">
            Type <span className="font-mono">{projectId}</span> to confirm:
          </Label>
          <Input
            id="delete-project-confirm"
            autoFocus
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            className="font-mono"
          />
        </div>
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="destructive" disabled={typed !== projectId || busy} onClick={() => void del()}>
            {busy ? "Deleting…" : "Delete project"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
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
    queueMicrotask(() => void load());
    const t = setInterval(() => void load(), 5000);
    return () => clearInterval(t);
  }, [load]);

  return (
    <div>
      <div className="mb-4 flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Projects</h1>
          <p className="text-sm text-muted-foreground">
            Upload PDFs → convert → review Markdown → approve → ingest. Root-level files belong to the virtual{" "}
            <span className="font-mono">global</span> scope.
          </p>
        </div>
        <Button onClick={() => setShowNew(true)}>New project</Button>
      </div>

      {error && (
        <Alert variant="destructive" className="mb-4">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {projects === null ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {projects.map((p) => (
            <Card
              key={p.project_id}
              size="sm"
              className="group relative gap-0 transition-all hover:shadow-md hover:ring-primary/30"
            >
              <Link href={`/projects/${encodeURIComponent(p.project_id)}`} className="block px-4">
                <div className="flex items-center gap-2">
                  <span className="font-mono text-base font-semibold">{p.project_id}</span>
                  {p.project_id === "global" && <Badge variant="secondary">virtual</Badge>}
                </div>
                <div className="mt-3 flex gap-4 text-sm text-muted-foreground">
                  <span>📥 {p.counts.inbox} inbox</span>
                  <span>📝 {p.counts.review} review</span>
                  <span>✅ {p.counts.docs} docs</span>
                </div>
              </Link>
              {p.project_id !== "global" && (
                <Button
                  variant="ghost"
                  size="xs"
                  onClick={() => setDeleting(p.project_id)}
                  className="absolute top-2 right-2 hidden text-destructive hover:text-destructive group-hover:inline-flex"
                >
                  delete
                </Button>
              )}
            </Card>
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
