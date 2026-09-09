"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";

interface Health {
  qdrant: string;
  ollama: string;
}

function Badge({ name, status }: { name: string; status: string }) {
  const ok = status === "ok";
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium ${
        ok ? "bg-green-100 text-green-800" : "bg-red-100 text-red-800"
      }`}
      title={status}
    >
      <span className={`inline-block h-2 w-2 rounded-full ${ok ? "bg-green-500" : "bg-red-500"}`} />
      {name}
    </span>
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
      <Link
        href={href}
        className={`rounded-md px-3 py-1.5 text-sm font-medium ${
          isActive ? "bg-gray-900 text-white" : "text-gray-600 hover:bg-gray-100 hover:text-gray-900"
        }`}
      >
        {label}
      </Link>
    );
  };

  return (
    <header className="border-b border-gray-200 bg-white">
      <div className="mx-auto flex max-w-6xl items-center justify-between gap-4 px-4 py-3">
        <div className="flex items-center gap-4">
          <span className="text-base font-semibold text-gray-900">RAG Workflow</span>
          <nav className="flex items-center gap-1">
            {link("/", "Projects")}
            {link("/search", "Search")}
          </nav>
        </div>
        <div className="flex items-center gap-2">
          {health ? (
            <>
              <Badge name="Qdrant" status={health.qdrant} />
              <Badge name="Ollama" status={health.ollama} />
            </>
          ) : (
            <span className="text-xs text-gray-400">checking services…</span>
          )}
        </div>
      </div>
    </header>
  );
}
