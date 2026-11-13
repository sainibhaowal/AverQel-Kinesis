"use client";

import { AverQelLoader } from "@/app/components/ui/AverQelLoading";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { BookOpen } from "lucide-react";
import { fetchWithAuth } from "@/lib/api";

interface Skill {
  name: string;
  description: string;
  provider?: string;
  source?: string;
  content_sha256?: string;
}

interface DeepSpaceSkillReferenceProps {
  conversationId: string | null;
  /** Receives the complete replacement query, not just the inserted token. */
  onSelect: (nextQuery: string) => void;
  /** The current composer query to match against. */
  query: string;
  /** The composer's current text selection, when it is not the end. */
  selectionStart?: number | null;
}

const TRIGGER = /^([@/][^\s]*)$/;

function tokenFor(query: string, selectionStart?: number | null) {
  const cursor = Math.max(0, Math.min(selectionStart ?? query.length, query.length));
  const before = query.slice(0, cursor);
  // A skill command is only valid as the whole prompt; the backend expands
  // `/name request` and `@skill:name request`, not a command embedded in text.
  const match = TRIGGER.exec(before);
  if (!match) return null;
  const token = match[1];
  return {
    start: 0,
    end: cursor,
    needle: token.slice(1).toLowerCase(),
    text: token,
  };
}

export function DeepSpaceSkillReference({
  conversationId,
  onSelect,
  query,
  selectionStart,
}: DeepSpaceSkillReferenceProps) {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [loading, setLoading] = useState(false);
  const [dismissedToken, setDismissedToken] = useState<string | null>(null);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const menuRef = useRef<HTMLDivElement>(null);

  const token = tokenFor(query, selectionStart);
  const shouldShow = Boolean(conversationId && token && token.text !== dismissedToken);

  useEffect(() => {
    if (!conversationId) return;
    const url = `/deepspace/chats/${encodeURIComponent(conversationId)}/skills?limit=100`;
    let cancelled = false;
    async function load() {
      setLoading(true);
      try {
        const response = await fetchWithAuth(url, { method: "GET" });
        if (!response.ok) throw new Error("Failed to load skills");
        const data = await response.json();
        if (!cancelled) setSkills((data ?? []) as Skill[]);
      } catch {
        if (!cancelled) setSkills([]);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, [conversationId]);

  const filtered = useMemo(() => {
    if (!token) return [];
    const needle = token.needle;
    return skills.filter(
      (skill) =>
        !needle ||
        skill.name.toLowerCase().includes(needle) ||
        skill.description.toLowerCase().includes(needle),
    );
  }, [skills, token]);

  const activeIndex = Math.min(selectedIndex, Math.max(filtered.length - 1, 0));

  const choose = useCallback(
    (skill: Skill) => {
      if (!token) return;
      const suffix = query.slice(token.end);
      const trailingSpace = suffix && !/^\s/.test(suffix) ? " " : suffix ? "" : " ";
      onSelect(`/${skill.name}${trailingSpace}${suffix}`);
    },
    [onSelect, query, token],
  );

  useEffect(() => {
    if (!shouldShow) return;
    function onPointerDown(event: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) {
        setDismissedToken(token?.text ?? null);
      }
    }
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [shouldShow, token?.text]);

  useEffect(() => {
    if (!shouldShow || filtered.length === 0) return;
    function onKeyDown(event: KeyboardEvent) {
      if (event.isComposing) return;
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        event.stopPropagation();
        setSelectedIndex((index) =>
          event.key === "ArrowDown"
            ? Math.min(index + 1, filtered.length - 1)
            : Math.max(index - 1, 0),
        );
      } else if ((event.key === "Enter" || event.key === "Tab") && !event.shiftKey) {
        event.preventDefault();
        event.stopPropagation();
        const skill = filtered[activeIndex];
        if (skill) choose(skill);
      } else if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        setDismissedToken(token?.text ?? null);
      }
    }
    window.addEventListener("keydown", onKeyDown, true);
    return () => window.removeEventListener("keydown", onKeyDown, true);
  }, [activeIndex, choose, filtered, shouldShow, token?.text]);

  if (!shouldShow) return null;

  return (
    <div
      ref={menuRef}
      className="border-border bg-background/95 absolute bottom-full left-0 z-[120] mb-2 w-[320px] max-w-[calc(100vw-2rem)] overflow-hidden rounded-lg border shadow-xl backdrop-blur-sm dark:bg-slate-950/95"
      role="listbox"
      aria-label="Skill reference"
    >
      <div className="border-border flex items-center gap-2 border-b px-3 py-2">
        <BookOpen size={14} className="text-foreground/60" aria-hidden="true" />
        <span className="text-sm font-medium">Skills</span>
        <span className="text-foreground/40 ml-auto text-[10px]">/name</span>
        {loading && <AverQelLoader size={12} className="text-foreground/40 ml-1 animate-spin" />}
      </div>
      <div className="custom-scrollbar max-h-[260px] overflow-y-auto">
        {filtered.length === 0 ? (
          <p className="text-foreground/50 px-3 py-3 text-xs">
            {loading ? "Loading skills…" : "No skill matches this text."}
          </p>
        ) : (
          filtered.map((skill, index) => (
            <button
              key={skill.name}
              type="button"
              role="option"
              aria-selected={index === activeIndex}
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => choose(skill)}
              onMouseEnter={() => setSelectedIndex(index)}
              className={`flex w-full flex-col gap-0.5 px-3 py-2 text-left text-sm ${
                index === activeIndex
                  ? "bg-primary/10 text-primary"
                  : "text-foreground/70 hover:bg-white/5 dark:hover:bg-black/5"
              }`}
            >
              <span className="flex w-full items-center gap-2">
                <span className="truncate font-medium">{skill.name}</span>
                {skill.provider ? (
                  <span className="text-foreground/35 ml-auto shrink-0 text-[9px] tracking-wide uppercase">
                    {skill.provider}
                  </span>
                ) : null}
              </span>
              {skill.description ? (
                <span className="text-foreground/45 line-clamp-2 text-[11px] leading-4">
                  {skill.description}
                </span>
              ) : null}
            </button>
          ))
        )}
      </div>
    </div>
  );
}

export default DeepSpaceSkillReference;
