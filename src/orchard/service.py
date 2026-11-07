"""Guided host workflow: boot the recovery, then run install_device.py on it (see install_remote)."""
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace

from .state import exclusive_operation, private_directory
from .progress import Clock, Progress
from .protocol import Client
from . import install_layout, install_remote as remote
from .install_account import ask_secrets
from .releases import default_manifest, direct as installs_directly, needs_download, resolve, select_manifest
from .devices import choose


def ask(clock, prompt, input_fn=input):
    with clock.exclude('input'):
        return input_fn(prompt).strip()


def save_report(path, report):
    # Persist commitment before handoff; a reporting/transport failure must not
    # erase evidence that the root was successfully installed.
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix='.install-', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(report, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


class Background:
    """A call running while the person answers. Daemon: saying no must not wait for a download."""
    def __init__(self, function, *args, **kwargs):
        self.value = self.error = None

        def run():
            try:
                self.value = function(*args, **kwargs)
            except BaseException as error:
                self.error = error
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def done(self):
        return not self.thread.is_alive()

    def result(self):
        self.thread.join()
        if self.error is not None:
            raise self.error
        return self.value


def fetch_chain(chain, progress, clock=None):
    """Everything these boot chain inputs still need, with bars; says nothing if all are here."""
    missing = [item for item in chain if not item.cached()]
    if missing:
        progress.say('Starting download in parallel...')
        for item in [Background(item.fetch, progress, clock) for item in missing]:
            item.result()
        progress.done()


def caveats(adapter):
    """What to know before installing: the features that are not working."""
    from . import hardware
    labels = hardware.features()
    groups = {'missing': 'Not working yet', 'untested': 'Untested'}
    rows = []
    for state, title in groups.items():
        names = [label for name, label in labels.items() if adapter.FEATURES.get(name) == state]
        if names:
            rows.append(f'- {title}: ' + ', '.join(names))
    lines = ['Caveats:', *rows] if rows else []
    if adapter.STATUS != 'supported':
        lines.append('This device has not been tested yet, proceed at your own risk!')
    return lines


def ask_account(account, clock, secrets_fn, input_fn, progress):
    """What the release's account needs from the person: (secrets, account with its SSH keys), or (None, account)."""
    secrets = None
    if account and (account['password'] or account['pin']):
        with clock.exclude('input'):
            secrets = secrets_fn(account) if secrets_fn is not ask_secrets else ask_secrets(account, say=progress.say)
    if account:
        # Like an OS installer's key import: this computer may sign in over SSH.
        answer = ask(clock, 'Allow SSH logins from this computer? [Y/n] ', input_fn)
        from . import computer_key
        account = {**account, 'ssh_keys': computer_key.public_keys()
                   if answer.lower() in ('', 'y', 'yes') else []}
        if account['ssh_keys']:
            computer_key.remember_user(account['user'])
    return secrets, account


def choose_target(layout, clock, *, name='Linux', input_fn=input, output=print):
    """Pick one install target from the device's layout; replacing a partition is typed out."""
    options = install_layout.options(layout)
    if not options:
        needed = (install_layout.MIN_ROOT_BYTES + install_layout.BOOT_BYTES) / 1024**3
        raise ValueError(f'No free space or Linux partition of at least {needed:.0f} GiB. '
                         'The installer never resizes iPadOS; free space has to exist already.')
    output(f'\nWhere should {name} be installed?')
    for number, option in enumerate(options, 1):
        output(f'  {number}. {option.description}')
    single = len(options) == 1
    answer = ask(clock, 'Choose [1]: ' if single else 'Choose: ', input_fn) or ('1' if single else '')
    if not answer.isdecimal() or not 1 <= int(answer) <= len(options):
        raise ValueError('No valid target was chosen; nothing was changed')
    option = options[int(answer) - 1]
    if option.kind == 'replace':
        phrase = f'erase {option.partition["number"]}'
        if option.partition['type'] == install_layout.LINUX_DATA:
            # Installs from the previous installer were one Linux-data partition; this
            # installer writes a boot partition and an arm64 root instead.
            output('This looks like an install from the previous Orchard installer. It cannot be '
                   'converted: copy anything you want to keep off the device first.')
        output('Everything on that partition will be deleted. No backup is made.')
        if ask(clock, f'Type "{phrase}" to confirm: ', input_fn) != phrase:
            raise ValueError('Not confirmed; nothing was changed')
    return option


def entry_for(record, devicetree):
    name = record.get('name', 'Linux')
    return {'id': re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-') or 'linux', 'title': name,
            # Quiet as the recovery: no console text over the splash; the system maps its console.
            'options': record.get('cmdline', 'rootwait') + ' fbcon=map:9 vt.global_cursor_default=0',
            'devicetree': devicetree}


def check_started(connection, user, ready, progress, seconds=180, *, hosts=None, poll=1):
    """Whether the started system is up, over its own SSH with this computer's key.

    Only a report: it never changes the system. Until the switch the recovery
    still answers, and a refused start is reported at once.
    """
    from . import computer_key
    progress.step('Waiting until it is up')
    system = computer_key.ssh_argv(user, forget=True)
    command = 'test -e ' + shlex.quote(ready) if ready else 'true'
    recovery = True
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if recovery:
                # The recovery answers until the switch; meanwhile the system is not up yet.
                result = subprocess.run(connection.argv + [
                    '[ -e /run/orchard/start.failed ] && { cat /run/orchard/start.log; exit 3; }; exit 0'],
                    stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
                if result.returncode == 3:
                    return 'The recovery could not start the system: ' + \
                        result.stdout.decode(errors='replace').strip()[-400:]
                if result.returncode == 0:
                    time.sleep(poll)
                    continue
                recovery = False
                system = computer_key.ssh_argv(user, forget=True)  # the system's own host key, learned afresh
            for host in (hosts() if hosts else (None,)):
                argv = system if host is None else computer_key.ssh_argv(user, host, timeout=1, forget=True)
                result = subprocess.run(argv + [command], stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
                if result.returncode == 0:
                    return None
        except subprocess.TimeoutExpired:
            pass
        time.sleep(poll)
    return ('The system did not report that it is ready within 3 minutes'
            if ready else 'The system did not answer over SSH within 3 minutes')


def install_direct(adapter, device, name, files, firmware_files, boot_files, chain_files, secrets, answered, *,
                   clock, input_fn, secrets_fn, progress):
    """An install from a block image: no recovery, see install_direct.py."""
    from . import install_direct as direct
    from .state import DeviceState
    if device['resumed']:
        raise ValueError('This release installs without a recovery. Put the device in DFU mode, then run: orchard install')
    disk = os.environ.get('ORCHARD_INSTALL_DISK', direct.DISK)
    interfaces = direct.link_local_interfaces()
    state = private_directory(DeviceState(adapter.PROFILE, device['ecid']).root)
    progress.section('Getting ready', keep_rows=True)
    boot_record, boot_assets = boot_files.result()
    for item in chain_files:
        item.result()
    with tempfile.TemporaryDirectory(prefix='install-', dir=state) as directory:
        client, host_key, authorized, dropbear_host_key = direct.make_keys(directory)
        extra = direct.add_on(boot_assets['install_tools'], device['ecid'], authorized, dropbear_host_key)
        image_file = Path(directory) / 'payload.bin'
        image_file.write_bytes(direct.boot_image(adapter, boot_assets, extra))
        with progress.alongside('Device'):
            found = direct.start(adapter, device, image_file, boot_assets, progress)
        progress.mark('Install mode on USB')
        progress.pause()
        progress.step('Connecting over SSH')
        with direct.session(adapter.PROFILE, device, found, directory, client, host_key, interfaces) as connection:
            progress.mark('SSH connected')
            layout = direct.inspect(connection, disk)
            progress.mark('Disk read')
            progress.done()
            option = choose_target(layout, clock, name=name, input_fn=input_fn, output=progress.say)
            if answered is not None:
                account = answered
            else:
                secrets, account = ask_account(boot_record.get('account'), clock, secrets_fn, input_fn, progress)
            # That was the last question; nothing has been written yet.
            progress.resume()
            if not (files.done() and firmware_files.done()):
                progress.section('Downloading the rest', keep_rows=True)
            record, assets = files.result()
            firmware = firmware_files.result()
            root = record['assets']['root']
            progress.section('Installing ' + name)
            progress.step('Preparing the installation')
            mixin, mixin_record = adapter.prepare_mixin(firmware, Path(directory) / 'mixin')
            remote.put(connection, mixin.read_bytes(), 'firmware.tar.gz')
            layer = record['assets']['layer']
            remote.put(connection, assets['layer'].read_bytes(), 'platform.tar.gz', timeout=600)
            image = {key: root[key] for key in ('bytes', 'sha256', 'expanded_bytes')}
            plan = install_layout.plan(layout, option, image, entry_for(record, adapter.DEVICETREE), account=account, layer={
                'path': remote.DIRECTORY + '/platform.tar.gz',
                **{key: layer[key] for key in ('bytes', 'sha256', 'expanded_bytes')}}, firmware={
                'path': remote.DIRECTORY + '/firmware.tar.gz',
                'bytes': mixin_record['bytes'], 'sha256': mixin_record['sha256']})
            nodes, partitions = direct.write_partitions(connection, plan, disk)
            progress.step('Partitions created')
            root_node = nodes[plan['root']['partuuid']]
            if plan['boot']['create']:
                sector = layout['disk']['sector']
                for offset, data in direct.D.fat32_regions(plan['boot']['blocks'], sector, int(plan['boot']['fs_uuid'], 16)):
                    direct.write_at(connection, disk, sector, plan['boot']['start'] + offset // sector, bytes(data))
                boot_node = nodes[plan['boot']['partuuid']]
            else:
                boot_node = direct.D.node(disk, plan['boot']['number'])
            ranges = json.loads(assets['map'].read_text())['ranges']
            with assets['root'].open('rb') as source:
                timings = direct.stream_image(connection, root_node, image, source,
                                              ''.join(f'{start} {count}\n' for start, count in ranges), progress)
            progress.step('System files written')
            direct.grow(connection, root_node, plan['root']['fs_uuid'], plan['root']['label'])
            progress.mark('Grown to the partition')
            direct.mount_new_root(connection, root_node, boot_node)
            progress.mark('Root mounted')
            replaced = (plan.get('remove') or {}).get('partuuid')
            plan['present'] = [p['partuuid'] for p in partitions if p['partuuid'] != replaced]
            remote.put(connection, remote.SCRIPT.read_bytes(), remote.SCRIPT.name)
            remote.put(connection, json.dumps(plan).encode(), 'plan.json')
            if secrets is not None:
                remote.put(connection, json.dumps(secrets).encode(), 'secrets.json')
            secrets = None
            inside = SimpleNamespace(argv=connection.argv + ['chroot /newroot'], check=connection.check)
            events = remote.run(inside, ['finish', remote.DIRECTORY + '/plan.json'], progress=progress)
            timeline = direct.shell(connection, 'cat /run/orchard/timeline').decode()
            progress.mark('Finished inside the root')
            direct.unmount_new_root(connection, plan['root']['partuuid'])
            progress.mark('Unmounted, boot continues')
            report = {'device_timeline': timeline, 'profile': connection.scope.device.profile, 'ecid': connection.scope.device.ecid,
                      'boot_id': connection.scope.boot_id, 'installed': True, 'release': record['release'],
                      'root': plan['root']['partuuid'], 'boot': plan['boot']['partuuid'],
                      'written': timings, 'events': events, 'timing': clock.result()}
            path = private_directory(connection.scope.root) / 'last-install.json'
            save_report(path, report)
            progress.done('Installed')
            progress.section('Starting ' + name)
            if account and account['ssh_keys']:
                problem = check_started(connection, account['user'], record.get('ready'), progress, poll=0.2,
                                        hosts=lambda: direct.device_hosts(interfaces))
                progress.mark('System reported ready')
                progress.done('Could not confirm that it started: ' + problem if problem else None)
                report['ready'] = problem is None
            report['started'] = True
            report['phases'] = progress.timings
            save_report(path, report)
    progress.done()
    login = (f'\nSign in over SSH: ssh {account["user"]}@172.16.42.1'
             if report.get('ready') and account and account['ssh_keys'] else '')
    progress.say(f'\n{name} is {"ready" if report.get("ready") else "starting"}.{login}\n'
                 'To start it again later, put the device in DFU mode and run: orchard launch')
    return report


@exclusive_operation
def install(manifest=None, *, input_fn=input, secrets_fn=ask_secrets, progress=None):
    clock, progress = Clock(), progress or Progress()
    adapter, device = choose()  # Definition of installation timing begins here.
    if not adapter.INSTALL:
        ram = ' RAM boot is available for testing.' if adapter.DEVICETREE else ''
        raise ValueError(f'Installing on the {adapter.NAME} is not available yet: '
                         f'{"; ".join(adapter.INSTALL_BLOCKERS)}.{ram}')
    source = manifest or default_manifest(adapter.PROFILE)
    document = select_manifest(source, adapter.PROFILE, None, clock)[0]
    name = document.get('name', 'Linux')
    # What the chain needs to start: only when it will run from DFU.
    chain = adapter.boot_inputs() if device.get('mode') == 'dfu' else []
    firmware_here = adapter.firmware_cached()
    if needs_download(document, adapter.PROFILE) or not firmware_here or any(not item.cached() for item in chain):
        progress.say('Starting download in parallel...')
    # Everything starts downloading now, while the person reads and answers.
    with progress.hold():
        files = Background(resolve, source, adapter.PROFILE, progress, clock=clock)
        # The small files on their own, so that nothing waits for the system to download. Both ask for
        # the same files by hash: the second waits for the first (materialize) and shows no bar of its own.
        boot_files = Background(resolve, source, adapter.PROFILE, progress, clock=clock, purpose='boot')
        # A cache hit is silent: its row would only say that nothing had to happen.
        firmware_files = Background(adapter.firmware_inputs, clock,
                                    Progress(io.StringIO()) if firmware_here else progress.task('Firmware'))
        chain_files = [Background(item.fetch, progress, clock) for item in chain]
        progress.say(f'Installing {name} on {adapter.NAME}')
        for line in caveats(adapter):
            progress.say('  ' + line)
        answer = ask(clock, 'Continue? [Y/n] ', input_fn)
        if answer.lower() not in ('', 'y', 'yes'):
            progress.say('Cancelled; nothing on the device was changed')
            return {'cancelled': True}
        # The only question left after these is where to install, which takes the running device.
        early_account = (document.get('install') or {}).get('account')
        secrets, answered = ask_account(early_account, clock, secrets_fn, input_fn, progress) if early_account else (None, None)
    if installs_directly(document):
        return install_direct(adapter, device, name, files, firmware_files, boot_files, chain_files, secrets, answered,
                              clock=clock, input_fn=input_fn, secrets_fn=secrets_fn, progress=progress)
    # Answered: the bars can show now, with the device starting beside them.
    if not device['resumed'] or not (files.done() and firmware_files.done()):
        progress.section('Getting ready', keep_rows=True)
    boot_record, boot_assets = boot_files.result()
    if not device['resumed']:
        # Each stage waits for what it needs, and only that: the system goes on downloading meanwhile.
        for item in chain_files:
            item.result()
        with progress.alongside('Device'):
            adapter.boot(device, boot_assets, boot_record, progress)
    progress.pause()
    progress.step('Connecting over SSH')
    with adapter.connect(device) as connection:
        private = private_directory(connection.scope.root)
        identity = {'profile': connection.scope.device.profile, 'ecid': connection.scope.device.ecid,
                    'boot_id': connection.scope.boot_id}
        status = Client(connection, progress).call('status')
        if status['release'] != boot_record['release']:
            raise ValueError('Running recovery is not the selected release.\n' + adapter.REBOOT_MESSAGE)
        remote.put(connection, remote.SCRIPT.read_bytes(), remote.SCRIPT.name)
        (layout,) = [e for e in remote.run(connection, ['inspect']) if e['event'] == 'layout']
        progress.done()
        option = choose_target(layout, clock, name=name, input_fn=input_fn, output=progress.say)
        # The account is what the release declares; it may ask for nothing.
        if answered is not None:
            account = answered
        else:
            secrets, account = ask_account(boot_record.get('account'), clock, secrets_fn, input_fn, progress)
        # That was the last question; nothing has been written yet. What's left to download is waited for
        # here, so someone can walk away from a slow download now.
        progress.resume()
        if not (files.done() and firmware_files.done()):
            progress.section('Downloading the rest', keep_rows=True)
        record, assets = files.result()
        firmware = firmware_files.result()
        root = record['assets']['root']
        if 'expanded_bytes' not in root:
            raise ValueError('This release does not state its unpacked size; it cannot be installed')
        progress.section('Installing ' + name)
        progress.step('Preparing the installation')
        with tempfile.TemporaryDirectory(prefix='installation-', dir=private) as directory:
            mixin, mixin_record = adapter.prepare_mixin(firmware, Path(directory) / 'mixin')
            remote.put(connection, mixin.read_bytes(), 'firmware.tar.gz')
        layer = record['assets']['layer']
        remote.put(connection, assets['layer'].read_bytes(), 'platform.tar.gz', timeout=600)
        image = {key: root[key] for key in ('bytes', 'sha256', 'expanded_bytes')}
        image['compression'] = root.get('compression', 'zstd')
        plan = install_layout.plan(layout, option, image, entry_for(record, adapter.DEVICETREE), account=account, layer={
            'path': remote.DIRECTORY + '/platform.tar.gz',
            **{key: layer[key] for key in ('bytes', 'sha256', 'expanded_bytes')}}, firmware={
            'path': remote.DIRECTORY + '/firmware.tar.gz',
            'bytes': mixin_record['bytes'], 'sha256': mixin_record['sha256']})
        remote.put(connection, json.dumps(plan).encode(), 'plan.json')
        if secrets is not None:
            remote.put(connection, json.dumps(secrets).encode(), 'secrets.json')
        secrets = None
        with assets['root'].open('rb') as source:
            events = remote.run(connection, ['run', remote.DIRECTORY + '/plan.json'],
                                source=source, size=root['bytes'], progress=progress)
        finished = [e for e in events if e.get('event') == 'finished']
        if len(finished) != 1:
            raise RuntimeError('The device did not confirm the installation; '
                               f'see {remote.DIRECTORY}/install.log on the recovery')
        report = {**identity, 'installed': True, 'release': record['release'],
                  'resumed': bool(device['resumed']), 'root': finished[0]['root'],
                  'boot': finished[0]['boot'], 'timing': clock.result()}
        path = private / 'last-install.json'
        save_report(path, report)
        # The recovery runs the same kernel the new entry boots, so it switches
        # into the new root right away: the first start needs no second DFU boot.
        # The switch adds nothing: the system boots as from its own entry.
        progress.done('Installed')
        progress.section('Starting ' + name)
        Client(connection, progress).call('start', {'root': report['root'], 'boot': report['boot']})
        report['started'] = True
        save_report(path, report)
        if account and account['ssh_keys']:
            problem = check_started(connection, account['user'], record.get('ready'), progress)
            progress.done('Could not confirm that it started: ' + problem if problem else None)
            report['ready'] = problem is None
        report['phases'] = progress.timings
        save_report(path, report)
    progress.done()
    login = (f'\nSign in over SSH: ssh {account["user"]}@172.16.42.1'
             if report.get('ready') and account and account['ssh_keys'] else '')
    progress.say(f'\n{name} is {"ready" if report.get("ready") else "starting"}.{login}\n'
                 'To start it again later, put the device in DFU mode and run: orchard launch')
    return report
