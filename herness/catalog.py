"""Discoverable forms for the local workspace API.

The catalog describes existing operations. It never executes them, embeds
credentials, or marks integrations available before a capability check.
"""


def field(name, label, kind='text', *, value=None, required=False, options=None, placeholder=None, accept=None):
    result = {'name': name, 'label': label, 'type': kind, 'required': required}
    if value is not None:
        result['value'] = value
    if options is not None:
        result['options'] = options
    if placeholder is not None:
        result['placeholder'] = placeholder
    if accept is not None:
        result['accept'] = accept
    return result


def action(label, path, fields=(), *, method='POST', values=None, description='', confirmation=None):
    result = {'label': label, 'method': method, 'path': path, 'fields': list(fields), 'description': description}
    if values:
        result['values'] = values
    if confirmation:
        result['confirmation'] = confirmation
    return result


def module(identity, title, description, actions):
    return {'id': identity, 'title': title, 'description': description, 'actions': actions}


def identifier(label='Item ID'):
    return field('id', label, required=True)


def payload(value=None, label='Additional fields (JSON object)'):
    return field('payload', label, 'json', value={} if value is None else value)


def catalog():
    modules = []
    modules.append(module('workbench', 'Files & code',
                          'Inspect project files, search symbols, review Git changes, and undo a recorded file change.', [
        action('Project file list', '/api/workbench/files', method='GET'),
        action('Read a file', '/api/files/read', [field('path', 'Relative file path', required=True)]),
        action('Search file contents', '/api/workbench/search', [field('query', 'Search text', required=True), field('path', 'Folder', value='.'), field('case_sensitive', 'Match case', 'checkbox')]),
        action('Find code symbols', '/api/workbench/symbols', [field('path', 'Folder', value='.')], description='Python uses its syntax tree; other supported languages use declaration scanning.'),
        action('Git status or diff', '/api/workbench/git', [field('kind', 'View', 'select', options=['status', 'diff'])]),
        action('Saved file checkpoints', '/api/workbench/checkpoints', method='GET'),
        action('Preview an undo', '/api/workbench/undo-preview', [identifier('Checkpoint ID')]),
        action('Apply reviewed undo', '/api/workbench/undo', [identifier('Checkpoint ID'), field('expected_hash', 'Expected hash from the undo preview', required=True)], confirmation='I reviewed this undo diff and want to apply it.'),
        action('Execution capabilities', '/api/workbench/sandbox', method='GET', description='Reports local execution and Docker availability. Container isolation is not configured.'),
    ]))

    modules.append(module('research', 'Research & saved sources',
                          'Fetch public web pages, search with a configured provider, and keep sources with citations.', [
        action('Connection capabilities', '/api/connections/status', method='GET'),
        action('Read and save a web page', '/api/connections/fetch', [field('url', 'Public HTTPS URL', 'url', required=True), field('collection', 'Save to collection (optional)')], description='Reads page text; sites requiring JavaScript or login may not be readable.'),
        action('Read several web pages', '/api/connections/fetch-many', [field('urls', 'URLs (JSON array, up to 8)', 'json', value=['https://example.com'], required=True), field('collection', 'Save to collection (optional)')]),
        action('Configure web search', '/api/connections/search/config', [field('provider', 'Search provider', 'select', options=['brave', 'tavily']), field('api_key', 'API key (kept in memory)', 'password'), field('clear_key', 'Clear the saved in-memory key', 'checkbox')]),
        action('Search the web', '/api/connections/search', [field('query', 'Search query', required=True), field('domains', 'Limit to domains (JSON array)', 'json', value=[]), field('from_date', 'Published from (YYYY-MM-DD)'), field('to_date', 'Published through (YYYY-MM-DD)')]),
        action('Saved sources', '/api/connections/sources', method='GET'),
        action('Filter a source collection', '/api/connections/sources', [field('collection', 'Collection name')], values={'action': 'list'}),
        action('Save a fetched source', '/api/connections/sources', [field('source', 'Source object from a page fetch', 'json', value={}, required=True), field('collection', 'Collection', value='Research')], values={'action': 'save'}),
        action('Refresh a saved source', '/api/connections/sources', [identifier('Source ID')], values={'action': 'refresh'}),
        action('Export research sources', '/api/connections/sources', [field('collection', 'Collection (blank for all)')], values={'action': 'export'}),
        action('Delete a saved source', '/api/connections/sources', [identifier('Source ID')], values={'action': 'delete'}),
        action('Read GitHub issues or pull requests', '/api/connections/github', [field('repository', 'Repository (owner/name)', required=True), field('resource', 'Resource', 'select', options=['issues', 'pulls'])], description='Public reads work without a token; private access uses HERNESS_GITHUB_TOKEN.'),
    ]))

    modules.append(module('connections', 'MCP connections',
                          'Connect to an HTTP MCP server, discover its tools, and review a tool call before execution.', [
        action('Configured MCP servers', '/api/connections/mcp', method='GET'),
        action('Configure an MCP server', '/api/connections/mcp/config', [field('name', 'Server name', required=True), field('url', 'Server HTTPS URL', 'url', required=True), field('api_key', 'Bearer token (kept in memory)', 'password'), field('enabled', 'Enabled', 'checkbox', value=True), field('clear_key', 'Clear the in-memory token', 'checkbox')], description='Supports HTTP JSON and finite SSE responses. OAuth and local stdio are not available.'),
        action('Remove an MCP server', '/api/connections/mcp/config', [field('name', 'Server name', required=True)], values={'remove': True}),
        action('Discover server tools', '/api/connections/mcp/list', [field('name', 'Server name', required=True)]),
        action('Preview an MCP tool call', '/api/connections/mcp/call', [field('name', 'Server name', required=True), field('tool', 'Tool name', required=True), field('arguments', 'Tool arguments', 'json', value={})]),
        action('Execute a reviewed MCP tool call', '/api/connections/mcp/call', [field('name', 'Server name', required=True), field('tool', 'Tool name', required=True), field('arguments', 'Reviewed tool arguments', 'json', value={})], confirmation='I reviewed this server, tool, and its arguments and approve this call.'),
    ]))

    modules.append(module('documents', 'Document library',
                          'Keep local documents, extract their text, and find matching passages with page, paragraph, sheet, or slide citations.', [
        action('Library, artifacts & capabilities', '/api/documents', method='GET'),
        action('Upload a document', '/api/documents/upload', [field('data', 'Document (up to 4 MB)', 'file', required=True, accept='.txt,.md,.csv,.json,.py,.js,.html,.pdf,.docx,.xlsx,.pptx,.png,.jpg,.jpeg,.webp')], description='Text PDF, DOCX, XLSX and PPTX extraction is available. Scanned PDFs need OCR; image understanding needs a vision model.'),
        action('Read an indexed document', '/api/documents/library', [identifier('Document ID')], values={'action': 'get'}),
        action('Search document passages', '/api/documents/library', [field('query', 'Search text', required=True)], values={'action': 'search'}),
        action('Download an original document', '/api/documents/library', [identifier('Document ID')], values={'action': 'download'}),
        action('Remove a library document', '/api/documents/library', [identifier('Document ID')], values={'action': 'delete'}, description='Removes the library copy. Existing conversation attachments remain in their conversations.'),
    ]))

    modules.append(module('artifacts', 'Artifacts & exports',
                          'Write a versioned artifact, compare revisions, preview static content, and download a document or bundle.', [
        action('Artifact list and templates', '/api/documents', method='GET'),
        action('Create an artifact', '/api/documents/artifact', [field('title', 'Title', required=True), field('format', 'Format', 'select', options=['markdown', 'text', 'html', 'svg', 'json']), field('template', 'Template (used when content is blank)', 'select', options=[{'value': '', 'label': 'No template'}, 'project-brief', 'meeting-notes', 'decision-record']), field('content', 'Content', 'textarea')], values={'action': 'create'}),
        action('Read an artifact and its versions', '/api/documents/artifact', [identifier('Artifact ID')], values={'action': 'get'}),
        action('Save an artifact revision', '/api/documents/artifact', [identifier('Artifact ID'), field('revision', 'Current revision number', 'number', required=True), field('title', 'Title (optional)'), field('content', 'Updated content', 'textarea', required=True)], values={'action': 'update'}, description='The current revision prevents accidentally overwriting a newer edit.'),
        action('Compare artifact revisions', '/api/documents/artifact', [identifier('Artifact ID'), field('from_version', 'Earlier revision (optional)', 'number'), field('to_version', 'Later revision (optional)', 'number')], values={'action': 'diff'}),
        action('Preview an artifact', '/api/documents/artifact', [identifier('Artifact ID')], values={'action': 'preview'}, description='Static preview with scripts, forms, and embedded web content disabled.'),
        action('Export an artifact', '/api/documents/artifact', [identifier('Artifact ID'), field('export_format', 'Download format', 'select', options=['markdown', 'text', 'html', 'docx', 'pdf', 'zip', 'json', 'svg'])], values={'action': 'export'}, description='PDF requires reportlab; the capability list reports whether it is installed. JSON and ZIP include version history.'),
        action('Delete an artifact', '/api/documents/artifact', [identifier('Artifact ID'), field('revision', 'Current revision number', 'number', required=True)], values={'action': 'delete'}),
    ]))

    csv_content = lambda label='CSV content': field('content', label, 'textarea', required=True, placeholder='name,value\nExample,10')
    modules.append(module('data', 'Data & read-only SQL',
                          'Profile CSV files, filter and compare datasets, save a chart, and inspect a local SQLite database.', [
        action('Profile a CSV dataset', '/api/documents/csv', [csv_content(), field('limit', 'Preview row limit', 'number', value=100)], values={'action': 'profile'}, description='Up to 10,000 rows and 100 columns. Reports missing values, duplicates, and numeric statistics.'),
        action('Filter a CSV dataset', '/api/documents/csv', [csv_content(), field('column', 'Column', required=True), field('operator', 'Filter', 'select', options=['contains', 'equals', 'not_equals', 'gt', 'lt']), field('value', 'Value', required=True), field('limit', 'Preview row limit', 'number', value=100)], values={'action': 'filter'}),
        action('Compare two CSV datasets', '/api/documents/csv', [csv_content('Original CSV'), field('other', 'Updated CSV', 'textarea', required=True)], values={'action': 'compare'}, description='Columns must match in the same order. Added and removed rows include duplicate counts.'),
        action('Download a CSV dataset', '/api/documents/csv', [csv_content()], values={'action': 'export'}),
        action('Create a bar chart artifact', '/api/documents/csv', [csv_content(), field('x', 'Label column', required=True), field('y', 'Numeric value column', required=True)], values={'action': 'chart'}, description='Creates a static SVG artifact from up to 40 rows with nonnegative numeric values.'),
        action('Inspect SQLite schema', '/api/documents/sql', [field('path', 'Database path within the project', required=True)], values={'action': 'schema'}),
        action('Query SQLite', '/api/documents/sql', [field('path', 'Database path within the project', required=True), field('sql', 'Read-only SELECT or WITH query', 'textarea', required=True), field('limit', 'Maximum rows', 'number', value=100)], values={'action': 'query'}, description='Read-only access with a 2-second query deadline and at most 500 returned rows. Extension loading and writes are disabled.'),
    ]))

    schedule = {'kind': 'daily', 'time': '09:00', 'timezone': 'UTC'}
    schedule_fields = [field('title', 'Title', required=True), field('prompt', 'Task prompt', 'textarea', required=True), field('schedule', 'Schedule (JSON)', 'json', value=schedule, required=True), field('mode', 'Execution mode', 'select', options=['plan', 'act']), field('enabled', 'Enable automatic runs', 'checkbox'), field('max_steps', 'Tool round limit per run', 'number', value=8), field('max_runs', 'Maximum total runs', 'number', value=100), field('catch_up', 'After downtime', 'select', options=[{'value': 'skip', 'label': 'Skip missed runs'}, {'value': 'latest', 'label': 'Run latest occurrence'}]), field('grace_seconds', 'Missed-run grace (seconds)', 'number', value=300)]
    modules.append(module('automation', 'Schedules & run history',
                          'Run saved tasks while this server is running. Use interval, daily, weekly, or one-time schedules with a timezone.', [
        action('Automation capabilities', '/api/automation/status', method='GET'),
        action('Saved schedules', '/api/automation/schedules', method='GET'),
        action('Schedule and team run history', '/api/automation/runs', method='GET'),
        action('Preview upcoming schedule times', '/api/automation/preview', [field('schedule', 'Schedule (JSON)', 'json', value=schedule), field('count', 'Occurrences', 'number', value=5), field('after', 'Start after (optional ISO time with timezone)')], description='Examples: {"kind":"interval","seconds":3600}, {"kind":"weekly","time":"09:00","weekdays":[0,2],"timezone":"Europe/London"}, or {"kind":"once","at":"2027-01-01T09:00:00Z"}.'),
        action('Create a schedule', '/api/automation/schedules', schedule_fields, values={'action': 'create'}, confirmation='I approve this prompt, schedule, and run limits.'),
        action('Update a schedule', '/api/automation/schedules', [identifier('Schedule ID'), payload({'title': 'Updated schedule'}, 'Changed schedule fields (JSON)')], values={'action': 'update'}, confirmation='I approve the updated schedule configuration.'),
        action('Pause a schedule', '/api/automation/schedules', [identifier('Schedule ID')], values={'action': 'pause'}),
        action('Resume a schedule', '/api/automation/schedules', [identifier('Schedule ID')], values={'action': 'resume'}, confirmation='I reviewed this saved schedule and approve resuming it.'),
        action('Run a schedule now', '/api/automation/run', [identifier('Schedule ID')], confirmation='I reviewed this task and approve running it now.'),
        action('Delete a schedule', '/api/automation/schedules', [identifier('Schedule ID')], values={'action': 'delete'}),
    ]))

    task_steps = [{'id': 'inspect', 'title': 'Inspect the current behavior', 'status': 'pending', 'depends_on': [], 'evidence': '', 'blocker': ''}, {'id': 'verify', 'title': 'Verify the result', 'status': 'pending', 'depends_on': ['inspect'], 'evidence': '', 'blocker': ''}]
    modules.append(module('tasks', 'Task plans & evidence',
                          'Track steps, dependencies, blockers, and acceptance criteria. Completed steps require evidence.', [
        action('Task plans', '/api/automation/tasks', method='GET'),
        action('Create a task plan', '/api/automation/tasks', [field('title', 'Title', required=True), field('description', 'Description', 'textarea'), field('acceptance', 'Acceptance criteria (JSON array)', 'json', value=[]), field('steps', 'Steps and dependencies (JSON array)', 'json', value=task_steps)], values={'action': 'create'}),
        action('Update a task plan', '/api/automation/tasks', [identifier('Task ID'), payload({'status': 'active'}, 'Changed fields, steps, or evidence (JSON)')], values={'action': 'update'}, description='Statuses: pending, active, completed, blocked, skipped. Include each completed step’s evidence in the steps array.'),
        action('Delete a task plan', '/api/automation/tasks', [identifier('Task ID')], values={'action': 'delete'}),
    ]))

    skill_schema = {'type': 'object', 'properties': {'topic': {'type': 'string', 'description': 'Topic to investigate'}}, 'required': ['topic']}
    modules.append(module('skills', 'Reusable skills',
                          'Save prompt templates with validated inputs, test them, and explicitly trust skills before running them.', [
        action('Saved skills and revision history', '/api/automation/skills', method='GET'),
        action('Create a skill', '/api/automation/skills', [field('title', 'Title', required=True), field('description', 'Description', 'textarea'), field('prompt', 'Prompt template', 'textarea', value='Investigate {{topic}} and cite the evidence.', required=True), field('schema', 'Input schema', 'json', value=skill_schema)], values={'action': 'create'}),
        action('Edit a skill', '/api/automation/skills', [identifier('Skill ID'), payload({'description': 'Updated description'}, 'Changed skill fields (JSON)')], values={'action': 'update'}, description='Changing the prompt or schema removes previous trust until you review it again.'),
        action('Trust a reviewed skill', '/api/automation/skills', [identifier('Skill ID')], values={'action': 'update', 'trusted': True}, confirmation='I read this skill’s prompt and input schema and trust it to run.'),
        action('Revoke skill trust', '/api/automation/skills', [identifier('Skill ID')], values={'action': 'update', 'trusted': False}),
        action('Test skill inputs', '/api/automation/skills', [identifier('Skill ID'), field('inputs', 'Input values', 'json', value={'topic': 'your project'})], values={'action': 'test'}, description='Renders the prompt without starting an agent run.'),
        action('Run a trusted skill', '/api/automation/skills/run', [identifier('Skill ID'), field('inputs', 'Input values', 'json', value={})], confirmation='I reviewed this skill and its inputs and approve running it.'),
        action('Export a skill', '/api/automation/skills', [identifier('Skill ID')], values={'action': 'export'}),
        action('Import a skill', '/api/automation/skills', [field('skill', 'Exported skill JSON', 'json', value={}, required=True)], values={'action': 'import'}, description='Imported skills always start untrusted.'),
        action('Delete a skill', '/api/automation/skills', [identifier('Skill ID')], values={'action': 'delete'}),
    ]))

    modules.append(module('teams', 'Specialist teams',
                          'Run independent specialists with read-only project tools. Each specialist has a bounded tool budget.', [
        action('Saved specialist teams', '/api/automation/teams', method='GET'),
        action('Create a specialist team', '/api/automation/teams', [field('title', 'Team title', required=True), field('specialists', 'Specialists (JSON array)', 'json', value=[{'name': 'Reviewer', 'instructions': 'Inspect the project and report correctness risks with file references.'}], required=True), field('concurrency', 'Parallel specialists (1–4)', 'number', value=2), field('max_steps', 'Tool rounds per specialist (1–8)', 'number', value=5), field('max_output_tokens', 'Output token limit per request', 'number', value=2048)], values={'action': 'create'}),
        action('Edit a specialist team', '/api/automation/teams', [identifier('Team ID'), payload({'concurrency': 2}, 'Changed team fields (JSON)')], values={'action': 'update'}),
        action('Run a specialist team', '/api/automation/teams/run', [identifier('Team ID'), field('inputs', 'Shared task', 'textarea', required=True)], confirmation='I reviewed this team and task and approve its model calls.'),
        action('Team results and status', '/api/automation/runs', method='GET'),
        action('Stop a running team', '/api/automation/teams/stop', [field('run_id', 'Run ID', required=True)]),
        action('Delete a team', '/api/automation/teams', [identifier('Team ID')], values={'action': 'delete'}),
    ]))

    modules.append(module('runtime', 'Models, context & diagnostics',
                          'Inspect the active context, choose a provider protocol, and set run limits. Cost figures use your configured prices.', [
        action('Current model settings', '/api/settings', method='GET'),
        action('Configure a model', '/api/settings', [field('protocol', 'Provider protocol', 'select', options=['chat_completions', 'anthropic', 'gemini', 'responses']), field('base_url', 'API base URL', 'url', required=True), field('model', 'Model ID', required=True), field('api_key', 'API key (kept in memory)', 'password'), field('demo', 'Use offline demo', 'checkbox'), field('clear_key', 'Clear in-memory API key', 'checkbox')]),
        action('Test current model connection', '/api/settings/test', description='Makes a small provider request using the current settings.'),
        action('Current run limits', '/api/run/options', method='GET'),
        action('Set run limits', '/api/run/options', [field('mode', 'Mode', 'select', options=['act', 'plan']), field('max_steps', 'Maximum tool rounds', 'number'), field('max_context_tokens', 'Maximum estimated context tokens', 'number'), field('max_output_tokens', 'Maximum output tokens', 'number'), field('max_cost_usd', 'Estimated cost limit (USD)', 'number'), field('input_usd_per_million', 'Input price per million tokens (USD)', 'number'), field('output_usd_per_million', 'Output price per million tokens (USD)', 'number')], description='Blank fields retain their current value. Costs are estimates and require configured prices.'),
        action('Clear selected run limits', '/api/run/options', [payload({'max_cost_usd': None}, 'Limits to clear (JSON values must be null)')]),
        action('Context inspector', '/api/context', method='GET'),
        action('Run metadata and timeline summaries', '/api/runs', method='GET'),
        action('Redacted diagnostics', '/api/diagnostics', method='GET'),
        action('Stop the active conversation run', '/api/stop'),
    ]))

    modules.append(module('models', 'Model profiles & discovery',
                          'Save provider and model presets, including your own price estimates. API keys remain in memory.', [
        action('Saved model profiles', '/api/models/profiles', method='GET'),
        action('Save a model profile', '/api/models/profiles', [field('name', 'Profile name', required=True), field('id', 'Existing profile ID (optional)'), field('protocol', 'Provider protocol', 'select', options=[{'value': '', 'label': 'Use current protocol'}, 'chat_completions', 'anthropic', 'gemini', 'responses']), field('model', 'Model ID (blank for current)'), field('base_url', 'API base URL (blank for current)', 'url'), field('defaults', 'Token limits and prices (JSON)', 'json', value={})], values={'action': 'save'}, description='Optional defaults: max_output_tokens, max_context_tokens, input_usd_per_million, output_usd_per_million.'),
        action('Select a model profile', '/api/models/profiles', [identifier('Profile ID')], values={'action': 'select'}),
        action('Delete a model profile', '/api/models/profiles', [identifier('Profile ID')], values={'action': 'delete'}),
        action('Discover provider models', '/api/models/discover', [field('cursor', 'Next-page cursor (optional)')], description='Queries the currently configured provider using its active endpoint and credentials.'),
    ]))

    modules.append(module('projects', 'Projects & memory',
                          'Switch between local project folders and maintain project instructions and editable memory.', [
        action('Available projects', '/api/projects', method='GET'),
        action('Add a local project', '/api/projects', [field('path', 'Existing folder path', required=True), field('title', 'Project title (optional)')], values={'action': 'add'}),
        action('Switch projects', '/api/projects', [identifier('Project ID')], values={'action': 'switch'}),
        action('Project instructions and goals', '/api/project', method='GET'),
        action('Update project configuration', '/api/project', [payload({}, 'Project fields (JSON)')], description='Supported fields: title, goals, instructions, ignored_paths (array), pinned_files (array). Include only the fields you want to change.'),
        action('Read project memory', '/api/memory', method='GET'),
        action('Save project memory', '/api/memory', [field('revision', 'Revision from Read project memory', required=True), field('content', 'Memory content', 'textarea', required=True)]),
        action('Delete project memory', '/api/memory/delete', [field('revision', 'Revision from Read project memory', required=True)]),
    ]))

    modules.append(module('conversations', 'Conversation organization',
                          'Search, organize, export, and branch saved conversations. The chat sidebar provides these controls too.', [
        action('Search conversations', '/api/library', [field('q', 'Search text'), field('trash', 'Search trash', 'checkbox')], method='GET'),
        action('Update conversation metadata', '/api/library', [field('names', 'Conversation names (JSON array)', 'json', value=[], required=True), field('values', 'Metadata changes (JSON)', 'json', value={'folder': '', 'tags': [], 'pinned': False})], values={'action': 'update'}),
        action('Move conversations to trash', '/api/library', [field('names', 'Conversation names (JSON array)', 'json', value=[], required=True)], values={'action': 'trash'}),
        action('Restore conversations', '/api/library', [field('names', 'Conversation names (JSON array)', 'json', value=[], required=True)], values={'action': 'restore'}),
        action('Export a conversation', '/api/export', [field('session', 'Conversation name (blank for current)'), field('format', 'Format', 'select', options=['markdown', 'json'])], method='GET'),
        action('Bookmark a current-chat message', '/api/bookmark', [field('index', 'Message index', 'number', required=True), field('enabled', 'Bookmarked', 'checkbox', value=True)]),
        action('Branch from a message', '/api/branch', [field('index', 'Message index', 'number', required=True), field('prompt', 'Edited user prompt (optional)', 'textarea')], description='Creates a separate conversation; an edited user prompt starts a new response.'),
        action('Retry the last user message', '/api/retry', description='Creates a new branch and runs the last user message again.'),
    ]))

    modules.append(module('privacy', 'Memory, privacy & backups',
                          'Manage individual memory entries, review permissions, and preview retention or backup changes before applying them.', [
        action('Memory entries, conflicts & history', '/api/privacy/memory', method='GET'),
        action('Save a memory entry', '/api/privacy/memory', [field('revision', 'Current memory revision', required=True), field('id', 'Entry ID (blank to create)'), field('label', 'Label', required=True), field('content', 'Memory text (up to 4 KB)', 'textarea', required=True), field('excluded', 'Exclude from agent context', 'checkbox'), field('expires_at', 'Expiration (Unix seconds, optional)', 'number')], values={'action': 'save'}),
        action('Change memory inclusion or expiration', '/api/privacy/memory', [field('revision', 'Current memory revision', required=True), identifier('Entry ID'), payload({'excluded': True}, 'Changed fields (JSON)')], values={'action': 'save'}, description='Set excluded to true/false or expires_at to Unix seconds; null clears an expiration.'),
        action('Restore a memory entry version', '/api/privacy/memory', [field('revision', 'Current memory revision', required=True), identifier('Entry ID'), field('version', 'Historical version number', 'number', required=True)], values={'action': 'restore'}),
        action('Delete a memory entry', '/api/privacy/memory', [field('revision', 'Current memory revision', required=True), identifier('Entry ID')], values={'action': 'delete'}),
        action('Export structured project memory', '/api/privacy/memory/export', method='GET'),
        action('Import structured project memory', '/api/privacy/memory/import', [field('revision', 'Current memory revision', required=True), field('content', 'Exported memory JSON', 'textarea', required=True)], description='Imports entries with duplicate detection; review any resulting conflicts.'),
        action('Current tool permissions', '/api/privacy/permissions', method='GET'),
        action('Set tool permission profile', '/api/privacy/permissions', [field('profile', 'Permission profile', 'select', options=['standard', 'read-only'])]),
        action('Privacy audit events', '/api/privacy/audit', method='GET'),
        action('Privacy diagnostics', '/api/privacy/diagnostics', method='GET'),
        action('Download a local backup', '/api/privacy/backup/export', method='GET', description='Downloads private project data in a ZIP. Provider keys, connectors, automation, and permission settings are excluded.'),
        action('Preview a backup restore', '/api/privacy/backup/preview', [field('data', 'Herness backup ZIP (up to 8 MB)', 'file', required=True, accept='.zip')]),
        action('Apply a reviewed backup restore', '/api/privacy/backup/restore', [field('data', 'Same reviewed backup ZIP', 'file', required=True, accept='.zip'), field('preview_token', 'Token from the restore preview', required=True), field('overwrite', 'Replace files listed as conflicts in the preview', 'checkbox')], confirmation='I reviewed the backup file list and approve restoring these files.'),
        action('Preview retention cleanup', '/api/privacy/retention/preview', [field('days', 'Keep the most recent number of days', 'number', value=30), field('categories', 'Categories (JSON array)', 'json', value=['sessions', 'runs', 'checkpoints', 'expired-memory'])], description='Shows candidate files and expired memories. The active conversation is excluded.'),
        action('Apply reviewed retention cleanup', '/api/privacy/retention/apply', [field('days', 'Same retention days used in preview', 'number', value=30), field('categories', 'Same categories used in preview', 'json', value=['sessions', 'runs', 'checkpoints', 'expired-memory']), field('preview_token', 'Token from the cleanup preview', required=True)], confirmation='I reviewed the cleanup preview and approve deleting those items.'),
    ]))
    return {'version': 1, 'modules': modules}
