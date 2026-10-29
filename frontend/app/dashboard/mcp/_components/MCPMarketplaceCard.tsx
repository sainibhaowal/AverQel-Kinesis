/* eslint-disable @next/next/no-img-element */
import Image from "next/image";
import Link from "next/link";
import { ArrowRight, BookOpen, CheckCircle2, Cpu, Database, ExternalLink, FolderTree, Globe, Loader2, Wrench } from "lucide-react";
import { useState, useMemo, type CSSProperties } from "react";

import type { MCPConnection, MCPMarketplaceEntry } from "@/lib/mcp-api";
import MCPToggleSwitch from "./MCPToggleSwitch";

export interface MCPLogoTarget {
  id?: string;
  name?: string;
  display_name?: string;
  server_name?: string;
  provider_slug?: string | null;
  trusted_logo_key?: string | null;
  logo_url?: string | null;
  author_website_url?: string | null;
}

const TRUSTED_LOCAL_LOGOS: Record<string, string> = {
  // Google Workspace & Services
  gmail: "/mcp/gmail.svg",
  "google-gmail": "/mcp/gmail.svg",
  "google-drive": "/mcp/google-drive.svg",
  googledrive: "/mcp/google-drive.svg",
  drive: "/mcp/google-drive.svg",
  "google-calendar": "/mcp/google-calendar.svg",
  googlecalendar: "/mcp/google-calendar.svg",
  calendar: "/mcp/google-calendar.svg",
  "google-docs": "/mcp/google-docs.svg",
  googledocs: "/mcp/google-docs.svg",
  docs: "/mcp/google-docs.svg",
  "google-sheets": "/mcp/google-sheets.svg",
  googlesheets: "/mcp/google-sheets.svg",
  sheets: "/mcp/google-sheets.svg",
  "google-chat": "/mcp/google-chat.svg",
  googlechat: "/mcp/google-chat.svg",
  chat: "/mcp/google-chat.svg",
  "google-people": "/mcp/google-people.svg",
  googlepeople: "/mcp/google-people.svg",
  people: "/mcp/google-people.svg",
  contacts: "/mcp/google-people.svg",
  "google-contacts": "/mcp/google-people.svg",
  google: "/mcp/google.svg",

  // Core apps & developer tools
  github: "/mcp/github.svg",
  "github-copilot": "/mcp/github.svg",
  notion: "/mcp/notion.svg",
  slack: "/mcp/slack.svg",
  salesforce: "/mcp/salesforce.svg",
  linear: "/mcp/linear.svg",
  jira: "/mcp/jira.svg",
  "atlassian-jira": "/mcp/jira.svg",
  discord: "/mcp/discord.svg",
  discordbot: "/mcp/discord.svg",
  airtable: "/mcp/airtable.svg",
  hubspot: "/mcp/hubspot.svg",
  postgresql: "/mcp/postgresql.svg",
  postgres: "/mcp/postgresql.svg",
  "local-postgres": "/mcp/postgresql.svg",
  sqlite: "/mcp/sqlite.svg",
  "local-sqlite": "/mcp/sqlite.svg",
  "local-sqlite-databases": "/mcp/sqlite.svg",
  git: "/mcp/git.svg",
  "local-git": "/mcp/git.svg",
  "local-git-repositories": "/mcp/git.svg",
  puppeteer: "/mcp/puppeteer.svg",
  "local-puppeteer": "/mcp/puppeteer.svg",
  "web-browser-puppeteer": "/mcp/puppeteer.svg",
  "web-browser-(puppeteer)": "/mcp/puppeteer.svg",
  composio: "/mcp/composio.svg",
  "composio-provider": "/mcp/composio.svg",
};


const SIMPLE_ICONS_SLUGS: Record<string, string> = {
  airtable: "airtable",
  asana: "asana",
  discord: "discord",
  discordbot: "discord",
  jira: "jira",
  linear: "linear",
  hubspot: "hubspot",
  github: "github",
  notion: "notion",
  google: "google",
  googledocs: "googledocs",
  "google-docs": "googledocs",
  googlesheets: "googlesheets",
  "google-sheets": "googlesheets",
  googledrive: "googledrive",
  "google-drive": "googledrive",
  googlecalendar: "googlecalendar",
  "google-calendar": "googlecalendar",
  calendar: "googlecalendar",
  googlechat: "googlechat",
  "google-chat": "googlechat",
  gmail: "gmail",
  "google-gmail": "gmail",
  git: "git",
  "local-git": "git",
  "local-git-repositories": "git",
  sqlite: "sqlite",
  "local-sqlite": "sqlite",
  "local-sqlite-databases": "sqlite",
  puppeteer: "puppeteer",
  "local-puppeteer": "puppeteer",
  "web-browser-puppeteer": "puppeteer",
  "web-browser-(puppeteer)": "puppeteer",
  huggingface: "huggingface",
  "hugging-face": "huggingface",
  datadog: "datadog",
  atlassian: "atlassian",
  auth0: "auth0",
  bitbucket: "bitbucket",
  bitwarden: "bitwarden",
  box: "box",
  braintree: "braintree",
  brex: "brex",
  cal: "caldotcom",
  calendly: "calendly",
  clickup: "clickup",
  cloudflare: "cloudflare",
  coda: "coda",
  coinbase: "coinbase",
  confluence: "confluence",
  contentful: "contentful",
  figma: "figma",
  gitlab: "gitlab",
  intercom: "intercom",
  mailchimp: "mailchimp",
  pagerduty: "pagerduty",
  postgresql: "postgresql",
  postgres: "postgresql",
  "local-postgres": "postgresql",
  redis: "redis",
  sentry: "sentry",
  shopify: "shopify",
  stripe: "stripe",
  supabase: "supabase",
  trello: "trello",
  twitch: "twitch",
  twitter: "x",
  webflow: "webflow",
  whatsapp: "whatsapp",
  youtube: "youtube",
  zendesk: "zendesk",
  zoom: "zoom",
};

const BRAND_DOMAINS: Record<string, string> = {
  accelo: "accelo.com",
  "aero-workflow": "aeroworkflow.com",
  acculynx: "acculynx.com",
  "active-campaign": "activecampaign.com",
  adobe: "adobe.com",
  affinity: "affinity.co",
  agencyzoom: "agencyzoom.com",
  ahrefs: "ahrefs.com",
  airtable: "airtable.com",
  alchemy: "alchemy.com",
  altoviz: "altoviz.com",
  amazon: "amazon.com",
  amcards: "amcards.com",
  amplitude: "amplitude.com",
  anthropic: "anthropic.com",
  apaleo: "apaleo.com",
  apollo: "apollo.io",
  appdrag: "appdrag.com",
  appsflyer: "appsflyer.com",
  asana: "asana.com",
  ashby: "ashbyhq.com",
  atlassian: "atlassian.com",
  attio: "attio.com",
  auth0: "auth0.com",
  axonaut: "axonaut.com",
  bamboohr: "bamboohr.com",
  bannerbear: "bannerbear.com",
  baserow: "baserow.io",
  battlenet: "battle.net",
  beeminder: "beeminder.com",
  bill: "bill.com",
  bitbucket: "bitbucket.org",
  bitwarden: "bitwarden.com",
  blackbaud: "blackbaud.com",
  blackboard: "blackboard.com",
  boldsign: "boldsign.com",
  bolna: "bolna.dev",
  borneo: "borneo.io",
  botbaba: "botbaba.com",
  box: "box.com",
  braintree: "braintreepayments.com",
  brandfetch: "brandfetch.com",
  "breezy-hr": "breezy.hr",
  brevo: "brevo.com",
  brex: "brex.com",
  "brex-staging": "brex.com",
  brightpearl: "brightpearl.com",
  browseai: "browse.ai",
  "browser-tool": "browserless.io",
  "browserbase-tool": "browserbase.com",
  browserhub: "browserhub.io",
  bubble: "bubble.io",
  cal: "cal.com",
  calendly: "calendly.com",
  canva: "canva.com",
  canvas: "instructure.com",
  chatwork: "chatwork.com",
  chmeetings: "chmeetings.com",
  clickup: "clickup.com",
  close: "close.com",
  cloudflare: "cloudflare.com",
  coda: "coda.io",
  coinbase: "coinbase.com",
  coinmarketcal: "coinmarketcal.com",
  composio: "composio.dev",
  "composio-search": "composio.dev",
  confluence: "atlassian.com",
  contentful: "contentful.com",
  crustdata: "crustdata.com",
  "customer-io": "customer.io",
  d2lbrightspace: "d2l.com",
  dailybot: "dailybot.com",
  datadog: "datadoghq.com",
  datagma: "datagma.com",
  datarobot: "datarobot.com",
  deel: "deel.com",
  demio: "demio.com",
  dialpad: "dialpad.com",
  digicert: "digicert.com",
  discord: "discord.com",
  discordbot: "discord.com",
  docmosis: "docmosis.com",
  docusign: "docusign.com",
  dropbox: "dropbox.com",
  "dropbox-sign": "hellosign.com",
  dynamics365: "microsoft.com",
  echtpost: "echtpost.de",
  elevenlabs: "elevenlabs.io",
  entelligence: "entelligence.ai",
  "epic-games": "epicgames.com",
  eventbrite: "eventbrite.com",
  exa: "exa.ai",
  exist: "exist.io",
  facebook: "facebook.com",
  factorial: "factorialhr.com",
  figma: "figma.com",
  finage: "finage.co.uk",
  firecrawl: "firecrawl.dev",
  fireflies: "fireflies.ai",
  fitbit: "fitbit.com",
  flutterwave: "flutterwave.com",
  fomo: "fomo.com",
  formcarry: "formcarry.com",
  formsite: "formsite.com",
  foursquare: "foursquare.com",
  freshbooks: "freshbooks.com",
  freshdesk: "freshworks.com",
  front: "front.com",
  git: "git-scm.com",
  github: "github.com",
  gmail: "mail.google.com",
  "go-to-webinar": "gotowebinar.com",
  gong: "gong.io",
  "google-analytics": "analytics.google.com",
  "google-calendar": "calendar.google.com",
  "google-chat": "chat.google.com",
  "google-docs": "docs.google.com",
  "google-drive": "drive.google.com",
  "google-gmail": "mail.google.com",
  "google-maps": "maps.google.com",
  "google-people": "contacts.google.com",
  "google-sheets": "sheets.google.com",
  googleads: "ads.google.com",
  googlebigquery: "cloud.google.com",
  googlecalendar: "calendar.google.com",
  googledocs: "docs.google.com",
  googledrive: "drive.google.com",
  googlemeet: "meet.google.com",
  googlephotos: "photos.google.com",
  googlesheets: "sheets.google.com",
  googlesuper: "google.com",
  googletasks: "tasks.google.com",
  gorgias: "gorgias.com",
  greptile: "greptile.com",
  gumroad: "gumroad.com",
  guru: "getguru.com",
  hackernews: "news.ycombinator.com",
  "hackerrank-work": "hackerrank.com",
  harvest: "getharvest.com",
  helcim: "helcim.com",
  heygen: "heygen.com",
  highlevel: "gohighlevel.com",
  hubspot: "hubspot.com",
  humanloop: "humanloop.com",
  "icims-talent-cloud": "icims.com",
  intercom: "intercom.com",
  jira: "atlassian.com",
  junglescout: "junglescout.com",
  keap: "keap.com",
  klaviyo: "klaviyo.com",
  klipfolio: "klipfolio.com",
  kommo: "kommo.com",
  lastpass: "lastpass.com",
  "launch-darkly": "launchdarkly.com",
  lever: "lever.co",
  "lever-sandbox": "lever.co",
  lexoffice: "lexoffice.de",
  linear: "linear.app",
  linkedin: "linkedin.com",
  lmnt: "lmnt.com",
  "local-fetch": "modelcontextprotocol.io",
  "local-filesystem": "modelcontextprotocol.io",
  "local-git": "git-scm.com",
  "local-memory": "modelcontextprotocol.io",
  "local-puppeteer": "pptr.dev",
  "local-sqlite": "sqlite.org",
  huggingface: "huggingface.co",
  "hugging-face": "huggingface.co",
  puppeteer: "pptr.dev",
  "web-browser-puppeteer": "pptr.dev",
  "web-browser-(puppeteer)": "pptr.dev",
  "web-page-fetcher": "modelcontextprotocol.io",
  fetch: "modelcontextprotocol.io",
  mailchimp: "mailchimp.com",
  mailerlite: "mailerlite.com",
  maintainx: "getmaintainx.com",
  mem0: "mem0.ai",
  metaads: "facebook.com",
  "microsoft-clarity": "clarity.microsoft.com",
  "microsoft-teams": "teams.microsoft.com",
  miro: "miro.com",
  mixpanel: "mixpanel.com",
  monday: "monday.com",
  mopinion: "mopinion.com",
  mural: "mural.co",
  neon: "neon.tech",
  netsuite: "netsuite.com",
  ngrok: "ngrok.com",
  notion: "notion.so",
  oncehub: "oncehub.com",
  "one-drive": "onedrive.live.com",
  "open-sea": "opensea.io",
  outlook: "outlook.com",
  pagerduty: "pagerduty.com",
  pandadoc: "pandadoc.com",
  peopledatalabs: "peopledatalabs.com",
  perplexityai: "perplexity.ai",
  pipedrive: "pipedrive.com",
  posthog: "posthog.com",
  precoro: "precoro.com",
  productboard: "productboard.com",
  reddit: "reddit.com",
  retellai: "retellai.com",
  "ring-central": "ringcentral.com",
  rippling: "rippling.com",
  "rocket-reach": "rocketreach.co",
  rocketlane: "rocketlane.com",
  sage: "sage.com",
  salesforce: "salesforce.com",
  seismic: "seismic.com",
  semanticscholar: "semanticscholar.org",
  semrush: "semrush.com",
  sendgrid: "sendgrid.com",
  sentry: "sentry.io",
  serpapi: "serpapi.com",
  servicenow: "servicenow.com",
  "share-point": "microsoft.com",
  shopify: "shopify.com",
  shortcut: "shortcut.com",
  slack: "slack.com",
  slackbot: "slack.com",
  smartrecruiters: "smartrecruiters.com",
  snowflake: "snowflake.com",
  square: "squareup.com",
  "stack-exchange": "stackexchange.com",
  stripe: "stripe.com",
  supabase: "supabase.com",
  "survey-monkey": "surveymonkey.com",
  tavily: "tavily.com",
  todoist: "todoist.com",
  trello: "trello.com",
  twitch: "twitch.tv",
  twitter: "twitter.com",
  "twitter-media": "twitter.com",
  typefully: "typefully.com",
  webex: "webex.com",
  webflow: "webflow.com",
  whatsapp: "whatsapp.com",
  wiz: "wiz.io",
  wrike: "wrike.com",
  xero: "xero.com",
  yandex: "yandex.com",
  ynab: "ynab.com",
  youtube: "youtube.com",
  zendesk: "zendesk.com",
  zenrows: "zenrows.com",
  zoho: "zoho.com",
  "zoho-bigin": "zoho.com",
  "zoho-books": "zoho.com",
  "zoho-desk": "zoho.com",
  "zoho-inventory": "zoho.com",
  "zoho-invoice": "zoho.com",
  "zoho-mail": "zoho.com",
  zoom: "zoom.us",
  zoominfo: "zoominfo.com",
};

const CARD_TINTS = [
  "rgba(16, 185, 129, 0.62)",
  "rgba(20, 184, 166, 0.62)",
  "rgba(245, 158, 11, 0.58)",
  "rgba(168, 85, 247, 0.58)",
  "rgba(244, 63, 94, 0.58)",
];

function cardTint(entry: MCPLogoTarget): string {
  const key = `${entry.id || ""}:${entry.provider_slug || entry.name || entry.server_name || "mcp"}`;
  let hash = 0;
  for (const character of key) hash = (hash * 31 + character.charCodeAt(0)) | 0;
  return CARD_TINTS[Math.abs(hash) % CARD_TINTS.length];
}

export function resolveTrustedLogoPath(key?: string | null): string | null {
  const normalized = String(key || "")
    .trim()
    .toLowerCase()
    .replace(/[()]/g, "")
    .replace(/\s+/g, "-");
  if (!normalized) return null;

  // Direct exact match
  if (TRUSTED_LOCAL_LOGOS[normalized]) return TRUSTED_LOCAL_LOGOS[normalized];

  // Specific Google apps take priority over generic google
  if (normalized.includes("gmail")) return TRUSTED_LOCAL_LOGOS.gmail;
  if (normalized.includes("drive")) return TRUSTED_LOCAL_LOGOS["google-drive"];
  if (normalized.includes("calendar")) return TRUSTED_LOCAL_LOGOS["google-calendar"];
  if (normalized.includes("docs") || normalized.includes("document")) return TRUSTED_LOCAL_LOGOS["google-docs"];
  if (normalized.includes("sheets") || normalized.includes("spreadsheet")) return TRUSTED_LOCAL_LOGOS["google-sheets"];
  if (normalized.includes("chat")) return TRUSTED_LOCAL_LOGOS["google-chat"];
  if (normalized.includes("people") || normalized.includes("contacts")) return TRUSTED_LOCAL_LOGOS["google-people"];

  // Specific developer & productivity tools
  if (normalized.includes("puppeteer")) return TRUSTED_LOCAL_LOGOS.puppeteer;
  if (normalized.includes("sqlite")) return TRUSTED_LOCAL_LOGOS.sqlite;
  if (normalized.includes("postgres")) return TRUSTED_LOCAL_LOGOS.postgresql;
  if (normalized.includes("linear")) return TRUSTED_LOCAL_LOGOS.linear;
  if (normalized.includes("jira")) return TRUSTED_LOCAL_LOGOS.jira;
  if (normalized.includes("discord")) return TRUSTED_LOCAL_LOGOS.discord;
  if (normalized.includes("airtable")) return TRUSTED_LOCAL_LOGOS.airtable;
  if (normalized.includes("hubspot")) return TRUSTED_LOCAL_LOGOS.hubspot;
  if (normalized.includes("git") && !normalized.includes("github") && !normalized.includes("gitlab")) return TRUSTED_LOCAL_LOGOS.git;

  // Generic provider fallbacks
  if (normalized.startsWith("google-") || normalized.startsWith("google")) return TRUSTED_LOCAL_LOGOS.google;
  if (normalized.startsWith("github-") || normalized.startsWith("github")) return TRUSTED_LOCAL_LOGOS.github;
  if (normalized.startsWith("slack-") || normalized.startsWith("slack")) return TRUSTED_LOCAL_LOGOS.slack;
  if (normalized.startsWith("notion-") || normalized.startsWith("notion")) return TRUSTED_LOCAL_LOGOS.notion;
  if (normalized.startsWith("salesforce-") || normalized.startsWith("salesforce")) return TRUSTED_LOCAL_LOGOS.salesforce;

  return null;
}

export function getLogoCandidates(entry: MCPLogoTarget): string[] {
  const candidates: string[] = [];

  // Prioritize specific service keys before generic ones
  const keysToTest: string[] = [];
  if (entry.provider_slug) keysToTest.push(entry.provider_slug);
  if (entry.server_name) keysToTest.push(entry.server_name);
  if (entry.name) keysToTest.push(entry.name);
  if (entry.display_name) keysToTest.push(entry.display_name);
  if (entry.trusted_logo_key) keysToTest.push(entry.trusted_logo_key);

  // 1. Direct local trusted SVG
  for (const k of keysToTest) {
    const local = resolveTrustedLogoPath(k);
    if (local && !candidates.includes(local)) {
      candidates.push(local);
      break;
    }
  }

  // 2. Direct entry.logo_url if provided
  if (entry.logo_url && /^https?:\/\//i.test(entry.logo_url) && !candidates.includes(entry.logo_url)) {
    candidates.push(entry.logo_url);
  }

  // 3. SimpleIcons direct SVG (clean vector)
  for (const k of keysToTest) {
    const normalized = k.toLowerCase().trim().replace(/[()]/g, "").replace(/\s+/g, "-");
    const simpleSlug = SIMPLE_ICONS_SLUGS[normalized];
    if (simpleSlug) {
      const url = `https://cdn.simpleicons.org/${simpleSlug}`;
      if (!candidates.includes(url)) candidates.push(url);
    }
  }

  // 4. Domain-based resolution via unavatar and google favicons (strictly verified domains only)
  for (const k of keysToTest) {
    const normalized = k.toLowerCase().trim().replace(/[()]/g, "").replace(/\s+/g, "-");
    const domain = BRAND_DOMAINS[normalized];
    if (domain) {
      const unavatarUrl = `https://unavatar.io/${domain}`;
      const faviconUrl = `https://www.google.com/s2/favicons?domain=${domain}&sz=128`;
      if (!candidates.includes(unavatarUrl)) candidates.push(unavatarUrl);
      if (!candidates.includes(faviconUrl)) candidates.push(faviconUrl);
      break;
    }
  }

  return candidates;
}

function FallbackIcon({ entry, size }: { entry: MCPLogoTarget; size: number }) {
  const name = entry.name || entry.server_name || entry.display_name || "App";
  const slug = (entry.server_name || entry.provider_slug || entry.name || "").toLowerCase();

  if (slug.includes("filesystem") || slug.includes("file")) {
    return <FolderTree className="text-emerald-600 dark:text-emerald-400" style={{ width: size * 0.85, height: size * 0.85 }} />;
  }
  if (slug.includes("sqlite") || slug.includes("database") || slug.includes("postgres")) {
    return <Database className="text-emerald-700 dark:text-emerald-400" style={{ width: size * 0.85, height: size * 0.85 }} />;
  }
  if (slug.includes("puppeteer") || slug.includes("browser") || slug.includes("fetch")) {
    return <Globe className="text-teal-600 dark:text-teal-400" style={{ width: size * 0.85, height: size * 0.85 }} />;
  }
  if (slug.includes("memory") || slug.includes("brain") || slug.includes("graph")) {
    return <Cpu className="text-purple-600 dark:text-purple-400" style={{ width: size * 0.85, height: size * 0.85 }} />;
  }
  if (slug.includes("hugging")) {
    return <span style={{ fontSize: size * 0.8 }}>🤗</span>;
  }
  if (slug.includes("knowledge") || slug.includes("company")) {
    return <BookOpen className="text-amber-600 dark:text-amber-400" style={{ width: size * 0.85, height: size * 0.85 }} />;
  }

  return (
    <div
      className="flex h-full w-full items-center justify-center font-bold text-white shadow-sm rounded-xl"
      style={{
        background: `linear-gradient(135deg, #059669, #0d9488)`,
      }}
    >
      <span style={{ fontSize: Math.max(12, Math.round(size * 0.38)) }}>
        {name.slice(0, 2).toUpperCase()}
      </span>
    </div>
  );
}

export function MCPLogo({
  entry,
  size = 42,
  className = "",
}: {
  entry: MCPLogoTarget;
  size?: number;
  className?: string;
}) {
  const candidates = useMemo(() => getLogoCandidates(entry), [entry]);
  const [candidateIndex, setCandidateIndex] = useState(0);

  const currentSrc = candidates[candidateIndex];
  const normalizedKey = (entry.trusted_logo_key || entry.provider_slug || entry.server_name || entry.name || "").toLowerCase();
  const isMonochrome =
    normalizedKey.includes("github") ||
    normalizedKey.includes("notion");

  return (
    <div
      className={`mcp-logo relative flex shrink-0 items-center justify-center bg-transparent transition-transform duration-200 group-hover:scale-105 ${className}`}
      style={{ height: size, width: size }}
      data-logo={normalizedKey}
      data-monochrome={isMonochrome ? "true" : undefined}
      title={entry.name || entry.server_name || "MCP App"}
    >
      {currentSrc ? (
        <img
          key={currentSrc}
          src={currentSrc}
          alt={entry.name || "MCP App"}
          width={size}
          height={size}
          className={`h-full w-full object-contain ${
            isMonochrome ? "dark:brightness-0 dark:invert" : ""
          }`}
          onError={() => setCandidateIndex((prev) => prev + 1)}
          loading="lazy"
        />
      ) : (
        <FallbackIcon entry={entry} size={size} />
      )}
    </div>
  );
}

function Badge({
  children,
  tone = "neutral",
}: {
  children: React.ReactNode;
  tone?: "neutral" | "green" | "teal" | "amber";
}) {
  const tones = {
    neutral: "border-slate-300 bg-slate-100 text-slate-800 font-bold dark:border-white/15 dark:bg-white/10 dark:text-white/80",
    green: "border-emerald-300 bg-emerald-100/90 text-emerald-900 font-bold dark:border-emerald-400/40 dark:bg-emerald-500/20 dark:text-emerald-300",
    teal: "border-teal-300 bg-teal-100/90 text-teal-900 font-bold dark:border-teal-400/40 dark:bg-teal-500/20 dark:text-teal-300",
    amber: "border-amber-300 bg-amber-100/90 text-amber-950 font-bold dark:border-amber-400/40 dark:bg-amber-500/20 dark:text-amber-300",
  };
  return (
    <span className={`rounded-full border px-2 py-0.5 text-[10px] ${tones[tone]}`}>{children}</span>
  );
}

function formatTransport(value?: string | null): string {
  switch (value) {
    case "streamable_http":
      return "Remote HTTP";
    case "sse":
      return "Remote SSE";
    case "stdio":
      return "Local stdio";
    case "ssh":
      return "Remote SSH";
    default:
      return value ? value.replaceAll("_", " ") : "Remote";
  }
}

function authLabel(value?: string | null): string {
  return value === "oauth" ? "OAuth" : value === "anonymous" ? "Anonymous" : "Key / Setup";
}

export default function MCPMarketplaceCard({
  entry,
  connectedServer,
  isConnecting,
  onConnect,
  onReconnect,
  onView,
  onManage,
  onToggle,
}: {
  entry: MCPMarketplaceEntry;
  connectedServer?: MCPConnection | null;
  isConnecting?: boolean;
  onConnect: (entry: MCPMarketplaceEntry) => void;
  onReconnect?: (server: MCPConnection) => void;
  onView?: (entry: MCPMarketplaceEntry) => void;
  onManage?: (server: MCPConnection) => void;
  onToggle?: (server: MCPConnection) => void;
}) {
  const badges = entry.badges || {};
  const preview = entry.tool_preview || [];
  const connectable = entry.connectable;
  const isConnected = connectedServer?.status === "connected";
  const needsAuth = connectedServer?.status === "needs_auth";

  const handleActionClick = () => {
    if (connectedServer) {
      if (onManage) onManage(connectedServer);
      else if (onReconnect) onReconnect(connectedServer);
      else window.location.assign(`/dashboard/mcp/inspector/${encodeURIComponent(connectedServer.id)}`);
    } else {
      onConnect(entry);
    }
  };

  const detailHref = connectedServer
    ? `/dashboard/mcp/inspector/${encodeURIComponent(connectedServer.id)}`
    : `/dashboard/mcp/providers/${encodeURIComponent(entry.id)}`;

  return (
    <article
      className="mcp-marketplace-card group relative flex flex-col justify-between overflow-hidden rounded-2xl border border-slate-200/90 bg-white p-4 shadow-xs transition-all duration-200 hover:-translate-y-1 hover:border-emerald-500/60 hover:shadow-[0_10px_28px_rgba(0,0,0,0.08),0_0_15px_rgba(16,185,129,0.16)] dark:border-white/10 dark:bg-[#111815]/95 dark:hover:bg-[#14201b] dark:hover:border-emerald-400/40 dark:hover:shadow-[0_8px_25px_rgba(0,0,0,0.5),0_0_15px_rgba(16,185,129,0.22)]"
    >
      <div>
        {/* Top Header: Logo + Badges / Toggle */}
        <div className="flex items-start justify-between gap-2.5">
          <MCPLogo entry={entry} size={44} />
          <div className="flex flex-wrap items-center justify-end gap-1">
            {connectedServer ? (
              <div
                className="flex items-center gap-1.5"
                onClick={(e) => e.stopPropagation()}
              >
                <MCPToggleSwitch
                  size="sm"
                  checked={Boolean(connectedServer.enabled)}
                  onChange={() => onToggle?.(connectedServer)}
                  label={connectedServer.enabled ? "Active" : "Off"}
                  title={
                    connectedServer.enabled
                      ? "Enabled in DeepSpace. Click to pause."
                      : "Paused. Click to enable in DeepSpace."
                  }
                />
              </div>
            ) : needsAuth ? (
              <span className="inline-flex items-center gap-1 rounded-full border border-amber-400 bg-amber-100/90 px-2 py-0.5 text-[10px] font-bold text-amber-950 dark:border-amber-400/40 dark:bg-amber-500/20 dark:text-amber-300">
                Needs Auth
              </span>
            ) : (
              <>
                {entry.publisher_type === "community" || badges.community ? (
                  <Badge tone="amber">Community</Badge>
                ) : null}
                {entry.official || badges.official ? (
                  <Badge tone="green">Official</Badge>
                ) : null}
                {entry.verified || badges.verified ? (
                  <Badge tone="teal">Verified</Badge>
                ) : null}
                {!entry.official && !entry.verified && entry.publisher_type !== "community" && (
                  <span className="rounded-full border border-slate-300 bg-slate-100 px-2 py-0.5 text-[10px] font-bold text-slate-800 dark:border-white/15 dark:bg-white/10 dark:text-white/80">
                    {entry.transport === "stdio" ? "Local" : "Cloud"}
                  </span>
                )}
              </>
            )}
          </div>
        </div>

        {/* Title, Publisher, and Categories */}
        <div className="mt-3">
          <div className="flex items-baseline justify-between gap-2">
            <h2
              onClick={() => onView?.(entry)}
              className="truncate text-sm font-bold text-slate-950 dark:text-white group-hover:text-emerald-600 dark:group-hover:text-emerald-300 transition-colors cursor-pointer"
              title={entry.name}
            >
              {entry.name}
            </h2>
            {entry.publisher && (
              <span className="shrink-0 text-[10.5px] text-slate-600 dark:text-white/50 font-semibold">
                by {entry.publisher}
              </span>
            )}
          </div>

          {/* Category badges */}
          <div className="mt-1.5 flex flex-wrap items-center gap-1">
            {(entry.categories?.length ? entry.categories : ["Integration"]).slice(0, 2).map((cat) => (
              <span
                key={cat}
                className="rounded-md border border-slate-300/80 bg-slate-100 dark:border-white/10 dark:bg-white/[0.06] px-1.5 py-0.5 text-[10px] text-slate-800 dark:text-slate-200 font-bold"
              >
                {cat}
              </span>
            ))}
            {entry.supported_products?.[0] && entry.supported_products[0] !== entry.name && (
              <span className="rounded-md border border-emerald-400 bg-emerald-50 dark:border-emerald-500/30 dark:bg-emerald-500/15 px-1.5 py-0.5 text-[10px] text-emerald-900 dark:text-emerald-300 font-bold truncate max-w-[110px]" title={entry.supported_products[0]}>
                {entry.supported_products[0]}
              </span>
            )}
          </div>

          {/* Description */}
          {entry.description && (
            <p
              className="mt-2 text-xs leading-relaxed text-slate-700 dark:text-white/70 line-clamp-2"
              title={entry.description}
            >
              {entry.description}
            </p>
          )}
        </div>

        {/* Compact Meta row: Tools count + preview + Transport + Auth */}
        <div className="mt-3 flex items-center justify-between text-[11px] text-slate-600 dark:text-white/50 pt-2.5 border-t border-slate-200/90 dark:border-white/10">
          <div className="flex items-center gap-1.5 truncate">
            <span className="text-emerald-600 dark:text-emerald-400 font-mono text-[11px] font-bold">⚡</span>
            {entry.tool_count ? (
              <span className="font-bold text-slate-900 dark:text-white/90">{entry.tool_count} {entry.tool_count === 1 ? 'tool' : 'tools'}</span>
            ) : null}
            {preview[0]?.name ? (
              <span className="truncate font-mono text-[10px] text-slate-600 dark:text-white/50 max-w-[120px]" title={preview[0].name}>
                {preview[0].name}
              </span>
            ) : null}
          </div>
          <div className="shrink-0 flex items-center gap-1 text-[10px] text-slate-600 dark:text-white/50">
            <span className="rounded border border-slate-300 bg-slate-100 dark:border-white/10 dark:bg-white/10 px-1.5 py-0.5 font-bold text-slate-800 dark:text-slate-200">{formatTransport(entry.transport)}</span>
            <span>·</span>
            <span className="font-semibold">{authLabel(entry.auth_type)}</span>
          </div>
        </div>

        {!connectable && entry.connectability_reason && (
          <p className="mt-2 text-[10.5px] leading-tight text-amber-800 dark:text-amber-300/90 font-medium line-clamp-2" title={entry.connectability_reason}>
            {entry.connectability_reason}
          </p>
        )}
      </div>

      {/* Action buttons */}
      <div className="mt-3.5 pt-2.5 border-t border-slate-200/90 dark:border-white/10">
        {connectedServer ? (
          <div className="flex items-center gap-1.5">
            <button
              type="button"
              onClick={handleActionClick}
              className="flex-1 min-w-0 flex items-center justify-center gap-1.5 rounded-xl bg-gradient-to-r from-emerald-600 to-teal-600 hover:from-emerald-700 hover:to-teal-700 !text-white font-bold py-2 px-2 text-xs shadow-xs hover:shadow-sm active:scale-95 border border-emerald-700/20 dark:from-emerald-500 dark:to-teal-500 dark:hover:from-emerald-400 dark:hover:to-teal-400 dark:!text-slate-950 dark:font-extrabold dark:shadow-[0_0_15px_rgba(16,185,129,0.35)] transition-all whitespace-nowrap"
            >
              <CheckCircle2 className="h-3.5 w-3.5 shrink-0 stroke-[2.5]" aria-hidden="true" />
              <span className="truncate">Manage & Configure</span>
            </button>
            <Link
              href={detailHref}
              onClick={(e) => {
                if (onView) {
                  e.preventDefault();
                  onView(entry);
                }
              }}
              className="shrink-0 inline-flex items-center justify-center rounded-xl border border-slate-300 hover:border-slate-400 bg-white hover:bg-slate-100 px-2.5 py-2 text-xs font-bold text-slate-800 hover:text-slate-950 shadow-2xs dark:border-white/15 dark:hover:border-white/30 dark:bg-slate-800/90 dark:hover:bg-slate-700 dark:text-slate-100 dark:hover:text-white dark:font-semibold transition-all active:scale-95 whitespace-nowrap"
            >
              View details
            </Link>
          </div>
        ) : (
          <div className="flex items-center gap-1.5">
            <button
              type="button"
              disabled={!connectable || isConnecting}
              onClick={handleActionClick}
              className="flex-1 min-w-0 flex items-center justify-center gap-1 rounded-xl bg-gradient-to-r from-teal-600 to-emerald-600 hover:from-teal-700 hover:to-emerald-700 !text-white font-bold py-2 px-2 text-xs shadow-xs hover:shadow-sm active:scale-95 border border-teal-700/20 dark:from-teal-500 dark:to-emerald-500 dark:hover:from-teal-400 dark:hover:to-emerald-400 dark:!text-slate-950 dark:font-extrabold dark:shadow-[0_0_15px_rgba(20,184,166,0.35)] disabled:cursor-not-allowed disabled:from-slate-200 disabled:to-slate-200 disabled:text-slate-500 disabled:border-slate-300 dark:disabled:from-white/10 dark:disabled:to-white/10 dark:disabled:text-white/40 dark:disabled:border-white/10 transition-all whitespace-nowrap"
            >
              {isConnecting ? (
                <>
                  <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin" aria-hidden="true" />
                  <span className="truncate">Connecting...</span>
                </>
              ) : connectable ? (
                <>
                  <ExternalLink className="h-3.5 w-3.5 shrink-0 stroke-[2.5]" aria-hidden="true" />
                  <span className="truncate">Connect</span>
                </>
              ) : (
                <span className="truncate">Setup pending</span>
              )}
            </button>
            <Link
              href={detailHref}
              onClick={(e) => {
                if (onView) {
                  e.preventDefault();
                  onView(entry);
                }
              }}
              className="shrink-0 inline-flex items-center justify-center rounded-xl border border-slate-300 hover:border-slate-400 bg-white hover:bg-slate-100 px-2.5 py-2 text-xs font-bold text-slate-800 hover:text-slate-950 shadow-2xs dark:border-white/15 dark:hover:border-white/30 dark:bg-slate-800/90 dark:hover:bg-slate-700 dark:text-slate-100 dark:hover:text-white dark:font-semibold transition-all active:scale-95 whitespace-nowrap"
            >
              View details
            </Link>
          </div>
        )}
      </div>
    </article>
  );
}
