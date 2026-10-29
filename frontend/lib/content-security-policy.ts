/**
 * Content Security Policy for the web renderer and local Electron frontend.
 *
 * Next.js emits inline Flight/bootstrap scripts, so inline scripts remain
 * allowed. Production disables dynamic code evaluation; development adds the
 * narrowly scoped exception React requires for callstack reconstruction.
 * Loopback wildcards cover the isolated desktop API, the web development
 * server, and its HMR socket without allowing arbitrary insecure network
 * connections.
 */
const SCRIPT_SOURCE = [
  "'self'",
  "'unsafe-inline'",
].join(" ");

const DEVELOPMENT_SCRIPT_SOURCE = [
  SCRIPT_SOURCE,
  "'unsafe-eval'",
].join(" ");

const createContentSecurityPolicy = (scriptSource: string) => [
  "default-src 'self'",
  "base-uri 'self'",
  "object-src 'none'",
  `script-src ${scriptSource}`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob: https:",
  "font-src 'self' data: https:",
  "connect-src 'self' http://localhost:* http://127.0.0.1:* ws://localhost:* ws://127.0.0.1:* https: wss:",
  "media-src 'self' data: blob: http: https:",
  "worker-src 'self' blob:",
  "frame-src 'self' http: https:",
];

const CONTENT_SECURITY_POLICY_DIRECTIVES = createContentSecurityPolicy(SCRIPT_SOURCE);
const DEVELOPMENT_CONTENT_SECURITY_POLICY_DIRECTIVES = createContentSecurityPolicy(
  DEVELOPMENT_SCRIPT_SOURCE,
);

/** CSP directives accepted in a document meta element. */
export const CONTENT_SECURITY_POLICY = CONTENT_SECURITY_POLICY_DIRECTIVES.join("; ");

/** Development CSP required by React's development callstack reconstruction. */
export const DEVELOPMENT_CONTENT_SECURITY_POLICY =
  DEVELOPMENT_CONTENT_SECURITY_POLICY_DIRECTIVES.join("; ");

/** Full CSP for an HTTP response, including the header-only frame policy. */
export const CONTENT_SECURITY_POLICY_HEADER = [
  ...CONTENT_SECURITY_POLICY_DIRECTIVES,
  "frame-ancestors 'none'",
].join("; ");

/** Development response CSP, including the header-only frame policy. */
export const DEVELOPMENT_CONTENT_SECURITY_POLICY_HEADER = [
  ...DEVELOPMENT_CONTENT_SECURITY_POLICY_DIRECTIVES,
  "frame-ancestors 'none'",
].join("; ");
