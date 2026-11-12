import { DocsCards, DocsSection, DocsShell } from "../_components/DocsShell";

export default function SandboxDocsPage() {
  return (
    <DocsShell
      title="Sandboxed Analysis"
      intro="DeepSpace runs local shell tools inside the strongest operating-system sandbox available. The desktop app does not require Docker or a separate sandbox server."
    >
      <DocsCards
        items={[
          {
            title: "Local operating-system sandbox",
            body: "DeepSpace shell commands run on this computer inside the available OS cage, scoped by the selected workspace and permission mode.",
          },
          {
            title: "No Docker dependency",
            body: "The desktop app uses native Linux, macOS, or Windows confinement when available and does not start a separate execution service.",
          },
          {
            title: "Permission-aware execution",
            body: "Read-only, workspace-write, and full-access modes control what commands may change. The runtime status reports whether the OS cage is available.",
          },
          {
            title: "Optional server executor",
            body: "A separate Python/SQL file-analysis executor can be configured by server operators, but it is disabled and not bundled in the desktop app.",
          },
        ]}
      />

      <DocsSection title="Request flow">
        <pre className="overflow-x-auto rounded-2xl border border-white/10 bg-black/30 p-5 text-xs leading-6 text-slate-300">
          <code>{`DeepSpace request → permission checks → local OS sandbox
              → workspace-scoped shell command → result in conversation`}</code>
        </pre>
      </DocsSection>

      <DocsSection title="Limits and safe usage">
        <ol className="list-decimal space-y-2 pl-6">
          <li>Up to five input files per execution, with per-file and aggregate size limits.</li>
          <li>Execution time and generated output are capped; failures return a visible status.</li>
          <li>Network access, host filesystem access, secrets, and unsafe imports are blocked.</li>
          <li>
            Use this for calculations, transformations, validation, and charts—not arbitrary server
            administration.
          </li>
        </ol>
      </DocsSection>

      <DocsSection title="Deployment contract">
        <p>
          No container runtime is needed for the desktop app. The separate file-analysis executor is
          an optional server integration; it is disabled by default and requires an operator to
          deploy and configure a compatible endpoint and secret.
        </p>
      </DocsSection>

      <DocsSection title="The local shell cage">
        <p>
          The local shell tools run commands on the machine that hosts the worker, so the deployment
          confines them with an operating-system cage whenever the host offers one.
          <strong> Settings → DeepSpace Runtime</strong> shows which cage is selected, whether it is
          actually available on this host, and whether it keeps network access.
        </p>
        <ul className="list-disc space-y-2 pl-6">
          <li>
            <strong>Linux</strong> uses bubblewrap: a private mount, PID, IPC, UTS, and cgroup
            namespace, with the workspace as the only writable path and the user&apos;s home
            directory hidden. Where the host does not allow unprivileged user namespaces, the
            Landlock kernel module confines the command by path instead, without a namespace.
          </li>
          <li>
            <strong>macOS</strong> uses the seatbelt sandbox, which denies everything except the
            system directories, the workspace, and temporary files.
          </li>
          <li>
            <strong>Windows</strong> uses a job object with kill-on-close, so the whole process tree
            ends with the runner, and a reduced environment that carries no host secrets. The runner
            fails closed: if the job object cannot be created, the command never starts. The
            boundary there is partial, because it contains the process tree and the environment
            rather than every file effect. The UI reports this honestly as a partial boundary.
          </li>
        </ul>
        <p>
          In Full access, a file search still starts in the selected workspace unless the tool call
          supplies an absolute host directory. The search result reports its exact root. Full access
          runs as the backend&apos;s local operating-system user; it does not grant root privileges
          or bypass file permissions. The runtime status page shows whether Playwright browser
          automation and computer use have the dependencies they need.
        </p>
        <p>
          Every cage is probed before it is used, so a host whose kernel refuses the rules is
          reported as unavailable rather than as a working sandbox. A command&apos;s failure is also
          reported with its kind, so a refused command and a broken cage are not confused.
        </p>
        <p>
          A deployment can require a cage, so a command is refused instead of running without one.
          Because the home directory is hidden, a command that needs a credential from it must use a
          workspace-local copy.
        </p>
      </DocsSection>

      <DocsSection title="The sandbox mode of a conversation">
        <p>
          The permission mode belongs to the conversation. The effective mode is the strictest of
          your selection, the agent preset, and the deployment maximum. The selector shows the
          selected mode and explains when a preset or deployment caps it.
        </p>
        <ul className="list-disc space-y-2 pl-6">
          <li>
            <strong>Read-only</strong> keeps read tools available and refuses edits, writes, and
            deletes. Shell and terminal inspection run with the workspace mounted read-only and
            network access disabled. Safe Git and PowerShell inspection commands are allowed. If a
            requested task needs a change, DeepSpace asks you to switch to Workspace.
          </li>
          <li>
            <strong>Workspace</strong> allows local file and shell changes inside the selected
            workspace. Paths outside it, and actions that change a browser, computer, remote host,
            or connected account, require Full access.
          </li>
          <li>
            <strong>Full access</strong> lets DeepSpace reach paths available to the local operating
            system user; it does not elevate to administrator or root. Routine host file edits,
            ordinary workspace deletes, common development commands, and ordinary browser actions
            do not ask for approval. Recursive or out-of-workspace deletes, `.git` changes,
            protected OS or user-configuration paths, system settings, and recognizable high-impact
            website actions do. Browser JavaScript execution and write-capable plugins keep their
            confirmation gate. Coordinate-only desktop clicks cannot be reliably classified by
            target. Disk wipe, OS-root removal, and privilege-elevation commands are blocked;
            shutdown and reboot commands ask for approval. Mutating SSH actions on a separate
            remote machine also ask for approval. Approval cards describe the affected
            path or command, including recursive delete scope.
          </li>
        </ul>
        <p>
          Your selection is saved with the conversation. Changing its mode, preset, or workspace
          closes existing local terminal sessions so a process started under an older boundary
          cannot keep running.
        </p>
      </DocsSection>

      <DocsSection title="Programs that call tools">
        <p>
          DeepSpace can run one program that calls its own tools as ordinary function calls. The
          program runs in the same cage as the shell tools, with no network, so every piece of data
          it uses has to come from a tool call it was given. A program that cannot be confined is
          refused rather than run without containment.
        </p>
      </DocsSection>
    </DocsShell>
  );
}
