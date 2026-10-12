"use client";

import { FormEvent, useEffect, useState } from "react";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000";

type Field = {
  key: string;
  value: string;
};

type Section = {
  dimension: string;
  fixed: boolean;
  fields: Field[];
};

export default function ProfilePanel({ onClose }: { onClose: () => void }) {
  const [sections, setSections] = useState<Section[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const [removed, setRemoved] = useState<Array<Field & { dimension: string }>>([]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const response = await fetch(`${API_BASE}/profile`);
        if (!response.ok) {
          throw new Error("读取失败");
        }
        const body = (await response.json()) as { sections: Section[] };
        if (!cancelled) {
          setSections(body.sections);
        }
      } catch {
        if (!cancelled) {
          setError("用户信息没有读出来");
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  function updateField(dimension: string, index: number, patch: Partial<Field>) {
    setSaved(false);
    setSections((current) =>
      current.map((section) => {
        if (section.dimension !== dimension) {
          return section;
        }
        const fields = section.fields.map((field, fieldIndex) =>
          fieldIndex === index ? { ...field, ...patch } : field
        );
        return { ...section, fields };
      })
    );
  }

  function addField(dimension: string) {
    setSaved(false);
    setSections((current) =>
      current.map((section) =>
        section.dimension === dimension
          ? { ...section, fields: [...section.fields, { key: "", value: "" }] }
          : section
      )
    );
  }

  function removeField(dimension: string, index: number) {
    const field = sections.find((section) => section.dimension === dimension)?.fields[index];
    setSaved(false);
    if (field?.key) {
      setRemoved((items) => [...items, { dimension, key: field.key, value: "" }]);
    }
    setSections((current) =>
      current.map((section) =>
        section.dimension === dimension
          ? { ...section, fields: section.fields.filter((_, fieldIndex) => fieldIndex !== index) }
          : section
      )
    );
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    setSaving(true);
    setError("");
    setSaved(false);
    try {
      const facts = [
        ...sections.flatMap((section) =>
          section.fields.map((field) => ({
            dimension: section.dimension,
            key: field.key,
            value: field.value,
          }))
        ),
        ...removed,
      ];
      const response = await fetch(`${API_BASE}/profile`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ facts }),
      });
      if (!response.ok) {
        throw new Error("保存失败");
      }
      const body = (await response.json()) as { sections: Section[] };
      setSections(body.sections);
      setRemoved([]);
      setSaved(true);
    } catch {
      setError("没有保存成功");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="profile-layer" role="presentation" onClick={onClose}>
      <form
        className="profile-card"
        aria-label="用户信息"
        onClick={(event) => event.stopPropagation()}
        onSubmit={save}
      >
        <div className="profile-head">
          <h2>用户信息</h2>
          <button type="button" className="new-chat" onClick={onClose}>
            关闭
          </button>
        </div>
        <p className="profile-note">对照下面的内容改一改，确认后会按你填写的保存。</p>
        {loading ? <p className="profile-note">正在读取…</p> : null}
        {sections.map((section) => (
          <fieldset className="profile-section" key={section.dimension}>
            <legend>{section.dimension}</legend>
            {section.fields.length === 0 ? <p className="profile-note">还没有记录。</p> : null}
            {section.fields.map((field, index) => (
              <label className="profile-row" key={`${section.dimension}-${index}`}>
                {section.fixed ? (
                  <span>{field.key}</span>
                ) : (
                  <input
                    aria-label={`${section.dimension}键`}
                    value={field.key}
                    placeholder="键"
                    onChange={(event) => updateField(section.dimension, index, { key: event.target.value })}
                  />
                )}
                <input
                  aria-label={section.fixed ? field.key : `${section.dimension}值`}
                  value={field.value}
                  placeholder="未填写"
                  onChange={(event) => updateField(section.dimension, index, { value: event.target.value })}
                />
                {section.fixed ? null : (
                  <button type="button" className="new-chat" onClick={() => removeField(section.dimension, index)}>
                    删除
                  </button>
                )}
              </label>
            ))}
            {section.fixed ? null : (
              <button type="button" className="new-chat" onClick={() => addField(section.dimension)}>
                添加
              </button>
            )}
          </fieldset>
        ))}
        {error ? <p className="error">{error}</p> : null}
        {saved ? <p className="profile-saved">已确认</p> : null}
        <div className="profile-actions">
          <button type="submit" className="send profile-save" disabled={loading || saving}>
            确认
          </button>
        </div>
      </form>
    </div>
  );
}
