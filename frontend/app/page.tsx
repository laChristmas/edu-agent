"use client";

import {
  ChangeEvent,
  ClipboardEvent as ReactClipboardEvent,
  DragEvent as ReactDragEvent,
  FormEvent,
  PointerEvent as ReactPointerEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import MessageBody from "./message-body";
import ProfilePanel from "./profile-panel";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000";
const CONVERSATION_KEY = "eduagent.conversation_id";
// 与 config.toml 里的 max_bytes 一致。先在页面拦住，避免选中后再被退回。
const PHOTO_BYTES = 16_000_000;
const PHOTO_TYPES = new Set(["image/jpeg", "image/png", "image/webp"]);

// 开发模式会把打开跑两遍。共用同一次请求，避免连写两条简报。
let openingDialog: Promise<{ id: string | null; messages: ChatMessage[] }> | null = null;

type Role = "user" | "assistant";

type ChatMessage = {
  role: Role;
  content: string;
  imageId?: string;
};

type ChatEvent = {
  type: "conversation" | "delta" | "done" | "error" | "later";
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
  const [photo, setPhoto] = useState<File | null>(null);
  const [photoUrl, setPhotoUrl] = useState<string | null>(null);
  const resizeDrag = useRef<{ y: number; height: number } | null>(null);
  const messagesRef = useRef<HTMLDivElement>(null);
  const shownRef = useRef(messages);
  shownRef.current = messages;
  const fileRef = useRef<HTMLInputElement>(null);
  const dragDepth = useRef(0);
  const [dragging, setDragging] = useState(false);

  const openDialog = useCallback(async () => {
    setRestoring(true);
    setError("");
    // 打开时先清空。上一段即使还指在本地，也不再铺进会话框。
    setMessages([]);
    setConversationId(null);
    localStorage.removeItem(CONVERSATION_KEY);
    try {
      if (!openingDialog) {
        openingDialog = fetch(`${API_BASE}/briefing`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ conversation_id: null }),
        }).then(async (response) => {
          if (!response.ok) {
            throw new Error("briefing");
          }
          return (await response.json()) as { id: string | null; messages: ChatMessage[] };
        });
      }
      const body = await openingDialog;
      if (body.id) {
        setConversationId(body.id);
        localStorage.setItem(CONVERSATION_KEY, body.id);
      } else {
        setConversationId(null);
        localStorage.removeItem(CONVERSATION_KEY);
      }
      setMessages(body.messages);
    } catch {
      setError("简报没有写成");
      setMessages([]);
    } finally {
      openingDialog = null;
      setRestoring(false);
    }
  }, []);

  function startNew() {
    if (sending || restoring) {
      return;
    }
    // 放下这一段再打开。已落库的原文仍留在库里，只是不显示。
    clearPhoto();
    void openDialog();
  }

  function clearPhoto() {
    if (photoUrl) {
      URL.revokeObjectURL(photoUrl);
    }
    setPhoto(null);
    setPhotoUrl(null);
    if (fileRef.current) {
      fileRef.current.value = "";
    }
  }

  function takePhoto(file: File | null) {
    if (!file || sending || restoring) {
      return;
    }
    // 类型空着时留给服务端认文件头。明确不是这三种的，当场退回。
    if ((file.type && !PHOTO_TYPES.has(file.type)) || file.size === 0 || file.size > PHOTO_BYTES) {
      setError("照片格式或大小不符合");
      return;
    }
    setError("");
    if (photoUrl) {
      URL.revokeObjectURL(photoUrl);
    }
    setPhoto(file);
    setPhotoUrl(URL.createObjectURL(file));
    if (fileRef.current) {
      fileRef.current.value = "";
    }
  }

  function looksLikePhoto(file: File): boolean {
    if (file.type.startsWith("image/")) {
      return true;
    }
    return /\.(jpe?g|png|webp)$/i.test(file.name);
  }

  function imageFile(files: FileList | null, items: DataTransferItemList | null): File | null {
    const listed = files ? [...files].find(looksLikePhoto) : null;
    if (listed) {
      return listed;
    }
    if (!items) {
      return null;
    }
    for (const item of items) {
      if (item.kind === "file" && item.type.startsWith("image/")) {
        const file = item.getAsFile();
        if (file && looksLikePhoto(file)) {
          return file;
        }
      }
    }
    return null;
  }

  function choosePhoto(event: ChangeEvent<HTMLInputElement>) {
    takePhoto(event.target.files?.[0] ?? null);
  }

  function enterDrag(event: ReactDragEvent<HTMLElement>) {
    if (!event.dataTransfer.types.includes("Files")) {
      return;
    }
    event.preventDefault();
    dragDepth.current += 1;
    setDragging(true);
  }

  function moveDrag(event: ReactDragEvent<HTMLElement>) {
    if (!event.dataTransfer.types.includes("Files")) {
      return;
    }
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
  }

  function leaveDrag(event: ReactDragEvent<HTMLElement>) {
    if (!event.dataTransfer.types.includes("Files")) {
      return;
    }
    event.preventDefault();
    dragDepth.current = Math.max(0, dragDepth.current - 1);
    if (dragDepth.current === 0) {
      setDragging(false);
    }
  }

  function dropPhoto(event: ReactDragEvent<HTMLElement>) {
    if (!event.dataTransfer.types.includes("Files")) {
      return;
    }
    event.preventDefault();
    dragDepth.current = 0;
    setDragging(false);
    const file = imageFile(event.dataTransfer.files, event.dataTransfer.items);
    if (!file) {
      setError("照片格式或大小不符合");
      return;
    }
    takePhoto(file);
  }

  function pastePhoto(event: ReactClipboardEvent<HTMLElement>) {
    const file = imageFile(event.clipboardData.files, event.clipboardData.items);
    if (!file) {
      return;
    }
    event.preventDefault();
    takePhoto(file);
  }

  useEffect(() => {
    void openDialog();
  }, [openDialog]);

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

  function rememberConversation(id: string) {
    // 会话编号只在流的第一条事件里给出。这里不留下，后面的文字就到不了页面。
    setConversationId(id);
    localStorage.setItem(CONVERSATION_KEY, id);
  }

  async function send(event: FormEvent) {
    event.preventDefault();
    const content = draft.trim();
    const chosen = photo;
    if ((!content && !chosen) || sending || restoring) {
      return;
    }

    setDraft("");
    setError("");
    setSending(true);
    let imageId: string | null = null;
    let nextConversation = conversationId;
    let waitForLater = false;
    try {
      if (chosen) {
        const form = new FormData();
        form.append("file", chosen);
        if (conversationId) {
          form.append("conversation_id", conversationId);
        }
        const uploaded = await fetch(`${API_BASE}/images`, { method: "POST", body: form });
        if (!uploaded.ok) {
          setDraft(content);
          setError(uploaded.status === 400 ? "照片格式或大小不符合" : "照片没有收下");
          return;
        }
        const saved = (await uploaded.json()) as { id: string; conversation_id: string };
        imageId = saved.id;
        nextConversation = saved.conversation_id;
        rememberConversation(saved.conversation_id);
        clearPhoto();
      }
      const shown = content || "（一张照片）";
      setMessages((current) => [
        ...current,
        { role: "user", content: shown, imageId: imageId ?? undefined },
        { role: "assistant", content: "" },
      ]);
      const response = await fetch(`${API_BASE}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          content,
          conversation_id: nextConversation,
          image_id: imageId,
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
            nextConversation = payload.id;
          } else if (payload.type === "later") {
            waitForLater = true;
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
      if (waitForLater && nextConversation) {
        void watchFollowUp(nextConversation);
      }
    }
  }

  async function watchFollowUp(conversation: string) {
    // 周计划排完才补一句。流已经结束，这里只把后补的那句接上，不挡住下一轮。
    const started = Date.now();
    while (Date.now() - started < 45000) {
      await new Promise((resolve) => setTimeout(resolve, 1500));
      try {
        const response = await fetch(`${API_BASE}/conversations/${conversation}/messages`);
        if (!response.ok) {
          continue;
        }
        const body = (await response.json()) as { messages: ChatMessage[] };
        const known = new Set(
          shownRef.current.filter((item) => item.role === "assistant").map((item) => item.content),
        );
        const extra = body.messages.filter(
          (item) => item.role === "assistant" && item.content && !known.has(item.content),
        );
        if (!extra.length) {
          continue;
        }
        setMessages((current) => {
          const have = new Set(
            current.filter((item) => item.role === "assistant").map((item) => item.content),
          );
          const fresh = extra.filter((item) => !have.has(item.content));
          if (!fresh.length) {
            return current;
          }
          return [...current, ...fresh.map((item) => ({ role: "assistant" as const, content: item.content }))];
        });
        return;
      } catch {
        // 接口重启时继续等，后补的那句还在原来的会话里。
      }
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
      <section
        className={dragging ? "dialog dragging" : "dialog"}
        aria-label="对话"
        onDragEnter={enterDrag}
        onDragOver={moveDrag}
        onDragLeave={leaveDrag}
        onDrop={dropPhoto}
        onPaste={pastePhoto}
      >
        {dragging ? <div className="drop-hint">松开即可添加照片</div> : null}
        <div className="messages" ref={messagesRef}>
          {messages.length === 0 && !restoring ? (
            <p className="empty">写在下面，讲解会出现在这里。</p>
          ) : null}
          {messages.map((message, index) => (
            <div className={`bubble ${message.role}`} key={`${message.role}-${index}`}>
              {message.imageId ? (
                <img className="photo" src={`${API_BASE}/images/${message.imageId}`} alt="发送的照片" />
              ) : null}
              {message.role === "assistant" && message.content ? (
                <MessageBody text={message.content} />
              ) : message.content ? (
                message.content
              ) : (
                <span className="pending" aria-label="正在回复" />
              )}
            </div>
          ))}
        </div>
        {error ? <p className="error">{error}</p> : null}
        <form className="composer" onSubmit={send}>
          {photoUrl ? (
            <div className="photo-preview">
              <img src={photoUrl} alt="待发送的照片" />
              <button type="button" onClick={clearPhoto}>
                移除
              </button>
            </div>
          ) : null}
          <div className="composer-field">
            <input
              ref={fileRef}
              type="file"
              accept="image/jpeg,image/png,image/webp"
              hidden
              onChange={choosePhoto}
            />
            <button
              type="button"
              className="attach"
              aria-label="选择照片"
              disabled={sending || restoring}
              onClick={() => fileRef.current?.click()}
            >
              照片
            </button>
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
            <button type="submit" className="send" disabled={sending || restoring || (!draft.trim() && !photo)}>
              发送
            </button>
          </div>
        </form>
      </section>
      {profileOpen ? <ProfilePanel onClose={() => setProfileOpen(false)} /> : null}
    </main>
  );
}
