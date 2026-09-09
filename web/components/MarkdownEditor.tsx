"use client";

import CodeMirror from "@uiw/react-codemirror";
import { markdown } from "@codemirror/lang-markdown";

export default function MarkdownEditor({
  value,
  onChange,
}: {
  value: string;
  onChange?: (value: string) => void;
}) {
  return (
    <div className="h-[calc(100vh-16rem)] min-h-96 overflow-hidden rounded-md border border-gray-300">
      <CodeMirror
        value={value}
        onChange={(v) => onChange?.(v)}
        extensions={[markdown()]}
        editable={onChange !== undefined}
        basicSetup={{ foldGutter: true, highlightActiveLine: true }}
      />
    </div>
  );
}
