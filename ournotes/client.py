"""Single-account gRPC client without automatic mutation retries.

A durable pending record is written before sending a mutation. Uncertain outcomes
remain pending and prevent subsequent writes, including after process restart.
"""
import re
import time
import uuid
from pathlib import Path

import grpc
from nnnotes.config import Config
from nnnotes.gameapi import channel_target, client_version, master_version

from .schema import Schema
from .storage import locked, read_json, write_json
from .transfer import generate_password, validate_password, profile_id

PLAYER = '/app.player.PlayerService/'
GACHA = '/app.gacha.GachaService/'
PRESENT = '/app.present.PresentService/'
BONUS = '/app.loginbonus.LoginBonusService/'
READ_METHODS = frozenset([PLAYER + 'Whoami', PLAYER + 'GetPlayerData', PRESENT + 'Fetch', PRESENT + 'History',
                          GACHA + 'History', GACHA + 'Probability', GACHA + 'CheckMaintenanceGacha',
                          '/app.masterdata.MasterdataService/Version'])
WRITE_METHODS = frozenset([PLAYER + 'Register', PLAYER + 'FinishTutorial', PLAYER + 'EditProfile',
                           PLAYER + 'RegisterPassword', PRESENT + 'Open', GACHA + 'Execute', BONUS + 'Update'])


class ApiError(RuntimeError):
    def __init__(self, method, status, game_code=None, *, uncertain=False):
        self.method, self.status, self.game_code, self.uncertain = method, status, game_code, uncertain
        message = f'{method.rsplit("/", 1)[-1]} failed: {status}'
        if game_code and re.fullmatch(r'[A-Z0-9_]{1,100}', game_code):
            message += f' ({game_code})'
        if uncertain:
            message += '; outcome unknown, pending operation preserved'
        super().__init__(message)


class PendingOperation(RuntimeError):
    pass


class Client:
    def __init__(self, config, schema, account, *, region='jp', timeout=20, channel=None):
        self.cfg = config if isinstance(config, Config) else Config.load(Path(config))
        self.schema = schema if isinstance(schema, Schema) else Schema(schema)
        self.account = Path(account)
        self.region, self.timeout = region, timeout
        self.target, tls = channel_target(self.cfg.require(f'servers.{region}', 'api'))
        # Bind credentials to the complete transport origin, not just a host name.
        self.origin = ('https://' if tls else 'http://') + self.target
        self.version = client_version(self.cfg, region)
        options = [('grpc.enable_retries', 0)]
        self.channel = channel or (grpc.secure_channel(self.target, grpc.ssl_channel_credentials(), options=options) if tls
                                   else grpc.insecure_channel(self.target, options=options))

    def close(self):
        self.channel.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def state(self):
        state = read_json(self.account) if self.account.exists() else {
            'format': 'ournotes.account/1', 'origin': self.origin, 'device_id': '',
            'device_bound': False,
            'credential': None, 'pending': None, 'completed': {}, 'master_version': '',
        }
        if state.get('format') != 'ournotes.account/1' or state.get('origin') != self.origin:
            raise ValueError('Account file format or API origin mismatch')
        if state.get('credential'):
            # Device IDs are server-issued. A newly registered player has none;
            # an invented UUID here makes even valid player credentials fail.
            state['device_id'] = state['credential'].get('device_id', '')
        return state

    def status(self):
        state = self.state()
        cred = state.get('credential') or {}
        return {'registered': bool(cred), 'player_id': cred.get('id'), 'profile_id': cred.get('profile_id'),
                'transfer_bound': state.get('transfer', {}).get('status') == 'bound',
                'pending': state.get('pending'), 'completed_steps': list(state['completed'])}

    def invoke(self, method, payload=None, *, operation=None):
        if method not in READ_METHODS | WRITE_METHODS:
            raise ValueError('RPC is outside the supported account operation set')
        payload = payload or {}
        request = self.schema.request(method, payload).SerializeToString()
        mutate = method in WRITE_METHODS
        with locked(self.account.with_suffix('.lock')):
            state = self.state()
            if operation and operation in state['completed']:
                record = state['completed'][operation]
                if record['method'] != method or record['payload'] != payload:
                    raise ValueError('Operation ID already belongs to a different request')
                return record['response']
            if mutate and state.get('pending'):
                raise PendingOperation('Unresolved write in account file; reconcile its result before continuing')
            if method == PLAYER + 'Register':
                if state['credential']:
                    raise ValueError('Account already registered; use login')
            elif not state['credential'] and method != '/app.masterdata.MasterdataService/Version':
                raise ValueError('Account is not registered or imported')
            if method == PLAYER + 'RegisterPassword':
                validate_password(payload.get('password'))
                transfer_id = profile_id(state)
            request_id = str(uuid.uuid4())
            headers = [('x-request-id', request_id), ('x-platform', 'android'),
                       ('x-client-version', self.version)]
            if state['device_id']:
                headers.append(('x-device-id', state['device_id']))
            if state['credential']:
                headers.extend([('x-player-id', state['credential']['id']),
                                ('x-player-credential', state['credential']['credential'])])
                # JP 1.0.4 SetDevice (0x6492B04) sets the override flag; the
                # first successful authenticated call initializes device state.
                if not state.get('device_bound', False):
                    headers.append(('x-override-device-id', '1'))
            if state.get('master_version'):
                headers.append(('x-master-version', state['master_version']))
            operation = operation or request_id
            if mutate:
                state['pending'] = {'operation': operation, 'method': method, 'request_id': request_id,
                                    'payload': payload, 'started_at': time.time()}
                write_json(self.account, state)
            try:
                raw = self.channel.unary_unary(method)(request, timeout=self.timeout, metadata=headers)
                result = self.schema.response(method, raw)
                if method == PLAYER + 'Register':
                    credential = result.get('credential', {})
                    if not credential.get('id') or not credential.get('credential'):
                        raise ValueError('Registration response lacks account credentials')
                    state['credential'] = credential
                    state['device_id'] = credential.get('device_id', '')
                if method == PLAYER + 'Whoami' and result.get('player_id') != state['credential']['id']:
                    raise ValueError('Whoami returned a different player ID')
                if method != PLAYER + 'Register' and state['credential'] and not state.get('device_bound', False):
                    state['device_bound'] = True
                    write_json(self.account, state)
                if mutate:
                    if method == PLAYER + 'RegisterPassword':
                        state['transfer'] = {'status': 'bound', 'profile_id': transfer_id,
                                             'password': payload['password'], 'bound_at': time.time(),
                                             'operation': operation}
                    state['completed'][operation] = {'method': method, 'payload': payload,
                                                     'response': result, 'completed_at': time.time()}
                    state['pending'] = None
                    write_json(self.account, state)
                if method == PLAYER + 'GetPlayerData':
                    state['last_player_data'] = result
                    write_json(self.account, state)
                return result
            except grpc.RpcError as error:
                # Do not assume any failed transport means a mutation was rolled back.
                code = error.code().name
                game = next((v for k, v in (error.trailing_metadata() or ()) if k == 'x-sirius-error-code'), None)
                raise ApiError(method, code, game, uncertain=mutate) from None

    def refresh_version(self):
        version = master_version(self.cfg, self.region).version
        with locked(self.account.with_suffix('.lock')):
            state = self.state()
            state['master_version'] = version
            write_json(self.account, state)
        return version

    def register(self, initial_data_group='', *, operation=None):
        return self.invoke(PLAYER + 'Register', {'initial_data_group': initial_data_group}, operation=operation)

    def login(self):
        return self.invoke(PLAYER + 'Whoami')

    def player_data(self):
        return self.invoke(PLAYER + 'GetPlayerData')

    def finish_tutorial(self, *, operation=None):
        return self.invoke(PLAYER + 'FinishTutorial', operation=operation)

    def login_bonus(self, *, operation=None):
        return self.invoke(BONUS + 'Update', operation=operation)

    def presents(self):
        return self.invoke(PRESENT + 'Fetch')

    def open_presents(self, ids, *, operation=None):
        if not ids:
            return {}
        return self.invoke(PRESENT + 'Open', {'present_ids': [str(i) for i in ids]}, operation=operation)

    def draw(self, gacha_id, product_id, selected_pick_up=(), *, operation=None):
        if int(gacha_id) <= 0 or int(product_id) <= 0:
            raise ValueError('gacha_id and product_id must be positive')
        return self.invoke(GACHA + 'Execute', {'gacha_id': str(gacha_id), 'product_id': str(product_id),
                                             'selected_pick_up': [str(i) for i in selected_pick_up]}, operation=operation)

    def bind_transfer(self, password=None):
        """Bind once, then export ID/password. Retry never replaces a saved password."""
        if password is not None:
            validate_password(password)
        with locked(self.account.with_suffix('.lock')):
            state = self.state()
            if state.get('pending'):
                raise PendingOperation('Unresolved write; transfer binding cannot proceed')
            existing = state.get('transfer')
            if existing:
                if password is not None and password != existing['password']:
                    raise ValueError('Transfer is already bound with a different password')
            else:
                profile_id(state)
                password = password or generate_password()
        if not existing:
            # invoke persists the generated password in pending BEFORE the RPC.
            # A completed RPC and its transfer record are saved in one replacement.
            self.invoke(PLAYER + 'RegisterPassword', {'password': password}, operation='bind_transfer')
        return self.export_transfer()

    def export_transfer(self):
        with locked(self.account.with_suffix('.lock')):
            state = self.state()
            transfer = state.get('transfer')
            if not transfer or transfer.get('status') != 'bound':
                raise ValueError('No successfully bound transfer password saved for this account')
            if state.get('pending', {}) and state['pending']['method'] == PLAYER + 'RegisterPassword':
                raise PendingOperation('Password update outcome is unknown; transfer export is blocked')
            path = self.account.with_suffix('.transfer.json')
            write_json(path, {'format': 'ournotes.transfer/1', 'account': self.account.stem, **transfer})
            return {'bound': True, 'profile_id': transfer['profile_id'], 'file': str(path)}

    def import_credential(self, credential):
        if not isinstance(credential, dict) or not all(
                isinstance(credential.get(k), str) and credential[k] for k in ('id', 'credential')):
            raise ValueError('Credential requires nonempty id and credential strings')
        if not isinstance(credential.get('device_id', ''), str):
            raise ValueError('device_id must be a server-issued string or omitted')
        with locked(self.account.with_suffix('.lock')):
            if self.account.exists():
                raise ValueError('Import requires a new account path')
            state = self.state()
            state['credential'] = credential
            state['device_id'] = credential.get('device_id', '')
            write_json(self.account, state)
