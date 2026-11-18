"""Read-back verified battery limits; never force a discharge on mains."""
import json
from pathlib import Path
import re
import sys
import syslog

from .common import Failure, output, private_write, run

DEFAULTS = dict(START_PERCENT='45', STOP_PERCENT='55', FULL_PERCENT='100',
                GUARD_ENABLED='yes', GUARD_SHUTDOWN_AT='20', BATTERY='auto')


def config(path=Path('/etc/battery-limits.conf')):
    values = DEFAULTS.copy()
    if path.exists():
        for line in path.read_text().splitlines():
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            key, sep, value = line.partition('=')
            if not sep or key.strip() not in values:
                raise Failure('Invalid battery configuration; edit the config file')
            values[key.strip()] = value.strip()
    for key in ('START_PERCENT', 'STOP_PERCENT', 'FULL_PERCENT', 'GUARD_SHUTDOWN_AT'):
        if not values[key].isdigit() or not 0 <= int(values[key]) <= 100:
            raise Failure('Invalid battery percentage; edit the config file')
        values[key] = int(values[key])
    if not values['START_PERCENT'] < values['STOP_PERCENT'] <= values['FULL_PERCENT']:
        raise Failure('Invalid battery corridor; edit the config file')
    if values['GUARD_ENABLED'] not in ('yes', 'no') or not re.fullmatch(r'auto|[A-Za-z0-9_-]+', values['BATTERY']):
        raise Failure('Invalid battery configuration; edit the config file')
    return values


def read(path, default=''):
    try:
        return path.read_text().strip()
    except OSError:
        return default


def batteries(root=Path('/sys/class/power_supply'), name='auto'):
    return [p for p in sorted(root.glob('*')) if read(p / 'type') == 'Battery'
            and read(p / 'present', '1') == '1' and (name == 'auto' or p.name == name)]


def laptop(root=Path('/sys/class/power_supply')):
    present = bool(batteries(root))
    chassis = read(Path('/sys/class/dmi/id/chassis_type')) in {'8', '9', '10', '14'}
    lid = Path('/proc/acpi/button/lid').exists()
    if not lid:
        try:
            lid = 'battery' in output(['upower', '-e'], timeout=5, check=False).lower()
        except Failure:
            pass
    return sum((present, chassis, lid)) >= 2


def ac_online(root):
    # Never infer mains from battery status. Unknown is distinct from offline.
    values = [read(p / 'online') for p in root.glob('*') if read(p / 'type') != 'Battery'
              and (p / 'online').exists()]
    if not values or any(x not in ('0', '1') for x in values):
        return None
    return '1' in values


def write_verified(path, value):
    path.write_text(str(value) + '\n')
    actual = path.read_text().strip()
    if actual != str(value) and f'[{value}]' not in actual:
        raise Failure('Battery write ignored; check firmware charge settings')


def thresholds(pack, start, stop):
    lower, upper = pack / 'charge_control_start_threshold', pack / 'charge_control_end_threshold'
    if not upper.exists():
        return False
    # Keep start below stop during both normal->full and full->normal changes.
    if lower.exists():
        write_verified(lower, 0)
    write_verified(upper, stop)
    if lower.exists():
        write_verified(lower, start)
    behaviour = pack / 'charge_behaviour'
    if behaviour.exists() and 'auto' in read(behaviour):
        write_verified(behaviour, 'auto')
    return True


def apply_pack(pack, cfg, mode):
    start = cfg['START_PERCENT'] if mode == 'normal' else 0
    stop = cfg['STOP_PERCENT'] if mode == 'normal' else cfg['FULL_PERCENT'] if mode == 'full' else 100
    try:
        if thresholds(pack, start, stop):
            return f'{pack.name}: {start}-{stop}% verified'
        behaviour = pack / 'charge_behaviour'
        available = read(behaviour)
        if 'inhibit-charge' in available and 'auto' in available:
            cap = int(read(pack / 'capacity'))
            active = 'inhibit-charge' if '[inhibit-charge]' in available else 'auto'
            desired = 'auto' if mode != 'normal' or cap <= start else 'inhibit-charge' if cap >= stop else active
            write_verified(behaviour, desired)
            return f'{pack.name}: behaviour {desired} verified (60s polling)'
    except (OSError, ValueError, Failure):
        pass
    # TLP is installed but never enabled globally alongside our policy.
    try:
        run(['tlp', 'setcharge', str(start), str(stop), pack.name], timeout=15)
        lower = pack / 'charge_control_start_threshold'
        if read(pack / 'charge_control_end_threshold') == str(stop) and (not lower.exists() or read(lower) == str(start)):
            return f'{pack.name}: TLP {stop}% verified'
    except Failure:
        pass
    raise Failure(f'{pack.name}: limits unavailable; check firmware (TLP unverified)')


def apply(cfg, mode, root=Path('/sys/class/power_supply')):
    packs = batteries(root, cfg['BATTERY'])
    if not packs:
        return ['No battery; policy skipped']
    messages = []
    for pack in packs:
        try:
            messages.append(apply_pack(pack, cfg, mode))
        except Failure as error:
            messages.append(str(error))
    return messages


def status(cfg, root=Path('/sys/class/power_supply')):
    mains = ac_online(root)
    result = ['AC online: ' + ('unknown' if mains is None else str(int(mains)))]
    for pack in batteries(root, cfg['BATTERY']):
        state = read(pack / 'status', 'unknown')
        result.append(f"{pack.name}: {read(pack / 'capacity', '?')}% {state}")
        result.append(f"Thresholds: {read(pack / 'charge_control_start_threshold', '-')} / {read(pack / 'charge_control_end_threshold', '-')}")
        if mains and state == 'Discharging':
            result.append('Warning: undersized adapter or deliberate firmware discharge')
    return result


def guard(cfg, root=Path('/sys/class/power_supply'), state_dir=Path('/run/battery-limits')):
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_file = state_dir / 'ac'
    online = ac_online(root)
    previous = read(state_file, 'unknown')
    if online is not None:
        private_write(state_file, str(int(online)))
        if not online and previous != '0':
            syslog.syslog(syslog.LOG_WARNING, 'battery-guard: adapter loss')
        elif online and previous == '0':
            syslog.syslog(syslog.LOG_INFO, 'battery-guard: adapter restored')
    if online is True:
        for pack in batteries(root):
            if read(pack / 'status') == 'Discharging':
                syslog.syslog(syslog.LOG_WARNING, 'battery-guard: undersized supply or firmware discharge')
    if cfg['GUARD_ENABLED'] == 'yes' and online is False:
        capacities = [int(read(p / 'capacity', '101')) for p in batteries(root, cfg['BATTERY'])]
        if capacities and min(capacities) < cfg['GUARD_SHUTDOWN_AT']:
            syslog.syslog(syslog.LOG_CRIT, 'battery-guard: reserve exhausted; powering off')
            run(['systemctl', 'poweroff'], timeout=10)


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else 'status'
    if command not in ('status', 'normal', 'full', 'off', 'apply', 'guard'):
        raise Failure('Usage: battery-limits status|normal|full|off')
    cfg = config()
    mode_file = Path('/var/lib/battery-limits/mode')
    mode = read(mode_file, 'normal')
    if mode not in ('normal', 'full', 'off'):
        raise Failure('Invalid battery mode; run battery-limits normal')
    if command in ('normal', 'full', 'off'):
        mode_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        private_write(mode_file, command)
        mode = command
    if command == 'status':
        print('\n'.join(['Mode: ' + mode] + status(cfg)))
        decision = Path('/etc/battery-limits-decision')
        if decision.exists():
            print(decision.read_text().strip())
    else:
        if command == 'guard':
            guard(cfg)
        messages = apply(cfg, mode)
        if command != 'guard':
            print('\n'.join(messages))
        for message in messages:
            if 'unavailable' in message:
                syslog.syslog(syslog.LOG_WARNING, message)
        if command != 'guard' and any('unavailable' in message for message in messages):
            return 2


if __name__ == '__main__':
    main()
