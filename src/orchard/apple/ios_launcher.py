# SPDX-License-Identifier: GPL-2.0-only
"""Prepare an on-device iOS launcher from a board's exact boot recipe.

This producer only reads host files and writes a new private output directory.
Device connection, installation and startup stay outside this command.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import struct
import subprocess
from types import SimpleNamespace
import zlib

from orchard import hardware

def run(*args):
    result = subprocess.run(args,capture_output=True,text=True,timeout=60)
    if result.returncode:
        raise RuntimeError(result.stdout+result.stderr)
    return result.stdout


def library(folder,name,install,symbols=(),classes=()):
    path = folder/(name+'.tbd')
    text = ('--- !tapi-tbd\ntbd-version: 4\ntargets: [ armv7-ios ]\n'
            f'install-name: {json.dumps(install)}\nexports:\n  - targets: [ armv7-ios ]\n')
    if symbols:
        text += '    symbols: '+json.dumps(list(symbols))+'\n'
    if classes:
        text += '    objc-classes: '+json.dumps(list(classes))+'\n'
    path.write_text(text+'...\n')
    return str(path)


def check_entry(path):
    """Require a 32-bit ARM executable whose LC_MAIN preserves entry mode."""
    raw = Path(path).read_bytes()
    if len(raw) < 28:
        raise ValueError('Incomplete Mach-O header')
    magic,cpu,subtype,kind,count,size,_ = struct.unpack_from('<7I',raw)
    if magic != 0xfeedface or cpu != 12 or subtype & 0xffffff != 9 or kind != 2:
        raise ValueError('Expected a thin ARMv7 Mach-O executable')
    end = 28 + size
    if end > len(raw) or count > size // 8:
        raise ValueError('Mach-O load commands exceed the file')
    cursor = 28
    text = main = symbols = code = None
    for _ in range(count):
        if cursor + 8 > end:
            raise ValueError('Incomplete Mach-O load command')
        command,length = struct.unpack_from('<II',raw,cursor)
        if length < 8 or length % 4 or cursor + length > end:
            raise ValueError('Invalid Mach-O load command length')
        if command == 1 and length >= 56:
            name = raw[cursor+8:cursor+24].rstrip(b'\0')
            vmaddr,vmsize,offset,filesize,_,protection,sections = struct.unpack_from('<7I',raw,cursor+24)
            if 56 + sections * 68 > length:
                raise ValueError('Mach-O sections exceed their segment')
            if name == b'__TEXT':
                if text is not None or offset != 0 or not protection & 4 or filesize > len(raw):
                    raise ValueError('Unexpected executable text segment')
                text = (vmaddr,vmsize,filesize)
                for index in range(sections):
                    section = cursor + 56 + index * 68
                    if raw[section:section+16].rstrip(b'\0') == b'__text':
                        if code is not None:
                            raise ValueError('Duplicate executable code section')
                        code = struct.unpack_from('<II',raw,section+32)
        elif command == 0x80000028:
            if main is not None or length != 24:
                raise ValueError('Unexpected LC_MAIN command')
            main = struct.unpack_from('<Q',raw,cursor+8)[0]
        elif command == 2:
            if symbols is not None or length != 24:
                raise ValueError('Unexpected symbol table command')
            symbols = struct.unpack_from('<4I',raw,cursor+8)
        cursor += length
    if cursor != end or any(item is None for item in (text,main,symbols,code)):
        raise ValueError('Missing executable entry metadata')
    symoff,nsyms,stroff,strsize = symbols
    if symoff + nsyms * 12 > len(raw) or stroff + strsize > len(raw):
        raise ValueError('Mach-O symbol table exceeds the file')
    names = raw[stroff:stroff+strsize]
    found = []
    for index in range(nsyms):
        name,kind,section,description,value = struct.unpack_from('<IBBHI',raw,symoff+index*12)
        if name >= len(names) or names.find(b'\0',name) < 0:
            raise ValueError('Invalid Mach-O symbol name')
        if names[name:names.find(b'\0',name)] == b'_main' and kind & 0x0e == 0x0e:
            found.append((description,value))
    if len(found) != 1:
        raise ValueError('Expected one defined _main symbol')
    description,value = found[0]
    thumb = bool(description & 8)
    target = text[0] + main
    if target != value | int(thumb):
        raise ValueError('LC_MAIN address or instruction mode differs from _main')
    address = target & ~1
    if not code[0] <= address < code[0] + code[1] or (main & ~1) >= text[2]:
        raise ValueError('LC_MAIN does not enter the executable code section')
    if not thumb and address % 4:
        raise ValueError('ARM entry is not word aligned')
    return {'mode':'thumb' if thumb else 'arm','file_offset':main,
            'virtual_address':value,'main_symbol_matches':True}




def boot_profile(device, *, root=None):
    described = hardware.device(device, root)
    recipe = hardware.boot_recipe(device, root)
    if 'ios-local-file' not in described.get('boot', {}).get('methods', ()):
        raise ValueError('The board has no iOS local-file boot method')
    profile = dict(recipe.get('methods', {}).get('ios-local-file', {}))
    profile['machine'] = recipe['device']
    profile['profile'] = described['device']['profile']
    profile['chip'] = described['match']['dfu']['chip']
    profile['board'] = described['match']['dfu']['board']
    validate_profile(profile)
    return profile


def recipe_digest(device):
    return hashlib.sha256(json.dumps(hardware.boot_recipe(device), sort_keys=True,
                                     separators=(',', ':')).encode('ascii')).hexdigest()


def validate_profile(profile):
    digest = profile.get('prepared_ibec_sha256', '')
    if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise ValueError('The prepared iBEC needs an exact SHA-256')
    integer_names = ('prepared_ibec_bytes', 'base', 'hook', 'finish_init', 'shim_start',
                     'shim_end', 'mount_bootfs', 'load_file', 'load_address',
                     'maximum_image_bytes', 'wake_delay_seconds')
    for key in integer_names:
        value = profile.get(key)
        if type(value) is not int or not 0 < value <= 0xffffffff:
            raise ValueError('Invalid boot recipe integer: ' + key)
    size, base = profile['prepared_ibec_bytes'], profile['base']
    if not (profile['hook'] + 4 <= size and profile['shim_start'] < profile['shim_end'] <= size
            and base + size <= 0x100000000 and profile['hook'] % 2 == 0
            and profile['shim_start'] % 2 == 0 and profile['load_address'] % 4 == 0
            and (profile['hook'] + 4 <= profile['shim_start'] or profile['hook'] >= profile['shim_end'])
            and 32768 < profile['maximum_image_bytes'] <= 64 * (1 << 20)
            and profile['load_address'] + profile['maximum_image_bytes'] <= base
            and 30 <= profile['wake_delay_seconds'] <= 300):
        raise ValueError('Boot recipe address or timing bounds differ')
    if any(not base <= profile[key] - 1 < base + size or not profile[key] & 1
           or base + profile['shim_start'] <= profile[key] - 1 < base + profile['shim_end']
           for key in ('finish_init', 'mount_bootfs', 'load_file')):
        raise ValueError('Native iBEC entry must be Thumb code inside the pinned image')
    for key in ('boot_path', 'ibec_path', 'kloader_path', 'helper_path', 'lease_path',
                'app_path', 'ibec_boot_path'):
        value = profile.get(key)
        if (not isinstance(value, str) or not value.startswith('/') or '\\' in value
                or str(PurePosixPath(value)) != value or '..' in PurePosixPath(value).parts
                or any(ord(c) < 32 or ord(c) > 126 for c in value)):
            raise ValueError('Invalid absolute installation path: ' + key)
    if len({profile[key] for key in ('boot_path', 'ibec_path', 'kloader_path', 'helper_path',
                                    'lease_path', 'app_path')}) != 6:
        raise ValueError('Installation paths must be distinct')
    for key in ('machine', 'ios_build', 'wake_owner', 'bundle_id', 'boot_device', 'minimum_ios'):
        value = profile.get(key)
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9,._-]{1,80}', value):
            raise ValueError('Invalid boot recipe string: ' + key)
    if profile.get('identity_source') not in (None, 'ios-device'):
        raise ValueError('Unsupported iOS identity source')
    if profile.get('identity_source') == 'ios-device':
        for key in ('profile', 'ios_model'):
            if not isinstance(profile.get(key), str) or not re.fullmatch(r'[A-Za-z0-9,._-]{1,80}', profile[key]):
                raise ValueError('Invalid iOS identity recipe: ' + key)
        if (type(profile.get('chip')) is not int or not 0 < profile['chip'] < 2**32 or
                type(profile.get('board')) is not int or not 0 <= profile['board'] < 2**32):
            raise ValueError('Invalid iOS identity chip/board')
        compatible = profile.get('ios_registry_compatible')
        if (not isinstance(compatible, list) or not compatible or
                any(not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9,._-]{1,80}', value) for value in compatible) or
                len(set(compatible)) != len(compatible) or
                sum(len(value) + 1 for value in compatible) > 128):
            raise ValueError('Invalid iOS registry compatible list')
        for key in ('attempt_boot_path', 'ibec_attempt_boot_path', 'attempt_ibec_path'):
            value = profile.get(key)
            if (not isinstance(value, str) or not value.startswith('/') or str(PurePosixPath(value)) != value or
                    '..' in PurePosixPath(value).parts or '\\' in value or any(ord(c) < 32 or ord(c) > 126 for c in value)):
                raise ValueError('Invalid fixed iOS attempt path')
        if profile['ibec_attempt_boot_path'] != '/boot' + profile['attempt_boot_path']:
            raise ValueError('iBEC and iOS attempt paths differ')
        if len({profile[key] for key in ('boot_path', 'ibec_path', 'kloader_path', 'helper_path', 'lease_path',
                                        'app_path', 'attempt_boot_path', 'attempt_ibec_path')}) != 8:
            raise ValueError('Attempt paths must be distinct from the installed inputs')


def thumb_bl(source, target):
    delta = target - (source + 4)
    if delta % 2 or not -(1 << 24) <= delta < 1 << 24:
        raise ValueError('Thumb branch exceeds its signed aligned range')
    value = delta & ((1 << 25) - 1)
    sign, i1, i2 = value >> 24, (value >> 23) & 1, (value >> 22) & 1
    return struct.pack('<HH', 0xf000 | sign << 10 | (value >> 12) & 0x3ff,
                       0xd000 | ((~(i1 ^ sign)) & 1) << 13 | ((~(i2 ^ sign)) & 1) << 11
                       | (value >> 1) & 0x7ff)


def patch_ibec(source, stub, profile):
    validate_profile(profile)
    if (len(source) != profile['prepared_ibec_bytes']
            or hashlib.sha256(source).hexdigest() != profile['prepared_ibec_sha256']):
        raise ValueError('Prepared iBEC differs from the exact supported image')
    hook, first, end, base = (profile[key] for key in ('hook', 'shim_start', 'shim_end', 'base'))
    if source[hook:hook + 4] != thumb_bl(base + hook, profile['finish_init'] & ~1):
        raise ValueError('Native finish-init call differs')
    if not 0 < len(stub) <= end - first or any(source[first:end]):
        raise ValueError('Shim exceeds checked zero text padding')
    changed = bytearray(source)
    changed[first:first + len(stub)] = stub
    changed[hook:hook + 4] = thumb_bl(base + hook, base + first)
    return bytes(changed)


def launcher_config(profile, title, identity=None, shim=None):
    validate_profile(profile)
    if not isinstance(title, str) or not 1 <= len(title) <= 40 or any(ord(c) < 32 for c in title):
        raise ValueError('Launcher title must contain 1-40 printable characters')
    strings = {'TARGET_MACHINE': 'machine', 'TARGET_IOS_BUILD': 'ios_build', 'ROOT_BOOT_PATH': 'boot_path', 'IBEC_PATH': 'ibec_path',
               'KLOADER_PATH': 'kloader_path', 'HELPER_PATH': 'helper_path', 'LEASE_PATH': 'lease_path',
               'WAKE_OWNER': 'wake_owner'}
    result = ''.join('#define ' + key + ' ' + json.dumps(profile[value]) + '\n'
                     for key, value in strings.items())
    result += f"#define WAKE_DELAY_SECONDS {profile['wake_delay_seconds']}\n"
    result += '#define LAUNCHER_TITLE @' + json.dumps(title) + '\n'
    result += '#define LAUNCHER_START_TITLE @' + json.dumps('Start ' + title) + '\n'
    message = f"Starting {title}...\nThe screen may stay black for up to {profile['wake_delay_seconds']} seconds."
    result += '#define LAUNCHER_STARTING_TITLE @' + json.dumps(message) + '\n'
    if identity is not None:
        raw = b'\0'.join(value.encode('ascii') for value in profile['ios_registry_compatible']) + b'\0'
        literal = '"' + ''.join(chr(value) if 32 <= value < 127 and chr(value) not in ('"', '\\')
                                else f'\\{value:03o}' for value in raw) + '"'
        for name, value in {'TARGET_PROFILE': profile['profile'], 'TARGET_MODEL': profile['ios_model'],
                            'ATTEMPT_BOOT_PATH': profile['attempt_boot_path'],
                            'ATTEMPT_IBEC_PATH': profile['attempt_ibec_path']}.items():
            result += '#define ' + name + ' ' + json.dumps(value) + '\n'
        result += '#define TARGET_REGISTRY_COMPATIBLE ' + literal + '\n'
        for name, value in {'TARGET_CHIP': profile['chip'], 'TARGET_BOARD': profile['board'],
                            'TARGET_REGISTRY_COMPATIBLE_BYTES': len(raw),
                            'IOS_IDENTITY_SLOT_OFFSET': identity['slot_offset'],
                            'IOS_IDENTITY_SLOT_BYTES': identity['slot_bytes'],
                            'IOS_IBEC_CRC_OFFSET': shim['mutable_crc_offset'],
                            'IOS_TEMPLATE_CRC32': shim['boot_image_crc32']}.items():
            result += f'#define {name} {int(value, 16) if isinstance(value, str) else value}u\n'
    return result


def build_shim(ibec, image, output, profile, *, clang='clang-19', linker='ld.lld-19', objcopy='llvm-objcopy-19', host_toolchain=False, identity=None):
    validate_profile(profile)
    raw, payload = Path(ibec).read_bytes(), Path(image).read_bytes()
    # Refuse the firmware before invoking a compiler or creating an output.
    if (len(raw) != profile['prepared_ibec_bytes']
            or hashlib.sha256(raw).hexdigest() != profile['prepared_ibec_sha256']):
        raise ValueError('Prepared iBEC differs from the exact supported image')
    if not 32768 < len(payload) < profile['maximum_image_bytes'] or payload[:4] != bytes.fromhex('c0000cf1'):
        raise ValueError('Expected an ARM32 staging bundle with its entry instruction at byte zero')
    output = Path(output)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    source = Path(hardware.ROOT) / 'boot/ios-launcher'
    crc = zlib.crc32(payload)
    config = ''.join(f'#define {name} 0x{profile[key]:08x}u\n' for name, key in (
        ('IBEC_FINISH_INIT', 'finish_init'), ('IBEC_MOUNT_BOOT', 'mount_bootfs'),
        ('IBEC_LOAD_FILE', 'load_file'), ('LOCAL_BOOT_ADDRESS', 'load_address')))
    config += '#define LOCAL_BOOT_DEVICE ' + json.dumps(profile['boot_device']) + '\n'
    config += '#define LOCAL_BOOT_PATH ' + json.dumps(profile['ibec_attempt_boot_path'] if identity is not None else profile['ibec_boot_path']) + '\n'
    if identity is not None:
        config += '#define LOCAL_BOOT_DYNAMIC_IDENTITY 1\n'
    config += f'#define LOCAL_BOOT_BYTES {len(payload)}u\n#define LOCAL_BOOT_CRC32 0x{crc:08x}u\n'
    (output / 'local-boot-config.h').write_text(config)
    address, end = profile['base'] + profile['shim_start'], profile['base'] + profile['shim_end']
    script = (f'ENTRY(orchard_local_boot)\nSECTIONS {{ . = 0x{address:x}; '
              '.text : { *(.text.entry) *(.text*) *(.rodata*) } '
              '/DISCARD/ : { *(.comment) *(.ARM.attributes) *(.ARM.exidx*) *(.ARM.extab*) } '
              f'ASSERT(. <= 0x{end:x}, "local boot shim exceeds checked padding") }}\n')
    (output / 'local.ld').write_text(script)
    commands = [
        [clang, '--target=arm-none-eabi', '-march=armv7-a', '-mthumb', '-mfloat-abi=soft',
         '-ffreestanding', '-fno-builtin', '-fno-stack-protector', '-fno-unwind-tables',
         '-fno-asynchronous-unwind-tables', '-Oz', '-Wall', '-Wextra', '-Werror',
         '-I', str(output), '-c', str(source / 'ibec-local.c'), '-o', str(output / 'local.o')],
        [linker, '--build-id=none', '-T', str(output / 'local.ld'), str(output / 'local.o'),
         '-o', str(output / 'local.elf')],
        [objcopy, '-O', 'binary', str(output / 'local.elf'), str(output / 'local.bin')]]
    if host_toolchain:
        for command in commands:
            subprocess.run(command, check=True, timeout=60)
    else:
        from orchard import buildenv
        image = 'localhost/orchard-hardware-kernel-toolchain:bookworm'
        if not buildenv.native() and subprocess.run(['podman', 'image', 'exists', image]).returncode:
            buildenv.build(image, hardware.ROOT / 'kernel/Containerfile', hardware.ROOT / 'kernel')
        # The cross compiler runs on the builder host's ISA. Only the small
        # generated configuration and checked source enter this container.
        buildenv.run(image, ['bash', '/source/shim-build'],
                     [(source, '/source', True), (output, '/output', False)],
                     env={'ORCHARD_CLANG': clang, 'ORCHARD_LINKER': linker,
                          'ORCHARD_OBJCOPY': objcopy})
    stub = (output / 'local.bin').read_bytes()
    changed = patch_ibec(raw, stub, profile)
    (output / 'ibec-local.raw').write_bytes(changed)
    report = {'schema': 1, 'device_operations': 0, 'source_ibec_sha256': hashlib.sha256(raw).hexdigest(),
              'output_sha256': hashlib.sha256(changed).hexdigest(), 'shim_bytes': len(stub),
              'boot_image_sha256': hashlib.sha256(payload).hexdigest(), 'boot_image_bytes': len(payload),
              'boot_image_crc32': f'{crc:08x}', 'source_files': {'ibec-local.c': hashlib.sha256((source / 'ibec-local.c').read_bytes()).hexdigest(),
                               'producer': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
              'on_device_boot_qualified': False}
    if identity is not None:
        from .ios_identity import elf_word_offset
        relative = elf_word_offset((output / 'local.elf').read_bytes(), 'orchard_local_boot_crc32', address, len(stub))
        offset = profile['shim_start'] + relative
        if struct.unpack_from('<I', changed, offset)[0] != crc:
            raise ValueError('The mutable iBEC CRC word differs from the launch template')
        report['mutable_crc_offset'] = offset
    (output / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


def build_launcher(boot_image, ibec, kloader, output, profile, title='Orchard', *, identity=None, shim=None):
    args = SimpleNamespace(boot_image=Path(boot_image), ibec=Path(ibec),
                           kloader=Path(kloader), output=Path(output))
    source = Path(hardware.ROOT) / 'boot/ios-launcher'
    if args.output.is_symlink():
        raise ValueError('Linked output directory refused')
    out = args.output.resolve()
    out.mkdir(mode=0o700,parents=True,exist_ok=False)
    assets = {'ROOT_BOOT':args.boot_image,'IBEC':args.ibec,'KLOADER':args.kloader}
    header = launcher_config(profile, title, identity, shim)
    inputs = {}
    for name,path in assets.items():
        raw = path.read_bytes()
        if not 0 < len(raw) < 64*(1<<20):
            raise ValueError('Unexpected launcher input size')
        sha = hashlib.sha256(raw).hexdigest()
        inputs[name] = {'sha256':sha,'bytes':len(raw)}
        header += f'#define {name}_BYTES {len(raw)}u\n#define {name}_SHA256 "{sha}"\n'
    (out/'ios-start-config.h').write_text(header)
    base = ['xcrun','--sdk','iphoneos','clang','-arch','armv7',
            '-miphoneos-version-min='+profile['minimum_ios'],'-Os','-Wall','-Wextra','-Werror']
    run(*base,'-std=c11','-marm','-fno-builtin','-D_FORTIFY_SOURCE=0','-I',str(out),
        '-c',str(source/'ios-start.c'),'-o',str(out/'start.o'))
    # Current Apple linkers omit the Thumb bit in ARMv7 LC_MAIN. Compile
    # the app in ARM mode as well, then verify both finished entry points.
    run(*base,'-marm','-fobjc-arc','-fblocks','-I',str(out),'-c',str(source/'ios-launcher.m'),'-o',str(out/'launcher.o'))
    libsystem = library(out,'libSystem','/usr/lib/libSystem.B.dylib',[
        '_'+name for name in ('CC_SHA256','__stack_chk_fail','__stack_chk_guard','close','execve',
        'fstat','fsync','geteuid','mach_port_deallocate','mach_task_self_','mmap','munmap','open',
        'setgid','setuid','snprintf','strcmp','sysctlbyname','task_for_pid','write',
        '_NSConcreteStackBlock','_dispatch_main_q','dispatch_async','dispatch_get_global_queue',
        'posix_spawn','waitpid','puts','strncmp','flock','ftruncate','pread',
        'dlopen','dlsym','dlclose','getpid','__error','memset','floor',
        'malloc','free','read','rename','unlink','arc4random_buf','memcmp','memcpy','strlen','strrchr')])
    objc = library(out,'libobjc','/usr/lib/libobjc.A.dylib',[
        '_'+name for name in ('_objc_empty_cache','objc_alloc','objc_autoreleasePoolPop',
        'objc_autoreleasePoolPush','objc_msgSend','objc_msgSend_stret','objc_release',
        'objc_retain','objc_retainAutoreleasedReturnValue','objc_storeStrong')])
    ui = library(out,'UIKit','/System/Library/Frameworks/UIKit.framework/UIKit',
        ['_UIApplicationMain'],['UIButton','UIColor','UIFont','UILabel','UIResponder',
        'UIScreen','UIViewController','UIWindow'])
    foundation = library(out,'Foundation','/System/Library/Frameworks/Foundation.framework/Foundation',
        ['_NSStringFromClass','_NSClassFromString'],['NSArray','NSObject'])
    core = library(out,'CoreFoundation','/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation',
        ['___CFConstantStringClassReference','_CFRelease','_CFGetTypeID','_CFArrayGetTypeID',
         '_CFArrayGetCount','_CFArrayGetValueAtIndex','_CFDictionaryGetTypeID',
         '_CFDictionaryGetValue','_CFEqual','_CFAbsoluteTimeGetCurrent','_CFDateCreate',
         '_CFStringCreateWithCString','_CFDataGetTypeID','_CFDataGetLength','_CFDataGetBytePtr'])
    graphics = library(out,'CoreGraphics','/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics',
        ['_CGRectZero'])
    iokit = ([library(out,'IOKit','/System/Library/Frameworks/IOKit.framework/Versions/A/IOKit',
              ['_IORegistryEntryFromPath','_IORegistryEntryCreateCFProperty','_IOObjectRelease'])]
             if identity is not None else [])
    run(*base,'-nostdlib',str(out/'start.o'),core,*iokit,libsystem,'-Wl,-bind_at_load','-Wl,-no_uuid',
        '-o',str(out/'orchard-ios-start'))
    app = out/'Orchard.app'
    app.mkdir()
    run(*base,'-nostdlib',str(out/'launcher.o'),ui,foundation,core,graphics,objc,libsystem,
        '-Wl,-bind_at_load','-Wl,-no_uuid','-o',str(app/'Orchard'))
    info = {'CFBundleIdentifier':profile['bundle_id'],'CFBundleName':title,
            'CFBundleDisplayName':title,'CFBundleExecutable':'Orchard',
            'CFBundlePackageType':'APPL','CFBundleVersion':'1','CFBundleShortVersionString':'0.1',
            'MinimumOSVersion':profile['minimum_ios'],'LSRequiresIPhoneOS':True,'UIDeviceFamily':[2],
            'UISupportedInterfaceOrientations':['UIInterfaceOrientationPortrait']}
    (app/'Info.plist').write_bytes(plistlib.dumps(info))
    entitlements = out/'entitlements.plist'
    entitlements.write_bytes(plistlib.dumps({'platform-application':True,
        'com.apple.private.security.no-container':True,'task_for_pid-allow':True}))
    for target in (out/'orchard-ios-start',app):
        run('/usr/bin/codesign','--force','--sign','-','--timestamp=none',
            '--digest-algorithm=sha1,sha256','--entitlements',str(entitlements),str(target))
        run('/usr/bin/codesign','--verify','--strict',str(target))
    outputs = {}
    for path in (out/'orchard-ios-start',app/'Orchard'):
        path.chmod(0o755)
        raw = path.read_bytes()
        outputs[str(path.relative_to(out))] = {'sha256':hashlib.sha256(raw).hexdigest(),
                                              'bytes':len(raw),'entry':check_entry(path)}
    report = {'schema':1,'device_operations':0,'architecture':'armv7','minimum_ios':profile['minimum_ios'],
              'inputs':inputs,'outputs':outputs,'signing':'ad-hoc SHA-1 and SHA-256; requires owner jailbreak',
              'source_files':{**{name:hashlib.sha256((source/name).read_bytes()).hexdigest()
                              for name in ('ios-start.c','ios-launcher.m',
                                           *(['ios-identity-bind.h','ios-live-identity.h'] if identity is not None else []))},
                              'producer': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
              'on_device_startup_qualified':False}
    (out/'result.json').write_text(json.dumps(report,indent=2)+'\n')
    return report



def checked_kloader(path, receipt):
    """Accept the pinned source build, with a receipt bound to the actual bytes."""
    from orchard.apple import ios_kloader
    path, receipt = Path(path), Path(receipt)
    if path.is_symlink() or receipt.is_symlink() or not path.is_file() or not receipt.is_file():
        raise ValueError('Kloader and its source-build receipt must be regular files')
    raw = path.read_bytes()
    record = json.loads(receipt.read_text())
    if (record.get('schema') != 1 or record.get('kind') != 'ios-kloader-build'
            or not 0 < len(raw) < 64 * (1 << 20)
            or record.get('hardening_patch_sha256') != ios_kloader.HARDENING_PATCH_SHA256
            or record.get('derived_source_sha256') != ios_kloader.DERIVED_SOURCE_SHA256
            or record.get('sleep_wait_seconds') != ios_kloader.SLEEP_WAIT_SECONDS
            or record.get('source_commit') != ios_kloader.SOURCE_COMMIT
            or record.get('source_sha256') != ios_kloader.SOURCE_SHA256
            or record.get('artifact_sha256') != hashlib.sha256(raw).hexdigest()
            or record.get('artifact_bytes') != len(raw) or record.get('signed') is not True):
        raise ValueError('Kloader differs from the pinned signed source-build receipt')
    check_entry(path)
    return record


def prepare(device, ibec, boot_image, kloader, kloader_receipt, output, *, title='Orchard',
            clang='clang-19', linker='ld.lld-19', objcopy='llvm-objcopy-19', host_toolchain=False):
    """Build ordinary host files and a private installation manifest, without device I/O."""
    profile = boot_profile(device)
    kloader_result = checked_kloader(kloader, kloader_receipt)
    from orchard.apple import ios_kloader
    if profile['wake_delay_seconds'] > ios_kloader.SLEEP_WAIT_SECONDS - 15:
        raise ValueError('The configured wake is later than this kloader build can wait')
    output = Path(output)
    if output.is_symlink():
        raise ValueError('Linked output directory refused')
    output = output.resolve()
    if output.exists() or output.is_symlink():
        raise ValueError('Choose a new output directory')
    # Validate assets before leaving a partial build directory behind.
    raw, bundle = Path(ibec).read_bytes(), Path(boot_image).read_bytes()
    if (len(raw) != profile['prepared_ibec_bytes']
            or hashlib.sha256(raw).hexdigest() != profile['prepared_ibec_sha256']):
        raise ValueError('Prepared iBEC differs from the exact supported image')
    if not 32768 < len(bundle) < profile['maximum_image_bytes'] or bundle[:4] != bytes.fromhex('c0000cf1'):
        raise ValueError('Expected the ARM32 staging bundle')
    from .ios_identity import prepare_template
    template, identity = prepare_template(bundle, profile)
    if len(template) >= profile['maximum_image_bytes']:
        raise ValueError('The fixed-slot launch template exceeds its upload bound')
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    selected_image = Path(boot_image)
    if identity is not None:
        selected_image = output / 'launch-template.bin'
        selected_image.write_bytes(template)
        selected_image.chmod(0o600)
    shim = output / 'shim'
    shim_result = build_shim(ibec, selected_image, shim, profile, clang=clang, linker=linker, objcopy=objcopy, host_toolchain=host_toolchain, identity=identity)
    native = output / 'native'
    native_result = build_launcher(selected_image, shim / 'ibec-local.raw', kloader, native, profile, title,
                                   identity=identity, shim=shim_result)
    files = []
    stage = output / 'files'
    def stage_file(source, destination, mode):
        source = Path(source)
        if source.is_symlink() or not source.is_file():
            raise ValueError('Every installation asset must be a regular file')
        target = stage / destination.lstrip('/')
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = source.read_bytes()
        with target.open('xb') as stream:
            stream.write(data)
        target.chmod(0o600)
        files.append({'source': str(target.relative_to(output)), 'destination': destination,
                      'uid': 0, 'gid': 0, 'mode': mode, 'bytes': len(data),
                      'sha256': hashlib.sha256(data).hexdigest()})
    stage_file(selected_image, profile['boot_path'], '0644')
    stage_file(shim / 'ibec-local.raw', profile['ibec_path'], '0644')
    stage_file(kloader, profile['kloader_path'], '0755')
    stage_file(native / 'orchard-ios-start', profile['helper_path'], '4755')
    for item in sorted((native / 'Orchard.app').rglob('*')):
        if item.is_file() or item.is_symlink():
            relative = item.relative_to(native / 'Orchard.app').as_posix()
            stage_file(item, profile['app_path'] + '/' + relative,
                       '0755' if relative == 'Orchard' else '0644')
    report = {'schema': 1, 'kind': 'ios-local-file-launcher', 'device': device,
              'machine': profile['machine'], 'device_operations': 0, 'files': files,
              'shim': shim_result, 'native': native_result, 'kloader': kloader_result,
              'on_device_startup_qualified': False}
    if identity is not None:
        report['identity'] = identity
        report['boot_recipe_sha256'] = recipe_digest(device)
    (output / 'installation.json').write_text(json.dumps(report, indent=2) + '\n')
    (output / 'installation.json').chmod(0o600)
    return report


def provision_handoff(device, endpoint, receipt=None):
    """Pin an offline start to an explicitly selected private provision output."""
    from orchard.contracts import RecoveryHandoff
    from orchard import cascadia_payload as cascadia
    from orchard import recovery_identity
    from orchard.state import BootState, DeviceState

    supplied = receipt or os.environ.get('ORCHARD_IOS_LAUNCHER')
    if not supplied:
        raise ValueError('Select the private iOS launcher output with ORCHARD_IOS_LAUNCHER before attaching')
    path = Path(supplied)
    if path.is_dir():
        path = path / 'installation.json'
    folder = path.parent.resolve()
    if (path.is_symlink() or not path.is_file() or path.stat().st_size > 1 << 20 or
            path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077 or
            path.parent.is_symlink() or folder.stat().st_uid != os.getuid() or folder.stat().st_mode & 0o077):
        raise ValueError('Expected a private regular iOS provision receipt')
    try:
        record = json.loads(path.read_text())
        identity = record['identity']
        profile = boot_profile(device)
        if (type(record['schema']) is not int or record['schema'] != 1 or record['kind'] != 'ios-local-file-launcher' or
                record['device'] != device or record['machine'] != profile['machine'] or
                record['boot_recipe_sha256'] != recipe_digest(device) or
                type(identity['schema']) is not int or identity['schema'] != 1 or identity['identity_source'] != 'ios-device' or
                (identity['profile'], identity['chip'], identity['board']) !=
                (device, profile['chip'], profile['board']) or
                not re.fullmatch('[0-9a-f]{64}', identity['public_input_sha256'])):
            raise ValueError('iOS provision identity differs from the selected recipe')
        checked = {}
        for asset in record['files']:
            source = asset['source']
            if (not isinstance(source, str) or str(PurePosixPath(source)) != source or
                    PurePosixPath(source).is_absolute() or '..' in PurePosixPath(source).parts):
                raise ValueError('iOS provision asset escapes its private output')
            candidate = folder / source
            if (candidate.is_symlink() or not candidate.is_file() or
                    any(parent.is_symlink() for parent in candidate.parents if parent != folder and folder in parent.parents) or
                    candidate.stat().st_uid != os.getuid() or candidate.stat().st_mode & 0o077 or
                    not 0 < candidate.stat().st_size <= profile['maximum_image_bytes'] or
                    candidate.stat().st_size != asset['bytes']):
                raise ValueError('iOS provision asset is not a private checked file')
            data = candidate.read_bytes()
            if hashlib.sha256(data).hexdigest() != asset['sha256'] or asset['destination'] in checked:
                raise ValueError('iOS provision asset differs from its receipt')
            checked[asset['destination']] = (asset, data)
        if not {profile[key] for key in ('boot_path', 'ibec_path', 'kloader_path', 'helper_path')} <= checked.keys():
            raise ValueError('The iOS provision inventory lacks a required boot asset')
        asset, template = checked[profile['boot_path']]
        if (asset['sha256'] != identity['template_sha256'] or len(template) != identity['template_bytes'] or
                type(identity['slot_offset']) is not int or type(identity['slot_bytes']) is not int or
                not 0 < identity['slot_bytes'] <= 1023 or
                not 0 <= identity['slot_offset'] <= len(template) - identity['slot_bytes'] or
                template[identity['slot_offset']:identity['slot_offset'] + identity['slot_bytes']] != b' ' * identity['slot_bytes']):
            raise ValueError('iOS provision template differs from its fixed identity slot')
        policy = cascadia.host_identity_policy(template)
        if (policy is None or not recovery_identity.allows_source(policy, 'ios-device') or
                (policy['profile'], policy['chip'], policy['board']) != (device, profile['chip'], profile['board'])):
            raise ValueError('The selected iOS template lacks its public identity policy')
        if endpoint.get('identity_source') != 'ios-device' or endpoint.get('profile') != device:
            raise ValueError('The recovery has no explicit iOS device origin')
        BootState(DeviceState(device, endpoint['ecid']), endpoint['boot_id'])
        return RecoveryHandoff(endpoint['handoff_id'], identity['template_sha256'],
                               profile['chip'], profile['board'], 'ios-device')
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise ValueError('The selected iOS provision receipt does not match this recovery: ' + str(error)) from error


def add_arguments(parser):
    parser.add_argument('--device', required=True, help='board with an ios-local-file recipe')
    for name in ('ibec', 'boot-image', 'kloader', 'kloader-receipt', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--title', default='Orchard')
    parser.add_argument('--clang', default='clang-19')
    parser.add_argument('--linker', default='ld.lld-19')
    parser.add_argument('--objcopy', default='llvm-objcopy-19')
    parser.add_argument('--host-toolchain', action='store_true', help='use local LLVM executables for the freestanding shim')


def from_arguments(args):
    return prepare(args.device, args.ibec, args.boot_image, args.kloader, args.kloader_receipt,
                   args.output, title=args.title, clang=args.clang, linker=args.linker, objcopy=args.objcopy,
                   host_toolchain=args.host_toolchain)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_arguments(parser)
    args = parser.parse_args()
    print(json.dumps(from_arguments(args), indent=2))


if __name__ == '__main__':
    main()
