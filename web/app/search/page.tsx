"use client";

import { useEffect, useState } from "react";
import { api, type PageMatch, type ProjectSummary, type SearchHit } from "@/lib/api";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

export default function SearchPage() {
  const [query, setQuery] = useState("");
  const [projectId, setProjectId] = useState("global");
  const [targetParents, setTargetParents] = useState(3);
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [results, setResults] = useState<SearchHit[] | null>(null);
  const [pages, setPages] = useState<PageMatch[] | null>(null);
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
      const data = await api<{ results: SearchHit[]; pages?: PageMatch[] }>("/api/search", {
        method: "POST",
        body: JSON.stringify({
          query,
          project_id: projectId === "" ? null : projectId,
          target_parents: targetParents,
        }),
      });
      setResults(data.results);
      setPages(data.pages ?? []);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setResults(null);
      setPages(null);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <h1 className="mb-1 text-xl font-semibold">Search</h1>
      <p className="mb-4 text-sm text-muted-foreground">
        Same retrieval as the MCP tool: child-chunk matching, deduplicated to complete parent sections. Without a
        project filter, only global documents are searched.
      </p>

      <div className="mb-5 flex flex-wrap items-end gap-3">
        <div className="grid min-w-64 flex-1 gap-1.5">
          <Label htmlFor="search-query">Query</Label>
          <Input
            id="search-query"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void run();
            }}
            placeholder="e.g. preliminary materials storage requirements"
          />
        </div>
        <div className="grid gap-1.5">
          <Label htmlFor="search-project">Project</Label>
          <Select value={projectId} onValueChange={setProjectId}>
            <SelectTrigger id="search-project" className="w-40">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="global">global only</SelectItem>
              {projects
                .filter((p) => p.project_id !== "global")
                .map((p) => (
                  <SelectItem key={p.project_id} value={p.project_id}>
                    {p.project_id}
                  </SelectItem>
                ))}
            </SelectContent>
          </Select>
        </div>
        <div className="grid gap-1.5">
          <Label htmlFor="search-sections">Sections</Label>
          <Input
            id="search-sections"
            type="number"
            min={1}
            max={10}
            value={targetParents}
            onChange={(e) => setTargetParents(Number(e.target.value) || 3)}
            className="w-20"
          />
        </div>
        <Button onClick={() => void run()} disabled={busy || !query.trim()}>
          {busy ? "Searching…" : "Search"}
        </Button>
      </div>

      {error && (
        <Alert variant="destructive" className="mb-4">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {results !== null && results.length === 0 && (pages?.length ?? 0) === 0 && (
        <p className="text-sm text-muted-foreground">No relevant information found.</p>
      )}

      {pages !== null && pages.length > 0 && (
        <section className="mb-5">
          <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-muted-foreground">
            Page matches ({pages.length})
          </h2>
          <div className="flex flex-wrap gap-3">
            {pages.map((p, i) => {
              const segs = p.page_image.split("/");
              const file = segs[segs.length - 1];
              const stem = segs.slice(1, -1).join("/");
              const url = `/api/projects/${encodeURIComponent(p.project_id)}/pages/${encodeURIComponent(stem)}/${file}`;
              return (
                <a
                  key={`${p.source}-${p.page_number}-${i}`}
                  href={url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="group w-48 overflow-hidden rounded-lg border bg-card transition-colors hover:border-primary/50"
                  title={`${p.source} — page ${p.page_number} (click to open full image)`}
                >
                  {/* eslint-disable-next-line @next/next/no-img-element -- served by the local API, not the image optimizer */}
                  <img src={url} alt={`${p.source} page ${p.page_number}`} loading="lazy" className="h-40 w-full object-cover object-top" />
                  <div className="flex flex-col gap-0.5 px-2 py-1.5 text-xs">
                    <span className="truncate font-mono" title={p.source}>
                      {p.source.replace(/\.pdf$/i, "")}
                    </span>
                    <span className="text-muted-foreground">
                      p.{p.page_number} · {(p.score * 100).toFixed(0)}% match
                    </span>
                  </div>
                </a>
              );
            })}
          </div>
        </section>
      )}

      <div className="flex flex-col gap-4">
        {results?.map((hit, i) => (
          <Card key={i} className="gap-0">
            <CardContent className="flex flex-col gap-2">
              <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                <Badge variant="secondary" className="font-mono">
                  {hit.project_id}
                </Badge>
                <span className="font-mono">{hit.source}</span>
                {hit.headers && <span className="italic">{hit.headers}</span>}
                <span className="ml-auto">score {hit.score?.toFixed?.(3) ?? hit.score}</span>
              </div>
              <pre className="max-h-96 overflow-auto whitespace-pre-wrap text-sm leading-relaxed">
                {hit.text}
              </pre>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}
