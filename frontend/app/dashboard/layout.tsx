"use client";

import { useState, useEffect, useRef } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { motion, AnimatePresence } from "framer-motion";
import {
  LayoutDashboard,
  FileText,
  ShieldAlert,
  Settings as SettingsIcon,
  LogOut,
  Menu,
  Database,
  FileSearch,
  Sun,
  Moon,
  User,
  Activity,
  Trash2,
  Cable,
  X,
  ScrollText,
  Flag,
  PanelLeftClose,
  PanelLeftOpen,
} from "lucide-react";

import { useAuth } from "../context/AuthContext";
import { useTheme } from "../context/ThemeContext";
import AuthGuard from "@/app/components/layout/AuthGuard";
import NotificationCenter from "@/app/components/layout/NotificationCenter";
import AverQelLogo from "@/app/components/ui/AverQelLogo";
import DeepSpaceMark from "@/app/components/ui/DeepSpaceMark";
import QelConnectMark from "@/app/components/ui/QelConnectMark";
import WelcomeFlow from "@/app/components/onboarding/WelcomeFlow";
import { APP_VERSION } from "@/lib/release";
import Breadcrumbs from "@/app/components/ui/Breadcrumbs";
import SystemStatus from "@/app/components/dashboard/SystemStatus";
import BetaNoticeBanner from "@/app/components/dashboard/BetaNoticeBanner";
import { hasAdminRole, hasNamedRole } from "@/lib/roles";
import CodexRuntimeBridge from "@/app/components/providers/CodexRuntimeBridge";

type NavLeaf = {
  name: string;
  href: string;
  icon: React.ReactNode;
  exact?: boolean;
  isHeader?: false;
  admin?: boolean;
  requiresRole?: "feedback_manager" | "collection_moderator" | "support_manager";
};
type NavHeader = {
  name: string;
  href: string;
  isHeader: true;
  admin?: boolean;
  staff?: boolean;
  items: NavLeaf[];
};
type NavItem = NavLeaf | NavHeader;

const BASE_NAV_ITEMS: NavItem[] = [
  {
    name: "DeepSpace",
    href: "/dashboard/deepspace",
    icon: (
      <DeepSpaceMark size={20} source="/DeepSpace Logo/DeepSpace_Logo_Only_NoBlack_Exact.svg" />
    ),
  },
  { name: "Documents Hub", href: "/dashboard/documents", icon: <FileText size={20} /> },
  { name: "Query", href: "/dashboard/query", icon: <FileSearch size={20} /> },
  { name: "MCP Servers", href: "/dashboard/mcp", icon: <Cable size={20} /> },
  { name: "Settings", href: "/dashboard/settings", icon: <SettingsIcon size={20} /> },
];

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  const { user, loading: authLoading, userDisabled } = useAuth();
  const { theme, toggleTheme } = useTheme();
  const pathname = usePathname();
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [isMobile, setIsMobile] = useState(false);
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const navContainerRef = useRef<HTMLDivElement>(null);
  const mainContentRef = useRef<HTMLDivElement>(null);

  const isAdminRoute = pathname.startsWith("/dashboard/admin");
  const isQueryRoute = pathname === "/dashboard/query";
  const isDeepSpaceRoute =
    pathname === "/dashboard/deepspace" || pathname.startsWith("/dashboard/deepspace/");
  const isCollectionsRoute =
    pathname === "/dashboard/collections" || pathname.startsWith("/dashboard/collections/");
  const isFullHeightRoute = isQueryRoute || isDeepSpaceRoute || isCollectionsRoute;
  // DeepSpace settings contains a long, independently scrollable editor. It
  // shares the DeepSpace route prefix with the chat workspace, but must keep
  // its natural height so changing a large tool allowlist cannot clip the
  // sections below it.
  const isDeepSpaceSettingsRoute = pathname === "/dashboard/settings/deepspace";
  const isProvidersSettingsRoute = pathname === "/dashboard/settings/providers";
  const fillsAvailableHeight =
    (isFullHeightRoute && !isDeepSpaceSettingsRoute) || isProvidersSettingsRoute;
  const hasAdminAccess = user ? hasAdminRole(user.roles) : false;
  const hasFeedbackAccess = user ? hasNamedRole(user.roles, "feedback_manager") : false;
  const hasModerationAccess = user ? hasNamedRole(user.roles, "collection_moderator") : false;
  const hasSupportAccess = user ? hasNamedRole(user.roles, "support_manager") : false;
  const isFeedbackStaffPage = pathname.startsWith("/dashboard/admin/feedback");
  const isModeratorStaffPage = pathname.startsWith("/dashboard/admin/collections/moderation");
  const isSupportStaffPage = pathname.startsWith("/dashboard/admin/support");
  const canAccessStaffPage =
    (isFeedbackStaffPage && hasFeedbackAccess) ||
    (isModeratorStaffPage && hasModerationAccess) ||
    (isSupportStaffPage && hasAdminAccess && hasSupportAccess);

  const navItems = BASE_NAV_ITEMS;

  useEffect(() => {
    const handleResize = () => {
      const mobile = window.innerWidth < 1024;
      setIsMobile(mobile);
      if (mobile) {
        setSidebarOpen(false);
      } else {
        setSidebarOpen(true);
      }
    };
    handleResize();
    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, []);

  useEffect(() => {
    // The keyboard reaches the sidebar, so the collapsed and expanded states
    // are one shortcut apart.
    const onKey = (event: KeyboardEvent) => {
      if (!event.ctrlKey || !event.altKey) return;
      if (event.key.toLowerCase() !== "b") return;
      event.preventDefault();
      setSidebarOpen((open) => !open);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    document.documentElement.toggleAttribute("data-dashboard-nav-open", isMobile && mobileMenuOpen);

    return () => {
      document.documentElement.removeAttribute("data-dashboard-nav-open");
    };
  }, [isMobile, mobileMenuOpen]);

  const shouldShowDisabledOverlay = userDisabled;

  if (authLoading) return null;

  const NavLink = ({ item, onClick }: { item: NavLeaf; onClick?: () => void }) => {
    // Improved active check: exact match for home, startsWith for others but avoid overlaps
    const isActive = item.exact
      ? pathname === item.href
      : pathname.startsWith(item.href) && (item.href !== "/dashboard" || pathname === "/dashboard");

    const className = `group sweeping-light-btn relative flex h-11 items-center overflow-hidden rounded-xl transition-all duration-200 ${
      isActive
        ? "text-primary shadow-sm"
        : "text-foreground/60 hover:bg-foreground/[0.04] hover:text-foreground"
    } ${!sidebarOpen && !isMobile ? "justify-center px-0" : ""}`;
    const contents = (
      <motion.div
        className={`relative z-10 flex h-full w-full items-center ${!sidebarOpen && !isMobile ? "justify-center gap-0 px-0" : "gap-3 px-3"}`}
        whileTap={{ scale: 0.96 }}
        transition={{ type: "spring", stiffness: 450, damping: 26 }}
      >
        {/* Left Indicator - Premium Sliding Bar */}
        {isActive && (
          <motion.div
            layoutId="active-nav-indicator"
            className={`bg-primary absolute left-0 rounded-r-full ${
              !sidebarOpen && !isMobile ? "h-6 w-[3px]" : "h-5 w-[3px]"
            }`}
            transition={{ duration: 0.18, ease: [0.22, 1, 0.36, 1] }}
          />
        )}

        <div
          className={`flex h-6 w-6 shrink-0 items-center justify-center transition-transform duration-200 ${isActive ? "scale-110" : "group-hover:scale-110"}`}
        >
          {item.icon}
        </div>
        <motion.span
          initial={false}
          animate={{
            maxWidth: sidebarOpen || isMobile ? 140 : 0,
            opacity: sidebarOpen || isMobile ? 1 : 0,
          }}
          transition={{ duration: 0.16, ease: [0.22, 1, 0.36, 1] }}
          className={`block truncate overflow-hidden text-sm font-bold tracking-tight whitespace-nowrap ${isActive ? "" : "font-medium"}`}
          aria-hidden={!sidebarOpen && !isMobile}
        >
          {item.name}
        </motion.span>
      </motion.div>
    );
    const activePill = isActive ? (
      <motion.div
        layoutId="active-nav-pill"
        className="bg-primary/10 absolute inset-0 z-0 rounded-xl"
        transition={{ duration: 0.18, ease: [0.22, 1, 0.36, 1] }}
      />
    ) : null;

    return (
      <Link
        href={item.href}
        prefetch={false}
        data-active={isActive}
        onClick={onClick}
        className={className}
      >
        {contents}
        {activePill}
      </Link>
    );
  };

  const renderSidebarContent = () => (
    <>
      <div
        className={`flex h-18 items-center px-3.5 pt-2 ${!sidebarOpen && !isMobile ? "justify-center px-0" : "justify-between"}`}
      >
        <AverQelLogo
          size="footer"
          showWordmark
          disableAnimation
          animateWordmark={!isMobile}
          wordmarkVisible={sidebarOpen || isMobile}
        />
        {(sidebarOpen || isMobile) && isMobile && (
          <button
            onClick={() => setMobileMenuOpen(false)}
            className="text-muted-foreground hover:text-foreground transition-colors"
          >
            <X size={20} />
          </button>
        )}
      </div>
      <nav
        ref={navContainerRef}
        className="relative flex-1 space-y-2 overflow-x-hidden overflow-y-auto p-3"
      >
        {navItems.map((item, idx) => {
          if (item.admin && !hasAdminAccess) return null;

          if (item.isHeader) {
            return (
              <div key={`header-${idx}`} className="pt-4 pb-1">
                {(sidebarOpen || isMobile) && (
                  <p className="text-muted-foreground mb-1.5 px-3 text-[10px] font-black tracking-[0.22em] uppercase">
                    {item.name}
                  </p>
                )}
                <div className="space-y-1">
                  {item.items
                    .filter(
                      (sub) =>
                        (!sub.admin || hasAdminAccess) &&
                        (!sub.requiresRole || hasNamedRole(user?.roles, sub.requiresRole)),
                    )
                    .map((subItem) => (
                      <NavLink
                        key={subItem.href}
                        item={subItem}
                        onClick={() => isMobile && setMobileMenuOpen(false)}
                      />
                    ))}
                </div>
              </div>
            );
          }

          return (
            <NavLink
              key={item.href}
              item={item as NavLeaf}
              onClick={() => isMobile && setMobileMenuOpen(false)}
            />
          );
        })}
      </nav>

      {!isMobile && (
        <div
          className={`space-y-2 p-3 ${pathname === "/dashboard/query" ? "" : "border-glass-border border-t"}`}
        >
          <motion.button
            onClick={() => setSidebarOpen((prev) => !prev)}
            className={`sidebar-collapse-control ui-btn btn-ghost sweeping-light-btn h-11 w-full justify-center gap-2 ${
              sidebarOpen
                ? "border-glass-border bg-surface-1/40 rounded-xl px-4"
                : "border-glass-border bg-surface-1/40 rounded-2xl px-0"
            }`}
            aria-label={
              sidebarOpen ? "Collapse sidebar (Ctrl+Alt+B)" : "Expand sidebar (Ctrl+Alt+B)"
            }
            whileTap={{ scale: 0.98 }}
          >
            <motion.span
              initial={false}
              animate={{ rotate: sidebarOpen ? 0 : 180 }}
              transition={{ duration: 0.16, ease: [0.22, 1, 0.36, 1] }}
            >
              {sidebarOpen ? <PanelLeftClose size={18} /> : <PanelLeftOpen size={18} />}
            </motion.span>
            <motion.span
              initial={false}
              animate={{
                maxWidth: sidebarOpen ? 100 : 0,
                opacity: sidebarOpen ? 1 : 0,
              }}
              transition={{ duration: 0.16, ease: [0.22, 1, 0.36, 1] }}
              className="block overflow-hidden text-sm font-bold whitespace-nowrap"
              aria-hidden={!sidebarOpen}
            >
              Collapse
            </motion.span>
          </motion.button>
          <motion.div
            initial={false}
            animate={{
              height: sidebarOpen ? "auto" : 0,
              opacity: sidebarOpen ? 1 : 0,
              scale: sidebarOpen ? 1 : 0.96,
            }}
            transition={{ duration: 0.18, ease: [0.22, 1, 0.36, 1] }}
            className="overflow-hidden"
            aria-hidden={!sidebarOpen}
          >
            <div className="bg-primary/5 border-primary/10 text-primary/70 flex items-center justify-center rounded-full border px-2 py-1 text-[10px] font-bold tracking-[0.14em] whitespace-nowrap uppercase">
              {APP_VERSION}
              {process.env.NEXT_PUBLIC_GIT_SHA && process.env.NEXT_PUBLIC_GIT_SHA !== "unknown"
                ? ` • ${String(process.env.NEXT_PUBLIC_GIT_SHA).slice(0, 7)}`
                : ""}
            </div>
          </motion.div>
        </div>
      )}
    </>
  );

  const renderRestrictedContent = (options: { title: string; body: string; chip: string }) => (
    <div className="mx-auto max-w-3xl space-y-6 px-2 py-8">
      <div className="theme-panel rounded-[1.5rem] p-8">
        <div className="theme-accent-pill inline-flex items-center gap-2 rounded-full px-3 py-1 text-[10px] font-semibold tracking-[0.2em] uppercase">
          <ShieldAlert size={12} />

          {options.chip}
        </div>
        <h1 className="text-foreground mt-5 text-3xl font-semibold tracking-tight">
          {options.title}
        </h1>
        <p className="text-muted-foreground mt-3 max-w-2xl text-sm leading-7">{options.body}</p>
      </div>
    </div>
  );

  return (
    <AuthGuard>
      <CodexRuntimeBridge />
      <div className="app-shell-grid text-foreground flex h-[100svh] overflow-hidden font-sans">
        {!isMobile && (
          <aside
            aria-label="Dashboard navigation"
            className="dashboard-sidebar-shell dashboard-sidebar-surface relative z-20 my-4 mr-3 ml-4 flex h-[calc(100svh-2rem)] flex-col overflow-hidden rounded-l-xl rounded-r-2xl border-r-0"
            style={{ width: sidebarOpen ? 208 : 76 }}
          >
            {renderSidebarContent()}
          </aside>
        )}

        <AnimatePresence>
          {isMobile && mobileMenuOpen && (
            <>
              <motion.div
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                onClick={() => setMobileMenuOpen(false)}
                className="bg-background/80 fixed inset-0 z-40 backdrop-blur-sm"
                style={{ position: "fixed" }}
              />
              <aside
                aria-label="Dashboard navigation"
                className="dashboard-mobile-sidebar dashboard-sidebar-surface fixed top-2 bottom-2 left-2 z-50 flex min-h-0 w-[min(15rem,calc(100vw-1rem))] flex-col overflow-y-auto rounded-l-xl rounded-r-2xl border-r-0"
                style={{ position: "fixed" }}
              >
                {renderSidebarContent()}
              </aside>
            </>
          )}
        </AnimatePresence>

        <div
          className={`relative my-2 flex h-[calc(100svh-1rem)] min-w-0 flex-1 flex-col overflow-x-clip overflow-y-clip sm:my-4 sm:h-[calc(100svh-2rem)] ${isMobile ? "mx-2" : "mr-3"}`}
        >
          <header className="border-glass-border bg-glass-bg/75 relative z-20 flex min-h-16 flex-nowrap items-center justify-between gap-3 overflow-visible rounded-2xl border px-3 py-3 backdrop-blur-md sm:backdrop-blur-xl md:min-h-18 md:px-6">
            <div className="relative z-10 flex min-w-0 flex-nowrap items-center gap-3">
              {isMobile && (
                <button
                  onClick={() => setMobileMenuOpen(true)}
                  className="border-glass-border bg-muted text-muted-foreground hover:text-foreground inline-flex h-10 w-10 items-center justify-center rounded-xl border transition-all active:scale-95"
                  aria-label="Open menu"
                >
                  <Menu size={20} />
                </button>
              )}

              <div className="hidden min-w-0 flex-col gap-0.5 sm:flex">
                <Breadcrumbs />
              </div>
            </div>

            <div
              id="header-layout-controls"
              className="relative z-10 flex max-w-full min-w-0 flex-1 items-center justify-center overflow-x-auto overflow-y-visible [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
              aria-label="DeepSpace controls"
            />

            <div className="relative z-10 flex items-center justify-end gap-1.5 md:gap-3">
              <div className="hidden sm:block">
                <SystemStatus />
              </div>
              <NotificationCenter />

              <button
                onClick={toggleTheme}
                data-tooltip={`Switch to ${theme === "dark" ? "light" : "dark"} mode`}
                className={`ui-tooltip bg-muted border-glass-border text-muted-foreground hover:text-foreground inline-flex h-9 w-9 items-center justify-center rounded-xl border transition-all hover:-translate-y-0.5 hover:shadow-md ${isDeepSpaceRoute ? "hidden sm:inline-flex" : ""}`}
                aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} mode`}
              >
                {theme === "dark" ? <Sun size={16} /> : <Moon size={16} />}
              </button>

              <div className="bg-glass-border hidden h-5 w-px sm:block" />
            </div>
          </header>

          <main
            ref={mainContentRef}
            className={`dashboard-main-surface flex-1 overflow-x-hidden ${
              fillsAvailableHeight && !isMobile ? "overflow-y-hidden" : "overflow-y-auto"
            } ${isProvidersSettingsRoute ? "scrollbar-hide" : ""} ${
              isQueryRoute || isDeepSpaceRoute
                ? "px-0 pt-3 pb-0 sm:pt-4"
                : "px-3 pt-3 pb-4 sm:px-6 sm:pt-4"
            }`}
          >
            <div
              className={fillsAvailableHeight ? "flex h-full min-h-0 w-full flex-col" : "w-full"}
            >
              {!isFullHeightRoute && <BetaNoticeBanner />}
              {(isAdminRoute && !hasAdminAccess && !canAccessStaffPage) ||
              ((isFeedbackStaffPage || isModeratorStaffPage || isSupportStaffPage) &&
                !canAccessStaffPage) ? (
                renderRestrictedContent({
                  title: "Admin Console Restricted",
                  body: "This area is reserved for admins. Normal users can continue using documents, Query, Qel Connect, profile, and trust/privacy settings.",
                  chip: "Admin Only",
                })
              ) : isProvidersSettingsRoute ? (
                <div className="flex min-h-0 flex-1 flex-col">{children}</div>
              ) : (
                children
              )}
            </div>
          </main>
        </div>
        <WelcomeFlow />
      </div>
    </AuthGuard>
  );
}
