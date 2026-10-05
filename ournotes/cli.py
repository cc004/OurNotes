import argparse
import json
import sys
from pathlib import Path

from nnnotes.config import Config
from .client import Client, PLAYER, GACHA, ApiError, PendingOperation
from .schema import Schema
from .storage import read_json
from .workflow import plan_summary, run_workflow, validate_plan

ROOT = Path(__file__).resolve().parents[1]


def redact(value):
    if isinstance(value, dict):
        return {k: ('<redacted>' if k in ('credential', 'password', 'auth_key', 'id_token') and v else redact(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def emit(value):
    text = json.dumps(redact(value), ensure_ascii=False, indent=2)
    sys.stdout.buffer.write((text + '\n').encode('utf-8'))


def gacha_catalog(cfg):
    master = cfg.require_path(*cfg.master(cfg.region()))
    gachas = read_json(master / 'MasterGacha.json')['_allData']
    products = {int(p['_id']): p for p in read_json(master / 'MasterGachaProduct.json')['_allData']}
    result = []
    for row in gachas:
        ids = [row.get(f'_productId{i}', 0) for i in range(1, 5)]
        result.append({'id': row['_id'], 'name_text_id': row.get('_nameTextId'), 'start': row.get('_startAt'),
                       'end': row.get('_endAt'), 'products': [products[i] for i in ids if i in products]})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='OurNotes JP protocol client')
    parser.add_argument('--config', type=Path, default=ROOT / 'nnnotes.toml')
    parser.add_argument('--schema', type=Path, default=ROOT / 'work/protocol/descriptor.pb')
    parser.add_argument('--accounts', type=Path, default=ROOT / 'work/accounts')
    parser.add_argument('--account', default='default')
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ['status', 'version', 'login', 'data', 'finish-tutorial', 'login-bonus', 'presents', 'gacha-list', 'gacha-history', 'export-transfer']:
        sub.add_parser(command)
    transfer = sub.add_parser('bind-transfer', help='bind a generated password and export player ID/password')
    transfer.add_argument('--password-file', type=Path, help='optional UTF-8 text file; otherwise generate a unique password')
    register = sub.add_parser('register')
    register.add_argument('--initial-data-group', default='')
    imp = sub.add_parser('import-credential')
    imp.add_argument('file', type=Path)
    edit = sub.add_parser('name')
    edit.add_argument('name')
    open_presents = sub.add_parser('open-presents')
    open_presents.add_argument('ids', nargs='+', type=int)
    draw = sub.add_parser('draw')
    draw.add_argument('--gacha-id', type=int, required=True)
    draw.add_argument('--product-id', type=int, required=True)
    draw.add_argument('--pick-up', type=int, nargs='*', default=[])
    methods = sub.add_parser('methods')
    methods.add_argument('--contains', default='')
    inspect = sub.add_parser('describe')
    inspect.add_argument('method')
    workflow = sub.add_parser('workflow')
    workflow.add_argument('file', type=Path)
    workflow.add_argument('--job', required=True)
    workflow.add_argument('--execute', action='store_true', help='run the plan; otherwise only describe it')
    args = parser.parse_args(argv)
    cfg = Config.load(args.config)
    if args.command == 'gacha-list':
        emit(gacha_catalog(cfg)); return
    if args.command == 'workflow':
        plan = validate_plan(read_json(args.file))
        # Validate the chosen products against the local master before creating accounts.
        valid = {(int(g['id']), int(p['_id'])) for g in gacha_catalog(cfg) for p in g['products']}
        for d in plan['draws']:
            if (d['gacha_id'], d['product_id']) not in valid:
                raise ValueError('Gacha/product combination absent from local master; refresh master or correct the plan')
        if not args.execute:
            emit(plan_summary(plan)); return
        schema = Schema(args.schema)
        result = run_workflow(plan, args.job, args.accounts,
                              lambda path: Client(cfg, schema, path), progress=emit)
        emit(result); return
    schema = Schema(args.schema)
    if args.command == 'methods':
        emit([m for m in sorted(schema.methods) if args.contains in m]); return
    if args.command == 'describe':
        emit(schema.describe(args.method)); return
    import re
    if not re.fullmatch('[A-Za-z0-9_-]{1,64}', args.account):
        raise ValueError('Invalid account name')
    with Client(cfg, schema, args.accounts / (args.account + '.json')) as client:
        command = args.command
        if command == 'status': result = client.status()
        elif command == 'version': result = {'master_version': client.refresh_version()}
        elif command == 'import-credential':
            client.import_credential(read_json(args.file)); result = client.status()
        elif command == 'register':
            client.refresh_version(); result = client.register(args.initial_data_group)
        elif command == 'login':
            client.refresh_version(); result = client.login()
        elif command == 'data': result = client.player_data()
        elif command == 'finish-tutorial': result = client.finish_tutorial()
        elif command == 'login-bonus': result = client.login_bonus()
        elif command == 'presents': result = client.presents()
        elif command == 'open-presents': result = client.open_presents(args.ids)
        elif command == 'name': result = client.invoke(PLAYER + 'EditProfile', {'name': args.name})
        elif command == 'draw': result = client.draw(args.gacha_id, args.product_id, args.pick_up)
        elif command == 'gacha-history': result = client.invoke(GACHA + 'History')
        elif command == 'bind-transfer':
            password = args.password_file.read_text(encoding='utf-8-sig').rstrip('\r\n') if args.password_file else None
            client.refresh_version()
            client.login()
            client.player_data()
            result = client.bind_transfer(password)
        elif command == 'export-transfer': result = client.export_transfer()
        emit(result)


def entrypoint():
    try:
        main()
    except (ApiError, PendingOperation, ValueError, FileNotFoundError, RuntimeError) as error:
        print(f'ournotes: {error}', file=sys.stderr)
        return 2
    return 0
