"""Recover the verified JP 1.0.4 profile from the user's own APK set.

The format decoder comes from the pinned dumper submodule.
No game keys or server addresses are embedded here.
Unknown APK revisions are rejected rather than interpreted with stale offsets.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import UnityPy
from Crypto.Cipher import AES
from nnnotes.addressables import BundleKey, decrypt
from nnnotes.apkset import ApkSet
from nnnotes.config import Config
from nnnotes.master import MasterKey, decode

ROOT = Path(__file__).resolve().parents[1]
HASHES = {
    'libil2cpp.so': 'b1600689ee83c435b4a3eaa1ef0709b1ed27783eeda90dace814515a54e0a7ce',
    'global-metadata.dat': 'bbf82e20b16ac39ad14c6d3547250efb84daa6ae2c754e46ae9bae5270f9386c',
}
# Located through CryptProvider.ctor and AssetBundleCryptKeySettings.cctor.
FIELDS = {'master.key': (0xDA1A48, 32), 'master.iv': (0xDA19D8, 32),
          'bundle.key': (0xE054E8, 16), 'bundle.nonce_seed': (0xE0D660, 8)}
# NetworkConfig.cctor's string usage slots, RVAs in this exact ELF revision.
URL_SLOTS = {'servers.jp.api': 0xC2CDAE8, 'servers.jp.cdn': 0xC2CDC28}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def unpack_metadata(raw, decoder):
    source = decoder.read_text(encoding='utf-8-sig')
    key_match = re.search(r'aes.Key = Encoding.ASCII.GetBytes\("([^"\n]+)"\)', source)
    iv_match = re.search(r'aes.IV = new byte\[\] \{([^}]+)\}', source)
    require(key_match and iv_match, 'Unsupported external decoder revision')
    key = key_match[1].encode('ascii')
    iv = bytes(int(part.strip()) for part in iv_match[1].split(','))
    data = bytearray(raw)

    def xor(start, end):
        for i in range(start, end):
            data[i] ^= 0x66

    xor(8, 0x1000)
    for start in range(0x1000, 0x5000, 0x800):
        data[start:start + 0x800] = AES.new(key, AES.MODE_CBC, iv).decrypt(data[start:start + 0x800])
    cursor = 0x11000
    while len(data) - cursor >= 0x10000:
        xor(cursor, cursor + 0x4000)
        cursor += 0x10000
    xor(cursor, len(data))
    sections = [struct.unpack_from('<III', data, 8 + i * 12) for i in range(31)]
    end = 0x17C
    for offset, size, _ in sections:
        require(end <= offset <= offset + size <= len(data), 'Invalid decoded metadata table')
        end = offset + size
    return bytes(data), sections


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only', action='store_true', help='validate without changing nnnotes.toml')
    parser.add_argument('--decoder', type=Path, default=ROOT / 'dumper/Il2CppDumper/Il2Cpp/AceMetadataDecryptor.cs')
    args = parser.parse_args()
    config_path = ROOT / 'nnnotes.toml'
    cfg = Config.load(config_path)
    out = ROOT / 'work/dump'
    out.mkdir(parents=True, exist_ok=True)
    with ApkSet(cfg.require_path('paths', 'apk')) as apk:
        raw = apk.read('assets/bin/Data/Managed/Metadata/global-metadata.dat')
        native = apk.read('lib/arm64-v8a/libil2cpp.so')
        for name, data in [('global-metadata.dat', raw), ('libil2cpp.so', native)]:
            require(hashlib.sha256(data).hexdigest() == HASHES[name], f'Unrecognized APK revision: {name}')
            (out / name).write_bytes(data)
        metadata, sections = unpack_metadata(raw, args.decoder)
        (out / 'metadata.dec.dat').write_bytes(metadata)

        def string(index):
            start = sections[2][0] + index
            return metadata[start:metadata.index(0, start)].decode('utf-8')

        defaults = {}
        base, _, count = sections[7]
        for i in range(count):
            field, _, data_index = struct.unpack_from('<iii', metadata, base + i * 12)
            if data_index >= 0:
                name_index = struct.unpack_from('<I', metadata, sections[11][0] + field * 12)[0]
                defaults[sections[8][0] + data_index] = string(name_index)
        values = {}
        for name, (offset, size) in FIELDS.items():
            value = metadata[offset:offset + size]
            require(hashlib.sha256(value).hexdigest().upper() == defaults.get(offset), f'Field digest mismatch: {name}')
            values[name] = value.hex()
        for name, rva in URL_SLOTS.items():
            token = struct.unpack_from('<I', native, rva - 0xC000)[0]
            require(token >> 29 == 5, 'Expected an IL2CPP string literal reference')
            index = (token & 0x1FFFFFFE) >> 1
            start, end = struct.unpack_from('<II', metadata, sections[0][0] + index * 4)
            value = metadata[sections[1][0] + start:sections[1][0] + end].decode('utf-8')
            url = urlsplit(value)
            require(url.scheme == 'https' and url.hostname and not url.username and not url.query, f'Invalid endpoint: {name}')
            values[name] = value

        # Validate actual encrypted content before changing any configuration.
        master = MasterKey(bytes.fromhex(values['master.key']), bytes.fromhex(values['master.iv']))
        master_name = next(n for n in apk.namelist() if n.startswith('assets/Master/') and n.endswith('.bin'))
        table = json.loads(decode(apk.read(master_name), master))
        require('_allData' in table, 'Decoded master table has an unexpected shape')
        bundle_name = next(n for n in apk.namelist() if n.endswith('.bundle'))
        bundle = BundleKey(bytes.fromhex(values['bundle.key']), bytes.fromhex(values['bundle.nonce_seed']))
        plain = decrypt(apk.read(bundle_name), Path(bundle_name).name, bundle)
        require(plain.startswith(b'UnityFS') and len(UnityPy.load(plain).objects) > 0, 'Bundle validation failed')

    if not args.check_only:
        with tempfile.TemporaryDirectory(dir=out) as temp:
            candidate = Path(temp) / 'nnnotes.toml'
            shutil.copyfile(config_path, candidate)
            for name, value in values.items():
                subprocess.run([sys.executable, '-m', 'nnnotes', '--config', str(candidate),
                                'config', 'set', name, '-'], input=value + '\n', text=True,
                               stdout=subprocess.DEVNULL, check=True)
            os.replace(candidate, config_path)
    report = {'profile': 'JP 1.0.4', 'sha256': HASHES, 'settings': list(values),
              'master_sample_valid': True, 'bundle_sample_valid': True,
              'config_written': not args.check_only}
    (out / 'validation.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print('Verified 6 settings against JP 1.0.4 APK; master and Unity bundle decoding passed.')
    print('Config unchanged.' if args.check_only else 'Updated nnnotes.toml (values not printed).')


if __name__ == '__main__':
    main()
