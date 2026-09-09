"use client";

import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

export default function MarkdownPreview({ content }: { content: string }) {
  return (
    <div className="prose prose-sm max-w-none h-[calc(100vh-16rem)] min-h-96 overflow-auto rounded-md border border-gray-300 bg-white p-4">
      <Markdown remarkPlugins={[remarkGfm]}>{content}</Markdown>
    </div>
  );
}
