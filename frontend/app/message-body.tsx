"use client";

import ReactMarkdown from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import "katex/dist/katex.min.css";

function convertDelimiters(source: string) {
  // 只替换已经闭合的定界符。流式输出写到一半时仍按原文显示。
  return source
    .replace(/\\\[([\s\S]+?)\\\]/g, (_, body: string) => `\n$$\n${body.trim()}\n$$\n`)
    .replace(/\\\(([\s\S]+?)\\\)/g, (_, body: string) => `$${body.trim()}$`);
}

function prepareMath(source: string) {
  const parts = source.split(/(```[\s\S]*?```)/g);
  return parts.map((part, index) => (index % 2 === 1 ? part : convertDelimiters(part))).join("");
}

export default function MessageBody({ text }: { text: string }) {
  return (
    <div className="rich">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[[rehypeKatex, { strict: "ignore" }]]}
      >
        {prepareMath(text)}
      </ReactMarkdown>
    </div>
  );
}
