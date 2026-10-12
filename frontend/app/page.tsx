"use client";

import { FormEvent, PointerEvent as ReactPointerEvent, useEffect, useRef, useState } from "react";

import ProfilePanel from "./profile-panel";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000";
const CONVERSATION_KEY = "eduagent.conversation_id";

type Role = "user" | "assistant";

type ChatMessage = {
  role: Role;
  content: string;
};

type ChatEvent = {
  type: "conversation" | "delta" | "done" | "error";
  id?: string;
  text?: string;
  detail?: string;
};

export default function ChatPage() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const [inputHeight, setInputHeight] = useState(96);
  const [restoring, setRestoring] = useState(true);
  const [profileOpen, setProfileOpen] = useState(false);
  const resizeDrag = useRef<{ y: number; height: number } | null>(null);
  const messagesRef = useRef<HTMLDivElement>(null);

  function rememberConversation(id: string) {
    setConversationId(id);
    localStorage.setItem(CONVERSATION_KEY, id);
  }

  function startNew() {
    if (sending || restoring) {
      return;
    }
    // 只放下这一段的本地指针。已落库的原文仍留在库里。
    setMessages([]);
    setConversationId(null);
    setError("");
    localStorage.removeItem(CONVERSATION_KEY);
  }

  useEffect(() => {
    const saved = localStorage.getItem(CONVERSATION_KEY);
    if (!saved) {
      setRestoring(false);
      return;
    }

    let cancelled = false;
    (async () => {
      try {
        const response = await fetch(`${API_BASE}/conversations/${saved}/messages`);
        if (response.status === 404 || response.status === 422) {
          localStorage.removeItem(CONVERSATION_KEY);
          return;
        }
        if (!response.ok) {
          setConversationId(saved);
          setError("会话没有恢复");
          return;
        }
        const body = (await response.json()) as { id: string; messages: ChatMessage[] };
        if (cancelled) {
          return;
        }
        setConversationId(body.id);
        setMessages(body.messages);
      } catch {
        setConversationId(saved);
        setError("会话没有恢复");
      } finally {
        if (!cancelled) {
          setRestoring(false);
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const node = messagesRef.current;
    if (node) {
      node.scrollTop = node.scrollHeight;
    }
  }, [messages]);

  function startResize(event: ReactPointerEvent<HTMLButtonElement>) {
    event.preventDefault();
    resizeDrag.current = { y: event.clientY, height: inputHeight };
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function moveResize(event: ReactPointerEvent<HTMLButtonElement>) {
    const drag = resizeDrag.current;
    if (!drag) {
      return;
    }
    // 右上角往上拖才变高，避免贴着屏幕底边往下拉。
    const next = drag.height + (drag.y - event.clientY);
    setInputHeight(Math.min(420, Math.max(96, next)));
  }

  function endResize() {
    resizeDrag.current = null;
  }

  async function send(event: FormEvent) {
    event.preventDefault();
    const content = draft.trim();
    if (!content || sending || restoring) {
      return;
    }

    setDraft("");
    setError("");
    setSending(true);
    setMessages((current) => [
      ...current,
      { role: "user", content },
      { role: "assistant", content: "" },
    ]);

    try {
      const response = await fetch(`${API_BASE}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          content,
          conversation_id: conversationId,
        }),
      });

      if (response.status === 409) {
        // 服务端没有收下这一轮，把刚加上的两条收回去。
        setMessages((current) => current.slice(0, -2));
        setDraft(content);
        setError("上一轮尚未结束");
        return;
      }
      if (!response.ok || !response.body) {
        setMessages((current) => current.slice(0, -2));
        setDraft(content);
        setError("发送失败");
        return;
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value, { stream: !done });
        const parts = buffer.split("\n\n");
        buffer = parts.pop() ?? "";
        for (const part of parts) {
          const line = part.split("\n").find((item) => item.startsWith("data:"));
          if (!line) {
            continue;
          }
          const payload = JSON.parse(line.slice(5).trim()) as ChatEvent;
          if (payload.type === "conversation" && payload.id) {
            rememberConversation(payload.id);
          } else if (payload.type === "delta" && payload.text) {
            const text = payload.text;
            setMessages((current) => {
              const next = [...current];
              const last = next[next.length - 1];
              if (last?.role === "assistant") {
                next[next.length - 1] = { role: "assistant", content: last.content + text };
              }
              return next;
            });
          }
        }
        if (done) {
          break;
        }
      }
    } catch {
      setError("连接中断");
    } finally {
      setSending(false);
    }
  }

  return (
    <main className="page">
      <div className="top">
        <div>
          <h1>EduAgent</h1>
          <p className="intro">一个对话里的学习助手。全学科问答、错题和复习，都从这句话开始。</p>
        </div>
        <div className="top-actions">
          <button type="button" className="new-chat" onClick={() => setProfileOpen(true)}>
            用户信息
          </button>
          <button
            type="button"
            className="new-chat"
            onClick={startNew}
            disabled={sending || restoring || (!conversationId && messages.length === 0)}
          >
            新会话
          </button>
        </div>
      </div>
      <section className="dialog" aria-label="对话">
        <div className="messages" ref={messagesRef}>
          {messages.length === 0 && !restoring ? (
            <p className="empty">写在下面，讲解会出现在这里。</p>
          ) : null}
          {messages.map((message, index) => (
            <div className={`bubble ${message.role}`} key={`${message.role}-${index}`}>
              {message.content ? (
                message.content
              ) : (
                <span className="pending" aria-label="正在回复" />
              )}
            </div>
          ))}
        </div>
        {error ? <p className="error">{error}</p> : null}
        <form className="composer" onSubmit={send}>
          <div className="composer-field">
            <textarea
              aria-label="消息"
              style={{ height: inputHeight }}
              value={draft}
              placeholder="问一个问题"
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  event.currentTarget.form?.requestSubmit();
                }
              }}
            />
            <button
              type="button"
              className="resize-handle"
              aria-label="调整输入框高度"
              onPointerDown={startResize}
              onPointerMove={moveResize}
              onPointerUp={endResize}
              onPointerCancel={endResize}
            >
              <svg width="10" height="14" viewBox="0 0 10 14" aria-hidden="true">
                <circle cx="2.25" cy="2.25" r="1.15" />
                <circle cx="7.75" cy="2.25" r="1.15" />
                <circle cx="2.25" cy="7" r="1.15" />
                <circle cx="7.75" cy="7" r="1.15" />
                <circle cx="2.25" cy="11.75" r="1.15" />
                <circle cx="7.75" cy="11.75" r="1.15" />
              </svg>
            </button>
            <button type="submit" className="send" disabled={sending || restoring || !draft.trim()}>
              发送
            </button>
          </div>
        </form>
      </section>
      {profileOpen ? <ProfilePanel onClose={() => setProfileOpen(false)} /> : null}
    </main>
  );
}
