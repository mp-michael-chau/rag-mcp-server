"use client";

import { useSyncExternalStore } from "react";
import CodeMirror from "@uiw/react-codemirror";
import { markdown } from "@codemirror/lang-markdown";

function subscribePrefersDark(onChange: () => void): () => void {
  const mq = window.matchMedia("(prefers-color-scheme: dark)");
  mq.addEventListener("change", onChange);
  return () => mq.removeEventListener("change", onChange);
}

function usePrefersDark(): boolean {
  return useSyncExternalStore(
    subscribePrefersDark,
    () => window.matchMedia("(prefers-color-scheme: dark)").matches,
    () => false,
  );
}

export default function MarkdownEditor({
  value,
  onChange,
}: {
  value: string;
  onChange?: (value: string) => void;
}) {
  const dark = usePrefersDark();
  return (
    <div className="h-[calc(100vh-16rem)] min-h-96 overflow-hidden rounded-md border border-input">
      <CodeMirror
        className="h-full"
        height="100%"
        value={value}
        onChange={(v) => onChange?.(v)}
        extensions={[markdown()]}
        editable={onChange !== undefined}
        theme={dark ? "dark" : "light"}
        basicSetup={{ foldGutter: true, highlightActiveLine: true }}
      />
    </div>
  );
}
