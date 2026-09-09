"use client";

import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { api, type Job } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";

interface JobsContextValue {
  /** queued + running jobs, polled live */
  active: Job[];
  /** recently finished jobs (done/error), kept briefly for toasts */
  finished: Job[];
  refresh: () => void;
  /**
   * True if an active job of this type targets the given file. Mirrors the backend rules:
   * a library-wide job (project_id null) targets everything; a project-wide job (name null)
   * targets the whole project; convert jobs match review mds by PDF stem (spec.md ↔ spec.pdf).
   */
  isTargeted: (type: Job["type"], projectId: string, name: string) => boolean;
}

const JobsContext = createContext<JobsContextValue | null>(null);

const FINISHED_TTL_MS = 8000;

export function useJobs(): JobsContextValue {
  const ctx = useContext(JobsContext);
  if (!ctx) throw new Error("useJobs must be used inside <JobsProvider>");
  return ctx;
}

export function JobsProvider({ children }: { children: React.ReactNode }) {
  const [active, setActive] = useState<Job[]>([]);
  const [finished, setFinished] = useState<Job[]>([]);
  const seenIds = useRef<Set<string>>(new Set());

  const poll = useCallback(async () => {
    try {
      const data = await api<{ jobs: Job[] }>("/api/jobs?active=1");
      const nowActive = data.jobs;

      // fetch final state of jobs that vanished from the active list (finished or failed)
      const goneIds = [...seenIds.current].filter((id) => !nowActive.some((a) => a.id === id));
      setActive(nowActive);
      seenIds.current = new Set(nowActive.map((j) => j.id));

      for (const id of goneIds) {
        try {
          const final = await api<Job>(`/api/jobs/${id}`);
          setFinished((prev) => [...prev.filter((p) => p.id !== final.id), final].slice(-10));
        } catch {
          // job pruned or server restarted; drop silently
        }
      }
      setFinished((prev) => prev.filter((f) => Date.now() / 1000 - (f.finished_at ?? 0) < FINISHED_TTL_MS / 1000));
    } catch {
      // backend down — badges elsewhere show it; keep last known state
    }
  }, []);

  const refresh = useCallback(() => {
    void poll();
  }, [poll]);

  useEffect(() => {
    queueMicrotask(() => void poll());
    const t = setInterval(() => void poll(), 1000);
    return () => clearInterval(t);
  }, [poll]);

  const isTargeted = useCallback(
    (type: Job["type"], projectId: string, name: string) => {
      const stem = (n: string) => n.replace(/\.[^.]*$/, "");
      return active.some((j) => {
        if (j.type !== type) return false;
        if (j.project_id !== null && j.project_id !== projectId) return false;
        if (j.name === null) return true; // project-wide (or library-wide) job
        if (j.name === name) return true; // exact match
        // convert jobs are keyed by the PDF name; review files share the stem
        return type === "convert" && stem(j.name) === stem(name);
      });
    },
    [active],
  );

  return (
    <JobsContext.Provider value={{ active, finished, refresh, isTargeted }}>
      {children}
      <JobPanel />
    </JobsContext.Provider>
  );
}

function parseProgress(progress: string): number | null {
  const m = progress.match(/^(\d+)\/(\d+)/);
  if (!m) return null;
  const done = Number(m[1]);
  const total = Number(m[2]);
  if (!total) return null;
  return Math.min(100, Math.round((done / total) * 100));
}

function JobPanel() {
  const { active, finished } = useJobs();
  if (active.length === 0 && finished.length === 0) return null;

  return (
    <div className="fixed bottom-4 right-4 z-50 flex w-80 flex-col gap-2">
      {finished.map((j) => (
        <div
          key={j.id}
          className={`rounded-lg px-3 py-2 text-sm shadow-lg ring-1 ${
            j.status === "error"
              ? "bg-destructive/10 text-destructive ring-destructive/30"
              : "bg-green-50 text-green-800 ring-green-300 dark:bg-green-950 dark:text-green-300 dark:ring-green-900"
          }`}
        >
          <div className="font-medium">
            {j.status === "error" ? "Failed" : "Done"}: {j.target}
          </div>
          {j.error && <div className="mt-1 break-all text-xs">{j.error}</div>}
        </div>
      ))}
      {active.map((j) => {
        const pct = parseProgress(j.progress);
        return (
          <Card key={j.id} className="gap-0 px-3 py-2 shadow-lg">
            <div className="flex items-center justify-between gap-2">
              <span className="truncate text-sm font-medium" title={j.target}>
                {j.target}
              </span>
              <Badge variant={j.status === "running" ? "secondary" : "outline"} className="shrink-0">
                {j.status === "running" ? "running" : "queued"}
              </Badge>
            </div>
            {j.progress && (
              <div className="mt-1.5">
                {pct === null ? <Progress value={100} className="animate-pulse" /> : <Progress value={pct} />}
                <div className="mt-0.5 text-right text-xs text-muted-foreground">{j.progress}</div>
              </div>
            )}
          </Card>
        );
      })}
    </div>
  );
}
