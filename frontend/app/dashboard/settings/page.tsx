"use client";

import Link from "next/link";
import { motion } from "framer-motion";
import { Settings as SettingsIcon, Cable, ArrowRight, ShieldCheck } from "lucide-react";

import DashboardSectionHeader from "@/app/components/ui/DashboardSectionHeader";
import { PRODUCT_NAME } from "@/lib/brand";

const containerVariants = {
  hidden: {},
  show: {
    transition: { staggerChildren: 0.08, delayChildren: 0.15 },
  },
};

const cardVariants = {
  hidden: { opacity: 0, y: 24, scale: 0.97 },
  show: {
    opacity: 1,
    y: 0,
    scale: 1,
    transition: { type: "spring" as const, stiffness: 320, damping: 28, mass: 0.8 },
  },
};

type SettingsSection = {
  title: string;
  href: string;
  icon: React.ReactNode;
  desc: string;
  accent: string;
  accentBg: string;
  glowColor: string;
};

export default function SettingsPage() {
  const sections: SettingsSection[] = [
    {
      title: "Providers",
      href: "/dashboard/settings/providers",
      icon: <Cable size={20} />,
      desc: "Configure LLM, embedding, and local runtime providers.",
      accent: "text-info",
      accentBg: "bg-info/10 border-info/20",
      glowColor: "group-hover:shadow-[0_0_24px_-4px_rgba(var(--info),0.35)]",
    },
    {
      title: "DeepSpace Runtime",
      href: "/dashboard/settings/deepspace",
      icon: <ShieldCheck size={20} />,
      desc: "Review the agent runtime: sandbox cage, approvals, and parallel tool budget.",
      accent: "text-emerald-400",
      accentBg: "bg-emerald-500/10 border-emerald-500/20",
      glowColor: "group-hover:shadow-[0_0_24px_-4px_rgba(16,185,129,0.35)]",
    },
  ];

  return (
    <div className="w-full space-y-8">
      <motion.div
        initial={{ opacity: 0, y: -10 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4, ease: "easeOut" }}
      >
        <DashboardSectionHeader
          title="Settings"
          subtitle="Personal And Workspace Preferences"
          icon={SettingsIcon}
          accentClassName="bg-slate-500 text-slate-500"
          accentGlowClassName="shadow-[0_0_20px_rgba(100,116,139,0.4)]"
          backHref="/dashboard/deepspace"
          backLabel="Back To DeepSpace"
        />
      </motion.div>

      <motion.div
        className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3"
        variants={containerVariants}
        initial="hidden"
        animate="show"
      >
        {sections.map((section) => {
          const cardClassName = `settings-card group relative flex h-full min-h-[10rem] cursor-pointer flex-col gap-4 rounded-[1.25rem] p-6 transition-all duration-300`;
          const cardContent = (
            <div className="relative z-[1] flex h-full w-full flex-col">
              <div className="flex items-start justify-between">
                <div
                  className={`settings-icon-glow flex h-10 w-10 items-center justify-center rounded-xl border ${section.accentBg} ${section.accent} ${section.glowColor} transition-all`}
                >
                  {section.icon}
                </div>
                <div className="text-muted-foreground/30 group-hover:text-primary transition-colors">
                  <ArrowRight size={16} />
                </div>
              </div>
              <div className="mt-4">
                <h3 className="text-foreground mb-1 text-base font-black tracking-tight">
                  {section.title}
                </h3>
                <p className="text-muted-foreground text-xs leading-relaxed font-medium">
                  {section.desc}
                </p>
              </div>
              <div className="text-primary mt-auto pt-4 text-[10px] font-bold tracking-[0.2em] uppercase opacity-0 transition-opacity duration-300 group-hover:opacity-100">
                Configure Now
              </div>
            </div>
          );

          return (
            <motion.div
              key={section.title}
              variants={cardVariants}
              className="h-full rounded-[1.25rem]"
            >
              <Link href={section.href} className={cardClassName}>
                {cardContent}
              </Link>
            </motion.div>
          );
        })}
      </motion.div>

      {/* About & Version with Developer Mode unlock */}
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ delay: 0.3 }}
        className="text-muted-foreground flex items-center justify-between border-t border-white/5 pt-6 text-xs"
      >
        <span className="font-medium text-slate-500">{PRODUCT_NAME} Desktop</span>
        <button
          type="button"
          onClick={async () => {
            const currentClicks = Number(sessionStorage.getItem("dev_clicks") || "0") + 1;
            sessionStorage.setItem("dev_clicks", String(currentClicks));
            if (currentClicks >= 5) {
              sessionStorage.removeItem("dev_clicks");
              await window.electron?.window?.unlockDevTools?.();
              await window.electron?.window?.toggleDevTools?.();
            }
          }}
          className="cursor-pointer font-mono text-slate-500 transition-colors select-none hover:text-slate-400"
        >
          v0.1.6
        </button>
      </motion.div>
    </div>
  );
}
