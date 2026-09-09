"use client";

import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { api, type Job } from "@/lib/api";

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
    void poll();
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
          className={`rounded-md border px-3 py-2 text-sm shadow-lg ${
            j.status === "error"
              ? "border-red-300 bg-red-50 text-red-800"
              : "border-green-300 bg-green-50 text-green-800"
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
          <div key={j.id} className="rounded-md border border-gray-300 bg-white px-3 py-2 text-sm shadow-lg">
            <div className="flex items-center justify-between gap-2">
              <span className="truncate font-medium" title={j.target}>
                {j.target}
              </span>
              <span className="shrink-0 rounded bg-gray-100 px-1.5 py-0.5 text-xs text-gray-600">
                {j.status === "running" ? "running" : "queued"}
              </span>
            </div>
            {j.progress && (
              <div className="mt-1.5">
                <div className="h-1.5 w-full overflow-hidden rounded bg-gray-200">
                  <div
                    className={`h-full rounded bg-blue-500 transition-all ${pct === null ? "w-full animate-pulse" : ""}`}
                    style={pct !== null ? { width: `${pct}%` } : undefined}
                  />
                </div>
                <div className="mt-0.5 text-right text-xs text-gray-500">{j.progress}</div>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
