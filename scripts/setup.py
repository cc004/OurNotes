"""Prepare a Windows workspace without changing tracked submodule files."""
import argparse
import json
import shutil
import subprocess
import sys
import tomllib
import venv
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--xapk', type=Path)
    args = parser.parse_args()
    source = ROOT / 'nnnotes' / 'src'
    if not (source / 'nnnotes' / '__init__.py').is_file():
        subprocess.run(['git', 'submodule', 'update', '--init', '--recursive'], cwd=ROOT, check=True)
    version = tomllib.loads((ROOT / 'nnnotes/rust/Cargo.toml').read_text(encoding='utf-8'))['package']['version']
    python = ROOT / '.venv/Scripts/python.exe'
    if not python.exists():
        venv.create(ROOT / '.venv', with_pip=True)
    subprocess.run([str(python), '-m', 'pip', 'install', '--index-url', 'https://pypi.org/simple',
                    '--only-binary=nnnotes', f'nnnotes=={version}'], check=True)
    subprocess.run([str(python), '-m', 'pip', 'install', '--index-url', 'https://pypi.org/simple',
                    '-r', str(ROOT / 'requirements-client.txt')], check=True)
    subprocess.run([str(python), '-m', 'pip', 'install', '--index-url', 'https://pypi.org/simple',
                    '--no-deps', '-e', str(ROOT)], check=True)
    # Use the checked-out Python code with the matching release wheel's Rust extension.
    # This also applies to Windows multiprocessing workers and the nnnotes.exe entry point.
    site = ROOT / '.venv/Lib/site-packages'
    (site / 'ournotes-source.pth').write_text(
        f'import sys; sys.path.insert(0, {str(source)!r}); import nnnotes; '
        f'nnnotes.__path__.append({str(site / "nnnotes")!r})\n', encoding='utf-8')

    archives = sorted(ROOT.glob('*.xapk')) + sorted((ROOT / 'xapk').glob('*.xapk'))
    archive = args.xapk.resolve() if args.xapk else (archives[0] if len(archives) == 1 else None)
    if archive is None:
        parser.error('Supply --xapk PATH when there is not exactly one XAPK in the root or xapk directory.')
    target = ROOT / 'work/apk/jp'
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as outer:
        manifest = json.loads(outer.read('manifest.json'))
        splits = manifest['split_apks']
        if sum(item['id'] == 'base' for item in splits) != 1:
            raise ValueError('XAPK must declare exactly one base APK')
        members = [('base.apk' if item['id'] == 'base' else Path(item['file']).name, item['file'])
                   for item in splits]
        if len({name for name, _ in members}) != len(members):
            raise ValueError('Duplicate APK filenames')
        extra = {p.name for p in target.glob('*.apk')} - {name for name, _ in members}
        if extra:
            raise ValueError('Old split APKs exist; use a clean work/apk/jp directory before switching APK sets')
        for name, member in members:
            dest = target / name
            with outer.open(member) as src, dest.with_suffix('.apk.tmp').open('wb') as dst:
                shutil.copyfileobj(src, dst)
            dest.with_suffix('.apk.tmp').replace(dest)
        print(f'Prepared {len(members)} APKs, game version {manifest["version_name"]}')
    for name in ('work/master/jp', 'cache', 'out'):
        (ROOT / name).mkdir(parents=True, exist_ok=True)
    config = ROOT / 'nnnotes.toml'
    if not config.exists():
        shutil.copyfile(ROOT / 'nnnotes.example.toml', config)
    subprocess.run([str(python), '-m', 'nnnotes', '--config', str(config), 'config', 'check'], check=True)
    print('Ready: .\\run.ps1 browse-apk (offline); configure endpoints/keys before .\\run.ps1 (online).')


if __name__ == '__main__':
    main()
