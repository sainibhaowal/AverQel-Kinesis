import Link from "next/link";
import { DocsCards, DocsSection, DocsShell } from "../_components/DocsShell";

export default function DeepSpaceDocsPage() {
  return (
    <DocsShell
      title="DeepSpace workspace"
      intro="DeepSpace is AverQel’s conversation-led workspace for research, writing, analysis, and continuing work. It brings the conversation and DeepSpace-owned working files together, while using optional providers and tools only when the deployment and user permissions allow them."
    >
      <DocsCards
        items={[
          {
            title: "Conversations",
            body: "Open and continue saved conversations. Responses may stream, and work that uses background execution can show queued or running state.",
          },
          {
            title: "Research & tools",
            body: "Configured web research, approved MCP tools, and other enabled capabilities can extend a conversation. Provider choice, policy, approvals, and service availability determine which actions can run.",
          },
          {
            title: "Working Library & notes",
            body: "DeepSpace has a Library and editor for files and deliverables associated with DeepSpace work. This is distinct from the source-file Documents Hub.",
          },
          {
            title: "Optional capabilities",
            body: "Sandbox execution, schedules, voice, and some integrations require additional services or credentials. See each guide for its limits and setup needs.",
          },
        ]}
      />
      <DocsSection title="Choose the right area">
        <ul className="list-disc space-y-2 pl-6">
          <li>
            <strong>Documents Hub:</strong> upload, inspect, organize, and manage source documents.
          </li>
          <li>
            <strong>Query:</strong> ask focused questions over accessible source documents and
            inspect cited evidence.
          </li>
          <li>
            <strong>DeepSpace:</strong> conduct broader conversational work and use supported tools,
            notes, or DeepSpace Library files when available.
          </li>
          <li>
            <strong>Qel Connect:</strong> collaborate with invited members around explicitly shared
            content.
          </li>
        </ul>
      </DocsSection>
      <DocsSection title="How DeepSpace works">
        <p>
          A DeepSpace turn is a loop, not a single request. The model is asked for a reply; if the
          reply asks for tools, the product runs them and sends the results back; the model is asked
          again. The turn ends when the model answers in words. The loop is guarded, so a model that
          keeps repeating itself is stopped instead of spending the budget.
        </p>
        <p>
          Every step is written to a typed log, so the whole trajectory - what the model asked, what
          the tools returned, and what was decided - can be read back later. The agent can also read
          that log itself, which is how it recalls what it already did in an earlier conversation
          instead of starting over.
        </p>
        <p>
          The tools are not a fixed list the model has to guess from. The model sees a compact
          catalogue and loads the exact instructions for a tool when it decides to use it, so a
          deployment with a hundred tools stays cheap to run. Eighty-four native tools cover files,
          the shell, terminals, remote hosts, search, planning, goals, agents and teams, jobs and
          schedules, skills, workflows, the session log, connected MCP servers, extensions and
          plugins, code and type graphs, and the web.
        </p>
      </DocsSection>
      <DocsSection title="Agents that work in parallel">
        <p>
          A long task does not have to run in one turn. The agent can start a child agent, hand it a
          piece of the work, and keep going itself. A child is a full agent with its own
          conversation, its own model, its own tools, and its own history; it runs in a worker, so
          the parent&apos;s turn is not blocked while it works. Up to sixteen children may run under
          one parent, and the depth of the tree is bounded by the deployment.
        </p>
        <p>
          A child can be one-shot, which runs once and finishes, or continuable, which can receive
          more messages later. The message goes into the child&apos;s own queue, so a message that
          arrives while the child is busy is answered by its next turn rather than lost.
        </p>
        <p>
          Named teammates are continuable children with a name, so the agent can address one the way
          it addresses a person: &ldquo;Ada, take the billing questions.&rdquo; The teammates of a
          conversation share a task board. An update names the revision it expects, so two teammates
          cannot quietly overwrite each other&apos;s work, and the agent can read what each teammate
          last reported.
        </p>
        <p>
          Read calls run together - the deployment allows eight at a time - while write calls run
          one at a time, because two writes at once would fight over the same files. A call that
          takes too long is stopped at its deadline, and a result too large to send to the model is
          spilled to storage and replaced with a small reference the model can read back.
        </p>
      </DocsSection>
      <DocsSection title="Work that continues in the background">
        <p>
          Three kinds of work outlive the turn. A <strong>job</strong> is a long tool call the
          product runs in the background; the agent can list the jobs, read their output, and stop
          one. A <strong>schedule</strong> is work the product runs later on a timetable, and the
          agent can create, change, and remove them. A <strong>terminal job</strong> runs a command
          in a terminal session in the background and hands back a handle to collect the result.
        </p>
        <p>
          A second prompt typed while a turn runs is queued rather than dropped. The composer shows
          the queue, and the user can edit a queued prompt, pause the queue, resume it, or retry a
          failed one. A prompt that failed is never left in the queue, and a failure or an interrupt
          pauses the queue while keeping everything that is already in it.
        </p>
      </DocsSection>
      <DocsSection title="Guards a deployment can add">
        <p>
          A hook is a small program the deployment runs around a tool call. It can let the call
          through, block it with a reason the model then reads, or ask about it. A hook that asks
          does not refuse the call: the call waits, the user sees the question with the hook&apos;s
          reason, and the call runs if the user approves. The hooks are not asked about the same
          call a second time, so an approval never turns into a loop of approvals.
        </p>
        <p>
          Hooks may be written in DeepSpace&apos;s own shape or in the Claude Code and Codex shape,
          and a hook that exits with status 2 and writes its reason on standard error blocks the
          call. Every invocation is written to the typed log, so a block can be read back
          afterwards. A hook that is broken, slow, or missing never blocks the agent: it is treated
          as allowing the call.
        </p>
      </DocsSection>
      <DocsSection title="Following the work">
        <p>
          Every turn leaves a trail: the messages, the model requests, each tool call and its
          result, each step, the mode changes, and the hook invocations. The conversation you read
          in the app is a view of that trail, which is why a conversation that was interrupted, or
          that ran in the background, can still be followed afterwards.
        </p>
        <p>
          When a conversation grows past what the model can hold, the older part is summarised and
          the recent messages are kept, so the work continues with the context that matters and
          without the transcript growing without limit.
        </p>
      </DocsSection>
      <DocsSection title="Safety, privacy, and the local mode">
        <p>
          Every conversation, file, terminal session, workspace, and tool call belongs to one tenant
          and one user. Another account&apos;s identifier is refused exactly like one that does not
          exist, so an identifier never reveals that something is there.
        </p>
        <p>
          Shell commands and extension code run inside an operating-system cage: bubblewrap on
          Linux, the seatbelt profile on macOS, a restricted token on Windows, or a container. The
          cage gives the command the workspace and little else - the user&apos;s home directory is
          not visible, and the network is off when the deployment says so. A conversation can also
          ask for a stricter mode, so one conversation can be locked down without changing the
          deployment.
        </p>
        <p>
          When DeepSpace runs on its own local database, it signs you in as the local operator with
          no password at all: the machine is the boundary. The Docker deployment uses a real
          database and real logins, and stored credentials and session records are encrypted at
          rest. Deployment secrets never reach a tool: a command and an extension receive the paths
          they need and none of the secrets.
        </p>
      </DocsSection>
      <DocsSection title="Context and token diagnostics">
        <p>
          The composer keeps two scopes visible: the latest serialized context sent to the provider
          and the cumulative serialized input/output estimate for the whole DeepSpace turn. Tool
          calls and recovery rounds can make the turn total larger than the latest request. These
          local estimates use a provider-neutral serialization because each model family has its own
          tokenizer.
        </p>
        <p>
          When an adapter returns usage, the panel shows the provider&apos;s exact input, output,
          cache-read, and cache-write fields and labels them as the latest reported model round.
          When every round reports a field, DeepSpace also shows the exact provider turn total;
          otherwise it marks that field incomplete. Codex, hosted OpenAI-compatible providers,
          Anthropic, Gemini, LM Studio, and Ollama are normalized into the same view. Local
          providers that do not return usage remain marked as unreported; DeepSpace does not invent
          a token count for them. Cache eligibility is also kept separate from a confirmed provider
          cache read.
        </p>
      </DocsSection>
      <DocsSection title="Choose a model in DeepSpace">
        <p>
          A conversation either names the model it runs on, or lets DeepSpace choose. The choice
          belongs to the conversation, so it is remembered: reopen the conversation tomorrow and the
          same model is waiting. The choice can be changed at any time, and it applies from the next
          turn. A manual choice may name any model you are allowed to use, not only the one the
          workspace was assigned.
        </p>
        <p>
          When DeepSpace chooses, the rule is explainable rather than random: a turn with an image
          wants a model that reads images, a turn that calls tools wants one that calls tools, and a
          long conversation wants one whose context holds it. The same turn always picks the same
          model. The composer shows a <strong>Let DeepSpace choose</strong> switch for this, and the
          model in force is always named in the composer.
        </p>
        <p>
          The model picker searches across available models from your connected chat providers. It
          groups results under each provider&apos;s display name, sorts provider groups and model
          names alphabetically, and lets you search by provider, model name, model ID, or
          quantization. The first provider group opens by default; opening another group closes the
          previous one. The selected model is marked within its provider group.
        </p>
        <p>
          The Think menu follows the selected model&apos;s advertised reasoning levels. For a local
          Codex CLI account, DeepSpace reads the per-model catalog, including Codex&apos;s{" "}
          <code>minimal</code> and <code>ultra</code> levels, and sends the selected model&apos;s
          native value. Other safe levels advertised by a model are shown with readable labels. The
          browser ChatGPT model catalog may omit per-model effort metadata; when it does, DeepSpace
          uses the existing OpenAI reasoning profile and does not present it as an exact
          account-specific list.
        </p>
        <p>
          Other providers use the selected model&apos;s verified capability profile or the effort
          levels advertised by its model catalog. LM Studio&apos;s model catalog can provide native
          reasoning options, and Ollama&apos;s model detail endpoint provides each installed
          model&apos;s named thinking values or On/Off support. Models without reliable metadata
          keep their provider default. If the selected model&apos;s metadata is missing or stale,
          DeepSpace refreshes that provider&apos;s catalog in the background and updates the menu
          when it completes. The configured default and current chat assignment are included even
          before their first model-catalog cache entry exists. For example, NVIDIA NIM Nemotron 3
          Ultra offers Off, Medium, and High; Nemotron 3 Super offers Off, Low, and High. DeepSpace
          sends the provider&apos;s native request values for those choices. When a model&apos;s
          capabilities remain unknown, the menu shows the provider default instead of suggesting
          unsupported levels. If a model always reasons and cannot disable that behavior, DeepSpace
          shows its available levels or On without offering a misleading Off choice.
        </p>
      </DocsSection>
      <DocsSection title="Provider health and conversation details">
        <p>
          The compact provider-health summary is based on your recent DeepSpace usage, not a live
          connection check. Expand it to see recent usage by provider and model, including sample
          count, failure rate, and p50/p95 latency. P50 is the median; p95 shows the slower end of
          recent requests. If there are no samples yet, DeepSpace says{" "}
          <strong>No recent usage yet</strong>.
        </p>
        <p>
          The expanded details also show the selected conversation&apos;s existing Conversation ID,
          which you can copy for support. It is the same ID used by the chat system; there is no
          separate Chat ID or DeepSpace ID.
        </p>
      </DocsSection>
      <DocsSection title="Response speed and first-token time">
        <p>
          While a reply is streaming, the composer shows a live output-token estimate and an
          estimated tokens-per-second rate. The live estimate includes answer, reasoning, and
          streamed function-argument chunks and refreshes as chunks arrive; it is intentionally
          marked as an estimate because the provider tokenizer and final usage are not available
          yet. Completed DeepSpace replies show output speed, end-to-end TTFT, and provider
          first-output time beside the response actions. End-to-end TTFT runs from the server
          starting the DeepSpace request to the first visible answer text. It includes routing,
          context preparation, provider waiting, and any reasoning or tool work before the answer.
          Provider first-output time starts when the first provider request begins and ends when the
          server receives its first output event. It includes network and connector overhead; Codex
          also includes the local desktop relay, so it is not the model vendor&apos;s internal
          compute time.
        </p>
        <p>
          Output speed divides exact provider-reported completion tokens by the measured interval
          between the first and last output events in each generation round. Provider counts may
          include hidden reasoning or tool-call tokens, but DeepSpace never displays reasoning text
          in this metric. If exact usage is missing, the provider sends only one output event, or a
          response uses a buffered fallback, speed is shown as unavailable instead of being guessed
          from text. The tooltip explains why. These measurements are saved with the reply and
          remain visible when you reopen its conversation.
        </p>
      </DocsSection>
      <DocsSection title="Use files and images in a chat">
        <p>
          Attach files to a message when you want DeepSpace to use them as context. The selected
          model can use the file-reading tools that fit your request, including reading extracted
          text, searching a longer document, comparing files, or analyzing tabular data. DeepSpace
          checks that attached files belong to your account and conversation before using them.
        </p>
        <p>
          The composer&rsquo;s attachment buttons cover the usual ways in: the paperclip picks files
          from your device, the camera takes a photo, the folder attaches a file you already keep in
          your Library, and the clipboard button pastes what your system clipboard holds. An image
          on the clipboard becomes an attachment, and text on it becomes part of the message.
          Dragging a file onto the composer and pressing <code>Ctrl</code> + <code>V</code> work the
          same way. A browser that refuses to let the page read the clipboard says so and asks you
          to paste instead.
        </p>
        <p>
          Images are sent directly to the model only when that model supports image input. If it
          does not, DeepSpace uses the text saved by upload-time extraction or OCR when available.
          If a model explicitly rejects an image, DeepSpace retries once without the image and uses
          saved OCR text. If visual input is unavailable and extraction found no readable text, the
          attachment shows that status and the assistant is told not to guess. Very large image sets
          may be limited.
        </p>
        <p>
          An attachment becomes ready as soon as its private file is stored. Text extraction and OCR
          then continue in the background; the composer shows “Uploaded · reading text” while that
          work is pending. Supported images can be sent to a model with visual input before OCR
          finishes. For other files, DeepSpace reports pending extraction instead of treating
          missing text as proof that the file is empty. When processing finishes, the attachment
          shows whether text was extracted, OCR was used, no readable text was found, or extraction
          was unavailable. Short whole-file reads use saved text directly; targeted questions and
          large documents can use document search.
        </p>
      </DocsSection>
      <DocsSection title="Choose how the agent works, and where it works">
        <p>
          The composer carries two menus beside the sandbox mode. <strong>Workspace</strong> names
          the directory the conversation works in: the files the panel lists, the directory a
          terminal starts in, and the place the agent&rsquo;s file and shell tools write. Pick
          another workspace to move the conversation into it, or choose{" "}
          <strong>Add workspace…</strong> to make a new one. <strong>Agent mode</strong> names how
          the agent works: <em>Standard</em> uses every tool the policy allows,{" "}
          <em>Programmatic tool calling</em> lets the agent write one program that calls the tools,{" "}
          <em>Minimal</em> limits it to read-only tools, and <em>Creator</em> gives it the notes,
          Library, and artifacts tools. The mode in force is marked, and the menu explains each one.
        </p>
        <p>
          The mode and the workspace belong to the conversation, so two conversations can work in
          different ways and different directories at the same time. Changing either takes effect
          for the turns that follow.
        </p>
      </DocsSection>
      <DocsSection title="Work beside the chat: files, terminal, and browser">
        <p>
          An empty conversation offers three cards: <strong>Workspace files</strong>,{" "}
          <strong>New terminal</strong>, and <strong>Browser</strong>. Each one opens the side panel
          on that surface, so the tools a task needs are one click away instead of hidden behind a
          menu. The same panel is reachable at any time from the composer.
        </p>
        <p>
          <strong>Workspace files</strong> shows the files of the conversation&rsquo;s own
          workspace. Folders expand in place, a file opens with its content, and the path shown
          above the list is the directory the panel is reading. A conversation that has not started
          yet has no workspace, so the cards stay disabled until it has one.
        </p>
        <p>
          <strong>New terminal</strong> opens a persistent shell session in that workspace, under
          the shell the host reports as available. The server refuses a shell the host cannot run.
          The panel is a real interactive terminal: ANSI colors and cursor movement render in the
          terminal emulator, keyboard input including Ctrl and Alt sequences is sent immediately,
          and resizing the panel resizes the server-side PTY. A session keeps running between visits,
          and the agent can open its own terminals in the same workspace, so you can watch the work
          it does.
        </p>
        <p>
          <strong>Browser</strong> opens a page beside the chat. A deployment that runs the browser
          runtime drives a real Chromium: click the screenshot to click the page, type into the text
          field, press Enter, Tab, Backspace, or Esc, and scroll up or down. A loopback address and
          this app&rsquo;s own origin open without a prompt; any other host waits until you allow
          external sites, and that permission is dropped as soon as you type a new address. A
          deployment without the runtime keeps the embedded view: enter an HTTP(S) address, then use
          the back, forward, and reload controls to move through the pages you visited. The framed
          page runs without access to this app&rsquo;s origin, and &ldquo;Open in new tab&rdquo;
          hands either view to your own browser.
        </p>
        <p>
          The workspace also answers the keyboard. <strong>Ctrl</strong> + <strong>\\</strong>{" "}
          switches the split view, <strong>Ctrl</strong> + <strong>Alt</strong> +{" "}
          <strong>Enter</strong> fills the screen and leaves it again, and <strong>Ctrl</strong> +{" "}
          <strong>Alt</strong> + <strong>B</strong> collapses or opens the sidebar. The buttons in
          the header show the same shortcuts.
        </p>
      </DocsSection>
      <DocsSection title="Rich answers, charts, and diagrams">
        <p>
          DeepSpace keeps normal Markdown as the source format and promotes supported fenced blocks
          into interactive surfaces. A <code>chart</code> fence accepts JSON with a title, a chart
          type (<code>line</code>, <code>bar</code>, <code>area</code>, <code>scatter</code>, or
          <code>pie</code>), and a series of labeled values. Bar charts center each category in its
          own slot and draw positive and negative values from zero. Completed charts support hover
          details, value editing, named multi-series data, left/right axes, annotations, interactive
          legends, a payload view, JSON download, and revisioned save back to the conversation. Add
          an <code>equation</code> field when a chart needs its formula shown beside the visual.
          Recalculating refreshes the chart JSON source from the same values and returns to the
          visual view. If the revision cannot be saved, the editor stays open so you can retry.
          Optional
          <code>x_label</code> and <code>y_label</code> fields appear as axis titles.
        </p>
        <p>
          Use Mermaid for flowcharts, sequence diagrams, UML, architecture, business models, and
          schedules such as Gantt charts. Gantt source can use a <code>gantt</code> fence or a
          <code>mermaid</code> fence beginning with <code>gantt</code>; make the{" "}
          <code>dateFormat</code>
          match task dates (for example, <code>YYYY-MM-DD</code> for <code>2026-01-05</code>).
          Mermaid diagrams include copy, Save artifact, JSON and Mermaid-source downloads, plus
          SVG/PNG/PDF exports. SVG stays vector-based; PNG and PDF use the SVG viewBox and render at
          up to 2× resolution within browser canvas limits. Labels use native SVG text so raster
          exports remain self-contained. UML class diagrams and Gantt schedules also expose
          structured controls that regenerate the Mermaid source. Use a <code>card</code> fence for
          a finding, recommendation, source, or media summary; cards can contain safe Markdown,
          metrics, an approved image URL, a source link, copy/download controls, and explicit
          actions. External URLs must come from an authorized search or read result. Card tones are
          info, success, warning, error, or neutral; common model labels such as positive and
          caution are normalized automatically. Raw HTML and scripts are not rendered.
        </p>
        <p>
          A <code>graph</code> fence opens the portable canvas editor: nodes can be moved and
          resized, connected, grouped, aligned, zoomed, or annotated with freehand paths, then
          copied or downloaded as DeepSpace <code>.draw.json</code>; that portable JSON is not an
          Excalidraw file. You can import the same format directly into the chat canvas. Mermaid
          source can be downloaded as <code>.mmd</code> or edited in place, which makes UML,
          business-model, and Gantt diagrams practical inside the chat and notes. A{" "}
          <code>gallery</code> fence creates a safe image sequence with captions, next/previous
          controls, source links, and downloads. DeepSpace copies usable raster images into private
          conversation storage so previews and exports do not rely on third-party CORS. These
          temporary copies expire after 30 days; choose <strong>Save image to Library</strong> to
          keep a photo. Temporary copies stay out of the regular Library list and are removed by
          scheduled storage cleanup. Visual revisions update the authenticated message and active
          note. If a visual is saved as a Library artifact, later chat, note, or artifact edits
          advance its version and synchronize the revision to the linked views. Delayed note
          refreshes preserve the latest saved visual revision; conflicting edits from another
          session are reported so the note can be reloaded. Providers may use a generic
          <code>json</code> fence for these same payloads; DeepSpace recognizes unambiguous card,
          chart, graph, and gallery JSON automatically. Invalid visual JSON is kept as plain code
          instead of being executed. A missing image or non-image URL shows a recoverable fallback
          and does not terminate the answer. JSON, SVG, PNG, and PDF exports are available for
          visual blocks; gallery SVG/PNG exports embed owned images when available and show a
          placeholder for a missing image. PNG/PDF export strips external SVG resources before
          rasterization; if a browser still blocks rasterization, the error is shown inline instead
          of escaping as an exception. Saved rich blocks in notes are restored as rendered visuals
          when the note is reopened, so the note preview does not fall back to raw visual-fence
          code.
        </p>
        <p>
          Tables include copy and CSV/XLSX export controls. In DeepSpace, with a conversation open,
          choose <strong>Save to Library</strong> to store a table as a private, versioned visual
          artifact that can be reopened from the Library. If clipboard access is blocked, DeepSpace
          tries a browser fallback and shows an error if copying still cannot complete. Export and
          Library-save failures are also reported beside the table controls.
        </p>
        <pre className="overflow-x-auto rounded-xl border border-white/10 bg-black/20 p-4 text-xs leading-6">
          <code>{`\`\`\`chart
{"title":"Quarterly revenue","chart_type":"line","equation":"y = mx + b","series":[{"label":"Q1","value":12},{"label":"Q2","value":19}]}
\`\`\`

\`\`\`card
{"title":"Decision brief","tone":"success","content":"**Ship** the verified workflow.","metrics":[{"label":"Confidence","value":"92%"}]}
\`\`\``}</code>
        </pre>
      </DocsSection>
      <DocsSection title="Share a conversation">
        <p>
          Use <strong>Copy link</strong> in the conversation list to copy the address of the
          conversation you are in. Opening that address opens the same conversation, so a link can
          be pasted into a chat, a ticket, or a browser. The link names one conversation, and a link
          that names a conversation you cannot open leaves the page in its normal empty state.
        </p>
        <p>
          In the desktop app the copied address points at the local server the app runs, so it opens
          on that machine. The web deployment produces a normal shareable address.
        </p>
      </DocsSection>

      <DocsSection title="Prompt and queue status">
        <p>
          Replies with saved versions have previous and next controls. Choosing a version shows it
          immediately and saves it as the active reply; all saved versions remain available. If
          saving the selection fails, DeepSpace reloads the saved reply. When you switch
          conversations, the old thread is temporarily read-only until the selected history loads. A
          background run continues on the server and can reconnect when you return to its chat. If
          history cannot be loaded, use <strong>Try again</strong> to retry without replacing the
          thread you were viewing.
        </p>
        <p>
          Use <strong>Regenerate</strong> on a saved reply to start a fresh generation for that
          response. DeepSpace keeps the previous reply as a version, uses the saved prompt and its
          Library attachments, and queues the work with the rest of the conversation. You can
          regenerate while another reply is running; the current reply remains visible until the
          regeneration starts. Regenerating an older reply uses only the conversation context that
          came before that reply. If the browser reconnects or the app is reopened, the response
          continues from its saved event stream. A queued regeneration can be canceled, but its
          source prompt cannot be edited.
        </p>
        <p>
          The message field grows with multiline text up to five visible rows; longer drafts scroll
          inside the field.
        </p>
        <p>
          For a saved conversation, DeepSpace stores each turn in its durable queue before a
          background worker starts it. While your current turn is waiting in that queue, its
          activity panel says <strong>Queued</strong>. After a worker claims it, the panel changes
          to <strong>Thinking &amp; activity</strong>. The composer queue is for other pending
          turns, so the current prompt is not listed there a second time.
        </p>
        <p>
          A failed prompt is not queue work. Its error stays in the conversation, and the queue
          pauses with the prompts that are waiting kept exactly where they are.{" "}
          <strong>Resume queue</strong> checks the last prompt first: when it finished, the queue
          runs the next waiting prompt; when it was interrupted, the queue stays paused and says
          why.
        </p>
        <p>
          If a prompt remains queued, the worker has not claimed it yet. Workspace administrators
          should check DeepSpace worker availability; users can keep other pending prompts in the
          composer queue while they wait. A queued prompt is shown as active only after a worker
          claims it, and the chat reconnects after that state change. If a worker claims a prompt
          just before you choose <strong>Steer</strong>, the queue refresh explains that it has
          already started; a running prompt cannot be reordered.
        </p>
        <p>
          A queue submission that meets a temporary service interruption is retried once; if it
          still cannot be saved, the draft stays in the composer and the error explains what
          happened. An initial stream HTTP error also gets one retry, then automatic reconnect stops
          so the activity panel does not stay live through a retry loop. This reports a connection
          failure; it does not confirm that an already-started server worker stopped. Reopen the
          conversation after service recovers to attach to its saved run. When you stop a running
          response, the activity panel waits briefly for the server confirmation. If that terminal
          event is unavailable, it stops the local timer and reloads saved history so an interrupted
          worker cannot remain displayed as running.
        </p>
        <p>
          Use <strong>Edit</strong> beside a queued prompt to bring it back into the composer.
          Saving updates that same queue entry and keeps its place in line; canceling restores the
          text you had in the composer before editing. Existing Library attachments remain on the
          queued prompt. A prompt cannot be edited after a worker starts it, or after it fails; in
          those cases the edited text stays in the composer and a failed prompt can be retried as a
          separate attempt.
        </p>
        <p>
          If a turn fails, DeepSpace keeps its error and advances to the next queued prompt. Use
          <strong> Retry</strong> on the red error card or <strong>Retry prompt</strong> in the
          queue to create a new attempt from the exact saved prompt and its original Library
          attachments. It resumes from a saved checkpoint when one is available; if that checkpoint
          has expired, DeepSpace restarts the saved prompt and completed external actions could run
          again. <strong>Continue queued prompts</strong> resumes a manually paused queue without
          retrying the failed turn.
        </p>
        <p>
          If a background worker stops unexpectedly, DeepSpace closes the old live stream, keeps any
          partial text, and marks the turn as retryable instead of leaving it labeled as running.
          Use <strong>Retry</strong> or <strong>Regenerate</strong> to start a new attempt.
        </p>
        <p>
          When DeepSpace resumes a clarification, approval, or failed run, it restores the original
          saved task even if it is older than the normal chat context window. Some providers also
          require private continuation data after a tool call; DeepSpace stores that data encrypted
          for resuming and does not return it in chat history.
        </p>
        <p>
          When a response creates a saved task plan, a collapsible task drawer appears immediately
          above the composer. It opens as the plan is created, shows each task&apos;s live status,
          and can be collapsed to a one-row progress summary that keeps the active task and its
          running indicator visible. The task drawer is the single place that shows plan progress;
          <strong>Thinking &amp; activity</strong> continues to show only the tool activity.
          DeepSpace checks the saved task ledger before it reports the final answer.
        </p>
        <p>
          <strong>Plan mode</strong> asks DeepSpace to design before it acts. Turn it on with the
          plan button beside the composer, and DeepSpace researches and inspects without changing
          anything, then presents a plan for your review. Approve the plan to start the work, or ask
          for changes and DeepSpace revises it. The mode belongs to the conversation, so it stays on
          for the next message.
        </p>
        <p>
          The <strong>agent tree</strong> button shows the child agents a conversation delegated to
          and the shape of that delegation, so you can see which work ran in parallel and which
          agent started it.
        </p>
        <p>
          DeepSpace can run one program that calls its tools as ordinary function calls, which
          replaces a long chain of round trips with a single step. The program runs in a confined
          sandbox with no network, so every piece of data it uses has to come from a tool call.
        </p>
        <p>
          A tool result that is far larger than the context window is offloaded to durable storage
          instead of being truncated. DeepSpace shows a preview and the model can read the complete
          result back when it needs it.
        </p>
        <p>
          Error headings describe the failure type. <strong>Request rejected</strong> means the
          provider received but refused the request, while <strong>Provider unavailable</strong>
          means a provider timeout or server outage. A request rejection usually needs the provider
          message or selected model settings to be checked; waiting alone may not fix it.
        </p>
      </DocsSection>
      <DocsSection title="The runtime status panel">
        <p>
          Settings holds a DeepSpace panel that shows what the deployment has turned on, and what
          the agent will be allowed to do on this host: the sandbox backend and whether the
          operating-system cage is active, whether the network is allowed inside it, the permission
          mode, the approval mode, the parallel tool budget, the spill threshold, the local shell
          tools, programmatic tool calling, plan mode, the Agent Client Protocol bridge, how the
          model is chosen, named teammates, and the remote runtime.
        </p>
        <p>
          The panel is worth reading before a task that touches the machine. It reports the host
          that will run the commands, not only the configuration file, and it says plainly when no
          cage is available - in that case the local shell tools run with inspection only, and
          installing bubblewrap on Linux, or running on macOS, is what confines them.
        </p>
      </DocsSection>
      <DocsSection title="The full DeepSpace manual">
        <p>
          This page is the short guide. The manual in <code>backend/docs/deepspace/</code> is the
          long one: one numbered chapter per subject, each with the use case, why it matters, how it
          works, and what to watch for. It is written for the person reading it, not for the code.
        </p>
        <p>
          <Link className="text-primary underline" href="/documentation/deepspace-manual">
            Read the full DeepSpace manual in the app
          </Link>
        </p>
        <ul className="list-disc space-y-2 pl-6">
          <li>
            <strong>01-02:</strong> what DeepSpace is, and the agentic loop - one turn, phase by
            phase.
          </li>
          <li>
            <strong>03-06:</strong> parallel execution, delegation and subagents, teammates and the
            swarm, and background work.
          </li>
          <li>
            <strong>07-08:</strong> the session log and the trajectory, and how the model is chosen.
          </li>
          <li>
            <strong>09-12:</strong> the tool catalogue, dynamic tool discovery, the native tool
            surface, and hooks.
          </li>
          <li>
            <strong>13-18:</strong> the sandbox, terminals and the remote runtime, workspaces and
            files, the in-app browser, extensions and plugins, and MCP connectors.
          </li>
          <li>
            <strong>19-21:</strong> the composer and the Start screen, security and encryption, and
            the agent modes.
          </li>
          <li>
            <strong>22-25:</strong> the settings reference, the local port map, operations and
            troubleshooting, and the glossary.
          </li>
        </ul>
      </DocsSection>
      <DocsSection title="DeepSpace guides">
        <ul className="list-disc space-y-2 pl-6">
          <li>
            <Link className="text-primary underline" href="/documentation/deepspace-library">
              DeepSpace Library
            </Link>
          </li>
          <li>
            <Link className="text-primary underline" href="/documentation/editor-files">
              Notes and editor
            </Link>
          </li>
          <li>
            <Link className="text-primary underline" href="/documentation/web-research">
              Web research
            </Link>
          </li>
          <li>
            <Link className="text-primary underline" href="/documentation/sandbox">
              Sandbox and data analysis
            </Link>
          </li>
          <li>
            <Link className="text-primary underline" href="/documentation/artifacts">
              Artifacts and exports
            </Link>
          </li>
          <li>
            <Link className="text-primary underline" href="/documentation/automation">
              Schedules and automation
            </Link>
          </li>
          <li>
            <Link className="text-primary underline" href="/documentation/memory-workspace">
              Memory
            </Link>
          </li>
          <li>
            <Link className="text-primary underline" href="/documentation/voice">
              Voice
            </Link>
          </li>
        </ul>
      </DocsSection>
      <DocsSection title="Availability and limits">
        <p>
          DeepSpace behavior depends on a configured model provider. Tool use is constrained by
          connection ownership, provider and tool policy, approval settings, and backend
          authorization. A model response is not itself proof that an external action completed;
          review the tool result or generated artifact status shown in the app.
        </p>
        <p>
          Durability and feature availability depend on the running API, database, workers, and any
          optional services used by a particular action. This guide does not claim a deployment is
          production-ready solely because the feature exists in the repository.
        </p>
      </DocsSection>
    </DocsShell>
  );
}
