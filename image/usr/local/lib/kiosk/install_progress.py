"""Recognize Anaconda 44 progress without exposing its raw output."""
from ast import literal_eval
from pathlib import Path
import re

from .common import log

STAGES = {
    'Starting installer, one moment...': 'Starting installer services',
    'Loading repositories...': 'Loading Fedora repositories',
    'Saving storage configuration...': 'Planning the target disk layout',
    'Checking storage configuration...': 'Checking the target disk layout',
    'Setting up the installation environment': 'Preparing the installation environment',
    'Configuring storage': 'Partitioning and formatting the target',
    'Running pre-installation scripts': 'Running pre-installation scripts',
    'Running pre-installation tasks': 'Preparing the target filesystem',
    'Installing the software': 'Preparing the Fedora package installation',
    'Downloading packages': 'Downloading Fedora packages',
    'Preparing transaction from installation source': 'Checking the package transaction',
    'Installing boot loader': 'Installing the boot loader',
    'Performing post-installation setup tasks': 'Configuring the installed system',
    'Importing certificates': 'Installing system certificates',
    'Configuring installed system': 'Writing system settings',
    'Writing network configuration': 'Writing network settings',
    'Creating users': 'Creating the server account',
    'Configuring addons': 'Configuring installer add-ons',
    'Generating initramfs': 'Generating the initramfs',
    'Storing configuration files and kickstarts': 'Saving system configuration',
    'Running post-installation scripts': 'Applying server settings',
    'Complete!': 'Finishing and unmounting the target',
}
SIZE = r'\d{1,8}(?:\.\d{1,3})? (?:[KMGTPE]iB|bytes?|B)'
DOWNLOAD = re.compile(rf'Downloading (\d{{1,6}}) RPMs, ({SIZE}) / ({SIZE}) \((\d{{1,3}})%\) done\.')
PACKAGE = re.compile(r'Installing ([A-Za-z0-9_.+-]+) \((\d{1,6})/(\d{1,6})\)')
SCRIPT = re.compile(r'Configuring ([A-Za-z0-9_.+-]+\.[a-z0-9_]+)')
INCOMPLETE_HEADER = 'The following mandatory spokes are not completed:'
DIAGNOSES = (
    (re.compile(r'error parsing kickstart|KickstartParseError', re.I), 'Anaconda rejected the kickstart settings'),
    (re.compile(r'No space left on device|not enough (?:free |disk )?space', re.I), 'The installation ran out of disk space'),
    (re.compile(r'failed to download|Curl error|No more mirrors to try', re.I), 'Package download failed. Check the network'),
    (re.compile(r'Error setting up repositories|failed to synchronize cache', re.I), 'Fedora repository setup failed'),
    (re.compile(r'conflicting requests|Unable to install the selected packages|nothing provides', re.I),
     'Anaconda could not resolve the selected packages'),
    (re.compile(r'Unable to set password for new user', re.I), 'Anaconda could not set the server account password'),
    (re.compile(r'Error running post-installation script|Kickstart script.*failed', re.I),
     'A server configuration script failed'),
)
SPOKES = {'Installation source': 'Installation source', 'Software selection': 'Software selection',
          'Installation Destination': 'Target disk', 'Network configuration': 'Network',
          'Time settings': 'Time', 'User creation': 'User', 'Root password': 'Root', 'Language settings': 'Language'}
SOURCE_ERRORS = (
    (re.compile(r'Could not resolve host|Curl error \(6\)', re.I), 'Fedora repository DNS lookup failed'),
    (re.compile(r'SSL certificate problem|Curl error \(60\)', re.I),
     'Fedora repository certificate check failed. Check the clock'),
    (re.compile(r'Failed to download metadata|Cannot download repomd\.xml|Cannot prepare internal mirrorlist', re.I),
     'Fedora repository metadata unavailable. Check the network'),
)
DISK_ERRORS = (
    (re.compile(r'No usable disks(?: selected)?\.|No disks selected|No disks detected', re.I),
     'Anaconda cannot use the selected disk'),
    (re.compile(r'You have not defined a root partition|You must create a new file system on the root device', re.I),
     'Anaconda could not plan a new root filesystem'),
    (re.compile(r'No valid boot loader target device found|You have not created a bootable partition', re.I),
     'Anaconda could not create a bootable layout on the target'),
    (re.compile(r'not enough (?:free |disk )?space|no space left on device', re.I),
     'Anaconda found too little space for the partition layout'),
    (re.compile(r'disk .* is read.only|device is read.only|Read-only file system', re.I),
     'Anaconda found a read-only target'),
    (re.compile(r'Device or resource busy|Selected disks.*unselected disks', re.I),
     'Anaconda found the selected disk still in use'),
    (re.compile(r'Input/output error|I/O error', re.I), 'Anaconda hit an I/O error on the target disk'),
)
STORAGE_TASK_ERROR = re.compile(r'\bpartitioning\.base_partitioning: (?:Storage|Bootloader) configuration has failed: (.+)')
STORAGE_REPORT = re.compile(r'\bui\.lib\.storage: Validation has been completed: '
                            r'ValidationReport\(error_messages=(\[.{1,2048}\]), warning_messages=')
PRIVATE_TEXT = re.compile(r'\b(?:password|passphrase|psk|private.?key|token|secret)\b|\$[56]\$', re.I)
LOG_TAIL_BYTES = 256 * 1024


def log_tail(path):
    try:
        with path.open('rb') as stream:
            end = stream.seek(0, 2)
            stream.seek(max(0, end - LOG_TAIL_BYTES))
            return stream.read(LOG_TAIL_BYTES).decode('utf-8', 'replace')
    except OSError:
        return ''


def storage_detail(logs):
    # Read only Anaconda's storage-task errors and storage validation report.
    for line in reversed(logs.splitlines()):
        task = STORAGE_TASK_ERROR.search(line)
        if task:
            message = task[1]
        elif report := STORAGE_REPORT.search(line):
            try:
                errors = literal_eval(report[1])
            except (SyntaxError, ValueError, RecursionError):
                continue
            message = errors[0] if errors and isinstance(errors[0], str) else ''
        else:
            continue
        if message and not PRIVATE_TEXT.search(message):
            return message.split('\n', 1)[0].strip()[:68]
    return None


class InstallProgress:
    def __init__(self, update=lambda _: None):
        self.update = update
        self.stage = self.message = 'Starting Anaconda'
        self.completed = False
        self.error = None
        self.disk_error = None
        self.detail = None
        self.missing = []
        self.incomplete = False
        self._reading_spokes = False

    def report(self, stage, message=None):
        if self.stage != stage:
            self.stage = stage
            log('anaconda: ' + stage)
        self.message = message or stage
        self.update(self.message)

    def consume(self, output):
        message = output.strip().lstrip('.').strip()
        if self.consume_spokes(message):
            return
        if message in STAGES:
            self.report(STAGES[message])
            self.completed |= message == 'Complete!'
        elif message.startswith('Starting automated install'):
            self.report('Checking repositories and the target disk')
        elif match := DOWNLOAD.fullmatch(message):
            count, done, total, percent = match.groups()
            self.report('Downloading Fedora packages', f'Download {count} RPMs   {done} / {total} ({percent}%)')
        elif match := PACKAGE.fullmatch(message):
            name, done, total = match.groups()
            percent = f' ({100 * int(done) // int(total)}%)' if 0 < int(done) <= int(total) else ''
            self.report('Installing Fedora packages', f'Install {done}/{total}{percent}   {name}')
        elif match := SCRIPT.fullmatch(message):
            self.report('Installing Fedora packages', f'Configure package {match[1]}')
        else:
            if self.stage in ('Planning the target disk layout', 'Checking the target disk layout'):
                self.disk_error = next((text for pattern, text in DISK_ERRORS if pattern.search(message)),
                                       self.disk_error)
            for pattern, explanation in DIAGNOSES:
                if pattern.search(message):
                    self.error = explanation
                    break

    def consume_spokes(self, message):
        if message.endswith(INCOMPLETE_HEADER):
            self.incomplete = self._reading_spokes = True
            return True
        title = message.removesuffix('.').rstrip()
        if self._reading_spokes and title in SPOKES:
            label = SPOKES[title]
            if label not in self.missing:
                self.missing.append(label)
            return True
        self._reading_spokes = False
        return False

    def inspect_failure(self, directory=Path('/tmp')):
        anaconda = log_tail(directory / 'anaconda.log')
        self._reading_spokes = False
        for line in anaconda.splitlines():
            self.consume_spokes(line.strip())
        if 'Installation source' in self.missing:
            logs = anaconda + log_tail(directory / 'packaging.log')
            self.error = next((text for pattern, text in SOURCE_ERRORS if pattern.search(logs)),
                              'Anaconda could not load Fedora repositories')
        elif 'Target disk' in self.missing:
            logs = anaconda + '\n' + log_tail(directory / 'storage.log')
            self.detail = storage_detail(logs)
            self.error = self.disk_error or next((text for pattern, text in DISK_ERRORS if pattern.search(logs)),
                                                 'Anaconda rejected the target disk layout')
        elif 'Software selection' in self.missing:
            self.error = 'Anaconda could not validate the selected Fedora packages'
        elif 'Network' in self.missing:
            self.error = 'Anaconda could not confirm the network connection'

    def failure(self, status):
        if self.error:
            return self.error
        if self.incomplete:
            return 'Anaconda found incomplete installation settings'
        if status < 0:
            return f'Anaconda was terminated by signal {-status}'
        if status:
            return 'Anaconda failed during installation'
        return 'Anaconda exited before completing installation'


async def read_progress(stream, progress, on_progress=lambda: None):
    while True:
        try:
            line = await stream.readline()
        except ValueError:
            # asyncio discards overlong lines. A traceback line can contain an
            # entire Kickstart directive, so never echo the raw stream.
            continue
        if not line:
            return
        previous = progress.message
        progress.consume(line.decode('utf-8', 'replace'))
        if progress.message != previous:
            on_progress()
