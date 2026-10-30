#!/usr/bin/env python3
"""Offline backup/restore for a stopped lab VM, with exact size and digest checks."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess

os.umask(0o077)
MODE=os.environ['TRANSFER_MODE']
NAME=os.environ['BACKUP_NAME']
if '/' in NAME or not NAME.endswith('.raw.gz'): raise RuntimeError('Invalid backup name')
PATH=Path('/backup')/NAME
DEVICE='/dev/vm-root'
CHUNK=4*1024*1024

def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(CHUNK),b''):h.update(chunk)
    return h.hexdigest()

if MODE=='backup':
    if PATH.exists(): raise RuntimeError('Backup already exists')
    count=0
    temp=PATH.with_suffix('.partial')
    with open(DEVICE,'rb',buffering=0) as src,gzip.open(temp,'wb',compresslevel=1) as dst:
        while True:
            chunk=src.read(CHUNK)
            if not chunk:break
            dst.write(chunk);count+=len(chunk)
    with temp.open('rb') as f:os.fsync(f.fileno())
    h=digest(temp)
    os.replace(temp,PATH)
    metadata={'sha256':h,'rawBytes':count,'compressedBytes':PATH.stat().st_size,'file':NAME}
    PATH.with_suffix('.json').write_text(json.dumps(metadata)+'\n')
    owner=PATH.parent.stat()
    for artifact in [PATH, PATH.with_suffix('.json')]:
        artifact.chmod(0o600)
        os.chown(artifact,owner.st_uid,owner.st_gid)
    os.sync()
    print('CLOUD_BACKUP_READY '+json.dumps(metadata),flush=True)
elif MODE=='restore':
    metadata=json.loads(PATH.with_suffix('.json').read_text())
    if digest(PATH)!=metadata['sha256']:raise RuntimeError('Backup checksum mismatch')
    size=int(subprocess.check_output(['blockdev','--getsize64',DEVICE],text=True))
    if size!=metadata['rawBytes']:raise RuntimeError('Restore device size differs from backup')
    signature=subprocess.run(['blkid','-p',DEVICE],capture_output=True)
    if signature.returncode!=2:raise RuntimeError('Restore requires a new unsigned PVC')
    with open(DEVICE,'rb') as f:
        if any(f.read(1024*1024)):raise RuntimeError('Restore destination contains data')
    count=0
    with gzip.open(PATH,'rb') as src,open(DEVICE,'wb',buffering=0) as dst:
        while True:
            chunk=src.read(CHUNK)
            if not chunk:break
            count+=len(chunk)
            if count>size:raise RuntimeError('Backup exceeds destination')
            dst.write(chunk)
        os.fsync(dst.fileno())
    if count!=size:raise RuntimeError('Backup is truncated')
    print('CLOUD_RESTORE_READY '+json.dumps(metadata),flush=True)
else:raise RuntimeError('Unknown transfer mode')
