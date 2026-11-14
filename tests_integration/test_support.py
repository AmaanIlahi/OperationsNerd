"""Schema-aware workflow help stays scoped, read-only and owner authenticated."""
import json
import unittest
from unittest.mock import patch

from tests_integration import test_umbrella_workflow as flow


class SupportTests(flow.UmbrellaHTTPTests):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from db import db as d
        from umbrella.businesses import initialize_registry
        d.init_db()
        initialize_registry()

    def ask(self, path):
        from agentic import chat
        from agentic.llm import LLMResult
        captured = []
        def explain(system, messages, schema):
            captured.append(system)
            return LLMResult(json.dumps({'reply': 'Review a draft before explicitly approving it.', 'operations': []}),
                             'fixture', 0, 0, 0)
        with patch.object(chat, 'run_llm', side_effect=explain):
            answer = self.response(self.owner.post(path + '/chat', json={'messages': [
                {'role': 'user', 'content': 'How should I handle supplier bills after receiving stock?'}]}))
        return answer, captured[0]

    def test_explains_enabled_workflows_without_private_records_or_operational_writes(self):
        self.enable()
        before = self.fixture.core_snapshot(self.bid)
        operational = self.workspace()
        answer, system = self.ask(self.base)
        context = json.loads(system.split('<WORKFLOW_CONTEXT>\n')[1].split('\n</WORKFLOW_CONTEXT>')[0])
        self.assertEqual(context['business_id'], self.bid)
        logistics = next(module for module in context['modules'] if module['id'] == 'logistics')
        self.assertEqual(logistics['status'], 'enabled')
        self.assertEqual(logistics['mappings'], self.bindings)
        self.assertIn('do not create or pay Accounting', ' '.join(logistics['workflow']))
        self.assertNotIn('Toy Customer', system)
        self.assertNotIn('supplier@example.com', system)
        self.assertNotIn('Gym member stays unchanged', system)
        self.assertNotEqual(answer.get('status'), 'pending')
        self.assertEqual(self.workspace(), operational)
        # Chat audit is expected; spec versions, records, mappings and commands
        # retain their snapshots. The assistant has no operational tools.
        after = self.fixture.core_snapshot(self.bid)
        for table in before:
            if table != 'proposals':
                self.assertEqual(after[table], before[table], table)
        recorded = [json.loads(row) for row in after['proposals']]
        self.assertEqual(len(recorded), len(before['proposals']) + 1)
        self.assertEqual(recorded[-1]['status'], 'no_changes')
        self.assertEqual(json.loads(recorded[-1]['ops_json']), [])
        self.response(self.stranger.post(self.base + '/chat', json={'messages': [
            {'role': 'user', 'content': 'Describe this business.'}]}), 404)

    def test_other_business_does_not_inherit_enabled_module_context(self):
        self.enable()
        _, system = self.ask(f"/api/businesses/{self.gym['id']}")
        context = json.loads(system.split('<WORKFLOW_CONTEXT>\n')[1].split('\n</WORKFLOW_CONTEXT>')[0])
        self.assertEqual(context['business_id'], self.gym['id'])
        self.assertEqual(context['modules'], [])


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(SupportTests(name) for name, method in SupportTests.__dict__.items()
        if name.startswith('test_') and callable(method))
