"use client";

import {
  Briefcase,
  FolderTree,
  GitBranch,
  Globe,
  History,
  Package,
  SquareTerminal,
} from "lucide-react";

import type { ModuleManifest } from "./module-registry";

/**
 * The side panel's built-in tabs, published through the module registry.
 *
 * The tab bodies stay inside `DeepSpaceSidePanel` because they share the
 * panel's terminal, browser, and workspace state. The registry owns the tab
 * metadata, so another module can add a tab without editing the shell.
 */
function PanelBodyPlaceholder() {
  return null;
}

interface BuiltinTab {
  id: string;
  name: string;
  description: string;
  icon: ModuleManifest["provides"][number]["icon"];
  priority: number;
}

const BUILTIN_TABS: BuiltinTab[] = [
  { id: "files", name: "Files", description: "Workspace files", icon: FolderTree, priority: 10 },
  {
    id: "terminal",
    name: "Terminal",
    description: "Interactive terminal sessions",
    icon: SquareTerminal,
    priority: 9,
  },
  { id: "browser", name: "Browser", description: "Browser preview", icon: Globe, priority: 8 },
  { id: "jobs", name: "Jobs", description: "Background jobs", icon: Briefcase, priority: 7 },
  {
    id: "workflows",
    name: "Workflows",
    description: "Workflow runs",
    icon: GitBranch,
    priority: 6,
  },
  {
    id: "trajectory",
    name: "Trajectory",
    description: "Conversation trajectory",
    icon: History,
    priority: 5,
  },
  {
    id: "deliverables",
    name: "Deliverables",
    description: "Delivered artifacts",
    icon: Package,
    priority: 4,
  },
];

export const BUILTIN_PANEL_MODULES: ModuleManifest[] = BUILTIN_TABS.map((tab) => ({
  id: `builtin-${tab.id}-panel`,
  name: tab.name,
  version: "1.0.0",
  provides: [
    {
      slot: "panel-tab",
      id: tab.id,
      name: tab.name,
      label: tab.name,
      icon: tab.icon,
      component: PanelBodyPlaceholder,
      priority: tab.priority,
    },
  ],
}));

/** The ids of the tabs the side panel renders itself. */
export const BUILTIN_PANEL_TAB_IDS = BUILTIN_TABS.map((tab) => tab.id);
