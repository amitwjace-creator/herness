"""Durable local schedules, task plans, reusable skills, and read-only teams.

The server owns lifecycle: call tick(state) periodically, and complete(run_id,...)
when a scheduled agent finishes. Nothing here sends external notifications.
"""
from __future__ import annotations

import concurrent.futures
import copy
import json
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .state import ProjectStore

UTC = timezone.utc
STATUSES = {'pending', 'active', 'completed', 'blocked', 'skipped'}
READ_TOOLS = {'list_files', 'read_file', 'search_files', 'project_symbols', 'git_status', 'git_diff'}


def text(value, field, limit=32000, empty=False):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise ValueError(f'{field} must be text of 1–{limit} characters')
    return value.strip() if not empty else value


def integer(value, field, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f'{field} must be an integer between {minimum} and {maximum}')
    return value


def epoch(value, field='time'):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        result = float(value)
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError(f'{field} must include a timezone offset')
        result = parsed.timestamp()
    else:
        raise ValueError(f'{field} must be an ISO timestamp or epoch seconds')
    if not 0 <= result <= 253402300799:
        raise ValueError(f'{field} is out of range')
    return result


def schedule_spec(value):
    if not isinstance(value, dict):
        raise ValueError('schedule must be an object')
    kind = value.get('kind', 'interval')
    zone = text(value.get('timezone', 'UTC'), 'timezone', 100)
    try:
        ZoneInfo(zone)
    except ZoneInfoNotFoundError:
        raise ValueError('Choose a valid IANA timezone, such as Asia/Jerusalem') from None
    result = {'kind': kind, 'timezone': zone}
    if kind == 'interval':
        result['seconds'] = integer(value.get('seconds', 3600), 'interval seconds', 60, 2678400)
    elif kind in {'daily', 'weekly'}:
        clock = value.get('time', '09:00')
        if not isinstance(clock, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', clock):
            raise ValueError('time must be HH:MM in 24-hour format')
        result['time'] = clock
        if kind == 'weekly':
            days = value.get('weekdays', [0])
            if not isinstance(days, list) or not days or len(days) > 7:
                raise ValueError('weekdays must contain Monday=0 through Sunday=6')
            result['weekdays'] = sorted(set(integer(day, 'weekday', 0, 6) for day in days))
    elif kind == 'once':
        result['at'] = epoch(value.get('at'), 'scheduled time')
    else:
        raise ValueError('Schedule kind must be interval, daily, weekly, or once')
    return result


def next_run(spec, after):
    """Return the next occurrence strictly after an epoch timestamp.

    Daily schedules use the first occurrence of an ambiguous wall time. A wall
    time skipped by daylight savings is skipped rather than run at a new time.
    """
    if spec['kind'] == 'interval':
        return after + spec['seconds']
    if spec['kind'] == 'once':
        return spec['at'] if spec['at'] > after else None
    zone = ZoneInfo(spec['timezone'])
    date = datetime.fromtimestamp(after, zone).date()
    hour, minute = map(int, spec['time'].split(':'))
    for offset in range(15):
        candidate_date = date + timedelta(days=offset)
        if spec['kind'] == 'weekly' and candidate_date.weekday() not in spec['weekdays']:
            continue
        candidate = datetime(candidate_date.year, candidate_date.month, candidate_date.day, hour, minute,
                             tzinfo=zone, fold=0)
        stamp = candidate.timestamp()
        roundtrip = datetime.fromtimestamp(stamp, zone)
        if (roundtrip.hour, roundtrip.minute, roundtrip.date()) != (hour, minute, candidate_date):
            continue
        if stamp > after:
            return stamp
    raise ValueError('Could not calculate the next schedule occurrence')


def validate_schema(schema):
    if schema is None:
        return {'type': 'object', 'properties': {}, 'required': []}
    if not isinstance(schema, dict) or schema.get('type', 'object') != 'object':
        raise ValueError('Skill schema must be a JSON object schema')
    props = schema.get('properties', {})
    required = schema.get('required', [])
    if not isinstance(props, dict) or len(props) > 30 or not isinstance(required, list):
        raise ValueError('Invalid skill input schema')
    clean = {}
    for name, definition in props.items():
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', name):
            raise ValueError('Invalid skill variable name')
        if not isinstance(definition, dict) or definition.get('type') not in {'string', 'number', 'integer', 'boolean'}:
            raise ValueError('Skill variables support string, number, integer, and boolean types')
        clean[name] = {'type': definition['type'], 'description': text(definition.get('description', ''), 'description', 500, True)}
    if any(not isinstance(name, str) or name not in clean for name in required):
        raise ValueError('Required skill variables must be defined in properties')
    return {'type': 'object', 'properties': clean, 'required': list(dict.fromkeys(required)), 'additionalProperties': False}


def render_skill(skill, inputs):
    if not isinstance(inputs, dict):
        raise ValueError('Skill inputs must be an object')
    schema = skill['schema']
    if set(inputs) - set(schema['properties']):
        raise ValueError('Unknown skill input')
    if any(name not in inputs for name in schema['required']):
        raise ValueError('A required skill input is missing')
    for name, value in inputs.items():
        kind = schema['properties'][name]['type']
        valid = ((kind == 'string' and isinstance(value, str)) or
                 (kind == 'boolean' and isinstance(value, bool)) or
                 (kind == 'integer' and isinstance(value, int) and not isinstance(value, bool)) or
                 (kind == 'number' and isinstance(value, (int, float)) and not isinstance(value, bool)))
        if not valid:
            raise ValueError(f'Input {name} must be {kind}')
    def replace(match):
        value = inputs.get(match.group(1), '')
        return value if isinstance(value, str) else json.dumps(value)
    result = re.sub(r'\{\{\s*([A-Za-z][A-Za-z0-9_]*)\s*\}\}', replace, skill['prompt'])
    if len(result) > 32000:
        raise ValueError('Rendered skill exceeds 32000 characters')
    return result


class Automation:
    def __init__(self, root):
        self.root = root
        self.store = ProjectStore(root)
        self.lock = threading.RLock()
        self.team_cancellations = {}
        raw = json.loads(self.store.read('automation.json', '{}'))
        if not isinstance(raw, dict) or raw.get('version', 1) != 1:
            raise ValueError('Unsupported automation state')
        self.data = {'version': 1, 'schedules': {}, 'tasks': {}, 'skills': {}, 'teams': {}, 'runs': []}
        self.data.update(raw)
        for name in ('schedules', 'tasks', 'skills', 'teams'):
            if not isinstance(self.data[name], dict):
                raise ValueError('Invalid automation state')
        if not isinstance(self.data['runs'], list):
            raise ValueError('Invalid run history')
        changed = False
        for run in self.data['runs']:
            if run.get('status') in {'starting', 'running'}:
                run.update(status='interrupted', ended=time.time(),
                           error='Server restarted; previous execution outcome is unknown. No automatic retry was performed.')
                changed = True
        if changed:
            self._save()

    def _save(self):
        if self.store.directory.is_symlink():
            raise ValueError('Agent state must not be a symlink')
        active = [run for run in self.data['runs'] if run['status'] in {'starting', 'running'}]
        finished = [run for run in self.data['runs'] if run['status'] not in {'starting', 'running'}]
        self.data['runs'] = sorted(active + finished[-500:], key=lambda run: run['started'])
        self.store.write('automation.json', json.dumps(self.data, indent=2, ensure_ascii=False))

    def close(self):
        """Stop local specialist workers when the host shuts down."""
        with self.lock:
            for cancellation in self.team_cancellations.values():
                cancellation.stop()

    def _get(self, collection, identity):
        if not isinstance(identity, str) or identity not in self.data[collection]:
            raise ValueError(f'Unknown {collection.rstrip("s")} id')
        return self.data[collection][identity]

    def _run(self, kind, identity, title, now):
        record = {'id': secrets.token_hex(8), 'kind': kind, 'source_id': identity, 'title': title,
                  'status': 'starting', 'started': now, 'ended': None}
        self.data['runs'].append(record)
        return record

    def complete(self, run_id, status='completed', summary='', error=''):
        """Called by the host after an isolated scheduled run actually terminates."""
        if status not in {'completed', 'failed', 'cancelled', 'interrupted'}:
            raise ValueError('Invalid terminal run status')
        with self.lock:
            record = next((run for run in self.data['runs'] if run['id'] == run_id), None)
            if record and record['status'] in {'running', 'starting'}:
                record.update(status=status, ended=time.time(), summary=str(summary)[:8000], error=str(error)[:2000])
                self._save()
            return copy.deepcopy(record)

    def _schedule(self, data, now):
        action = data.get('action', 'create')
        if action in {'delete', 'pause', 'resume'}:
            item = self._get('schedules', data.get('id'))
            if action == 'delete':
                del self.data['schedules'][item['id']]
            elif action == 'pause':
                item['enabled'] = False
            else:
                if data.get('confirmed') is not True:
                    raise ValueError('Review the schedule and confirm before resuming it')
                item.update(enabled=True, approved=True, next_run=next_run(item['schedule'], now))
            self._save()
            return {'ok': True}
        before = self._get('schedules', data.get('id')) if action == 'update' else {}
        if action not in {'create', 'update'}:
            raise ValueError('Unknown schedule action')
        incoming = data.get('schedule_record', data)
        if not isinstance(incoming, dict):
            raise ValueError('schedule_record must be an object')
        item = {**before, **{k: v for k, v in incoming.items() if k in {
            'title', 'prompt', 'schedule', 'enabled', 'catch_up', 'grace_seconds', 'max_runs', 'max_steps', 'mode'}}}
        item['title'] = text(item.get('title'), 'title', 120)
        item['prompt'] = text(item.get('prompt'), 'prompt')
        item['schedule'] = schedule_spec(item.get('schedule', {}))
        item['catch_up'] = item.get('catch_up', 'skip')
        if item['catch_up'] not in {'skip', 'latest'}:
            raise ValueError('catch_up must be skip or latest')
        item['grace_seconds'] = integer(item.get('grace_seconds', 300), 'grace_seconds', 0, 86400)
        item['max_runs'] = integer(item.get('max_runs', 100), 'max_runs', 1, 10000)
        item['max_steps'] = integer(item.get('max_steps', 8), 'max_steps', 1, 30)
        item['mode'] = item.get('mode', 'plan')
        if item['mode'] not in {'plan', 'act'}:
            raise ValueError('mode must be plan or act')
        if not isinstance(item.get('enabled', False), bool):
            raise ValueError('enabled must be boolean')
        relevant = ('prompt', 'schedule', 'catch_up', 'grace_seconds', 'max_runs', 'max_steps', 'mode')
        scope_changed = any(item.get(key) != before.get(key) for key in relevant)
        item['approved'] = data.get('confirmed') is True or (before.get('approved', False) and not scope_changed)
        item['enabled'] = item.get('enabled', False) and item['approved']
        item['id'] = before.get('id', secrets.token_hex(8))
        item['created'] = before.get('created', now)
        item['updated'] = now
        item['runs_started'] = before.get('runs_started', 0)
        if scope_changed or 'next_run' not in item:
            item['next_run'] = next_run(item['schedule'], now)
        self.data['schedules'][item['id']] = item
        self._save()
        return {'schedule': copy.deepcopy(item), 'approval_required': not item['approved']}

    def _launch_schedule(self, item, state, now, manual=False):
        if self.team_cancellations:
            return {'started': False, 'reason': 'A specialist team is running'}
        if getattr(state, 'busy', False):
            return {'started': False, 'reason': 'Workspace is busy'}
        if item['runs_started'] >= item['max_runs']:
            item['enabled'] = False
            self._save()
            return {'started': False, 'reason': 'Schedule run limit reached'}
        if any(r['source_id'] == item['id'] and r['status'] in {'starting', 'running'} for r in self.data['runs']):
            return {'started': False, 'reason': 'This schedule already has an active run'}
        if not hasattr(state, 'start_automation'):
            return {'started': False, 'reason': 'The host must provide isolated start_automation support'}
        run = self._run('schedule', item['id'], item['title'], now)
        run['mode'] = item['mode']
        previous_due, previous_enabled = item['next_run'], item['enabled']
        # Persist the claim before dispatching. Restart recovery never repeats an uncertain action.
        item['next_run'] = next_run(item['schedule'], now)
        item['runs_started'] += 1
        if item['next_run'] is None:
            item['enabled'] = False
        self._save()
        try:
            accepted = state.start_automation(item['prompt'], run['id'], {
                'mode': item['mode'], 'max_steps': item['max_steps'], 'schedule_id': item['id']})
            if not accepted:
                run.update(status='skipped', ended=now, error='Host was busy when dispatching')
                item['runs_started'] -= 1
                item['next_run'], item['enabled'] = previous_due, previous_enabled
            elif run['status'] == 'starting':
                run['status'] = 'running'
        except Exception as exc:
            run.update(status='failed', ended=now, error=f'Dispatch failed: {type(exc).__name__}')
        self._save()
        return {'started': run['status'] in {'starting', 'running', 'completed'}, 'run': copy.deepcopy(run)}

    def tick(self, state, now=None):
        now = time.time() if now is None else float(now)
        with self.lock:
            # At most one scheduled task is dispatched per tick, with host busy gating.
            for item in sorted(self.data['schedules'].values(), key=lambda x: x.get('next_run') or float('inf')):
                due = item.get('next_run')
                if not item.get('enabled') or not item.get('approved') or due is None or due > now:
                    continue
                if now - due > item['grace_seconds'] and item['catch_up'] == 'skip':
                    skipped = self._run('schedule', item['id'], item['title'], now)
                    skipped.update(status='skipped', ended=now, error='Missed occurrence skipped by policy', scheduled_for=due)
                    item['next_run'] = next_run(item['schedule'], now)
                    if item['next_run'] is None:
                        item['enabled'] = False
                    self._save()
                    continue
                return self._launch_schedule(item, state, now)
        return {'started': False}

    def _task(self, data, now):
        action = data.get('action', 'create')
        before = self._get('tasks', data.get('id')) if action in {'update', 'delete'} else {}
        if action == 'delete':
            del self.data['tasks'][before['id']]
            self._save()
            return {'ok': True}
        if action not in {'create', 'update'}:
            raise ValueError('Unknown task action')
        item = {**before, **{k: v for k, v in data.items() if k in {'title', 'description', 'steps', 'acceptance', 'status'}}}
        item['title'] = text(item.get('title'), 'title', 160)
        item['description'] = text(item.get('description', ''), 'description', 16000, True)
        item['status'] = item.get('status', 'pending')
        if item['status'] not in STATUSES:
            raise ValueError('Unknown task status')
        acceptance = item.get('acceptance', [])
        if not isinstance(acceptance, list) or len(acceptance) > 30:
            raise ValueError('acceptance must contain up to 30 criteria')
        item['acceptance'] = [text(value, 'acceptance criterion', 1000) for value in acceptance]
        steps = item.get('steps', [])
        if not isinstance(steps, list) or len(steps) > 100:
            raise ValueError('steps must contain up to 100 steps')
        clean, identities = [], set()
        for step in steps:
            if isinstance(step, str):
                step = {'title': step}
            if not isinstance(step, dict):
                raise ValueError('Each task step must be text or an object')
            identity = step.get('id', secrets.token_hex(5))
            if not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', identity) or identity in identities:
                raise ValueError('Step ids must be unique safe identifiers')
            identities.add(identity)
            status = step.get('status', 'pending')
            if status not in STATUSES:
                raise ValueError('Unknown step status')
            dependencies = step.get('depends_on', [])
            if not isinstance(dependencies, list) or any(not isinstance(d, str) for d in dependencies):
                raise ValueError('depends_on must contain step ids')
            clean.append({'id': identity, 'title': text(step.get('title'), 'step title', 500), 'status': status,
                          'depends_on': list(dict.fromkeys(dependencies)),
                          'evidence': text(step.get('evidence', ''), 'evidence', 4000, True),
                          'blocker': text(step.get('blocker', ''), 'blocker', 2000, True)})
        graph = {step['id']: step['depends_on'] for step in clean}
        visited, stack = set(), set()
        def visit(identity):
            if identity in stack:
                raise ValueError('Task dependencies contain a cycle')
            if identity not in graph:
                raise ValueError('Task dependency references an unknown step')
            if identity in visited:
                return
            stack.add(identity)
            for dependency in graph[identity]:
                visit(dependency)
            stack.remove(identity)
            visited.add(identity)
        for identity in graph:
            visit(identity)
        for step in clean:
            if step['status'] == 'completed' and not step['evidence'].strip():
                raise ValueError('Completed steps need evidence')
        item['steps'] = clean
        if item['status'] == 'completed' and any(step['status'] not in {'completed', 'skipped'} for step in clean):
            raise ValueError('Complete or explicitly skip remaining steps first')
        item.update(id=before.get('id', secrets.token_hex(8)), created=before.get('created', now), updated=now)
        self.data['tasks'][item['id']] = item
        self._save()
        return {'task': copy.deepcopy(item)}

    def _skill(self, data, now):
        action = data.get('action', 'create')
        before = self._get('skills', data.get('id')) if action in {'update', 'delete', 'export', 'test', 'run'} else {}
        if action == 'delete':
            del self.data['skills'][before['id']]
            self._save()
            return {'ok': True}
        if action == 'export':
            export = {key: before[key] for key in ('title', 'description', 'prompt', 'schema')}
            return {'filename': before['id'] + '.skill.json', 'content': json.dumps({'format': 'herness-skill-v1', **export}, indent=2), 'mime': 'application/json'}
        if action in {'test', 'run'}:
            return {'prompt': render_skill(before, data.get('inputs', {})), 'trusted': before['trusted'], 'skill_id': before['id']}
        if action == 'import':
            imported = data.get('skill')
            if isinstance(imported, str):
                imported = json.loads(imported)
            if not isinstance(imported, dict) or imported.get('format') != 'herness-skill-v1':
                raise ValueError('Expected a herness-skill-v1 export')
            data = {**imported, 'trusted': False, 'confirmed': False}
        elif action not in {'create', 'update'}:
            raise ValueError('Unknown skill action')
        item = {**before, **{k: v for k, v in data.items() if k in {'title', 'description', 'prompt', 'schema', 'trusted'}}}
        item['title'] = text(item.get('title'), 'skill title', 120)
        item['description'] = text(item.get('description', ''), 'skill description', 2000, True)
        item['prompt'] = text(item.get('prompt'), 'skill prompt')
        item['schema'] = validate_schema(item.get('schema'))
        variables = set(re.findall(r'\{\{\s*([A-Za-z][A-Za-z0-9_]*)\s*\}\}', item['prompt']))
        if variables - set(item['schema']['properties']):
            raise ValueError('Every prompt variable must be declared in the input schema')
        changed = any(item.get(key) != before.get(key) for key in ('prompt', 'schema'))
        if 'trusted' in data and not isinstance(data['trusted'], bool):
            raise ValueError('trusted must be boolean')
        item['trusted'] = (data.get('confirmed') is True and data.get('trusted') is True or
                           (not changed and before.get('trusted', False) and data.get('trusted', True)))
        item['id'] = before.get('id', secrets.token_hex(8))
        item['revision'] = before.get('revision', 0) + 1
        history = list(before.get('history', []))
        if before:
            history.append({key: before[key] for key in ('revision', 'title', 'description', 'prompt', 'schema', 'updated')})
        item['history'] = history[-20:]
        item.update(created=before.get('created', now), updated=now)
        self.data['skills'][item['id']] = item
        self._save()
        return {'skill': copy.deepcopy(item)}

    def _team(self, data, now):
        action = data.get('action', 'create')
        before = self._get('teams', data.get('id')) if action in {'update', 'delete'} else {}
        if action == 'delete':
            del self.data['teams'][before['id']]
            self._save()
            return {'ok': True}
        if action not in {'create', 'update'}:
            raise ValueError('Unknown team action')
        item = {**before, **{k: v for k, v in data.items() if k in {'title', 'specialists', 'concurrency', 'max_steps', 'max_output_tokens'}}}
        item['title'] = text(item.get('title'), 'team title', 120)
        item['concurrency'] = integer(item.get('concurrency', 2), 'concurrency', 1, 4)
        item['max_steps'] = integer(item.get('max_steps', 5), 'max_steps', 1, 8)
        item['max_output_tokens'] = integer(item.get('max_output_tokens', 2048), 'max_output_tokens', 128, 16384)
        specialists = item.get('specialists', [])
        if not isinstance(specialists, list) or not 1 <= len(specialists) <= 8:
            raise ValueError('Teams need between 1 and 8 specialists')
        clean = []
        for specialist in specialists:
            if not isinstance(specialist, dict):
                raise ValueError('Invalid specialist')
            clean.append({'name': text(specialist.get('name'), 'specialist name', 80),
                          'instructions': text(specialist.get('instructions'), 'instructions', 8000)})
        item['specialists'] = clean
        item.update(id=before.get('id', secrets.token_hex(8)), created=before.get('created', now), updated=now)
        self.data['teams'][item['id']] = item
        self._save()
        return {'team': copy.deepcopy(item)}

    def launch_team(self, data, state):
        if data.get('confirmed') is not True:
            raise ValueError('Review the team and confirm before running it')
        team = copy.deepcopy(self._get('teams', data.get('id')))
        inputs = text(data.get('inputs', ''), 'team task', 16000, True)
        if self.team_cancellations:
            raise ValueError('A team is already running; stop or finish it first')
        if getattr(state, 'busy', False):
            raise ValueError('Finish the active conversation before launching a team')
        from .core import Agent, Cancellation, Session, TurnCancelled
        from .tools import ToolRunner
        cancel = Cancellation()
        record = self._run('team', team['id'], team['title'], time.time())
        record.update(status='running', mode='plan', results=[], max_tool_rounds=len(team['specialists']) * team['max_steps'],
                      max_output_tokens_per_request=team['max_output_tokens'])
        self.team_cancellations[record['id']] = cancel
        self._save()
        client = copy.copy(state.client)
        root = self.root
        class ReadOnlyRunner(ToolRunner):
            def execute(self, name, arguments):
                if name not in READ_TOOLS:
                    return {'denied': True, 'message': 'Team specialists have read-only tools'}
                return super().execute(name, arguments)
        def specialist_task(index, specialist):
            messages = []
            result = {'name': specialist['name'], 'status': 'running', 'output': ''}
            with self.lock:
                record['results'].append(result)
                self._save()
            try:
                cancel.check()
                agent = Agent(copy.copy(client), ReadOnlyRunner(root, lambda _: False, cancel=cancel, mode='read-only'),
                              Session(root, 'team-' + record['id'] + '-' + str(index)),
                              emit=lambda value: messages.append(str(value)), max_steps=team['max_steps'], cancel=cancel,
                              mode='plan', delta=lambda _: None,
                              runtime_options={'max_output_tokens': team['max_output_tokens'], 'max_context_tokens': 64000})
                output = agent.run('You are a read-only specialist. Do not modify files or execute commands.\n'
                                   + specialist['instructions'] + '\n\nUser task:\n' + inputs)
                result.update(status='completed', output=output)
            except TurnCancelled:
                result.update(status='cancelled', output='\n'.join(messages)[-16000:])
            except Exception as exc:
                result.update(status='failed', error=f'{type(exc).__name__}: specialist failed', output='\n'.join(messages)[-16000:])
            with self.lock:
                self._save()
        def worker():
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=team['concurrency']) as pool:
                    futures = [pool.submit(specialist_task, index, specialist) for index, specialist in enumerate(team['specialists'])]
                    for future in futures:
                        future.result()
                status = 'cancelled' if cancel.event.is_set() else ('failed' if any(r['status'] == 'failed' for r in record['results']) else 'completed')
                self.complete(record['id'], status)
            finally:
                with self.lock:
                    self.team_cancellations.pop(record['id'], None)
        threading.Thread(target=worker, name='herness-team-' + record['id'], daemon=True).start()
        return {'run': copy.deepcopy(record)}

    def handle(self, method, path, data, state):
        if not path.startswith('/api/automation/'):
            return None
        resource = path[len('/api/automation/'):].split('?', 1)[0]
        now = time.time()
        data = data or {}
        if not isinstance(data, dict):
            raise ValueError('Automation requests must be JSON objects')
        with self.lock:
            if method == 'GET':
                if resource == 'status':
                    return {'scheduler': 'available', 'isolated_scheduler': hasattr(state, 'start_automation'),
                            'teams': 'read-only specialists', 'max_team_concurrency': 4,
                            'unavailable': ['external delivery channels', 'cron expressions', 'dollar-based budget accounting',
                                            'cross-provider team routing', 'writable team integration']}
                if resource in {'schedules', 'tasks', 'skills', 'teams'}:
                    return {resource: copy.deepcopy(sorted(self.data[resource].values(), key=lambda item: item.get('updated', 0), reverse=True))}
                if resource == 'runs':
                    return {'runs': copy.deepcopy(list(reversed(self.data['runs'])))}
            if method == 'POST':
                if resource == 'schedules':
                    return self._schedule(data, now)
                if resource == 'preview':
                    spec = schedule_spec(data.get('schedule', {}))
                    after = epoch(data['after']) if 'after' in data else now
                    count = integer(data.get('count', 5), 'count', 1, 20)
                    values = []
                    for _ in range(count):
                        after = next_run(spec, after)
                        if after is None:
                            break
                        values.append({'epoch': after, 'utc': datetime.fromtimestamp(after, UTC).isoformat(),
                                       'local': datetime.fromtimestamp(after, ZoneInfo(spec['timezone'])).isoformat()})
                    return {'occurrences': values}
                if resource == 'run':
                    if data.get('confirmed') is not True:
                        raise ValueError('Review the task and confirm before running it')
                    item = self._get('schedules', data.get('id'))
                    if not item['approved']:
                        raise ValueError('Approve the current schedule configuration first')
                    return self._launch_schedule(item, state, now, manual=True)
                if resource == 'tasks':
                    return self._task(data, now)
                if resource == 'skills':
                    return self._skill(data, now)
                if resource == 'skills/run':
                    item = self._get('skills', data.get('id'))
                    if data.get('confirmed') is not True or not item['trusted']:
                        raise ValueError('Review and trust the skill, then confirm its run')
                    if getattr(state, 'busy', False):
                        return {'started': False, 'reason': 'Workspace is busy'}
                    prompt = render_skill(item, data.get('inputs', {}))
                    return {'started': bool(state.start(prompt)), 'skill_id': item['id']}
                if resource == 'teams':
                    return self._team(data, now)
                if resource == 'teams/run':
                    return self.launch_team(data, state)
                if resource == 'teams/stop':
                    cancel = self.team_cancellations.get(data.get('run_id'))
                    if not cancel:
                        raise ValueError('No running team with this id')
                    cancel.stop()
                    return {'stopping': True}
        raise ValueError('Unknown automation endpoint or method')
