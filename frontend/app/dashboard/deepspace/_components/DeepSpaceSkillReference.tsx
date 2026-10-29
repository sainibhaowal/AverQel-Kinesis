"use client";

import { useEffect, useRef, useState } from "react";
import { BookOpen, ChevronDown, X, Loader2, Search } from "lucide-react";
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
  const [filtered, setFiltered] = useState<Skill[]>([]);
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState(false);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const menuRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!conversationId) {
      setSkills([]);
      return;
    }
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

  useEffect(() => {
    const needle = query.slice(1).toLowerCase();
    const list = skills.filter(
      (s) =>
        s.name.toLowerCase().includes(needle) ||
        s.description.toLowerCase().includes(needle),
    );
    setFiltered(list);
    setSelectedIndex(0);
  }, [query, skills]);

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
        setSelectedIndex((i) => Math.min(i + 1, filtered.length - 1));
        break;
      case "ArrowUp":
        e.preventDefault();
        setSelectedIndex((i) => Math.max(i - 1, 0));
        break;
      case "Enter":
        e.preventDefault();
        if (filtered[selectedIndex]) insertSkill(filtered[selectedIndex]);
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
      className="fixed z-50 mt-1 w-[280px] rounded-lg border border-white/10 bg-background/95 backdrop-blur-sm shadow-lg dark:bg-slate-950/95"
      role="listbox"
      aria-label="Skill reference"
    >
      <div className="flex items-center gap-2 px-3 py-2 border-b border-white/10">
        <BookOpen size={14} className="text-foreground/60" aria-hidden="true" />
        <span className="text-sm font-medium">Skills</span>
        {loading && <Loader2 size={12} className="animate-spin ml-auto text-foreground/40" />}
      </div>
      <div className="max-h-[240px] overflow-y-auto custom-scrollbar">
        {filtered.map((skill, index) => (
          <button
            key={skill.name}
            type="button"
            role="option"
            aria-selected={index === selectedIndex}
            onClick={() => insertSkill(skill)}
            onMouseEnter={() => setSelectedIndex(index)}
            className={`w-full flex items-center gap-2 px-3 py-2 text-left text-sm ${
              index === selectedIndex
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
