"""Recognize Anaconda 44 progress without exposing its raw output."""
import re

from .common import log

STAGES = {
    'Starting installer, one moment...': 'Starting installer services',
    'Loading repositories...': 'Loading Fedora repositories',
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
DIAGNOSES = (
    (re.compile(r'not completed:|CmdlineError', re.I), 'Anaconda found incomplete installation settings'),
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
SPOKES = {'Installation source', 'Software selection', 'Installation Destination', 'Network configuration',
          'Time settings', 'User creation', 'Root account'}


class InstallProgress:
    def __init__(self, update=lambda _: None):
        self.update = update
        self.stage = self.message = 'Starting Anaconda'
        self.completed = False
        self.error = None
        self.missing = []

    def report(self, stage, message=None):
        if self.stage != stage:
            self.stage = stage
            log('anaconda: ' + stage)
        self.message = message or stage
        self.update(self.message)

    def consume(self, output):
        message = output.strip().lstrip('.').strip()
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
        elif message in SPOKES and self.error == DIAGNOSES[0][1]:
            if message not in self.missing:
                self.missing.append(message)
        else:
            for pattern, explanation in DIAGNOSES:
                if pattern.search(message):
                    self.error = explanation
                    break

    def failure(self, status):
        if self.error:
            return self.error
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
