"use client";

import { ModuleRegistryProvider } from "./_lib/module-registry";
import { BUILTIN_PANEL_MODULES } from "./_lib/builtin-modules";
import DeepSpacePageClient from "./_components/DeepSpacePageClient";

export default function DeepSpacePage() {
  return (
    <ModuleRegistryProvider initialModules={BUILTIN_PANEL_MODULES}>
      <DeepSpacePageClient />
    </ModuleRegistryProvider>
  );
}
