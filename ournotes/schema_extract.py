"""Recover generated protobuf descriptors from JP 1.0.4 ARM64 initializers.

This is a bounded static decoder of the observed IL2CPP string construction,
not execution of APK code. The preparation stage first checks binary hashes.
"""
import base64
import json
import struct
from pathlib import Path

from capstone import Cs, CS_ARCH_ARM64, CS_MODE_LITTLE_ENDIAN
from elftools.elf.elffile import ELFFile
from google.protobuf.descriptor_pb2 import FileDescriptorProto, FileDescriptorSet

from .schema import Schema
from .storage import write_json

BASE64_METHOD = 0xA52FA80
CONCAT_METHODS = {0xA427AD4: 2, 0xA435978: 3, 0xA435EDC: 4}


def extract(native_path, metadata_path, script_path, output):
    native = bytearray(Path(native_path).read_bytes())
    metadata = Path(metadata_path).read_bytes()
    sections = [struct.unpack_from('<III', metadata, 8 + i * 12) for i in range(31)]
    start, _, count = sections[0]
    offsets = struct.unpack_from('<' + str(count) + 'I', metadata, start)
    base, length, _ = sections[1]
    if not all(0 <= a <= b <= length for a, b in zip(offsets, offsets[1:])):
        raise ValueError('Invalid literal offsets')
    literals = [metadata[base + a:base + b].decode('utf-8') for a, b in zip(offsets, offsets[1:])]
    with Path(native_path).open('rb') as stream:
        elf = ELFFile(stream)
        segments = [dict(p.header) for p in elf.iter_segments() if p['p_type'] == 'PT_LOAD']
        relocations = {a: c for a, info, c in struct.iter_unpack('<QQq', elf.get_section_by_name('.rela.dyn').data())
                       if info & 0xFFFFFFFF == 1027}
        translate = bytes(i ^ 0x36 for i in range(256))
        for name in ('.text', 'il2cpp'):
            section = elf.get_section_by_name(name)
            start, end = section['sh_addr'], section['sh_addr'] + section['sh_size']
            for page in range(start & ~0xFFFF, end, 0x10000):
                a, b = max(start, page + 0x1000), min(end, page + 0x5000)
                if a < b:
                    off = a - start + section['sh_offset']
                    native[off:off + b - a] = native[off:off + b - a].translate(translate)

    def file_offset(address):
        for p in segments:
            if p['p_vaddr'] <= address < p['p_vaddr'] + p['p_filesz']:
                return address - p['p_vaddr'] + p['p_offset']
        raise ValueError('Address not backed by ELF bytes')

    def read(address):
        return relocations.get(address, struct.unpack_from('<Q', native, file_offset(address))[0])

    def literal(token):
        if isinstance(token, str):
            return token
        if isinstance(token, int) and token < 2 ** 32 and token >> 29 == 5 and token & 1:
            index = (token & 0x1FFFFFFE) >> 1
            return literals[index] if index < len(literals) else None
        return None

    script = json.loads(Path(script_path).read_text(encoding='utf-8'))
    decoder = Cs(CS_ARCH_ARM64, CS_MODE_LITTLE_ENDIAN)
    decoder.detail = True
    files = {}
    failures = []
    for method in script['ScriptMethod']:
        if 'Reflection$$.cctor' not in method['Name'] or not method['Name'].startswith(('App.Protobuf.', 'Google.Protobuf.')):
            continue
        registers, parts = {}, []
        found = False
        offset = file_offset(method['Address'])
        for ins in decoder.disasm(native[offset:offset + 100000], method['Address']):
            operands = ins.operands
            def reg(index):
                return ins.reg_name(operands[index].reg).replace('w', 'x', 1)
            if ins.mnemonic in ('adrp', 'adr'):
                registers[reg(0)] = operands[1].imm
            elif ins.mnemonic == 'mov':
                registers[reg(0)] = operands[1].imm if operands[1].type == 2 else registers.get(reg(1))
            elif ins.mnemonic in ('ldr', 'ldur') and len(operands) > 1 and operands[1].type == 3:
                value = registers.get(ins.reg_name(operands[1].mem.base))
                try:
                    registers[reg(0)] = read(value + operands[1].mem.disp) if isinstance(value, int) else None
                except (ValueError, struct.error):
                    registers[reg(0)] = None
            elif ins.mnemonic == 'str' and operands[0].type == 1:
                value = literal(registers.get(reg(0)))
                if value is not None:
                    parts.append(value)
            elif ins.mnemonic == 'bl':
                target = operands[0].imm
                if target == BASE64_METHOD:
                    if not parts:
                        value = literal(registers.get('x0'))
                        parts = [value] if value is not None else []
                    try:
                        data = base64.b64decode(''.join(parts), validate=True)
                        descriptor = FileDescriptorProto.FromString(data)
                        if not descriptor.name.endswith('.proto'):
                            raise ValueError('No descriptor name')
                        if descriptor.name in files and files[descriptor.name] != descriptor:
                            raise ValueError('Conflicting descriptors')
                        files[descriptor.name] = descriptor
                        found = True
                    except Exception:
                        pass
                    break
                result = None
                if target in CONCAT_METHODS:
                    values = [literal(registers.get('x' + str(i))) for i in range(CONCAT_METHODS[target])]
                    if all(v is not None for v in values):
                        result = ''.join(values)
                for i in range(19):
                    registers.pop('x' + str(i), None)
                if result is not None:
                    registers['x0'] = result
            elif ins.mnemonic == 'ret':
                break
        if not found:
            failures.append(method['Name'])
    if failures:
        raise ValueError('Cannot recover descriptor initializers: ' + ', '.join(failures))
    if not files:
        raise ValueError('No protocol descriptors recovered')
    descriptor_set = FileDescriptorSet()
    descriptor_set.file.extend(files[k] for k in sorted(files))
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    candidate = output.with_suffix('.pb.tmp')
    candidate.write_bytes(descriptor_set.SerializeToString(deterministic=True))
    schema = Schema(candidate)  # Resolve all imports and message references before replacing output.
    candidate.replace(output)
    report = {'files': len(files), 'services': sum(len(f.service) for f in files.values()),
              'methods': len(schema.methods), 'file_names': sorted(files)}
    write_json(output.with_suffix('.json'), report)
    return report
