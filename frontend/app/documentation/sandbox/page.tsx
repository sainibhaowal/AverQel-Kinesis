import { DocsCards, DocsSection, DocsShell } from "../_components/DocsShell";

export default function SandboxDocsPage() {
  return (
    <DocsShell
      title="Sandboxed Analysis"
      intro="Run bounded Python or read-only SQL against selected Library files without exposing the API host, credentials, or private network."
    >
      <DocsCards
        items={[
          {
            title: "Library-first inputs",
            body: "Select authorized file IDs from Library. AverQel stages a temporary copy after tenant, user, and conversation checks; raw host paths and arbitrary URLs are never accepted.",
          },
          {
            title: "Supported analysis",
            body: "CSV and JSON analysis, pandas/OpenPyXL transformations, read-only SQL, and Matplotlib chart generation are supported in the isolated image.",
          },
          {
            title: "Bounded execution",
            body: "The executor uses a private Docker network, no host mounts, dropped capabilities, import checks, CPU/memory/time limits, and capped output.",
          },
          {
            title: "Automatic cleanup",
            body: "Staged inputs, generated files, and temporary working directories are removed after each run. Persisted outputs become private, authenticated artifacts only when requested.",
          },
        ]}
      />

      <DocsSection title="Request flow">
        <pre className="overflow-x-auto rounded-2xl border border-white/10 bg-black/30 p-5 text-xs leading-6 text-slate-300">
          <code>{`Library file IDs → authorization → temporary staging
                  → sandbox-executor → result validation
                  → optional private artifact → DeepSpace panel`}</code>
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
          The API remains compatible when the feature is disabled. To execute locally, start the
          sandbox Compose profile and set the API&apos;s sandbox URL, token, and enabled flag. Keep
          the token unique per environment and do not place it in frontend code or chat prompts.
        </p>
      </DocsSection>

      <DocsSection title="The local shell cage">
        <p>
          The local shell tools run commands on the machine that hosts the worker, so the
          deployment confines them with an operating-system cage whenever the host offers one.
          <strong> Settings → DeepSpace Runtime</strong> shows which cage is selected, whether it
          is actually available on this host, and whether it keeps network access.
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
            <strong>Windows</strong> uses a job object with kill-on-close, so the whole process
            tree ends with the runner, and a reduced environment that carries no host secrets. The
            runner fails closed: if the job object cannot be created, the command never starts. The
            boundary there is partial, because it contains the process tree and the environment
            rather than every file effect. Configure a Windows container image for a full
            file-system boundary.
          </li>
          <li>
            <strong>Docker</strong> runs the command in a container with the workspace mounted, and
            needs an image that contains the shell.
          </li>
        </ul>
        <p>
          Every cage is probed before it is used, so a host whose kernel refuses the rules is
          reported as unavailable rather than as a working sandbox. A command&apos;s failure is also
          reported with its kind, so a refused command and a broken cage are not confused.
        </p>
        <p>
          A deployment can require a cage, so a command is refused instead of running without one.
          Because the home directory is hidden, a command that needs a credential from it must use
          a workspace-local copy.
        </p>
      </DocsSection>

      <DocsSection title="The sandbox mode of a conversation">
        <p>
          The sandbox mode says how much of the host a shell command may reach. It belongs to the
          conversation, so two conversations never share a boundary, and you can tighten one
          conversation without changing the deployment. Choose it with the selector beside the
          composer: <strong>Read-only</strong> refuses anything that changes state,{" "}
          <strong>Workspace</strong> allows changes inside the project workspace only, and{" "}
          <strong>Full access</strong> removes the restriction. The choice is recorded with the
          conversation, so it survives a restart.
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
