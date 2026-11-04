import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from herness.automation import Automation, next_run, schedule_spec


def stamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


class FakeState:
    busy = False

    def __init__(self, client=None):
        self.client = client
        self.starts = []

    def start_automation(self, prompt, run_id, options):
        self.starts.append((prompt, run_id, options))
        self.busy = True
        return True

    def start(self, prompt):
        self.starts.append(prompt)
        self.busy = True
        return True


class ScheduleTimeTest(unittest.TestCase):
    def test_daily_tracks_daylight_savings_in_local_time(self):
        schedule = schedule_spec({'kind': 'daily', 'time': '09:00', 'timezone': 'America/New_York'})
        self.assertEqual(next_run(schedule, stamp('2026-03-07T15:00:00Z')), stamp('2026-03-08T13:00:00Z'))

    def test_nonexistent_time_is_skipped_and_ambiguous_runs_once(self):
        schedule = schedule_spec({'kind': 'daily', 'time': '02:30', 'timezone': 'America/New_York'})
        self.assertEqual(next_run(schedule, stamp('2026-03-07T08:00:00Z')), stamp('2026-03-09T06:30:00Z'))
        schedule['time'] = '01:30'
        first = next_run(schedule, stamp('2026-10-31T08:00:00Z'))
        self.assertEqual(first, stamp('2026-11-01T05:30:00Z'))
        self.assertEqual(next_run(schedule, first), stamp('2026-11-02T06:30:00Z'))

    def test_weekly_and_once(self):
        schedule = schedule_spec({'kind': 'weekly', 'weekdays': [0, 4], 'time': '14:10', 'timezone': 'Asia/Jerusalem'})
        self.assertEqual(next_run(schedule, stamp('2026-11-04T12:00:00Z')), stamp('2026-11-06T12:10:00Z'))
        once = schedule_spec({'kind': 'once', 'at': '2026-11-04T13:00:00Z'})
        self.assertEqual(next_run(once, stamp('2026-11-04T12:00:00Z')), once['at'])
        self.assertIsNone(next_run(once, once['at']))

    def test_invalid_schedule_is_rejected(self):
        for schedule in ({'timezone': 'Mars/Base'}, {'seconds': True}, {'seconds': 3},
                         {'kind': 'daily', 'time': '24:00'}, {'kind': 'weekly', 'weekdays': []},
                         {'kind': 'once', 'at': '2026-11-04T13:00:00'}):
            with self.subTest(schedule=schedule), self.assertRaises(ValueError):
                schedule_spec(schedule)


class AutomationFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.automation = Automation(self.root)
        self.addCleanup(self.automation.close)
        self.state = FakeState()
        self.now = stamp('2026-11-04T12:00:00Z')

    def call(self, resource, data=None, method='POST', now=None):
        with patch('herness.automation.time.time', return_value=self.now if now is None else now):
            return self.automation.handle(method, '/api/automation/' + resource, data, self.state)

    def schedule(self, **fields):
        return self.call('schedules', {'title': 'Review the project', 'prompt': 'Read the project and summarize.',
                                     'schedule': {'kind': 'interval', 'seconds': 60}, 'enabled': True,
                                     'confirmed': True, **fields})['schedule']


class AutomationTest(AutomationFixture):

    def test_schedule_requires_review_and_changed_scope_revokes_approval(self):
        schedule = self.schedule(confirmed=False)
        self.assertFalse(schedule['enabled'])
        self.assertFalse(schedule['approved'])
        self.assertFalse(self.automation.tick(self.state, self.now + 61)['started'])
        self.call('schedules', {'action': 'resume', 'id': schedule['id'], 'confirmed': True})
        saved = self.call('schedules', method='GET')['schedules'][0]
        self.assertTrue(saved['enabled'])
        changed = self.call('schedules', {'action': 'update', 'id': schedule['id'], 'prompt': 'New scope'})['schedule']
        self.assertFalse(changed['enabled'])
        self.assertFalse(changed['approved'])
        with self.assertRaisesRegex(ValueError, 'Approve'):
            self.call('run', {'id': schedule['id'], 'confirmed': True})

    def test_scheduler_busy_no_overlap_completion_and_limit(self):
        schedule = self.schedule(max_runs=1)
        self.state.busy = True
        self.assertFalse(self.automation.tick(self.state, self.now + 60)['started'])
        self.assertEqual(self.call('runs', method='GET')['runs'], [])
        self.state.busy = False
        launched = self.automation.tick(self.state, self.now + 61)
        self.assertTrue(launched['started'])
        self.assertEqual(self.state.starts[0][2], {'mode': 'plan', 'max_steps': 8, 'schedule_id': schedule['id']})
        self.state.busy = False
        self.assertFalse(self.automation.tick(self.state, self.now + 121)['started'])
        self.automation.complete(launched['run']['id'], summary='Inspected successfully')
        self.assertEqual(self.call('runs', method='GET')['runs'][0]['status'], 'completed')
        self.assertFalse(self.automation.tick(self.state, self.now + 182)['started'])
        self.assertEqual(len(self.state.starts), 1)
        self.assertFalse(self.call('schedules', method='GET')['schedules'][0]['enabled'])

    def test_no_overlap_with_remaining_budget(self):
        self.schedule(max_runs=3)
        launched = self.automation.tick(self.state, self.now + 60)
        self.state.busy = False
        rejected = self.automation.tick(self.state, self.now + 120)
        self.assertFalse(rejected['started'])
        self.assertIn('active run', rejected['reason'])
        self.automation.complete(launched['run']['id'])
        self.assertTrue(self.automation.tick(self.state, self.now + 121)['started'])

    def test_host_busy_race_does_not_consume_occurrence(self):
        schedule = self.schedule(schedule={'kind': 'once', 'at': self.now + 60})
        with patch.object(self.state, 'start_automation', return_value=False):
            self.assertFalse(self.automation.tick(self.state, self.now + 60)['started'])
        saved = self.call('schedules', method='GET')['schedules'][0]
        self.assertEqual(saved['next_run'], schedule['next_run'])
        self.assertEqual(saved['runs_started'], 0)
        self.assertTrue(saved['enabled'])
        self.assertTrue(self.automation.tick(self.state, self.now + 61)['started'])

    def test_missed_policy_skip_and_latest(self):
        skipped = self.schedule(grace_seconds=10)
        self.assertFalse(self.automation.tick(self.state, self.now + 1000)['started'])
        self.assertEqual(self.call('runs', method='GET')['runs'][0]['status'], 'skipped')
        saved = self.call('schedules', method='GET')['schedules'][0]
        self.assertEqual(saved['next_run'], self.now + 1060)
        self.call('schedules', {'action': 'delete', 'id': skipped['id']})
        self.schedule(grace_seconds=10, catch_up='latest')
        self.assertTrue(self.automation.tick(self.state, self.now + 1000)['started'])
        self.assertEqual(len(self.state.starts), 1)

    def test_restart_preserves_claim_and_marks_unknown_outcome(self):
        self.schedule()
        launched = self.automation.tick(self.state, self.now + 60)
        restored = Automation(self.root)
        self.addCleanup(restored.close)
        run = restored.handle('GET', '/api/automation/runs', {}, self.state)['runs'][0]
        self.assertEqual(run['id'], launched['run']['id'])
        self.assertEqual(run['status'], 'interrupted')
        self.assertIn('No automatic retry', run['error'])
        self.state.busy = False
        self.assertFalse(restored.tick(self.state, self.now + 61)['started'])
        self.assertEqual(len(self.state.starts), 1)

    def test_web_host_executes_isolated_schedule_and_records_completion(self):
        from herness.cli import DemoClient
        from herness.web import make_server
        schedule = self.schedule()
        server = make_server(self.root, DemoClient(), 0, demo=True, force_demo=True)
        self.addCleanup(server.server_close)
        state = server.workspace
        module = state.application.automation
        launched = module.handle('POST', '/api/automation/run', {'id': schedule['id'], 'confirmed': True}, state)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            record = module.handle('GET', '/api/automation/runs', {}, state)['runs'][0]
            if record['status'] not in {'starting', 'running'}:
                break
            time.sleep(.01)
        self.assertEqual(record['status'], 'completed', record)
        self.assertEqual(state.session_name, 'auto-' + launched['run']['id'])
        self.assertIn('Offline demo complete', state.session.messages[-1]['content'])
        self.assertFalse(state.busy)

    def test_preview_and_no_unsafe_host_fallback(self):
        preview = self.call('preview', {'schedule': {'kind': 'daily', 'time': '10:00', 'timezone': 'Asia/Tokyo'}, 'count': 2})
        self.assertEqual(len(preview['occurrences']), 2)
        self.assertTrue(preview['occurrences'][0]['local'].endswith('+09:00'))
        self.schedule()
        result = self.automation.tick(object(), self.now + 60)
        self.assertFalse(result['started'])
        self.assertIn('isolated', result['reason'])

    def test_task_plan_dependencies_evidence_and_persistence(self):
        task = self.call('tasks', {'title': 'Deliver app', 'acceptance': ['Tests pass'], 'steps': [
            {'id': 'build', 'title': 'Build'}, {'id': 'verify', 'title': 'Verify', 'depends_on': ['build']} ]})['task']
        with self.assertRaisesRegex(ValueError, 'cycle'):
            self.call('tasks', {'action': 'update', 'id': task['id'], 'steps': [
                {'id': 'a', 'title': 'A', 'depends_on': ['b']}, {'id': 'b', 'title': 'B', 'depends_on': ['a']}]})
        with self.assertRaisesRegex(ValueError, 'evidence'):
            self.call('tasks', {'action': 'update', 'id': task['id'], 'steps': [{'title': 'Build', 'status': 'completed'}]})
        with self.assertRaisesRegex(ValueError, 'remaining steps'):
            self.call('tasks', {'action': 'update', 'id': task['id'], 'status': 'completed'})
        task['steps'][0].update(status='completed', evidence='Working build')
        task['steps'][1].update(status='completed', evidence='All tests pass')
        self.call('tasks', {'action': 'update', 'id': task['id'], 'steps': task['steps'], 'status': 'completed'})
        restored = Automation(self.root)
        result = restored.handle('GET', '/api/automation/tasks', {}, self.state)['tasks'][0]
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['acceptance'], ['Tests pass'])

    def skill(self):
        return self.call('skills', {'title': 'Review', 'prompt': 'Review {{target}}. Thorough: {{deep}}.',
            'schema': {'type': 'object', 'properties': {'target': {'type': 'string'}, 'deep': {'type': 'boolean'}},
                       'required': ['target', 'deep']}, 'trusted': True, 'confirmed': True})['skill']

    def test_skill_schema_literal_render_version_and_import_trust(self):
        skill = self.skill()
        output = self.call('skills', {'action': 'test', 'id': skill['id'], 'inputs': {'target': '{{deep}} $(touch nope)', 'deep': True}})
        self.assertEqual(output['prompt'], 'Review {{deep}} $(touch nope). Thorough: true.')
        for inputs in ({'target': 'app'}, {'target': 3, 'deep': True}, {'target': 'app', 'deep': True, 'extra': ''}):
            with self.subTest(inputs=inputs), self.assertRaises(ValueError):
                self.call('skills', {'action': 'test', 'id': skill['id'], 'inputs': inputs})
        changed = self.call('skills', {'action': 'update', 'id': skill['id'], 'prompt': 'Check {{target}} with {{deep}}'})['skill']
        self.assertFalse(changed['trusted'])
        self.assertEqual(changed['revision'], 2)
        self.assertEqual(changed['history'][0]['prompt'], skill['prompt'])
        exported = self.call('skills', {'action': 'export', 'id': skill['id']})
        imported = self.call('skills', {'action': 'import', 'skill': exported['content'], 'trusted': True, 'confirmed': True})['skill']
        self.assertFalse(imported['trusted'])
        self.assertNotEqual(imported['id'], skill['id'])
        self.assertFalse((self.root / 'nope').exists())

    def test_skill_trust_revocation_and_explicit_execution(self):
        skill = self.skill()
        with self.assertRaisesRegex(ValueError, 'confirm'):
            self.call('skills/run', {'id': skill['id'], 'inputs': {'target': 'app', 'deep': False}})
        result = self.call('skills/run', {'id': skill['id'], 'confirmed': True, 'inputs': {'target': 'app', 'deep': False}})
        self.assertTrue(result['started'])
        self.assertEqual(self.state.starts, ['Review app. Thorough: false.'])
        untrusted = self.call('skills', {'action': 'update', 'id': skill['id'], 'trusted': False})['skill']
        self.assertFalse(untrusted['trusted'])
        with self.assertRaisesRegex(ValueError, 'trust'):
            self.call('skills/run', {'id': skill['id'], 'confirmed': True, 'inputs': {'target': 'app', 'deep': True}})

    def test_state_symlinks_are_rejected(self):
        path = self.root / '.herness' / 'automation.json'
        outside = self.root / 'outside.json'
        outside.write_text('{}')
        path.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            Automation(self.root)
        self.assertEqual(outside.read_text(), '{}')

    def test_unknown_routes_and_malformed_input(self):
        self.assertIsNone(self.automation.handle('GET', '/api/elsewhere', {}, self.state))
        with self.assertRaises(ValueError):
            self.call('schedules', ['invalid'])
        with self.assertRaises(ValueError):
            self.call('missing', method='GET')


class ReadOnlyClient:
    model = 'test'

    def complete(self, messages):
        if messages[-1]['role'] == 'tool':
            result = json.loads(messages[-1]['content'])
            return {'role': 'assistant', 'content': 'Write denied: ' + str(bool(result.get('denied') or result.get('error')))}
        return {'role': 'assistant', 'content': None, 'tool_calls': [{
            'id': 'write-attempt', 'type': 'function', 'function': {'name': 'write_file',
            'arguments': json.dumps({'path': 'should-not-exist.txt', 'content': 'no'})}}]}


class TeamTest(AutomationFixture):
    def wait_run(self, run_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            runs = self.call('runs', method='GET')['runs']
            run = next(run for run in runs if run['id'] == run_id)
            if run['status'] not in {'starting', 'running'}:
                return run
            time.sleep(.01)
        self.fail('Team did not finish')

    def test_actual_specialists_are_isolated_and_cannot_write(self):
        self.state.client = ReadOnlyClient()
        team = self.call('teams', {'title': 'Review team', 'specialists': [
            {'name': 'Security', 'instructions': 'Review security'}, {'name': 'Quality', 'instructions': 'Review quality'}],
            'concurrency': 2, 'max_steps': 3})['team']
        with self.assertRaisesRegex(ValueError, 'confirm'):
            self.call('teams/run', {'id': team['id']})
        launched = self.call('teams/run', {'id': team['id'], 'confirmed': True, 'inputs': 'Inspect this project'})['run']
        run = self.wait_run(launched['id'])
        self.assertEqual(run['status'], 'completed')
        self.assertEqual(len(run['results']), 2)
        self.assertTrue(all('True' in item['output'] for item in run['results']), run)
        self.assertFalse((self.root / 'should-not-exist.txt').exists())
        sessions = list((self.root / '.herness' / 'sessions').glob('team-*.json'))
        self.assertEqual(len(sessions), 2)
        self.assertNotEqual(sessions[0].name, sessions[1].name)
        self.assertFalse(hasattr(self.state.client, 'tool_schemas'), 'Team must not modify the active provider client')

    def test_team_can_be_cancelled_and_blocks_scheduled_dispatch(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        class WaitingClient:
            model = 'test'
            def complete(self, messages):
                started.set()
                release.wait(3)
                return {'role': 'assistant', 'content': 'finished'}
        self.state.client = WaitingClient()
        team = self.call('teams', {'title': 'Slow team', 'specialists': [{'name': 'Review', 'instructions': 'Read the app'}]})['team']
        run = self.call('teams/run', {'id': team['id'], 'confirmed': True})['run']
        self.assertTrue(started.wait(2))
        self.schedule()
        self.assertFalse(self.automation.tick(self.state, self.now + 60)['started'])
        self.assertTrue(self.call('teams/stop', {'run_id': run['id']})['stopping'])
        release.set()
        finished = self.wait_run(run['id'])
        self.assertEqual(finished['status'], 'cancelled')
        self.assertEqual(finished['results'][0]['status'], 'cancelled')


if __name__ == '__main__':
    unittest.main()
