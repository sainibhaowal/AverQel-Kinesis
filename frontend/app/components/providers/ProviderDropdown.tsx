"use client";

import { AnimatePresence, motion } from "framer-motion";
import { Check, ChevronDown, Search } from "lucide-react";
import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";

interface ProviderDropdownOption {
  value: string;
  label: string;
  hint?: string;
  searchText?: string;
  icon?: ReactNode;
}

interface ProviderDropdownProps {
  label?: string;
  value: string;
  options: ProviderDropdownOption[];
  onChange: (value: string) => void;
  placeholder?: string;
  disabled?: boolean;
  name?: string;
  className?: string;
  searchable?: boolean;
  searchPlaceholder?: string;
  allowCustomValue?: boolean;
}

function normalizeSearchText(value: string): string {
  return value
    .normalize("NFKD")
    .replace(/\p{M}/gu, "")
    .toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, " ")
    .trim();
}

const emptySubscribe = () => () => undefined;
const clientMountedSnapshot = () => true;
const serverMountedSnapshot = () => false;

export default function ProviderDropdown({
  label,
  value,
  options,
  onChange,
  placeholder = "Select an option",
  disabled = false,
  name,
  className,
  searchable = false,
  searchPlaceholder = "Search options",
  allowCustomValue = false,
}: ProviderDropdownProps) {
  const [open, setOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const mounted = useSyncExternalStore(
    emptySubscribe,
    clientMountedSnapshot,
    serverMountedSnapshot,
  );
  const [dropdownStyle, setDropdownStyle] = useState<React.CSSProperties>({});
  const rootRef = useRef<HTMLDivElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  const searchInputRef = useRef<HTMLInputElement | null>(null);
  const selectId = useId();

  const selectedOption = useMemo(
    () => options.find((option) => option.value === value) ?? null,
    [options, value],
  );

  const matchingOptions = useMemo(() => {
    const query = normalizeSearchText(searchQuery);
    if (!searchable || !query) return options;

    const terms = query.split(/\s+/).filter(Boolean);
    return options.filter((option) => {
      const haystack = normalizeSearchText(
        [option.value, option.label, option.hint, option.searchText].filter(Boolean).join(" "),
      );
      const compactHaystack = haystack.replace(/\s/g, "");
      return terms.every((term) => haystack.includes(term) || compactHaystack.includes(term));
    });
  }, [options, searchQuery, searchable]);

  const customValueOption = useMemo<ProviderDropdownOption | null>(() => {
    const customValue = searchQuery.trim();
    if (!searchable || !allowCustomValue || !customValue) return null;
    const normalizedValue = normalizeSearchText(customValue);
    if (options.some((option) => normalizeSearchText(option.value) === normalizedValue))
      return null;
    const displayValue = customValue.length > 80 ? `${customValue.slice(0, 77)}…` : customValue;
    return {
      value: customValue,
      label: `Use exact ID: ${displayValue}`,
      hint: "Use this model ID even if discovery does not list it.",
    };
  }, [allowCustomValue, options, searchQuery, searchable]);

  const visibleOptions = customValueOption
    ? [...matchingOptions, customValueOption]
    : matchingOptions;

  function handleOpen() {
    if (disabled) return;
    if (!open && buttonRef.current) {
      const rect = buttonRef.current.getBoundingClientRect();
      setDropdownStyle({
        position: "fixed",
        top: rect.bottom + 4,
        left: rect.left,
        width: rect.width,
        zIndex: 9999,
      });
      setSearchQuery("");
    }
    setOpen((c) => !c);
  }

  function closeDropdown() {
    setOpen(false);
    setSearchQuery("");
  }

  function selectOption(option: ProviderDropdownOption) {
    onChange(option.value);
    closeDropdown();
  }

  useEffect(() => {
    function handlePointerDown(event: MouseEvent) {
      const target = event.target as Node;
      if (!rootRef.current?.contains(target) && !menuRef.current?.contains(target)) {
        setOpen(false);
        setSearchQuery("");
      }
    }
    function handleEscape(event: KeyboardEvent) {
      if (event.key === "Escape" && open) {
        setOpen(false);
        setSearchQuery("");
      }
    }
    function handleScroll() {
      if (open && buttonRef.current) {
        const rect = buttonRef.current.getBoundingClientRect();
        setDropdownStyle((s) => ({
          ...s,
          top: rect.bottom + 4,
          left: rect.left,
          width: rect.width,
        }));
      }
    }
    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("keydown", handleEscape);
    window.addEventListener("scroll", handleScroll, true);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("keydown", handleEscape);
      window.removeEventListener("scroll", handleScroll, true);
    };
  }, [open]);

  useEffect(() => {
    if (open && searchable) searchInputRef.current?.focus();
  }, [open, searchable]);

  function handleSearchKeyDown(event: React.KeyboardEvent<HTMLInputElement>) {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      menuRef.current?.querySelector<HTMLButtonElement>('[role="option"]')?.focus();
    } else if (event.key === "Enter") {
      event.preventDefault();
      if (visibleOptions[0]) selectOption(visibleOptions[0]);
    }
  }

  const menu = (
    <AnimatePresence>
      {open && !disabled ? (
        <motion.div
          id={`${selectId}-menu`}
          initial={{ opacity: 0, y: 6, scale: 0.98 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          exit={{ opacity: 0, y: 4, scale: 0.98 }}
          transition={{ duration: 0.14 }}
          ref={menuRef}
          style={{
            ...dropdownStyle,
            maxHeight: "400px",
            overflowY: "auto",
          }}
          className="rounded-2xl border border-slate-200 bg-white p-2 shadow-2xl backdrop-blur-xl dark:border-white/10 dark:bg-slate-900"
        >
          {searchable ? (
            <div className="sticky top-0 z-10 mb-2 space-y-1.5 bg-white pb-2 dark:bg-slate-900">
              <label htmlFor={`${selectId}-search`} className="sr-only">
                {searchPlaceholder}
              </label>
              <div className="flex h-9.5 items-center gap-2.5 rounded-xl border border-slate-200 bg-slate-50 px-3 transition-colors focus-within:border-emerald-500 focus-within:ring-2 focus-within:ring-emerald-500/20 dark:border-white/10 dark:bg-slate-800">
                <Search size={14} aria-hidden="true" className="shrink-0 text-slate-400" />
                <input
                  ref={searchInputRef}
                  id={`${selectId}-search`}
                  type="text"
                  role="searchbox"
                  value={searchQuery}
                  onChange={(event) => setSearchQuery(event.target.value)}
                  onKeyDown={handleSearchKeyDown}
                  placeholder={searchPlaceholder}
                  autoComplete="off"
                  className="search-box-input provider-dropdown-search-input min-w-0 flex-1 !border-0 !border-none !bg-transparent p-0 text-xs font-medium text-slate-900 !shadow-none !ring-0 !outline-none placeholder:text-slate-400 focus:!border-none focus:!ring-0 focus:!outline-none dark:text-white"
                />
                <span className="shrink-0 font-mono text-[10px] font-bold text-slate-400 tabular-nums">
                  {matchingOptions.length}/{options.length}
                </span>
              </div>
            </div>
          ) : null}

          <div id={`${selectId}-options`} role="listbox" aria-label={label || placeholder}>
            {visibleOptions.length === 0 ? (
              <div className="rounded-xl px-3 py-3 text-sm font-medium text-slate-500">
                {searchable && searchQuery.trim()
                  ? "No models match your search."
                  : "No options available"}
              </div>
            ) : (
              visibleOptions.map((option) => {
                const selected = option.value === value;
                return (
                  <button
                    key={`${name || label}-option-${option.value}`}
                    type="button"
                    role="option"
                    aria-selected={selected}
                    onClick={() => selectOption(option)}
                    className={`flex w-full items-start justify-between gap-3 rounded-xl px-3.5 py-2.5 text-left transition-colors ${
                      selected
                        ? "bg-gradient-to-r from-emerald-600 to-teal-600 font-black !text-white shadow-xs dark:from-emerald-500 dark:to-teal-500 dark:!text-slate-950"
                        : "font-semibold text-slate-800 hover:bg-slate-100 dark:text-slate-200 dark:hover:bg-slate-800"
                    }`}
                  >
                    <span className="flex min-w-0 items-start gap-2.5">
                      {option.icon ? (
                        <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center">
                          {option.icon}
                        </span>
                      ) : null}
                      <span className="min-w-0">
                        <span
                          className={`block text-xs font-semibold ${
                            selected ? "text-white dark:text-slate-950" : ""
                          }`}
                        >
                          {option.label}
                        </span>
                        {option.hint ? (
                          <span
                            className={`mt-0.5 block text-[11px] ${
                              selected
                                ? "text-white/90 dark:text-slate-950/75"
                                : "text-muted-foreground"
                            }`}
                          >
                            {option.hint}
                          </span>
                        ) : null}
                      </span>
                    </span>
                    {selected ? (
                      <Check size={14} className="mt-0.5 shrink-0 text-white dark:text-slate-950" />
                    ) : null}
                  </button>
                );
              })
            )}
          </div>
        </motion.div>
      ) : null}
    </AnimatePresence>
  );

  return (
    <div ref={rootRef} className="space-y-2">
      {label && (
        <label
          htmlFor={selectId}
          className="text-foreground/45 block text-[10px] font-semibold tracking-[0.22em] uppercase"
        >
          {label}
        </label>
      )}

      <select
        id={selectId}
        name={name}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        disabled={disabled}
        className="sr-only"
      >
        <option value="">{placeholder}</option>
        {allowCustomValue && value && !options.some((option) => option.value === value) ? (
          <option value={value}>{value}</option>
        ) : null}
        {options.map((option) => (
          <option key={`${name || label}-${option.value}`} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>

      <button
        ref={buttonRef}
        type="button"
        aria-expanded={open}
        aria-haspopup="listbox"
        aria-controls={`${selectId}-menu`}
        disabled={disabled}
        onClick={handleOpen}
        className={`flex w-full items-center justify-between gap-3 rounded-xl border border-slate-300 bg-white px-3.5 py-2.5 text-left text-xs font-bold text-slate-900 shadow-2xs transition-colors outline-none hover:border-emerald-500/60 focus:border-emerald-500 focus:ring-2 focus:ring-emerald-500/20 disabled:cursor-not-allowed disabled:opacity-50 dark:border-white/15 dark:bg-slate-900 dark:text-white ${className || ""}`}
      >
        <span
          className={`flex min-w-0 items-center gap-2 ${selectedOption || (allowCustomValue && value) ? "font-bold text-slate-900 dark:text-white" : "font-medium text-slate-400"}`}
        >
          {selectedOption?.icon ? (
            <span className="flex h-5 w-5 shrink-0 items-center justify-center">
              {selectedOption.icon}
            </span>
          ) : null}
          <span className="truncate">
            {selectedOption?.label || (allowCustomValue && value) || placeholder}
          </span>
        </span>
        <ChevronDown
          size={16}
          className={`shrink-0 text-slate-500 transition-transform dark:text-slate-400 ${open ? "rotate-180" : ""}`}
        />
      </button>

      {mounted ? createPortal(menu, document.body) : null}
    </div>
  );
}
