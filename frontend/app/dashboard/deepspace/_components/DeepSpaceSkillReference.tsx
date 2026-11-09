"use client";

import { AverQelLoader } from "@/app/components/ui/AverQelLoading";

import { useEffect, useMemo, useRef, useState } from "react";
import { BookOpen } from "lucide-react";
import { fetchWithAuth } from "@/lib/api";

interface Skill {
  name: string;
  description: string;
}

interface DeepSpaceSkillReferenceProps {
  conversationId: string | null;
  onInsert: (text: string) => void;
  /** The current composer query to match against. */
  query: string;
}

export function DeepSpaceSkillReference({
  conversationId,
  onInsert,
  query,
}: DeepSpaceSkillReferenceProps) {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState(false);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const menuRef = useRef<HTMLDivElement>(null);

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
        if (!cancelled) setSkills(data ?? []);
      } catch {
        if (!cancelled) setSkills([]);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [conversationId]);

  const filtered = useMemo(() => {
    const needle = query.slice(1).toLowerCase();
    const availableSkills = conversationId ? skills : [];
    return availableSkills.filter(
      (s) =>
        s.name.toLowerCase().includes(needle) ||
        s.description.toLowerCase().includes(needle),
    );
  }, [conversationId, query, skills]);

  const activeIndex = Math.min(selectedIndex, Math.max(filtered.length - 1, 0));

  useEffect(() => {
    if (!open) return;
    function onClickOutside(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", onClickOutside);
    return () => document.removeEventListener("mousedown", onClickOutside);
  }, [open]);

  function insertSkill(skill: Skill) {
    onInsert(`@skill:${skill.name} `);
    setOpen(false);
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (!open) return;
    switch (e.key) {
      case "ArrowDown":
        e.preventDefault();
        setSelectedIndex(Math.min(activeIndex + 1, filtered.length - 1));
        break;
      case "ArrowUp":
        e.preventDefault();
        setSelectedIndex(Math.max(activeIndex - 1, 0));
        break;
      case "Enter":
        e.preventDefault();
        if (filtered[activeIndex]) insertSkill(filtered[activeIndex]);
        break;
      case "Escape":
        setOpen(false);
        break;
    }
  }

  const lastAtSkill = query.lastIndexOf("@skill:");
  const shouldShow = lastAtSkill !== -1 && !query.slice(lastAtSkill).includes(" ");

  if (!shouldShow || filtered.length === 0) return null;

  return (
    <div
      ref={menuRef}
      tabIndex={0}
      onKeyDown={handleKeyDown}
      className="fixed z-50 mt-1 w-[280px] rounded-lg border border-white/10 bg-background/95 backdrop-blur-sm shadow-lg dark:bg-slate-950/95"
      role="listbox"
      aria-label="Skill reference"
    >
      <div className="flex items-center gap-2 px-3 py-2 border-b border-white/10">
        <BookOpen size={14} className="text-foreground/60" aria-hidden="true" />
        <span className="text-sm font-medium">Skills</span>
        {loading && <AverQelLoader size={12} className="animate-spin ml-auto text-foreground/40" />}
      </div>
      <div className="max-h-[240px] overflow-y-auto custom-scrollbar">
        {filtered.map((skill, index) => (
          <button
            key={skill.name}
            type="button"
            role="option"
            aria-selected={index === activeIndex}
            onClick={() => insertSkill(skill)}
            onMouseEnter={() => setSelectedIndex(index)}
            className={`w-full flex items-center gap-2 px-3 py-2 text-left text-sm ${
              index === activeIndex
                ? "bg-primary/10 text-primary"
                : "text-foreground/70 hover:bg-white/5 dark:hover:bg-black/5"
            }`}
          >
            <span className="font-medium truncate">{skill.name}</span>
            {skill.description && (
              <span className="ml-auto text-[10px] text-foreground/40 truncate max-w-[120px]">
                {skill.description}
              </span>
            )}
          </button>
        ))}
      </div>
    </div>
  );
}

export default DeepSpaceSkillReference;
