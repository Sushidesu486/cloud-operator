#!/usr/bin/env python3
"""Initialize one newly allocated lab block PVC from a pinned OS root archive."""
import hashlib
import json
import lzma
import shutil
import os
from pathlib import Path
import subprocess
import uuid

ROOT = Path('/work/root')
DEVICE = '/dev/vm-root'

def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)

def write(name, text, mode=0o644):
    path = ROOT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink(): path.unlink()
    path.write_text(text)
    path.chmod(mode)

def main():
    entry = json.loads(Path('/assets/catalog.json').read_text())[os.environ['OS_IMAGE']]
    archive = Path('/assets/images') / entry['file']
    h = hashlib.sha256()
    with archive.open('rb') as f:
        for chunk in iter(lambda: f.read(1024*1024), b''): h.update(chunk)
    digest = h.hexdigest()
    if digest != entry['sha256']: raise RuntimeError('Image checksum mismatch')
    sig = subprocess.run(['blkid', '-p', DEVICE], capture_output=True, text=True)
    if sig.returncode != 2: raise RuntimeError('Root must be a fresh unsigned block device')
    # Check the beginning before allowing formatting; this is only invoked for a new PVC.
    with open(DEVICE, 'rb') as f:
        if any(f.read(1024*1024)): raise RuntimeError('Root contains existing data')
    ROOT.mkdir(parents=True, exist_ok=False)
    with lzma.open(archive, 'rb') as src:
        proc = subprocess.Popen(['tar', '--numeric-owner', '-xpf', '-', '-C', str(ROOT)], stdin=subprocess.PIPE)
        try: shutil.copyfileobj(src, proc.stdin, 1024*1024)
        finally: proc.stdin.close()
        if proc.wait(): raise RuntimeError('Root archive extraction failed')
    write('etc/cloud/cloud-init.disabled', '')
    write('etc/hostname', os.environ['VM_NAME']+'\n')
    write('etc/hosts', '127.0.0.1 localhost\n127.0.1.1 '+os.environ['VM_NAME']+'\n')
    write('etc/machine-id', uuid.uuid4().hex+'\n')
    write('etc/fstab', '/dev/vda / ext4 defaults 0 1\n')
    write('etc/resolv.conf', 'nameserver 192.0.2.1\n')
    write('etc/systemd/network/10-cloud.network', '[Match]\nMACAddress=06:00:c0:00:02:02\n[Network]\nAddress=192.0.2.2/30\nGateway=192.0.2.1\nLinkLocalAddressing=no\nIPv6AcceptRA=no\n')
    write('etc/apt/sources.list.d/ubuntu.sources', '''Types: deb
URIs: https://mirrors.zju.edu.cn/ubuntu/
Suites: noble noble-updates noble-security
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
''')
    write('etc/apt/sources.list', '')
    run('chroot', str(ROOT), '/usr/sbin/useradd', '-m', '-s', '/bin/bash', 'cloud')
    run('chroot', str(ROOT), '/usr/sbin/usermod', '-p', '*', 'cloud')
    write('etc/sudoers.d/90-cloud', 'cloud ALL=(ALL) NOPASSWD:ALL\n', 0o440)
    write('home/cloud/.ssh/authorized_keys', os.environ['SSH_PUBLIC_KEY'].strip()+'\n', 0o600)
    uid = next(int(line.split(':')[2]) for line in (ROOT/'etc/passwd').read_text().splitlines() if line.startswith('cloud:'))
    for path in [ROOT/'home/cloud/.ssh', ROOT/'home/cloud/.ssh/authorized_keys']:
        os.chown(path, uid, uid)
    (ROOT/'home/cloud/.ssh').chmod(0o700)
    write('etc/ssh/sshd_config.d/00-cloud.conf', 'PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\nPubkeyAuthentication yes\n')
    run('chroot', str(ROOT), '/usr/bin/ssh-keygen', '-A')
    for pub in (ROOT/'etc/ssh').glob('ssh_host_*_key.pub'):
        print('CLOUD_HOST_KEY '+pub.read_text().strip(), flush=True)
    run('cp', '/assets/cloud-guest-helper', str(ROOT/'usr/local/sbin/cloud-guest-helper'))
    write('etc/systemd/system/cloud-guest-helper.service', '''[Unit]
Description=Cloud VM shutdown control
After=basic.target
[Service]
ExecStart=/usr/local/sbin/cloud-guest-helper --systemd
Restart=on-failure
[Install]
WantedBy=multi-user.target
''')
    # systemd tears down services and unmounts filesystems, then reset exits Firecracker.
    write('etc/systemd/system/systemd-poweroff.service.d/firecracker.conf', '[Unit]\nSuccessAction=reboot-force\n')
    for service in ['cloud-guest-helper.service', 'systemd-networkd.service', 'ssh.service']:
        run('systemctl', '--root='+str(ROOT), 'enable', service)
    run('systemctl', '--root='+str(ROOT), 'disable', 'ssh.socket')
    for service in ['systemd-resolved.service','ssh.socket','multipathd.service','multipathd.socket']:
        run('systemctl', '--root='+str(ROOT), 'mask', service)
    write('etc/cloud-image.json', json.dumps({'image': os.environ['OS_IMAGE'], 'build': entry['build'], 'sha256':digest})+'\n')
    run('mkfs.ext4', '-q', '-F', '-L', 'cloud-root', '-O', '^metadata_csum_seed', '-d', str(ROOT), DEVICE)
    run('sync')
    print('CLOUD_IMAGE_READY '+json.dumps({'os':os.environ['OS_IMAGE'], 'sha256':digest}), flush=True)

if __name__=='__main__': main()
