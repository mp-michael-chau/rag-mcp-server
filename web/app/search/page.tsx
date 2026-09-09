"use client";

import { useEffect, useState } from "react";
import { api, type ProjectSummary, type SearchHit } from "@/lib/api";

export default function SearchPage() {
  const [query, setQuery] = useState("");
  const [projectId, setProjectId] = useState("global");
  const [targetParents, setTargetParents] = useState(3);
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [results, setResults] = useState<SearchHit[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<{ projects: ProjectSummary[] }>("/api/projects")
      .then((d) => setProjects(d.projects))
      .catch(() => setProjects([]));
  }, []);

  const run = async () => {
    if (!query.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const data = await api<{ results: SearchHit[] }>("/api/search", {
        method: "POST",
        body: JSON.stringify({
          query,
          project_id: projectId === "" ? null : projectId,
          target_parents: targetParents,
        }),
      });
      setResults(data.results);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setResults(null);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <h1 className="mb-1 text-xl font-semibold">Search</h1>
      <p className="mb-4 text-sm text-gray-500">
        Same retrieval as the MCP tool: child-chunk matching, deduplicated to complete parent sections. Without a
        project filter, only global documents are searched.
      </p>

      <div className="mb-5 flex flex-wrap items-end gap-3">
        <label className="flex-1 min-w-64 text-sm font-medium text-gray-700">
          Query
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void run();
            }}
            placeholder="e.g. preliminary materials storage requirements"
            className="mt-1 w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none"
          />
        </label>
        <label className="text-sm font-medium text-gray-700">
          Project
          <select
            value={projectId}
            onChange={(e) => setProjectId(e.target.value)}
            className="mt-1 block rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none"
          >
            <option value="global">global only</option>
            {projects
              .filter((p) => p.project_id !== "global")
              .map((p) => (
                <option key={p.project_id} value={p.project_id}>
                  {p.project_id}
                </option>
              ))}
          </select>
        </label>
        <label className="text-sm font-medium text-gray-700">
          Sections
          <input
            type="number"
            min={1}
            max={10}
            value={targetParents}
            onChange={(e) => setTargetParents(Number(e.target.value) || 3)}
            className="mt-1 block w-20 rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none"
          />
        </label>
        <button
          onClick={() => void run()}
          disabled={busy || !query.trim()}
          className="rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-40"
        >
          {busy ? "Searching…" : "Search"}
        </button>
      </div>

      {error && (
        <p className="mb-4 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p>
      )}

      {results !== null && results.length === 0 && (
        <p className="text-sm text-gray-500">No relevant information found.</p>
      )}

      <div className="flex flex-col gap-4">
        {results?.map((hit, i) => (
          <div key={i} className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
            <div className="mb-2 flex flex-wrap items-center gap-2 text-xs text-gray-500">
              <span className="rounded bg-blue-50 px-2 py-0.5 font-medium text-blue-700">{hit.project_id}</span>
              <span className="font-mono">{hit.source}</span>
              {hit.headers && <span className="italic">{hit.headers}</span>}
              <span className="ml-auto">score {hit.score?.toFixed?.(3) ?? hit.score}</span>
            </div>
            <pre className="max-h-96 overflow-auto whitespace-pre-wrap text-sm leading-relaxed text-gray-800">
              {hit.text}
            </pre>
          </div>
        ))}
      </div>
    </div>
  );
}
