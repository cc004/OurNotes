"""Idempotent local pipeline: verify/extract keys, build dumper, recover protocol."""
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run(*args):
    subprocess.run([str(a) for a in args], cwd=ROOT, check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--refresh-master', action='store_true')
    parser.add_argument('--force-dump', action='store_true')
    args = parser.parse_args()
    if not (ROOT / 'dumper/Il2CppDumper/Il2CppDumper.csproj').exists():
        run('git', 'submodule', 'update', '--init', '--recursive')
    run(sys.executable, ROOT / 'scripts/dump_config.py')
    work = ROOT / 'work/dump'
    result = work / 'result'
    result.mkdir(exist_ok=True)
    stamp = result / 'source.json'
    source = {'native': hashlib.sha256((work / 'libil2cpp.so').read_bytes()).hexdigest(),
              'metadata': hashlib.sha256((work / 'global-metadata.dat').read_bytes()).hexdigest(),
              'dumper': subprocess.check_output(['git', '-C', str(ROOT / 'dumper'), 'rev-parse', 'HEAD'], text=True).strip()}
    cached = stamp.exists() and json.loads(stamp.read_text()) == source and (result / 'script.json').exists() and (result / 'dump.cs').exists()
    if args.force_dump or not cached:
        run('dotnet', 'build', ROOT / 'dumper/Il2CppDumper/Il2CppDumper.csproj', '-c', 'Release', '-f', 'net8.0', '--nologo', '-v', 'quiet')
        dll = ROOT / 'dumper/Il2CppDumper/bin/Release/net8.0/Il2CppDumper.dll'
        # The upstream CLI only accepts an output directory if it already exists.
        # Remove the success stamp before a new run; failed runs never become cached successes.
        stamp.unlink(missing_ok=True)
        for name in ('script.json', 'dump.cs'):
            (result / name).unlink(missing_ok=True)
        run('dotnet', dll, work / 'libil2cpp.so', work / 'global-metadata.dat', result)
        for name in ('script.json', 'dump.cs'):
            if not (result / name).is_file() or (result / name).stat().st_size == 0:
                raise RuntimeError('Dumper did not produce ' + name)
        stamp.write_text(json.dumps(source, indent=2) + '\n', encoding='utf-8')
    else:
        print('Reusing dump with identical APK and dumper revision.', flush=True)
    from ournotes.schema_extract import extract
    report = extract(work / 'libil2cpp.so', work / 'metadata.dec.dat', result / 'script.json',
                     ROOT / 'work/protocol/descriptor.pb')
    print(f'Protocol: {report["files"]} files, {report["services"]} services, {report["methods"]} RPCs.', flush=True)
    if args.refresh_master:
        run(sys.executable, '-m', 'nnnotes', '--config', ROOT / 'nnnotes.toml', 'master', 'download', '--latest', '-o', 'work/master-bin/jp-current')
        run(sys.executable, '-m', 'nnnotes', '--config', ROOT / 'nnnotes.toml', 'master', 'decode', 'work/master-bin/jp-current', '-o', 'work/master/jp')
        import shutil
        shutil.copyfile(ROOT / 'work/master-bin/jp-current/MasterManifest.json', ROOT / 'work/master/jp/MasterManifest.json')
    run(sys.executable, '-m', 'nnnotes', '--config', ROOT / 'nnnotes.toml', 'config', 'check')
    print('Ready: .\\client.ps1 --help')


if __name__ == '__main__':
    main()
