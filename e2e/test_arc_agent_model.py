"""Negative controls for the real-model experiment's scope and schedule."""
import unittest

from arc_agent_model import navigation_allowed, schedule, tool_specs, validate_inputs


class ArcAgentModelTests(unittest.TestCase):
    def test_pairs_are_interleaved_and_seed_matched(self):
        self.assertEqual(schedule(201, 2), [
            {'seed': 201, 'mode': 'baseline'}, {'seed': 201, 'mode': 'candidate'},
            {'seed': 202, 'mode': 'candidate'}, {'seed': 202, 'mode': 'baseline'}])

    def test_planning_cannot_activate_mutations(self):
        for label in ('Save invoice', 'Review invoice', 'Reload invoice', 'Delete record', None, ''):
            self.assertFalse(navigation_allowed(label), label)
        for label in ('Inbox', 'Open message Updated receiving', 'Open invoice 1234', 'Open policy Invoice reconciliation policy'):
            self.assertTrue(navigation_allowed(label), label)

    def test_literals_require_unambiguous_amount_and_known_route(self):
        valid = {'amount': '123.45', 'route': 'Standard', 'note': 'RCPT-1234'}
        self.assertEqual(validate_inputs(valid), valid)
        for field, values in {'amount': [123.45, '1e3', '-1.00', '1.001', 'NaN'],
                              'route': ['standard', 'Wire transfer', None],
                              'note': ['', 'x' * 501, None]}.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    validate_inputs({**valid, field: value})
        with self.assertRaises(ValueError): validate_inputs({**valid, 'expected': 'hidden answer'})

    def test_candidate_cannot_send_freeform_form_values(self):
        tools = {tool['name']: tool for tool in tool_specs('candidate')}
        execute = tools['execute_form']['inputSchema']
        self.assertEqual(set(execute['properties']), {'choice_id', 'finish'})
        self.assertFalse(execute['additionalProperties'])
        self.assertEqual(set(tools['begin_execution']['inputSchema']['required']), {'amount', 'route', 'note'})


if __name__ == '__main__': unittest.main()
