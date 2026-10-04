#!/usr/bin/env python3
"""Real public-tool regressions for typing ownership and native select contracts.

Uses only disposable fixtures. No evaluator state is changed outside page handlers.
SC_E2E_APP=build/SecondCursor.app python3 e2e/browser_target_contracts.py
"""
import re
import sys

from run import FixtureServer, Run, SCENARIOS, ToolError, scenario

SCENARIOS.clear()


def open_fixture(ctx, base):
    tab = ctx.call('browser_open', url=base + '/browser-targets.html')['tab']['id']
    ctx.cleanups.append(lambda: ctx.mcp.call('browser_close', tab_id=tab))
    return tab


def command(ctx, tab, operation, **args):
    return ctx.call('browser_command', tab_id=tab, operation=operation, **args)


def inspect(ctx, tab, selector):
    return command(ctx, tab, 'inspect', locator={'selector': selector})


def outline(ctx, tab):
    return ctx.call('browser_snapshot', tab_id=tab, full=True)['outline']


def ref(text, label, role='textbox'):
    match = re.search(r'\[([^\]]+)\] ' + re.escape(role) + r' "' + re.escape(label) + r'"', text)
    if not match:
        raise AssertionError(f'Missing {role} {label!r}: {text[:1600]}')
    return match[1]


def refusal(ctx, tool, **args):
    try:
        ctx.call(tool, **args)
    except ToolError as exc:
        return str(exc)
    return None


@scenario
def browser_typing_refuses_wrong_targets(ctx):
    with FixtureServer() as base:
        tab = open_fixture(ctx, base)
        for label, role, selector in [('Route', 'combobox', '#route'), ('Read only', 'textbox', '#readonly'),
                                      ('Disabled', 'textbox', '#disabled'), ('Redirected', 'textbox', '#redirected'),
                                      ('Replaced', 'textbox', '#replaced')]:
            command(ctx, tab, 'click', locator={'selector': '#name'})
            before = inspect(ctx, tab, selector).get('text')
            error = refusal(ctx, 'browser_type', tab_id=tab, ref=ref(outline(ctx, tab), label, role), text='MUST-NOT-INSERT')
            ctx.check(label + ' refuses before text dispatch', bool(error), error)
            after = inspect(ctx, tab, selector).get('text')
            ctx.check(label + ' preserves the intended target', after == before, {'before': before, 'after': after})
            ctx.check(label + ' never types into the previous Name field',
                      inspect(ctx, tab, '#name').get('text') == 'Ada Lovelace', inspect(ctx, tab, '#name'))
        command(ctx, tab, 'click', locator={'selector': '#button'})
        error = refusal(ctx, 'browser_type', tab_id=tab, text='MUST-NOT-INSERT')
        ctx.check('typing without a ref rejects a focused noneditable button', bool(error), error)
        ctx.check('all rejected calls emit no input event', inspect(ctx, tab, '#events').get('text') == 'No input events')


@scenario
def browser_typing_keeps_supported_editors(ctx):
    with FixtureServer() as base:
        tab = open_fixture(ctx, base)
        for label, role, selector, text in [('Name', 'textbox', '#name', ' Lovelace'),
                                           ('Search route', 'combobox', '#search', 'Priority'),
                                           ('Notes', 'textbox', '#notes', 'Line one\nLine two'),
                                           ('Editable note', 'textbox', '#editable', ' updated')]:
            before = inspect(ctx, tab, selector).get('text', '')
            result = ctx.call('browser_type', tab_id=tab, ref=ref(outline(ctx, tab), label, role), text=text)
            after = inspect(ctx, tab, selector).get('text', '')
            # Chrome preserves leading spaces in contenteditable text as NBSP.
            ctx.check(label + ' accepts text on its editable target', text in after.replace('\xa0', ' ') and after != before,
                      {'before': before, 'after': after, 'outcome': result.get('outcome')})
        command(ctx, tab, 'click', locator={'selector': '#name'})
        ctx.call('browser_type', tab_id=tab, text=' no-ref')
        ctx.check('typing without a ref accepts a known editable focus', ' no-ref' in inspect(ctx, tab, '#name').get('text', ''))
        ctx.call('browser_press_key', tab_id=tab, keys='Cmd+A')
        ctx.call('browser_type', tab_id=tab, text='Selected all')
        ctx.check('uppercase Cmd+A selects all without adding Shift', inspect(ctx, tab, '#name').get('text') == 'Selected all')
        for label in ['Open shadow input', 'Closed shadow input']:
            ctx.call('browser_type', tab_id=tab, ref=ref(outline(ctx, tab), label), text='shadow-ok')
            ctx.check(label + ' receives its own text', re.search(re.escape(label) + r'"[^\n]*value="shadow-ok"', outline(ctx, tab)))
        for title in ['Local input frame', 'Remote input frame']:
            state = outline(ctx, tab)
            section = state.split('iframe "' + title + '"', 1)[1].split('iframe "', 1)[0]
            target = ref(section, 'Frame input')
            if title.startswith('Remote'):
                ctx.require('remote input uses a frame-scoped ref', bool(re.match(r'f\d+e\d+', target)), target)
            ctx.call('browser_type', tab_id=tab, ref=target, text='frame-ok')
            state = outline(ctx, tab).split('iframe "' + title + '"', 1)[1].split('iframe "', 1)[0]
            ctx.check(title + ' receives its own text', 'Frame received: frame-ok' in state, state)


@scenario
def browser_typing_withholds_submit_after_focus_changes(ctx):
    with FixtureServer() as base:
        tab = open_fixture(ctx, base)
        error = refusal(ctx, 'browser_type', tab_id=tab, ref=ref(outline(ctx, tab), 'Submit redirect'),
                        text='inserted', submit=True)
        ctx.check('submit refusal reports the text that was already inserted',
                  bool(error) and 'Text was inserted, but Enter was not sent' in error, error)
        ctx.check('text reaches the original target', inspect(ctx, tab, '#submit-redirect').get('text') == 'inserted')
        ctx.check('redirected focus never receives Enter', inspect(ctx, tab, '#keys').get('text') == 'No Enter key')
        ctx.check('redirected focus receives no extra text', inspect(ctx, tab, '#name').get('text') == 'Ada Lovelace')


@scenario
def browser_select_options_are_discoverable_and_exact(ctx):
    with FixtureServer() as base:
        tab = open_fixture(ctx, base)
        route_ref = ref(outline(ctx, tab), 'Route', 'combobox')
        for operation in ['select_option', 'check']:
            error = refusal(ctx, 'browser_command', tab_id=tab, operation=operation,
                            ref=route_ref, value='Priority', checked=True)
            ctx.check(operation + ' explains that locator is required instead of ref',
                      bool(error) and 'require locator' in error and 'top-level ref is unsupported' in error, error)
        ctx.check('malformed selection calls cause no input event', inspect(ctx, tab, '#events').get('text') == 'No input events')
        detail = inspect(ctx, tab, '#route')
        options = detail.get('options', [])
        ctx.check('inspect exposes native option labels and values',
                  any(o.get('label') == 'Priority' and o.get('value') == 'priority' for o in options), detail)
        ctx.check('inspect exposes disabled options',
                  any(o.get('value') == 'closed' and o.get('disabled') is True for o in options), detail)
        tree = command(ctx, tab, 'dom_snapshot')
        def walk(rows):
            for row in rows:
                yield row
                yield from walk(row.get('children', []))
        ctx.check('DOM snapshot exposes select option mappings',
                  any(any(o.get('value') == 'priority' and o.get('label') == 'Priority' for o in row.get('options', []))
                      for row in walk(tree.get('elements', []))), tree)
        selected = command(ctx, tab, 'select_option', locator={'selector': '#route'}, value='Priority')
        ctx.check('ordinary selection accepts a unique label and verifies the actual value',
                  selected.get('outcome', {}).get('verified') is True and inspect(ctx, tab, '#route').get('text') == 'priority', selected)
        for selector, value in [('#route', 'Closed'), ('#ambiguous', 'Same'), ('#grouped', 'Closed group')]:
            before = inspect(ctx, tab, selector).get('text')
            error = refusal(ctx, 'browser_command', tab_id=tab, operation='select_option', locator={'selector': selector}, value=value)
            ctx.check(selector + ' rejects disabled or ambiguous selection before changing it',
                      bool(error) and inspect(ctx, tab, selector).get('text') == before, error)
        command(ctx, tab, 'select_option', locator={'selector': '#precedence'}, value='Express')
        ctx.check('an exact value takes precedence over another option label', inspect(ctx, tab, '#precedence').get('text') == 'Express')
        selected = command(ctx, tab, 'select_option', locator={'selector': '#multiple'}, value=['Alpha', 'Beta'])
        ctx.check('multiple selection resolves each unique label', selected.get('outcome', {}).get('actual') == ['a', 'b'], selected)


if __name__ == '__main__':
    sys.exit(0 if Run(browser=True).run(sys.argv[1:]) else 1)
