"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

interface Health {
  qdrant: string;
  ollama: string;
}

function HealthBadge({ name, status }: { name: string; status: string }) {
  const ok = status === "ok";
  return (
    <Badge
      variant="outline"
      title={status}
      className={
        ok ? "text-green-700 dark:text-green-400" : "border-destructive/30 text-destructive"
      }
    >
      <span className={`inline-block size-2 rounded-full ${ok ? "bg-green-500" : "bg-red-500"}`} />
      {name}
    </Badge>
  );
}

export default function NavBar() {
  const [health, setHealth] = useState<Health | null>(null);
  const pathname = usePathname();

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const res = await fetch("/api/health");
        const data = (await res.json()) as Health;
        if (!cancelled) setHealth(data);
      } catch {
        if (!cancelled) setHealth({ qdrant: "down", ollama: "down" });
      }
    };
    void load();
    const t = setInterval(() => void load(), 15000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, []);

  const link = (href: string, label: string) => {
    const isActive = href === "/" ? pathname === "/" : pathname.startsWith(href);
    return (
      <Button
        asChild
        variant="ghost"
        className={isActive ? "bg-muted text-foreground hover:bg-muted" : undefined}
      >
        <Link href={href}>{label}</Link>
      </Button>
    );
  };

  return (
    <header className="border-b border-border bg-background">
      <div className="mx-auto flex max-w-6xl items-center justify-between gap-4 px-4 py-3">
        <div className="flex items-center gap-4">
          <span className="text-base font-semibold">RAG Workflow</span>
          <nav className="flex items-center gap-1">
            {link("/", "Projects")}
            {link("/search", "Search")}
          </nav>
        </div>
        <div className="flex items-center gap-2">
          {health ? (
            <>
              <HealthBadge name="Qdrant" status={health.qdrant} />
              <HealthBadge name="Ollama" status={health.ollama} />
            </>
          ) : (
            <span className="text-xs text-muted-foreground">checking services…</span>
          )}
        </div>
      </div>
    </header>
  );
}
