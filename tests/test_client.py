import json
import tempfile
import unittest
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import grpc
from google.protobuf import descriptor_pb2, message_factory, json_format
from nnnotes.config import Config

from ournotes.client import Client, ApiError, PendingOperation, PLAYER, GACHA, PRESENT, BONUS
from ournotes.schema import Schema
from ournotes.storage import locked, read_json, write_json
from ournotes.workflow import run_workflow, validate_plan
from ournotes.cli import redact


def synthetic_schema(path):
    """Only a small synthetic contract; no APK/proprietary fixture required."""
    fds = descriptor_pb2.FileDescriptorSet()
    entity = fds.file.add(name='entity.proto', package='entity', syntax='proto3')
    def message(file, name, fields):
        m = file.message_type.add(name=name)
        for i, (key, kind, repeated) in enumerate(fields, 1):
            field = m.field.add(name=key, number=i, label=3 if repeated else 1)
            if isinstance(kind, str):
                field.type, field.type_name = 11, kind
            else:
                field.type = kind
        return m
    message(entity, 'Credential', [('id', 9, False), ('credential', 9, False), ('device_id', 9, False), ('profile_id', 3, False)])
    message(entity, 'Card', [('master_id', 3, False)])
    message(entity, 'Gem', [('free', 5, False)])
    message(entity, 'Profile', [('profile_id', 3, False)])
    message(entity, 'PlayerData', [('member_cards', '.entity.Card', True), ('gem', '.entity.Gem', False),
                                 ('has_password', 8, False), ('my_profile', '.entity.Profile', False)])
    message(entity, 'Present', [('present_id', 4, False)])
    specs = {
        PLAYER: {
            'Register': ([('initial_data_group', 9, False)], [('credential', '.entity.Credential', False)]),
            'Whoami': ([], [('player_id', 9, False)]),
            'GetPlayerData': ([], [('player_data', '.entity.PlayerData', False)]),
            'FinishTutorial': ([], []), 'EditProfile': ([('name', 9, False)], []),
            'RegisterPassword': ([('password', 9, False)], []),
        },
        GACHA: {'Execute': ([('gacha_id', 3, False), ('product_id', 3, False), ('selected_pick_up', 3, True)],
                            [('prize_id', 3, False)])},
        PRESENT: {'Fetch': ([], [('presents', '.entity.Present', True)]),
                  'Open': ([('present_ids', 4, True)], [])},
        BONUS: {'Update': ([], [])},
    }
    for prefix, methods in specs.items():
        full = prefix.strip('/')
        package, service_name = full.rsplit('.', 1)
        f = fds.file.add(name=package + '.proto', package=package, syntax='proto3')
        f.dependency.append('entity.proto')
        service = f.service.add(name=service_name)
        for name, (request, response) in methods.items():
            message(f, name + 'Request', request)
            message(f, name + 'Response', response)
            service.method.add(name=name, input_type=f'.{package}.{name}Request', output_type=f'.{package}.{name}Response')
    path.write_bytes(fds.SerializeToString())
    return Schema(path)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.schema = synthetic_schema(self.root / 'schema.pb')
        self.calls, self.accounts, self.fail = [], {}, None
        self.bound_devices = {}
        self.passwords = {}
        self.server = grpc.server(ThreadPoolExecutor(max_workers=3))
        groups = {}
        for path, method in self.schema.methods.items():
            service, name = path.strip('/').split('/')
            def handler(raw, context, path=path, descriptor=method):
                headers = dict(context.invocation_metadata())
                request = message_factory.GetMessageClass(descriptor.input_type).FromString(raw)
                data = json_format.MessageToDict(request, preserving_proto_field_name=True)
                self.calls.append((path, data, headers))
                if self.fail == path:
                    context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, 'synthetic uncertainty')
                result = {}
                if path == PLAYER + 'Register':
                    ident = 'player-' + str(len(self.accounts) + 1)
                    self.accounts[ident] = []
                    result = {'credential': {'id': ident, 'credential': 'secret-' + ident, 'profile_id': str(1000 + len(self.accounts))}}
                else:
                    ident = headers.get('x-player-id')
                    if headers.get('x-player-credential') != 'secret-' + str(ident):
                        context.abort(grpc.StatusCode.UNAUTHENTICATED, 'bad credential')
                    if ident not in self.bound_devices:
                        if headers.get('x-override-device-id') != '1':
                            context.abort(grpc.StatusCode.UNKNOWN, 'authentication failed')
                        self.bound_devices[ident] = headers.get('x-device-id', '')
                    if headers.get('x-device-id', '') != self.bound_devices[ident]:
                        context.abort(grpc.StatusCode.UNKNOWN, 'authentication failed')
                    if path == PLAYER + 'Whoami': result = {'player_id': ident}
                    if path == PLAYER + 'GetPlayerData':
                        result = {'player_data': {'member_cards': [{'master_id': i} for i in self.accounts[ident]],
                                  'gem': {'free': 1200}, 'has_password': ident in self.passwords,
                                  'my_profile': {'profile_id': str(1000 + int(ident.split('-')[1]))}}}
                    if path == PLAYER + 'RegisterPassword':
                        self.passwords[ident] = data['password']
                    if path == PRESENT + 'Fetch': result = {'presents': [{'present_id': '7'}]}
                    if path == GACHA + 'Execute':
                        self.accounts[ident].append('42')
                        result = {'prize_id': '42'}
                response = message_factory.GetMessageClass(descriptor.output_type)()
                return json_format.ParseDict(result, response).SerializeToString()
            groups.setdefault(service, {})[name] = grpc.unary_unary_rpc_method_handler(handler)
        self.server.add_generic_rpc_handlers([grpc.method_handlers_generic_handler(s, m) for s, m in groups.items()])
        self.port = self.server.add_insecure_port('127.0.0.1:0')
        self.server.start()
        self.cfg = Config({'servers': {'jp': {'api': f'http://127.0.0.1:{self.port}', 'client_version': '1.0.4'}}})
        self.version_patch = patch('ournotes.client.master_version', return_value=SimpleNamespace(version='test-master'))
        self.version_patch.start()

    def tearDown(self):
        self.version_patch.stop()
        self.server.stop(0).wait()
        self.temp.cleanup()

    def client(self, path=None):
        return Client(self.cfg, self.schema, path or self.root / 'account.json', timeout=1)

    def test_registration_login_and_authenticated_draw(self):
        with self.client() as c:
            c.refresh_version()
            c.register(operation='create')
            self.assertEqual(c.login()['player_id'], 'player-1')
            c.finish_tutorial()
            c.login_bonus()
            c.open_presents(['7'])
            c.draw(10, 20, [30], operation='one-draw')
            c.draw(10, 20, [30], operation='one-draw')
            data = c.player_data()
            self.assertEqual(data['player_data']['member_cards'][0]['master_id'], '42')
        with self.client() as c:
            self.assertEqual(c.login()['player_id'], 'player-1')
            c.draw(10, 20, [30], operation='one-draw')
        draws = [x for x in self.calls if x[0] == GACHA + 'Execute']
        self.assertEqual(len(draws), 1)
        self.assertEqual(draws[0][1]['selected_pick_up'], ['30'])
        self.assertEqual(draws[0][2]['x-master-version'], 'test-master')
        logins = [headers for path, _, headers in self.calls if path == PLAYER + 'Whoami']
        self.assertEqual(logins[0]['x-override-device-id'], '1')
        self.assertNotIn('x-device-id', logins[0])
        self.assertNotIn('x-override-device-id', logins[-1])
        self.assertNotIn('secret-player', json.dumps(redact(read_json(self.root / 'account.json'))))

    def test_unknown_outcome_is_not_replayed_after_restart(self):
        with self.client() as c:
            c.register()
            self.fail = GACHA + 'Execute'
            with self.assertRaises(ApiError): c.draw(10, 20, operation='uncertain')
        self.fail = None
        with self.client() as c:
            with self.assertRaises(PendingOperation): c.draw(10, 20, operation='uncertain')
            with self.assertRaises(PendingOperation): c.finish_tutorial()
            c.login()  # Reads remain usable for reconciliation.
        self.assertEqual(sum(p == GACHA + 'Execute' for p, _, _ in self.calls), 1)

    def test_server_issued_empty_device_id_overrides_legacy_random_uuid(self):
        with self.client() as c:
            c.register()
        path = self.root / 'account.json'
        state = read_json(path)
        state['device_id'] = 'legacy-invented-device'
        state.pop('device_bound', None)
        write_json(path, state)
        with self.client() as c:
            c.login()
        self.assertNotIn('x-device-id', self.calls[-1][2])
        self.assertEqual(read_json(path)['device_id'], '')

    def test_import_accepts_credentials_without_server_device(self):
        with self.client() as c:
            credential = c.register()['credential']
        with self.client(self.root / 'imported.json') as c:
            c.import_credential(credential)
            self.assertEqual(c.login()['player_id'], credential['id'])
        self.assertNotIn('x-device-id', self.calls[-1][2])

    def test_invalid_requests_and_origin_do_not_send_credentials(self):
        with self.client() as c:
            with self.assertRaises(Exception): c.invoke(PLAYER + 'Register', {'unknown': 1})
            self.assertFalse(self.calls)
            c.register()
            with self.assertRaises(ValueError): c.register()
            with self.assertRaises(ValueError): c.invoke('/app.debug.DebugService/Grant', {})
        cfg = Config({'servers': {'jp': {'api': 'http://127.0.0.1:9', 'client_version': '1.0.4'}}})
        with Client(cfg, self.schema, self.root / 'account.json') as c:
            with self.assertRaises(ValueError): c.login()

    def test_account_lock(self):
        with locked(self.root / 'account.lock'):
            with self.client() as c:
                with self.assertRaises(RuntimeError): c.register()
        self.assertFalse(self.calls)

    def test_batch_stops_on_match_and_resume_does_not_repeat(self):
        plan = {'max_accounts': 3, 'draws': [{'gacha_id': 10, 'product_id': 20, 'times': 2}],
                'target_card_ids': [42], 'account_delay_seconds': 0}
        result = run_workflow(plan, 'test', self.root / 'accounts', self.client)
        self.assertEqual(result['status'], 'matched')
        self.assertEqual(len(result['accounts']), 1)
        before = len(self.calls)
        resumed = run_workflow(plan, 'test', self.root / 'accounts', self.client)
        self.assertEqual(resumed['status'], 'matched')
        self.assertEqual(len(self.calls), before)
        self.assertEqual(sum(p == GACHA + 'Execute' for p, _, _ in self.calls), 1)

    def test_batch_respects_account_and_draw_limits(self):
        plan = {'max_accounts': 2, 'draws': [{'gacha_id': 10, 'product_id': 20, 'times': 2}],
                'account_delay_seconds': 0}
        result = run_workflow(plan, 'two', self.root / 'accounts', self.client)
        self.assertEqual(result['status'], 'complete')
        count = Counter(p for p, _, _ in self.calls)
        self.assertEqual(count[PLAYER + 'Register'], 2)
        self.assertEqual(count[GACHA + 'Execute'], 4)
        with self.assertRaises(ValueError):
            run_workflow({**plan, 'max_accounts': 3}, 'two', self.root / 'accounts', self.client)

    def test_transfer_binds_after_draw_before_matched_stop(self):
        plan = {'max_accounts': 3, 'draws': [{'gacha_id': 10, 'product_id': 20, 'times': 2}],
                'target_card_ids': [42], 'bind_transfer': True, 'account_delay_seconds': 0}
        result = run_workflow(plan, 'transfer', self.root / 'accounts', self.client)
        self.assertEqual(result['status'], 'matched')
        self.assertTrue(result['accounts'][0]['transfer']['bound'])
        mutations = [p for p, _, _ in self.calls if p in (GACHA + 'Execute', PLAYER + 'RegisterPassword')]
        self.assertEqual(mutations, [GACHA + 'Execute', PLAYER + 'RegisterPassword'])
        artifact = read_json(self.root / 'accounts/transfer_0001.transfer.json')
        self.assertEqual(artifact['profile_id'], '1001')
        self.assertEqual(artifact['password'], self.passwords['player-1'])
        self.assertEqual(len(artifact['password']), 16)
        self.assertNotIn(artifact['password'], json.dumps(result))
        count = len(self.calls)
        run_workflow(plan, 'transfer', self.root / 'accounts', self.client)
        self.assertEqual(len(self.calls), count)

    def test_transfer_reexport_after_restart_does_not_reset_password(self):
        with self.client() as c:
            c.register()
            c.bind_transfer('Abc12345!')
        path = self.root / 'account.transfer.json'
        path.unlink()  # Simulate crash before export; the account success record survives.
        count = len(self.calls)
        with self.client() as c:
            c.bind_transfer()
            self.assertEqual(read_json(path)['password'], 'Abc12345!')
            with self.assertRaises(ValueError): c.bind_transfer('Different123!')
        self.assertEqual(len(self.calls), count)
        self.assertNotIn('Abc12345!', json.dumps(redact(read_json(self.root / 'account.json'))))

    def test_failed_transfer_stops_batch_and_preserves_pending_password(self):
        self.fail = PLAYER + 'RegisterPassword'
        plan = {'max_accounts': 2, 'bind_transfer': True, 'account_delay_seconds': 0,
                'draws': [{'gacha_id': 10, 'product_id': 20, 'times': 1}]}
        with self.assertRaises(ApiError): run_workflow(plan, 'failed_bind', self.root / 'accounts', self.client)
        account = self.root / 'accounts/failed_bind_0001.json'
        saved = read_json(account)['pending']['payload']['password']
        self.assertFalse(account.with_suffix('.transfer.json').exists())
        self.fail = None
        with self.assertRaises(PendingOperation): run_workflow(plan, 'failed_bind', self.root / 'accounts', self.client)
        self.assertEqual(read_json(account)['pending']['payload']['password'], saved)
        count = Counter(p for p, _, _ in self.calls)
        self.assertEqual(count[PLAYER + 'Register'], 1)
        self.assertEqual(count[GACHA + 'Execute'], 1)
        self.assertEqual(count[PLAYER + 'RegisterPassword'], 1)

    def test_transfer_profile_fallback_and_validation(self):
        with self.client() as c:
            credential = c.register()['credential']
        credential.pop('profile_id')
        with self.client(self.root / 'imported.json') as c:
            c.import_credential(credential)
            count = len(self.calls)
            with self.assertRaises(ValueError): c.bind_transfer()
            for password in ['', 'short', 'spaces are bad', 'x' * 17]:
                with self.assertRaises(ValueError): c.bind_transfer(password)
            self.assertEqual(len(self.calls), count)
            c.player_data()
            self.assertEqual(c.bind_transfer()['profile_id'], '1001')

    def test_legacy_plan_can_resume_with_disabled_transfer(self):
        import hashlib
        plan = {'max_accounts': 1}
        result = run_workflow(plan, 'legacy', self.root / 'accounts', self.client)
        result['plan'].pop('bind_transfer')
        result['plan_sha256'] = hashlib.sha256(json.dumps(result['plan'], sort_keys=True).encode()).hexdigest()
        write_json(self.root / 'accounts/jobs/legacy.json', result)
        count = len(self.calls)
        run_workflow(plan, 'legacy', self.root / 'accounts', self.client)
        self.assertEqual(len(self.calls), count)
        with self.assertRaises(ValueError):
            run_workflow({**plan, 'bind_transfer': True}, 'legacy', self.root / 'accounts', self.client)

    def test_resume_between_account_match_and_job_completion(self):
        plan = {'max_accounts': 3, 'draws': [{'gacha_id': 10, 'product_id': 20, 'times': 1}],
                'target_card_ids': [42], 'account_delay_seconds': 0}
        directory = self.root / 'accounts'
        state = run_workflow(plan, 'crash', directory, self.client)
        state['status'] = 'running'  # Process died before saving the job-level match.
        write_json(directory / 'jobs/crash.json', state)
        before = len(self.calls)
        resumed = run_workflow(plan, 'crash', directory, self.client)
        self.assertEqual(resumed['status'], 'matched')
        self.assertEqual(len(resumed['accounts']), 1)
        self.assertEqual(len(self.calls), before)

    def test_failed_registration_preserves_identity_and_stops_batch(self):
        self.fail = PLAYER + 'Register'
        plan = {'max_accounts': 3}
        with self.assertRaises(ApiError): run_workflow(plan, 'fail', self.root / 'accounts', self.client)
        state = read_json(self.root / 'accounts/fail_0001.json')
        self.assertTrue(state['pending'])
        with self.assertRaises(PendingOperation): run_workflow(plan, 'fail', self.root / 'accounts', self.client)
        self.assertEqual(len(self.calls), 1)

    def test_plan_rejects_unbounded_and_malformed_operations(self):
        for plan in [{}, {'max_accounts': True}, {'max_accounts': 0}, {'max_accounts': 1, 'draws': [{'times': -1}]},
                     {'max_accounts': 1, 'target_card_ids': ['42']}, {'max_accounts': 1, 'typo': True},
                     {'max_accounts': 1, 'bind_transfer': 'yes'}]:
            with self.assertRaises(ValueError): validate_plan(plan)


if __name__ == '__main__':
    unittest.main()
