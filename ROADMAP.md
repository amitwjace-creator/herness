# Herness roadmap implementation inventory

This is a **normalized implementation inventory from the prior 300-item roadmap**, preserving its twenty subject groups and fifteen-item ranges. Labels are reconstructed from the available roadmap summary, so wording and a few split items may differ from the original list; the IDs are approximate correspondences, not an exact verbatim reproduction.

**Not all 300 requested features have been delivered.** Working features, limited implementations, configured-service requirements and unfinished work are distinguished below. A capability catalog or settings form does not by itself count as implementing a feature. No deployment or publication is implied.

| Status | Meaning | Count |
| --- | --- | ---: |
| Implemented | Implemented code exercised by local automated tests or the recorded browser smoke test. Provider tests use local mock servers, not live-account certification. | 120 |
| Partial | A useful subset exists; the missing requested scope is stated. | 74 |
| Requires service | The adapter exists, but a configured provider or optional dependency is needed. | 2 |
| Planned | The requested capability is not implemented. | 104 |

Group evidence links implementation and tests, not planned capabilities. Tests do not establish compatibility with every provider or browser, precise billing, production uptime, or comprehensive security. Configured model, search, MCP and GitHub services still need appropriate access. Commands run on the local host; Docker detection is not container isolation.

Local verification: `python -m unittest discover -s tests -v`, `python -m compileall -q herness`, and optional `node tests/browser_smoke.cjs` with Playwright and Chromium installed. The tests use local fixtures rather than paid provider calls.

## 001–015: Chat experience

Evidence: `herness/index.html`; `herness/library.py`; `herness/web.py`; `tests/browser_smoke.cjs`; `tests/test_application.py`; `tests/test_features.py`.

| ID | Status | Feature and current scope |
| --- | --- | --- |
| 001 | Partial | Markdown rendering: safe headings, lists, quotes and tables; full CommonMark, math and Mermaid are absent |
| 002 | Implemented | Syntax-highlighted code blocks |
| 003 | Implemented | Copy code blocks and complete messages |
| 004 | Implemented | Edit a user message into a new branch |
| 005 | Implemented | Regenerate the latest response while preserving its original conversation |
| 006 | Implemented | Branch from a selected conversation message |
| 007 | Implemented | Quote selected response text into the composer |
| 008 | Implemented | Persist drafts per project and conversation |
| 009 | Planned | Queue additional messages while an agent is running |
| 010 | Planned | Steer an active run without cancelling and restarting it |
| 011 | Implemented | Bookmark individual messages |
| 012 | Implemented | Find text within the current conversation |
| 013 | Planned | Unread-response markers and reliable read-position tracking |
| 014 | Partial | Retry a failed turn through a new branch; selective failed-step retry is absent |
| 015 | Implemented | Export conversations as Markdown or JSON |

## 016–030: Sidebar and conversation library

Evidence: `herness/index.html`; `herness/library.py`; `herness/application.py`; `tests/browser_smoke.cjs`; `tests/test_application.py`; `tests/test_herness.py`.

| ID | Status | Feature and current scope |
| --- | --- | --- |
| 016 | Implemented | Rename conversations |
| 017 | Implemented | Trash conversations and restore them |
| 018 | Implemented | Pin conversations |
| 019 | Implemented | Organize conversations into folders |
| 020 | Implemented | Tag conversations |
| 021 | Implemented | Search full user and assistant conversation text |
| 022 | Partial | Filter by search text and trash state; a dedicated multi-filter library is incomplete |
| 023 | Implemented | Bulk trash and restore conversations |
| 024 | Implemented | Resize and collapse the sidebar |
| 025 | Planned | Reorder conversations and folders through drag and drop |
| 026 | Implemented | Searchable command palette and keyboard shortcuts |
| 027 | Planned | Sidebar unread badges for background activity |
| 028 | Partial | Conversation tabs: eight local tabs share a single server active conversation |
| 029 | Implemented | Dark, light and system themes with the requested dark gray and blue styling |
| 030 | Implemented | Right-to-left layout support |

## 031–045: Projects and context

Evidence: `herness/library.py`; `herness/runtime.py`; `herness/workbench.py`; `herness/application.py`; `tests/test_runtime.py`; `tests/test_application.py`; `tests/browser_smoke.cjs`.

| ID | Status | Feature and current scope |
| --- | --- | --- |
| 031 | Implemented | Register and switch local projects with separate state |
| 032 | Implemented | Persist project instructions and include them in model context |
| 033 | Implemented | Load repository AGENTS.md instructions |
| 034 | Partial | Project-local provider settings exist; per-project model profiles require manual selection |
| 035 | Implemented | Persist project goals and include them in model context |
| 036 | Implemented | Pin bounded reference files into project context with path protections |
| 037 | Planned | Treat multiple repositories as one coordinated project |
| 038 | Implemented | Browse the project file tree and preview text files |
| 039 | Partial | Reference files from chat and pin context files; automatic relevance selection is absent |
| 040 | Implemented | Search project file contents with source locations |
| 041 | Planned | Watch project files and update context automatically |
| 042 | Implemented | Configure ignored paths for indexing and pinned context |
| 043 | Planned | Create projects from reusable project templates |
| 044 | Partial | Inspect Git diffs and checkpoints; a unified recent-changes activity view is absent |
| 045 | Partial | Project backup includes selected local data; full portable environment export is absent |

## 046–060: Models and provider management

Evidence: `herness/core.py`; `herness/providers.py`; `herness/models.py`; `herness/web.py`; `tests/test_runtime.py`; `tests/test_features.py`; `tests/test_security_api.py`.

| ID | Status | Feature and current scope |
| --- | --- | --- |
| 046 | Implemented | Native Anthropic Messages transport with tool, image and streaming conversion |
| 047 | Implemented | Native Gemini transport with tool, image and streaming conversion |
| 048 | Implemented | OpenAI Responses transport with tool, image and streaming conversion |
| 049 | Implemented | Discover models from the active provider endpoint |
| 050 | Partial | Expose protocol capabilities and test connection; automated per-model capability certification is absent |
| 051 | Planned | Store provider credentials in an operating-system keychain |
| 052 | Implemented | Save and select credential-free model profiles with context, output and price defaults |
| 053 | Planned | Persist a separate model choice for each conversation |
| 054 | Planned | Compare multiple model answers side by side |
| 055 | Planned | Vote on model outputs and retain preference statistics |
| 056 | Planned | Automatically route tasks by complexity or capability |
| 057 | Planned | Use separate planner and executor models in a coordinated run |
| 058 | Planned | Automatic provider fallback |
| 059 | Partial | Record request duration; model latency comparison dashboards are absent |
| 060 | Planned | Adaptive timeout and latency policies per model |

## 061–075: Plans and task management

Evidence: `herness/automation.py`; `herness/core.py`; `herness/tools.py`; `tests/test_automation.py`; `tests/test_runtime.py`; `tests/test_workbench.py`.

| ID | Status | Feature and current scope |
| --- | --- | --- |
| 061 | Implemented | Explicit Plan mode with enforced read-only tools |
| 062 | Planned | Execute a saved plan as a resumable task graph |
| 063 | Implemented | Persist editable task checklists |
| 064 | Implemented | Store task acceptance criteria |
| 065 | Planned | Independently verify acceptance criteria before declaring success |
| 066 | Partial | Agents can ask clarifying questions in chat; structured clarification gates are absent |
| 067 | Partial | Stop an active turn; resumable pause at an arbitrary plan step is absent |
| 068 | Implemented | Persist task and step statuses |
| 069 | Implemented | Edit and reorder plan steps |
| 070 | Partial | Validate dependencies and reject cycles; automatic dependency-driven execution is absent |
| 071 | Implemented | Constrain available tools through permission scopes |
| 072 | Implemented | Preview file and memory diffs before approving changes |
| 073 | Partial | Read-only planning supports inspection; full side-effect simulation is absent |
| 074 | Partial | Completed steps require user-supplied evidence text; the evidence is not independently verified |
| 075 | Implemented | Record blockers for task steps |

## 076–090: MCP, skills and connectors

Evidence: `herness/connections.py`; `herness/agent_tools.py`; `herness/automation.py`; `tests/test_connections.py`; `tests/test_agent_tools.py`; `tests/test_automation.py`.

| ID | Status | Feature and current scope |
| --- | --- | --- |
| 076 | Implemented | Use HTTP MCP servers with initialization, discovery and tool calls |
| 077 | Implemented | Save and enable or disable named MCP server configurations |
| 078 | Planned | Connector OAuth sign-in and token refresh |
| 079 | Partial | Local read and execute scopes gate MCP access; per-remote-tool permission policies are absent |
| 080 | Implemented | Require approval of a remote tool destination and arguments before invocation |
| 081 | Implemented | Create and run reusable skills with typed prompt inputs |
| 082 | Implemented | Version skills and retain prior prompts and schemas |
| 083 | Partial | Validate and preview skill inputs; behavioral skill evaluation suites are absent |
| 084 | Planned | Install lifecycle hooks around tools, runs and conversations |
| 085 | Partial | Read GitHub issues and pull requests; writing and broader GitHub workflows are absent |
| 086 | Planned | Native Google Drive connector |
| 087 | Planned | Native Notion connector |
| 088 | Planned | Native calendar connector |
| 089 | Planned | Native email connector with draft and send workflows |
| 090 | Implemented | Import and export declarative skills with trust reset on import |

## 091–105: Research and source grounding

Evidence: `herness/connections.py`; `herness/agent_tools.py`; `tests/test_connections.py`; `tests/test_agent_tools.py`.

| ID | Status | Feature and current scope |
| --- | --- | --- |
| 091 | Implemented | Fetch readable text from public HTTPS pages through UI or agent tools |
| 092 | Requires service | Search through Brave or Tavily adapters requires configured provider credentials and network access |
| 093 | Partial | Return source citation IDs and locations; generated answer claims are not automatically validated against them |
| 094 | Implemented | Record retrieval time and available publication timestamps |
| 095 | Planned | Generate and execute a multi-stage research plan |
| 096 | Implemented | Fetch multiple selected URLs in parallel |
| 097 | Planned | Detect contradictory claims across sources |
| 098 | Planned | Rank primary sources over secondary reporting |
| 099 | Implemented | Save and organize local source collections |
| 100 | Implemented | Filter search results by domain |
| 101 | Partial | Pass date filters to the selected provider; independent publication-date verification is absent |
| 102 | Planned | Search X through a native social-search integration |
| 103 | Partial | Export Markdown source bundles; automated synthesized research reports are absent |
| 104 | Implemented | Refresh saved sources and show content changes |
| 105 | Planned | Flag unsupported or weakly supported claims automatically |

## 106–120: Coding workflows

Evidence: `herness/tools.py`; `herness/workbench.py`; `tests/test_workbench.py`; `tests/test_herness.py`.

| ID | Status | Feature and current scope |
| --- | --- | --- |
| 106 | Partial | Generate code symbol maps for supported languages; full language-server understanding is absent |
| 107 | Partial | Locate references with literal search; semantic cross-language references are absent |
| 108 | Implemented | Apply unambiguous targeted file patches after review |
| 109 | Implemented | Protect user edits by checking file contents again after approval |
| 110 | Partial | An agent can write proposed tests with normal file approval; a dedicated test-generation workflow is absent |
| 111 | Implemented | Run approved tests and report real output and exit status |
| 112 | Partial | Expose command failures and output; structured compiler and test diagnostics are incomplete |
| 113 | Partial | Use reviewed edits for refactors; semantic rename and dependency-aware refactors are absent |
| 114 | Implemented | Save pre-change checkpoints and perform reviewed conflict-aware undo |
| 115 | Implemented | Inspect Git status |
| 116 | Implemented | Inspect safe staged and unstaged Git diffs |
| 117 | Partial | Approved shell commands can create commits; a dedicated commit review workflow is absent |
| 118 | Planned | Create and manage pull requests from the application |
| 119 | Planned | Monitor CI and automatically propose repairs |
| 120 | Planned | Integrate with an IDE through a maintained extension |

## 121–135: Execution environments and browser use

Evidence: `herness/tools.py`; `herness/workbench.py`; `herness/documents.py`; `tests/test_workbench.py`; `tests/test_documents.py`.

| ID | Status | Feature and current scope |
| --- | --- | --- |
| 121 | Planned | Execute agent tools inside an enforced container sandbox |
| 122 | Planned | Configure workspace mounts for isolated execution |
| 123 | Planned | Enforce task CPU, memory and disk limits |
| 124 | Planned | Enforce network access policies for shell tools |
| 125 | Planned | Create disposable execution environments for each task |
| 126 | Planned | Snapshot and restore complete execution environments |
| 127 | Planned | Let the agent navigate an interactive browser |
| 128 | Planned | Let the agent capture browser screenshots |
| 129 | Planned | Inspect browser accessibility trees through agent tools |
| 130 | Planned | Review and confirm browser form submissions before execution |
| 131 | Planned | Record and replay browser interaction sequences |
| 132 | Planned | Generate and run browser tests from a product browser-testing workflow |
| 133 | Planned | Operate desktop applications through a computer-use agent |
| 134 | Planned | Run tasks on managed SSH execution targets |
| 135 | Partial | Preview sanitized static HTML artifacts; running application previews are not managed |

## 136–150: Memory

Evidence: `herness/privacy.py`; `herness/state.py`; `herness/runtime.py`; `herness/tools.py`; `tests/test_privacy.py`; `tests/test_runtime.py`; `tests/test_features.py`; `herness/vectors.py`; `herness/vector_store.py`; `herness/embeddings.py`; `tests/test_vectors.py`; `tests/test_vector_agent.py`; `tests/test_vector_api.py`; `tests/browser_vector.cjs`.

| ID | Status | Feature and current scope |
| --- | --- | --- |
| 136 | Partial | Project memory is persistent; separate global and session memory scopes are absent |
| 137 | Implemented | Record structured memory provenance and timestamps |
| 138 | Implemented | Expire structured memory entries |
| 139 | Partial | Detect conflicting entries with matching labels; semantic contradiction detection is absent |
| 140 | Implemented | Review, edit and delete saved memory |
| 141 | Implemented | Inspect memory included in model context and its source |
| 142 | Implemented | Retrieve indexed active project memory by semantic similarity through local sqlite-vec and a configured Ollama or OpenAI-compatible embedding provider; expired, excluded, conflicting and stale entries are filtered |
| 143 | Partial | Store decisions as labeled entries; automatic decision extraction is absent |
| 144 | Planned | Compress long conversation history into verified memory summaries |
| 145 | Partial | Use skills and saved entries as procedural memory; automatic procedure learning is absent |
| 146 | Partial | Store glossary entries manually; a dedicated glossary workflow is absent |
| 147 | Implemented | Keep memory version history and restore previous entries |
| 148 | Implemented | Exclude selected entries from future model context |
| 149 | Implemented | Merge imported memory with explicit conflict handling |
| 150 | Implemented | Export structured memory as JSON |

## 151–165: Documents and uploads

Evidence: `herness/state.py`; `herness/documents.py`; `herness/agent_tools.py`; `tests/test_documents.py`; `tests/test_features.py`; `tests/test_agent_tools.py`; `tests/browser_smoke.cjs`.

| ID | Status | Feature and current scope |
| --- | --- | --- |
| 151 | Implemented | Preview staged text and image attachments before sending |
| 152 | Planned | OCR scanned PDFs and images |
| 153 | Implemented | Extract DOCX paragraph text |
| 154 | Implemented | Extract XLSX sheet and cell text |
| 155 | Implemented | Extract PPTX slide text |
| 156 | Implemented | Extract selectable PDF text by page |
| 157 | Implemented | Return document page or section source labels with extracted text |
| 158 | Planned | Compare documents semantically and explain changes |
| 159 | Partial | Agents can retrieve cited document passages with vector or literal search; a dedicated collection-wide question-answering workflow is absent |
| 160 | Planned | Extract and complete structured document forms |
| 161 | Implemented | Deduplicate uploads by content hash |
| 162 | Implemented | Search uploaded documents literally or through the Knowledge vector index, with citation labels and the agent semantic_search tool |
| 163 | Implemented | Keep a persistent document library independent of conversations |
| 164 | Implemented | Explain unsupported formats, invalid files and size limits |
| 165 | Partial | Remove staged files and library originals; existing transcript attachments are retained |

## 166–180: Artifacts and deliverables

Evidence: `herness/documents.py`; `herness/agent_tools.py`; `herness/index.html`; `tests/test_documents.py`; `tests/test_agent_tools.py`; `tests/browser_smoke.cjs`.

| ID | Status | Feature and current scope |
| --- | --- | --- |
| 166 | Partial | Workspace forms expose artifacts; a dedicated resizable artifact side panel is absent |
| 167 | Implemented | Create, edit and delete artifacts with stale-edit detection |
| 168 | Implemented | Retain artifact versions and compare their text |
| 169 | Implemented | Preview sanitized static HTML without script or network access |
| 170 | Planned | Render and edit Mermaid diagrams interactively |
| 171 | Partial | Generate static SVG bar-chart artifacts; interactive and broader chart support is absent |
| 172 | Requires service | Export PDFs using the optional reportlab dependency |
| 173 | Implemented | Export real DOCX documents |
| 174 | Planned | Export presentation decks as PPTX |
| 175 | Implemented | Start artifacts from built-in document templates |
| 176 | Planned | Apply persistent document style and brand profiles |
| 177 | Planned | Rewrite a selected artifact passage with inline AI editing |
| 178 | Planned | Leave comments and review suggestions on artifacts |
| 179 | Planned | Publish artifacts as access-controlled shareable pages |
| 180 | Implemented | Export artifact content, metadata and preview as a ZIP |

## 181–195: Data analysis

Evidence: `herness/documents.py`; `tests/test_documents.py`.

| ID | Status | Feature and current scope |
| --- | --- | --- |
| 181 | Planned | Execute Python analysis in an isolated environment |
| 182 | Implemented | Load and preview bounded CSV tables |
| 183 | Partial | Extract spreadsheet values from XLSX; spreadsheet editing and formulas are absent |
| 184 | Implemented | Profile columns, missing values, duplicates and descriptive statistics |
| 185 | Partial | Generate bounded static bar charts; general interactive visualization is absent |
| 186 | Planned | Show and rerun generated analysis code in a dedicated analysis workspace |
| 187 | Implemented | Export CSV data and filtered results |
| 188 | Planned | Save reproducible notebooks and their environments |
| 189 | Implemented | Query local SQLite databases in enforced read-only mode |
| 190 | Implemented | Inspect database schema |
| 191 | Implemented | Run bounded SELECT and read-only WITH queries |
| 192 | Partial | Compute descriptive statistics and SQL expressions; a general verified calculation tool is absent |
| 193 | Implemented | Compare datasets with duplicate-aware added and removed rows |
| 194 | Partial | Validate CSV shape and numeric chart input; user-defined dataset validation suites are absent |
| 195 | Planned | Schedule reproducible data-analysis pipelines with result delivery |

## 196–210: Voice and media

Evidence: `herness/index.html`; `herness/state.py`; `herness/providers.py`; `tests/browser_smoke.cjs`; `tests/test_features.py`; `tests/test_runtime.py`.

| ID | Status | Feature and current scope |
| --- | --- | --- |
| 196 | Partial | Browser speech dictation requires compatible browser speech services |
| 197 | Partial | Read responses aloud through available browser speech synthesis |
| 198 | Planned | Realtime duplex voice conversations |
| 199 | Planned | Interrupt spoken output and steer a realtime voice session |
| 200 | Planned | Transcribe uploaded audio files |
| 201 | Planned | Identify speakers in audio recordings |
| 202 | Planned | Extract meeting actions from a meeting-recording workflow |
| 203 | Planned | Translate audio or speech through a dedicated media workflow |
| 204 | Partial | Attach screenshots as images; capturing the screen within the app is absent |
| 205 | Planned | Annotate images and screenshots in the application |
| 206 | Partial | Send multiple images to a capable model; dedicated image comparison tools are absent |
| 207 | Planned | Upload and analyze video timelines |
| 208 | Planned | Search spoken or visual content within videos |
| 209 | Planned | Generate images through an integrated provider workflow |
| 210 | Planned | Edit images through an integrated provider workflow |

## 211–225: Scheduling and automation

Evidence: `herness/automation.py`; `herness/application.py`; `herness/web.py`; `tests/test_automation.py`; `tests/test_application.py`.

| ID | Status | Feature and current scope |
| --- | --- | --- |
| 211 | Planned | Create schedules from natural-language recurrence descriptions |
| 212 | Implemented | Run daily and weekly schedules with IANA timezones and daylight-saving handling |
| 213 | Partial | Support interval, once, daily and weekly schedules; calendar exceptions and holiday calendars are absent |
| 214 | Implemented | Preview upcoming scheduled run times |
| 215 | Implemented | Pause and resume schedules |
| 216 | Partial | Limit run counts and model rounds; schedule-specific shared dollar budgets are absent |
| 217 | Implemented | Require renewed approval when schedule execution scope changes |
| 218 | Partial | A scheduled prompt can prepare a briefing in a local conversation; external delivery is absent |
| 219 | Planned | Trigger tasks when watched files change |
| 220 | Planned | Trigger tasks from authenticated incoming webhooks |
| 221 | Planned | Trigger tasks from repository issues and pull-request events |
| 222 | Implemented | Persist schedule history and actual completion outcomes |
| 223 | Partial | Record failures for local inspection; push or channel failure notifications are absent |
| 224 | Implemented | Prevent overlapping scheduled runs and defer while the workspace is busy |
| 225 | Implemented | Choose missed-run skipping or one latest catch-up without replaying uncertain work |

## 226–240: Agent teams

Evidence: `herness/automation.py`; `herness/core.py`; `tests/test_automation.py`.

| ID | Status | Feature and current scope |
| --- | --- | --- |
| 226 | Implemented | Run specialists with separate role instructions |
| 227 | Implemented | Enforce read-only specialist tools |
| 228 | Implemented | Isolate specialist conversation histories and provider request configuration |
| 229 | Planned | Choose a different model or provider for each specialist |
| 230 | Partial | Show persisted specialist status and outputs; a dedicated live team dashboard is absent |
| 231 | Partial | Limit specialist model rounds and output tokens; shared dollar budgets are absent |
| 232 | Implemented | Bound specialist concurrency |
| 233 | Implemented | Cancel an active team and propagate cancellation |
| 234 | Planned | Hand off intermediate work through a structured specialist pipeline |
| 235 | Partial | Specialists can produce independent review text; automatic critique and revision loops are absent |
| 236 | Planned | Use an independent verifier to accept or reject another agent's work |
| 237 | Planned | Resolve conflicting specialist edits or conclusions automatically |
| 238 | Planned | Save and execute multi-stage agent workflows |
| 239 | Partial | Review each specialist output; an integration and acceptance workflow is absent |
| 240 | Implemented | Save and reuse team templates |

## 241–255: Performance and runtime reliability

Evidence: `herness/core.py`; `herness/runtime.py`; `herness/automation.py`; `herness/connections.py`; `tests/test_runtime.py`; `tests/test_features.py`; `tests/test_automation.py`.

| ID | Status | Feature and current scope |
| --- | --- | --- |
| 241 | Implemented | Inspect included context sources and per-message context estimates |
| 242 | Partial | Estimate token use with provider usage where available; precise model tokenization is absent |
| 243 | Partial | Estimate cost from configured token prices; provider invoice reconciliation is absent |
| 244 | Partial | Preflight task cost budgets are estimates, not guaranteed provider billing caps |
| 245 | Partial | Enforce estimated context-window limits; exact tokenizer accounting is absent |
| 246 | Implemented | Retry transient provider errors with bounded backoff |
| 247 | Implemented | Persist pending tool batches and runtime execution checkpoints |
| 248 | Partial | Mark interrupted runs after restart; automatic safe task resumption is absent |
| 249 | Partial | Avoid replaying uncertain side effects; distributed external idempotency guarantees are absent |
| 250 | Implemented | Detect repeated identical tool-call loops |
| 251 | Partial | Parallel URL fetch and specialist execution exist; arbitrary tool-call parallelization is absent |
| 252 | Planned | Cache model responses and tool results with validated invalidation |
| 253 | Implemented | Invalidate file-change approval when underlying content changes |
| 254 | Implemented | Show live runtime events and command output |
| 255 | Planned | Retry a selected failed step without repeating completed work |

## 256–270: Permissions, privacy and safety

Evidence: `herness/privacy.py`; `herness/tools.py`; `herness/web.py`; `herness/runtime.py`; `herness/connections.py`; `tests/test_privacy.py`; `tests/test_workbench.py`; `tests/test_security_api.py`; `tests/test_connections.py`.

| ID | Status | Feature and current scope |
| --- | --- | --- |
| 256 | Implemented | Enforce read, write, execute and memory tool scopes |
| 257 | Implemented | Save and select standard or read-only permission profiles |
| 258 | Implemented | Enforce read-only behavior independently of model instructions |
| 259 | Partial | Review each sensitive action; configurable session-wide approval grants are absent |
| 260 | Partial | Block known credential paths; comprehensive secret detection is absent |
| 261 | Partial | Redact common credential fields and token patterns from diagnostic export; coverage is not comprehensive |
| 262 | Partial | Document provider transmission and local retention; a complete per-action data disclosure ledger is absent |
| 263 | Planned | Temporary conversations with guaranteed no local persistence |
| 264 | Implemented | Preview and confirm retention cleanup of selected local data categories |
| 265 | Partial | Record metadata-only privacy audit actions; comprehensive organization audit coverage is absent |
| 266 | Implemented | Disable configured MCP access and remove destination-bound credentials |
| 267 | Partial | Track memory and source provenance; end-to-end data lineage is absent |
| 268 | Planned | Encrypt local state with managed keys |
| 269 | Planned | Multi-user authentication and authorization |
| 270 | Planned | Opt-in telemetry collection and administration controls |

## 271–285: Developer tooling and evaluations

Evidence: `herness/runtime.py`; `herness/core.py`; `herness/automation.py`; `herness/web.py`; `tests/test_runtime.py`; `tests/test_automation.py`; `tests/test_security_api.py`.

| ID | Status | Feature and current scope |
| --- | --- | --- |
| 271 | Implemented | Inspect durable metadata-only run traces |
| 272 | Implemented | Inspect model and tool event timelines |
| 273 | Implemented | Inspect local runtime diagnostics |
| 274 | Planned | Replay tasks against frozen environment snapshots |
| 275 | Planned | Create and run user-defined agent evaluation datasets |
| 276 | Planned | Benchmark and compare models on the same evaluation tasks |
| 277 | Partial | Repository regression tests exist; user-managed agent regression suites are absent |
| 278 | Partial | Validate tool schemas and requests in code; a plugin schema conformance workbench is absent |
| 279 | Partial | Tests simulate failures with mocks; a user-facing failure-injection environment is absent |
| 280 | Planned | Run independent acceptance tests as an agent evaluation gate |
| 281 | Partial | Record model and tool durations; dedicated latency profiling and comparative reports are absent |
| 282 | Partial | Version reusable skill prompts; system-prompt experiment tracking is absent |
| 283 | Planned | Publish a stable plugin SDK and packaging lifecycle |
| 284 | Partial | Expose authenticated local HTTP APIs and a terminal entry point; a stable public automation SDK is absent |
| 285 | Planned | Export OpenTelemetry traces and metrics |

## 286–300: Distribution and collaboration

Evidence: `herness/privacy.py`; `herness/index.html`; `herness/cli.py`; `tests/test_privacy.py`; `tests/browser_smoke.cjs`; `tests/test_herness.py`.

| ID | Status | Feature and current scope |
| --- | --- | --- |
| 286 | Planned | Sync workspaces and conversations across devices |
| 287 | Planned | Install an offline-capable progressive web app |
| 288 | Planned | Distribute signed desktop and mobile installers; native SwiftUI iPhone sources and an Xcode build workflow now exist, but no signed installer has been produced |
| 289 | Planned | Deliver background task push notifications |
| 290 | Planned | Use the harness through Telegram |
| 291 | Planned | Use the harness through Discord |
| 292 | Planned | Use the harness through Slack |
| 293 | Planned | Continue one conversation across web and messaging channels |
| 294 | Planned | Collaborate live on shared conversations and artifacts |
| 295 | Planned | Assign organization roles and shared workspace permissions |
| 296 | Planned | Comment and review collaboratively |
| 297 | Planned | Create authenticated share links for conversations and projects |
| 298 | Implemented | Export selected local state backups and restore reviewed conflicts with integrity checks |
| 299 | Partial | Keyboard navigation, appearance controls, RTL and responsive layouts exist; formal accessibility certification is absent |
| 300 | Partial | Demo mode, connection tests and environment diagnostics support setup; a guided onboarding tour is absent |
