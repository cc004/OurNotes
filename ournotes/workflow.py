"""Bounded, sequential new-account workflows with durable operation IDs."""
import hashlib
import json
import re
import time
from pathlib import Path

from .client import Client, PLAYER
from .storage import locked, read_json, write_json


def validate_plan(plan):
    allowed = {'max_accounts', 'initial_data_group', 'nickname', 'finish_tutorial', 'login_bonus',
               'claim_presents', 'draws', 'target_card_ids', 'stop_on_match', 'account_delay_seconds', 'bind_transfer'}
    if not isinstance(plan, dict) or set(plan) - allowed:
        raise ValueError('Unknown workflow options')
    plan = {'initial_data_group': '', 'nickname': '', 'finish_tutorial': True, 'login_bonus': True,
            'claim_presents': True, 'draws': [], 'target_card_ids': [], 'stop_on_match': True,
            'account_delay_seconds': 3, 'bind_transfer': False, **plan}
    if type(plan.get('max_accounts')) is not int or not 1 <= plan['max_accounts'] <= 10000:
        raise ValueError('max_accounts must be an integer between 1 and 10000')
    for key in ['finish_tutorial', 'login_bonus', 'claim_presents', 'stop_on_match', 'bind_transfer']:
        if type(plan[key]) is not bool:
            raise ValueError(f'{key} must be boolean')
    if any(not isinstance(plan[k], str) for k in ['initial_data_group', 'nickname']):
        raise ValueError('initial_data_group and nickname must be strings')
    delay = plan['account_delay_seconds']
    if type(delay) not in (int, float) or not 0 <= delay <= 3600:
        raise ValueError('account_delay_seconds must be between 0 and 3600')
    if not isinstance(plan['target_card_ids'], list) or any(type(v) is not int or v <= 0 for v in plan['target_card_ids']):
        raise ValueError('target_card_ids must contain positive integer master card IDs')
    if not isinstance(plan['draws'], list):
        raise ValueError('draws must be a list')
    for draw in plan['draws']:
        if not isinstance(draw, dict) or set(draw) - {'gacha_id', 'product_id', 'times', 'selected_pick_up'}:
            raise ValueError('Invalid draw specification')
        for key in ['gacha_id', 'product_id', 'times']:
            if type(draw.get(key)) is not int or draw[key] <= 0:
                raise ValueError(f'draw.{key} must be a positive integer')
        if draw['times'] > 1000:
            raise ValueError('A draw step supports at most 1000 executions')
        if not isinstance(draw.get('selected_pick_up', []), list) or any(
                type(v) is not int or v <= 0 for v in draw.get('selected_pick_up', [])):
            raise ValueError('selected_pick_up must be a list of positive integers')
    return plan


def plan_summary(plan):
    plan = validate_plan(plan)
    return {'max_accounts': plan['max_accounts'], 'gacha_rpc_calls_per_account': sum(d['times'] for d in plan['draws']),
            'maximum_gacha_rpc_calls': plan['max_accounts'] * sum(d['times'] for d in plan['draws']),
            'target_card_ids': plan['target_card_ids'], 'plan': plan}


def run_workflow(plan, job, directory, make_client, *, sleep=time.sleep, progress=lambda value: None):
    """make_client(account_path) returns a Client; dependency injection enables local tests."""
    plan = validate_plan(plan)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', job):
        raise ValueError('job must be 1-64 letters, digits, underscores or dashes')
    directory = Path(directory)
    manifest = directory / 'jobs' / (job + '.json')
    digest = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    with locked(manifest.with_suffix('.lock')):
        state = read_json(manifest) if manifest.exists() else {
            'format': 'ournotes.workflow/1', 'plan_sha256': digest, 'plan': plan, 'accounts': [], 'status': 'running'}
        if state.get('plan_sha256') != digest:
            # Adding an optional disabled step must not invalidate older jobs.
            if validate_plan(state['plan']) != plan:
                raise ValueError('Existing job uses a different plan; choose another job name')
            state['plan'], state['plan_sha256'] = plan, digest
            write_json(manifest, state)
        if state['status'] in ('matched', 'complete'):
            return state
        for index in range(plan['max_accounts']):
            if index == len(state['accounts']):
                state['accounts'].append({'account': f'{job}_{index + 1:04d}', 'status': 'running'})
            record = state['accounts'][index]
            if record['status'] == 'matched' and plan['stop_on_match']:
                state['status'] = 'matched'
                write_json(manifest, state)
                return state
            if record['status'] in ('complete', 'matched'):
                continue
            account = directory / (record['account'] + '.json')
            record['status'] = 'running'
            write_json(manifest, state)
            try:
                with make_client(account) as client:
                    client.refresh_version()
                    if not client.status()['registered']:
                        client.register(plan['initial_data_group'], operation='register')
                    client.login()
                    if plan['nickname']:
                        client.invoke(PLAYER + 'EditProfile', {'name': plan['nickname']}, operation='nickname')
                    if plan['finish_tutorial']:
                        client.finish_tutorial(operation='finish_tutorial')
                    if plan['login_bonus']:
                        client.login_bonus(operation='login_bonus')
                    if plan['claim_presents']:
                        if 'present_ids' not in record:
                            record['present_ids'] = [p['present_id'] for p in client.presents().get('presents', [])]
                            write_json(manifest, state)
                        client.open_presents(record['present_ids'], operation='open_presents')

                    def inspect():
                        data = client.player_data().get('player_data', {})
                        cards = sorted({int(c['master_id']) for c in data.get('member_cards', [])})
                        record['card_ids'] = cards
                        record['gem'] = data.get('gem', {})
                        record['matched_card_ids'] = sorted(set(cards) & set(plan['target_card_ids']))
                        write_json(manifest, state)
                        return bool(record['matched_card_ids'])

                    matched = inspect()
                    for step, draw in enumerate(plan['draws']):
                        for repeat in range(draw['times']):
                            if matched and plan['stop_on_match']:
                                break
                            client.draw(draw['gacha_id'], draw['product_id'], draw.get('selected_pick_up', []),
                                        operation=f'draw:{step}:{repeat}')
                            matched = inspect()
                    if plan['bind_transfer']:
                        record['transfer'] = client.bind_transfer()
                    record['status'] = 'matched' if matched else 'complete'
                    record.pop('error', None)
                    write_json(manifest, state)
                    progress({'account': record['account'], 'status': record['status'],
                              'matched_card_ids': record['matched_card_ids']})
                    if matched and plan['stop_on_match']:
                        state['status'] = 'matched'
                        write_json(manifest, state)
                        return state
            except Exception as error:
                record['status'] = 'failed'
                # Do not serialize arbitrary exception strings that may include tokens.
                record['error'] = type(error).__name__
                state['status'] = 'stopped'
                write_json(manifest, state)
                raise
            if index + 1 < plan['max_accounts']:
                sleep(plan['account_delay_seconds'])
        state['status'] = 'complete'
        write_json(manifest, state)
        return state
