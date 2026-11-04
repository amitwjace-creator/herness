# Herness

A personal AI workspace with a dark gray and blue web interface, local project
files, explicit tool approvals, and inspectable history. Python 3.11+.

## Run locally

```bash
python -m pip install -e '.[documents]'
python -m herness --workspace /path/to/project web --demo
```

Open `http://127.0.0.1:8765`. In a remote workspace, use your platform's authorized
port forwarding. The scripted demo exercises real file tools without a model or
API key. Start without `--demo` to configure a live model in **Model settings**.
The optional `documents` extra enables PDF export; basic installation with
`pip install -e .` includes PDF text extraction and all other core functionality.

Nothing is published or deployed by starting this app. It binds to loopback.

## Native iPhone app

The SwiftUI iPhone project is in [`ios/`](ios/README.md). It uses the same agent,
conversation history and SQLite vector database through an authenticated API.
The Python agent runs on your computer or private server; the iPhone is its
native client. The app is not a web-view wrapper.

Create a separate pairing key, then start the mobile backend instead of the web
backend for that workspace:

```bash
python -m herness mobile-key --output /private/path/herness-mobile.key
python -m herness --workspace /path/to/project mobile \
  --public-origin https://your-private-server.example \
  --key-file /private/path/herness-mobile.key
```

The example hostname and paths must be replaced. The server stays on loopback
port 8766. A private HTTPS reverse proxy or VPN gateway must forward requests to
that port. Enter its HTTPS URL and the pairing key in the app. The mobile server
requires its bearer key for API access and does not serve the browser bootstrap
page. Run one Herness backend process per workspace; devices share one active
conversation and agent turn. Use `--demo` for the scripted backend demo.

The iOS project includes a shared Xcode scheme, unit tests and a macOS CI build
workflow. The [November 4, 2026 Mac build](https://github.com/amitwjace-creator/herness/actions/runs/41882933868)
passed its iPhone simulator tests and saved an unsigned iPhone archive.
For personal installation without a paid Apple Developer membership, use
[AltStore Classic with a Windows PC](ios/SIDELOAD-WINDOWS.md). AltStore signs the
app with a free Apple ID; it needs refreshing through AltServer every seven days.
TestFlight distribution requires paid Apple Developer membership, signing and an
App Store Connect app record; no signed app or store upload has been produced.
See the [iOS setup and release instructions](ios/README.md).

## Connect a provider

**Model settings** offers OpenAI-compatible Chat Completions, OpenAI Responses,
native Anthropic Messages, and native Gemini. Select the protocol, endpoint,
model and API key, then test and save. DeepSeek and compatible local servers use
Chat Completions. The model must support the selected protocol and tool calling;
image attachments require a vision-capable model. Preset model names are editable.

API keys entered in the UI remain in server memory and are scoped to their
endpoint. They are not saved in browser storage or settings files. Settings
persist in the selected project's `.herness/settings.json`; after a restart,
reenter an in-memory key or configure the matching provider in the environment:

```bash
export HERNESS_PROTOCOL=chat_completions
export HERNESS_API_BASE=https://api.openai.com/v1
export HERNESS_MODEL=gpt-4.1-mini
export HERNESS_API_KEY='your-provider-key'
python -m herness web
```

`HERNESS_PROTOCOL` also accepts `responses`, `anthropic`, or `gemini`.
Anthropic endpoints normally end in `/v1`; Gemini endpoints in `/v1beta`.
`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, and `GEMINI_API_KEY` are provider-specific
fallbacks. Local HTTP is allowed only on loopback; remote endpoints require HTTPS.
Saved web settings take priority over environment defaults. Model profiles and
discovery are in **Workspace → Model profiles & discovery**. Connection tests and
live agent runs can incur provider charges.

## Workspace features

- Chat streams responses and tool activity. Stop cancels pending approvals,
  interrupts model output, and terminates running command process groups.
  Markdown, fenced code highlighting, tables, copying, quoting, saved messages,
  find, edit-and-branch, retry, JSON/Markdown exports, drafts and tabs are available.
- The sidebar keeps New chat, Search, Knowledge and Workspace together, with
  recent conversations below. **Organize conversations** reveals folder, pin,
  trash and bulk controls. The bottom workspace menu holds settings and memory.
  Appearance includes dark/light/system themes, text size, direction, contrast
  and reduced motion. The opening screen has a centered greeting, shortcuts
  above a compact bottom composer, and a model picker in the header. The layout
  adapts to mobile, including safe-area spacing and horizontal shortcut scrolling.
  The header speaker toggles reading newly completed replies aloud; it starts
  off and remembers your choice in this browser. Dictation, when supported by
  the browser, fills the composer for review before sending.
- **Project settings** in the workspace menu manages local folders, instructions, goals, pinned reference files
  and ignored paths. Repository `AGENTS.md` instructions are included in context.
  **Files** browses source files; the context inspector shows context sources and
  approximate token counts. Ctrl/Cmd+K searches chats, Ctrl/Cmd+Shift+P opens
  commands, and Ctrl/Cmd+F finds chat text. Conversation and message overflow
  menus hold their secondary actions.
- The composer's **+** menu contains attachments, agent tools and **Plan/Act**.
  **Plan** exposes read-only tools. **Act** can propose file edits, commands and
  memory changes, each requiring approval. Full unified diffs, changed-file
  checks, automatic file checkpoints and reviewed undo protect existing edits.
  Search, code symbols, Git status/diff and test-command tools inspect real files.
- **Workspace** organizes research, document/data tools, artifacts, schedules,
  task plans, reusable skills, specialist teams, model profiles and privacy.
  Choose a category or search for an action to open its focused form.
  Capability responses explicitly identify unavailable integrations.

## Vector database and semantic search

Open **Knowledge** in the sidebar. Herness uses **SQLite + sqlite-vec** to keep a
separate vector database in each project's `.herness/vectors.sqlite3`. No separate
database server is required; the normal install command includes sqlite-vec.

1. In **Embeddings**, configure an embedding model and save its settings. Choose
   **Ollama** for a reachable local server (default `http://127.0.0.1:11434`, model
   `nomic-embed-text`) or an **OpenAI-compatible** embeddings endpoint (typically
   a base URL ending in `/v1`, with a model such as `text-embedding-3-small`).
   Ollama's embedding model must already be installed on that server. Here,
   `127.0.0.1` means the machine running Herness, which may be a cloud workspace.
2. Use **Test connection**. This sends a small test string to the saved endpoint.
   Embedding settings and credentials are separate from the chat model. Keys stay
   in memory and must be reentered after restarting or reopening a project.
3. In **Sources**, add documents, select the documents or memories you want to
   index, and choose **Index selected**. The destination is shown before indexing.
   Source text is sent to that embedding provider; the resulting vectors stay
   in the project database. Opening Knowledge or uploading a file does not send
   content for embedding automatically.
4. Search by meaning in **Search**, filter by source type, and use cited excerpts
   in chat. The web agent also has a read-only `semantic_search` tool when
   embeddings are enabled. Similarity scores describe closeness, not factual
   confidence. Search queries are sent to the configured embedding provider.
   Embedding API charges are separate from the chat model's estimated budget.

Indexable sources are extracted document text, active structured project memories,
and the project memory note. Images without extracted text require OCR elsewhere.
Passages retain page/section labels and overlapping character ranges. Indexing is
cancellable and updates each source atomically; already completed sources remain
available if a later source fails. Unchanged sources are skipped. Each job accepts
up to 100 sources and 2,000 passages; embeddings support up to 4,096 dimensions.

Changed or deleted source text and expired, excluded or conflicted memories are
removed from retrieval. Updated text must be explicitly reindexed. Changing the
embedding model or endpoint requires clearing and rebuilding the index to avoid
mixing incompatible vectors. **Remove** and **Clear index** preserve the original
documents and memory. Project backups keep the originals, not this derived index;
rebuild the index after restoration. Storage is local plaintext, without cloud sync.

No embedding model is bundled and no synthetic embeddings replace a missing
provider. Real provider connectivity still needs your configured model/server.
The tests use local HTTP embedding fixtures with the real SQLite vector engine.

An optional end-to-end browser test covers Knowledge separately:
`node tests/browser_vector.cjs` (same Playwright/Chromium requirements as below).

## Documents, artifacts and research

The document library accepts text/code, selectable-text PDFs, DOCX, XLSX, PPTX
and common images. Extracted passages have page, paragraph, sheet or slide labels.
Content hashes deduplicate library uploads; search, original downloads and deletion
are supported. Chat attachments support text, PDFs and common images. Scanned PDF
OCR is not implemented. Library deletion does not remove existing chat attachments.

Artifacts support content editing, revision checks, version history, diffs, static
HTML previews and downloads. Export formats include plain text, Markdown, HTML,
JSON, SVG, DOCX, ZIP and optionally PDF. HTML previews run in a sandboxed iframe
with active content removed. Spreadsheet tools profile/filter/export CSV, compare
datasets, and create SVG bar charts. SQLite queries use read-only connections,
authorizers and bounded execution; no Python notebook sandbox is configured.

Research can fetch public HTTPS pages, retrieve several pages concurrently,
extract text, save source collections, refresh and export sources. Web search
requires a Brave or Tavily key configured in Workspace. Public GitHub issue/PR reads
are available; private reads use `HERNESS_GITHUB_TOKEN`. HTTP MCP supports tool
discovery and explicitly reviewed calls. OAuth and stdio transports are not
implemented. External content is untrusted reference material. A citation records
the source of an extracted passage; it does not establish that a claim is true.
The web agent can call the same web search/fetch, document retrieval, artifact
creation and MCP services. Artifact creation and every MCP tool call require
review. Network requests have timeouts; Stop may wait for an in-flight web
request to finish before the agent releases the turn.

## Tasks, automation and runtime

Schedules support one-time, interval, daily and weekly execution with IANA time
zones, next-run previews, approval, pause/resume, occurrence limits and a missed-run
policy. The local server must remain running. Each scheduled run has its own
conversation; Plan is the default. Act still requires interactive approvals. Changes
to a schedule's scope revoke its approval. Interrupted work is recorded and is not
automatically replayed. External notification/delivery channels are not configured.

Task plans record dependencies, status, acceptance criteria and evidence. Reusable
prompt skills have validated inputs, versions and explicit trust. Specialist teams
run isolated, read-only sessions with bounded concurrency, step limits and
cancellation; they do not automatically merge file changes or arbitrate conclusions.

Runtime records include tool/model timing, steps, usage, errors and checkpoints.
Context and cost limits are configurable. Cost figures use user-entered token
prices and estimated token counts where provider usage is absent; they are not a
hard provider billing cap. Duplicate tool loops stop automatically. Transient
provider errors have bounded retries. Interrupted tool batches are repaired without
repeating actions whose outcome is unknown.

## Memory, permissions and local data

**Project memory** in the workspace menu edits the legacy project note. Structured
project memory in Workspace adds provenance, versions, expiry, exclusion, conflict reporting and
import/export. Active memory is included in future model context; memory deletion
does not erase text already present in conversations.

Agent permission profiles can restrict read/write/execute/memory scopes. Privacy
actions in Workspace offer explicit retention previews and deletion, metadata-only audit records,
and project ZIP backup/restore with content hashes, collision review and stale
preview protection. Backups contain private project data; they do not include
provider credentials, connector configuration or automation configuration. Data is
stored locally without application-level encryption or cloud synchronization.

This is a single-user local application, not a hosted multi-user service or an OS
sandbox. Approved commands inherit the server environment and can access files and
network outside the project. Docker availability is reported separately; execution
is currently local. Do not expose this server publicly. File tools reject escaping
paths, sensitive directories and common credential filenames. This is not
comprehensive secret detection. Uploaded content, tool results and chat messages
can be sent to the selected provider and are retained in local history.

## Terminal and verification

```bash
python -m herness doctor
python -m herness chat --demo 'Inspect this project'
python -m herness chat --plan --session planning 'Plan this change'
python -m unittest discover -s tests -v
python -m compileall -q herness
node tests/browser_smoke.cjs
node tests/browser_navigation.cjs
```

Tests cover real file and shell behavior, permission denial, path validation,
checkpoint/undo conflicts, protocol contracts against local mock providers,
streaming/cancellation, API authorization, conversation branches, schedules,
specialists, documents, data operations, memory and backup boundaries. They do not
validate paid provider access or third-party accounts. The browser check requires
Playwright for Node and Chromium. Set `PLAYWRIGHT_MODULE` to an absolute module
path and `CHROMIUM_PATH` to your browser binary when they are outside the normal
locations. It starts an ephemeral offline server and removes its temporary data.
The navigation check covers menus, keyboard focus, mobile layout and every
Workspace action without running those actions. The separate Knowledge browser
check exercises vector indexing and retrieval against a local embedding fixture.

[ROADMAP.md](ROADMAP.md) tracks the requested 300-feature scope, with implemented,
partial, planned and service-dependent items. The full roadmap is not yet complete.
