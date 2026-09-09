import type { Metadata } from "next";
import "./globals.css";
import NavBar from "@/components/NavBar";
import { JobsProvider } from "@/components/JobsProvider";

export const metadata: Metadata = {
  title: "RAG Workflow",
  description: "Local document workflow for the shared RAG knowledge base",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="min-h-full bg-gray-50 text-gray-900">
        <JobsProvider>
          <NavBar />
          <main className="mx-auto max-w-6xl px-4 py-6">{children}</main>
        </JobsProvider>
      </body>
    </html>
  );
}
