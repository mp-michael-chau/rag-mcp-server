"use client";

import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

export default function MarkdownPreview({ content }: { content: string }) {
  return (
    <div className="prose prose-sm dark:prose-invert h-[calc(100vh-16rem)] min-h-96 max-w-none overflow-auto rounded-md border border-input bg-background p-4">
      <Markdown remarkPlugins={[remarkGfm]}>{content}</Markdown>
    </div>
  );
}
