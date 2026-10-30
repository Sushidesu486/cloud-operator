#!/usr/bin/env python3
"""Persistent two-user Ubuntu acceptance environment on m600.

Uses the explicitly labelled cloud-operator-lab. Run prepare once, then create,
ssh, stop, start, restore and check. Resources remain available for acceptance.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
import lab

HERE, LOCAL, NS = lab.HERE, lab.LOCAL, lab.NS
ASSETS = lab.REMOTE+'/assets'
CATALOG = json.loads((HERE/'images/catalog.json').read_text())
OWNERS = ('alice', 'bob')


def ensure_owned(kind, name):
    return lab.owned(kind, name)


def apply(obj):
    ensure_owned(obj['kind'], obj['metadata']['name'])
    lab.apply(obj)


def copy(src, dst):
    subprocess.run(['scp', str(src), 'm600:'+dst+'.upload'], check=True, capture_output=True)
    lab.ssh(shlex.join(['mv', dst+'.upload', dst]))


def wait(desc, fn, seconds=240):
    print('Waiting: '+desc, flush=True)
    return lab.wait_for(desc, fn, seconds)


def key(owner):
    path=LOCAL/(owner+'-acceptance-key')
    if not path.exists():
        subprocess.run(['ssh-keygen','-q','-t','ed25519','-N','','-C','cloud-acceptance-'+owner,'-f',str(path)],check=True)
    return path


def prepare():
    ensure_owned('namespace',NS); ensure_owned('crd',lab.CRD)
    lab.ssh('mkdir -p '+shlex.quote(ASSETS+'/images'))
    for src,dst in [(HERE/'bin/cloud-operator',ASSETS+'/cloud-operator'),
        (HERE.parent/'cloud-launcher/bin/cloud-launcher',ASSETS+'/cloud-launcher'),
        (HERE.parent/'cloud-launcher/bin/cloud-guest-helper',ASSETS+'/cloud-guest-helper'),
        (HERE/'images/build-root.py',ASSETS+'/build-ubuntu.py'),
        (HERE/'images/volume-transfer.py',ASSETS+'/volume-transfer.py'),
        (HERE/'images/catalog.json',ASSETS+'/catalog.json')]: copy(src,dst)
    crd=lab.decode(lab.kubectl('create','--dry-run=client','--validate=false','-f','-','-o','json',data=(HERE/'config/crd/cloud.zjusct.io_cloudvms.yaml').read_text()))
    crd['metadata'].setdefault('labels',{})[lab.LABEL]=lab.TAG
    apply(crd)
    lab.kubectl('wait','--for=condition=Established','crd/'+lab.CRD,'--timeout=30s')
    apply({'apiVersion':'v1','kind':'Namespace','metadata':{'name':NS,'labels':{lab.LABEL:lab.TAG}}})
    apply(lab.resource('networking.k8s.io/v1','NetworkPolicy','deny-all',spec={'podSelector':{},'policyTypes':['Ingress','Egress'],'ingress':[],'egress':[]}))
    apply(lab.resource('v1','ResourceQuota','budget',spec={'hard':{'pods':'8','requests.cpu':'8','limits.cpu':'8','requests.memory':'10Gi','limits.memory':'10Gi','persistentvolumeclaims':'5','requests.storage':'40Gi','count/cloudvms.cloud.zjusct.io':'4'}}))
    apply(lab.resource('v1','ServiceAccount','operator'))
    apply(lab.resource('rbac.authorization.k8s.io/v1','Role','operator',rules=[
      {'apiGroups':['cloud.zjusct.io'],'resources':['cloudvms'],'verbs':['get','list','watch']},
      {'apiGroups':['cloud.zjusct.io'],'resources':['cloudvms/status'],'verbs':['get','patch','update']},
      {'apiGroups':[''],'resources':['pods'],'verbs':['get','list','watch','create','delete']},
      {'apiGroups':[''],'resources':['persistentvolumeclaims'],'verbs':['get','list','watch','update','patch']},
      {'apiGroups':[''],'resources':['events'],'verbs':['create','patch']},
      {'apiGroups':['coordination.k8s.io'],'resources':['leases'],'verbs':['get','list','watch','create','update','patch']},
    ]))
    apply(lab.resource('rbac.authorization.k8s.io/v1','RoleBinding','operator',roleRef={'apiGroup':'rbac.authorization.k8s.io','kind':'Role','name':'operator'},subjects=[{'kind':'ServiceAccount','name':'operator','namespace':NS}]))
    apply(lab.resource('cilium.io/v2','CiliumNetworkPolicy','operator-api',spec={'endpointSelector':{'matchLabels':{'app':'cloud-operator'}},'egress':[{'toEntities':['kube-apiserver'],'toPorts':[{'ports':[{'port':'6443','protocol':'TCP'},{'port':'443','protocol':'TCP'}]}]}]}))
    apply(lab.resource('cilium.io/v2','CiliumNetworkPolicy','vm-network',spec={
      'endpointSelector':{'matchExpressions':[{'key':'cloud.zjusct.io/owner','operator':'Exists'}]},
      'ingress':[{'fromEntities':['host','remote-node'],'toPorts':[{'ports':[{'port':'2222','protocol':'TCP'}]}]}],
      'egress':[
        {'toEndpoints':[{'matchLabels':{'k8s:io.kubernetes.pod.namespace':'kube-system','k8s:k8s-app':'kube-dns'}}], 'toPorts':[{'ports':[{'port':'53','protocol':'ANY'}],'rules':{'dns':[{'matchPattern':'*'}]}}]},
        {'toFQDNs':[{'matchName':'mirrors.zju.edu.cn'}],'toPorts':[{'ports':[{'port':'443','protocol':'TCP'},{'port':'80','protocol':'TCP'}]}]},
        {'toCIDRSet':[{'cidr':'0.0.0.0/0','except':['10.0.0.0/8','172.16.0.0/12','192.168.0.0/16','169.254.0.0/16','127.0.0.0/8','100.64.0.0/10','0.0.0.0/8','224.0.0.0/4','240.0.0.0/4']}],'toPorts':[{'ports':[{'port':'443','protocol':'TCP'},{'port':'80','protocol':'TCP'}]}]},
      ]}))
    deploy=lab.resource('apps/v1','Deployment','cloud-operator',spec={'replicas':1,'selector':{'matchLabels':{'app':'cloud-operator'}},'template':{'metadata':{'labels':{'app':'cloud-operator'},'annotations':{'cloud.zjusct.io/build':hashlib.sha256((HERE/'bin/cloud-operator').read_bytes()).hexdigest()}},'spec':{
      'serviceAccountName':'operator','nodeSelector':{'kubernetes.io/hostname':lab.NODE},
      'containers':[{'name':'operator','image':lab.IMAGE,'command':['/assets/cloud-operator','--namespace='+NS,'--development','--network-nat','--node='+lab.NODE,'--assets='+ASSETS,'--runner-image='+lab.IMAGE],
       'resources':{'requests':{'cpu':'100m','memory':'256Mi'},'limits':{'cpu':'500m','memory':'512Mi'}},
       'securityContext':{'readOnlyRootFilesystem':True,'allowPrivilegeEscalation':False},
       'volumeMounts':[{'name':'assets','mountPath':'/assets','readOnly':True}]}],
      'volumes':[{'name':'assets','hostPath':{'path':ASSETS,'type':'Directory'}}]}}})
    apply(deploy)
    for owner in OWNERS: key(owner)
    print('Acceptance environment prepared; operator runs as a Deployment.',flush=True)


def download(image):
    entry=CATALOG[image]
    # Fixed catalog URL only. Never accept a caller-provided download location.
    script='''import hashlib,json,pathlib,urllib.request,os
entry=json.loads(%r)
p=pathlib.Path(%r)/entry['file']
def valid():
 if not p.exists(): return False
 with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()==entry['sha256']
if not valid():
 tmp=p.with_suffix('.partial')
 with urllib.request.urlopen(entry['url'],timeout=60) as src,tmp.open('wb') as dst:
  while chunk:=src.read(1024*1024):dst.write(chunk)
 with tmp.open('rb') as f:
  if hashlib.file_digest(f,'sha256').hexdigest()!=entry['sha256']:raise RuntimeError('checksum mismatch')
 os.replace(tmp,p)
print('Image cache verified: '+entry['file'])
'''%(json.dumps(entry),ASSETS+'/images')
    print(lab.ssh('python3 -',script,timeout=180).strip(),flush=True)


def pvc(name, owner, source=None):
    obj=lab.resource('v1','PersistentVolumeClaim',name,spec={'accessModes':['ReadWriteOnce'],'volumeMode':'Block','storageClassName':'ceph-block','resources':{'requests':{'storage':'8Gi'}}})
    obj['metadata']['labels']['cloud.zjusct.io/owner']=owner
    if source: obj['spec']['dataSource']={'kind':'PersistentVolumeClaim','name':source}
    if not ensure_owned('pvc',name): lab.kubectl('create','-f','-',data=obj)
    return obj


def vm(name, owner, root):
    obj=lab.resource('cloud.zjusct.io/v1alpha1','CloudVM',name,spec={'owner':owner,'rootVolumeClaim':root,'vcpus':1,'memoryMiB':1024,'powerState':'Running','bootID':1})
    if not ensure_owned('cloudvm',name): lab.kubectl('create','-f','-',data=obj)
    current=lab.get('cloudvm',name)
    svc=lab.resource('v1','Service',name+'-ssh',spec={'selector':{'cloud.zjusct.io/vm-uid':current['metadata']['uid']},'ports':[{'name':'ssh','port':22,'targetPort':2222}]})
    apply(svc)


def create(owner, image):
    if owner not in OWNERS: raise RuntimeError('This acceptance environment has two static users: alice, bob')
    download(image)
    name=owner+'-root'
    pvc(name,owner)
    jobname='image-'+owner
    previous=ensure_owned('job',jobname)
    if previous and any(c.get('type')=='Failed' and c.get('status')=='True' for c in previous.get('status',{}).get('conditions',[])):
        (LOCAL/(jobname+'-failed.log')).write_text(lab.kubectl('logs','job/'+jobname))
        lab.kubectl('delete','job',jobname,'--wait=true')
        previous=None
    if not previous:
        job=lab.resource('batch/v1','Job',jobname,spec={'backoffLimit':0,'template':{'spec':{
          'restartPolicy':'Never','automountServiceAccountToken':False,'nodeSelector':{'kubernetes.io/hostname':lab.NODE},
          'containers':[{'name':'image','image':lab.IMAGE,'command':['python3','/assets/build-ubuntu.py'],
            'env':[{'name':'OS_IMAGE','value':image},{'name':'VM_NAME','value':owner},{'name':'SSH_PUBLIC_KEY','value':key(owner).with_suffix('.pub').read_text()}],
            'resources':{'requests':{'cpu':'1','memory':'1Gi'},'limits':{'cpu':'2','memory':'2Gi'}},
            'volumeMounts':[{'name':'assets','mountPath':'/assets','readOnly':True},{'name':'work','mountPath':'/work'}],
            'volumeDevices':[{'name':'root','devicePath':'/dev/vm-root'}]}],
          'volumes':[{'name':'assets','hostPath':{'path':ASSETS,'type':'Directory'}},{'name':'work','emptyDir':{'sizeLimit':'4Gi'}},{'name':'root','persistentVolumeClaim':{'claimName':name}}]}}})
        lab.kubectl('create','-f','-',data=job)
    def ready():
        j=lab.get('job',jobname)
        for c in j.get('status',{}).get('conditions',[]):
            if c['type']=='Failed' and c['status']=='True':raise RuntimeError(lab.kubectl('logs','job/'+jobname,'--tail=60'))
            if c['type']=='Complete' and c['status']=='True':return True
    wait('OS root preparation for '+owner,ready,300)
    logs=lab.kubectl('logs','job/'+jobname)
    hostkeys=[line.split('CLOUD_HOST_KEY ',1)[1] for line in logs.splitlines() if 'CLOUD_HOST_KEY ' in line]
    if not hostkeys: raise RuntimeError('No guest host keys from image builder')
    (LOCAL/(owner+'-known-hosts')).write_text(''.join(owner+' '+k+'\n' for k in hostkeys))
    vm(owner,owner,name)
    wait_vm(owner,'Running')
    print(owner+' is ready: python3 hack/acceptance.py ssh '+owner,flush=True)


def wait_vm(name,phase):
    def ready():
        v=lab.get('cloudvm',name)
        s=v.get('status',{})
        if s.get('phase') in ('Failed','Blocked'):raise RuntimeError(json.dumps(s))
        return v if s.get('phase')==phase else None
    return wait(name+' '+phase,ready)


def ssh_args(name, command=None, identity=None):
    owner='alice' if name=='alice-restored' else name
    service=lab.get('service',name+'-ssh')['spec']['clusterIP']
    proxy=shlex.join(['ssh','-o','BatchMode=yes','-W',service+':22','m600'])
    args=['ssh','-o','BatchMode=yes','-o','ConnectTimeout=8','-o','IdentitiesOnly=yes','-o','StrictHostKeyChecking=yes',
          '-o','UserKnownHostsFile='+str(LOCAL/(owner+'-known-hosts')),'-o','HostKeyAlias='+owner,
          '-o','ProxyCommand='+proxy,'-i',str(key(identity or owner)),'cloud@'+service]
    if command:args.append(command)
    return args


def guest(name, command, identity=None):
    r=subprocess.run(ssh_args(name,command,identity),text=True,capture_output=True,timeout=90)
    if r.returncode:raise RuntimeError(r.stderr[-1500:]+r.stdout[-1500:])
    return r.stdout


def power(name, state):
    v=ensure_owned('cloudvm',name)
    if not v:raise RuntimeError('VM not found')
    if state=='Running' and v['spec']['powerState']=='Running' and v.get('status',{}).get('phase')=='Running':
        print(name+' is already running',flush=True)
        return
    patch={'powerState':state}
    if state=='Running':patch['bootID']=v['spec']['bootID']+1
    follower=None; stream=None
    if state=='Stopped' and v.get('status',{}).get('phase') in ('Pending','Running'):
        pod=v['status'].get('podName')
        if pod:
            stream=(LOCAL/(name+'-stop.log')).open('w')
            follower=subprocess.Popen(['ssh','-o','BatchMode=yes','m600',shlex.join(['kubectl','-n',NS,'logs','-f',pod,'--tail=20'])],stdout=stream,stderr=subprocess.STDOUT)
            time.sleep(1)
    try:
        lab.kubectl('patch','cloudvm',name,'--type=merge','-p',json.dumps({'spec':patch}))
        wait_vm(name,'Running' if state=='Running' else 'Stopped')
        if follower:
            follower.wait(timeout=15)
            stream.flush()
            logs=(LOCAL/(name+'-stop.log')).read_text()
            results=[]
            for line in logs.splitlines():
                try:r=json.loads(line)
                except ValueError:continue
                if 'exitCode' in r and 'reason' in r:results.append(r)
            if not results or results[-1]['reason']!='RequestedStop' or results[-1]['exitCode']!=0:
                raise RuntimeError('Guest stop result requires inspection: '+logs[-1000:])
    finally:
        if follower and follower.poll() is None:follower.terminate()
        if stream:stream.close()


def stopped(name):
    v=lab.get('cloudvm',name)
    if v['status']['phase']!='Stopped':raise RuntimeError('Stop '+name+' before disk transfer')
    pods=lab.decode(lab.kubectl('get','pods','-l','cloud.zjusct.io/vm-uid='+v['metadata']['uid'],'-o','json'))['items']
    if any(p['status']['phase'] not in ('Succeeded','Failed') for p in pods):raise RuntimeError('Wait for old writer to exit')
    return v


def transfer(mode, root, filename):
    name=mode+'-'+str(int(time.time()))
    backup_dir=lab.REMOTE+'/backups'
    lab.ssh(shlex.join(['mkdir','-p',backup_dir])+' && '+shlex.join(['chmod','700',backup_dir]))
    job=lab.resource('batch/v1','Job',name,spec={'backoffLimit':0,'template':{'spec':{
      'restartPolicy':'Never','automountServiceAccountToken':False,'nodeSelector':{'kubernetes.io/hostname':lab.NODE},
      'containers':[{'name':'transfer','image':lab.IMAGE,'command':['python3','/assets/volume-transfer.py'],
        'env':[{'name':'TRANSFER_MODE','value':mode},{'name':'BACKUP_NAME','value':filename}],
        'resources':{'requests':{'cpu':'1','memory':'256Mi'},'limits':{'cpu':'2','memory':'512Mi'}},
        'volumeMounts':[{'name':'assets','mountPath':'/assets','readOnly':True},{'name':'backup','mountPath':'/backup','readOnly':mode=='restore'}],
        'volumeDevices':[{'name':'root','devicePath':'/dev/vm-root'}]}],
      'volumes':[{'name':'assets','hostPath':{'path':ASSETS,'type':'Directory'}},{'name':'backup','hostPath':{'path':backup_dir,'type':'Directory'}},{'name':'root','persistentVolumeClaim':{'claimName':root,'readOnly':mode=='backup'}}]}}})
    lab.kubectl('create','-f','-',data=job)
    def ready():
        for c in lab.get('job',name).get('status',{}).get('conditions',[]):
            if c['type']=='Failed' and c['status']=='True':raise RuntimeError(lab.kubectl('logs','job/'+name,'--tail=30'))
            if c['type']=='Complete' and c['status']=='True':return True
    wait(mode+' disk transfer',ready,600)
    logs=lab.kubectl('logs','job/'+name)
    (LOCAL/(name+'.log')).write_text(logs)
    lab.kubectl('delete','job',name,'--wait=true')
    return logs


def backup(name):
    v=stopped(name)
    filename=name+'-'+str(int(time.time()))+'.raw.gz'
    transfer('backup',v['spec']['rootVolumeClaim'],filename)
    record={'file':filename,'vm':v,'knownHosts':(LOCAL/(name+'-known-hosts')).read_text()}
    (LOCAL/('backup-'+name+'.json')).write_text(json.dumps(record,indent=2)+'\n')
    # Configuration accompanies the disk archive outside Ceph.
    copy(LOCAL/('backup-'+name+'.json'),lab.REMOTE+'/backups/'+filename+'.vm.json')
    lab.ssh(shlex.join(['chmod','600',lab.REMOTE+'/backups/'+filename+'.vm.json']))
    return filename


def restore():
    record=json.loads((LOCAL/'backup-alice.json').read_text())
    if ensure_owned('cloudvm','alice-restored'):raise RuntimeError('Restore VM already exists; preserve it or explicitly retire it before a new restore')
    if ensure_owned('pvc','alice-restored-root'):raise RuntimeError('Restore destination already exists; choose a fresh PVC')
    pvc('alice-restored-root','alice')
    transfer('restore','alice-restored-root',record['file'])
    vm('alice-restored','alice','alice-restored-root')
    wait_vm('alice-restored','Running')


def check():
    report={'time':datetime.datetime.now().astimezone().isoformat(),'checks':[]}
    evidence=HERE/'evidence/acceptance-results.json'
    def record(name,**fields):
        report['checks'].append({'name':name,'passed':True,**fields})
        evidence.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        print('PASS '+name,flush=True)
    for owner in OWNERS:
        v=wait_vm(owner,'Running');p=lab.get('pod',v['status']['podName'])
        assert p['spec']['schedulerName']=='default-scheduler' and p['spec']['nodeName']==lab.NODE
        out=guest(owner,'cat /etc/os-release; systemctl is-active ssh; sudo id -u; cat /etc/cloud-image.json')
        assert 'Ubuntu 24.04' in out and '\nactive\n0\n' in out,out
        record(owner+' Ubuntu SSH and sudo',pod_uid=p['metadata']['uid'],image='ubuntu-24.04')
    guest('alice','curl -fsS --connect-timeout 10 --max-time 20 https://mirrors.zju.edu.cn/ubuntu/dists/noble/Release >/dev/null')
    record('guest DNS and HTTPS through Pod NAT')
    bob=lab.get('pod',lab.get('cloudvm','bob')['status']['podName'])['status']['podIP']
    probe="import socket,json; targets="+repr([(bob,2222),('172.25.4.60',22),('172.25.3.240',22),('172.27.0.1',443)])+"; results=[]\nfor host,port in targets:\n try:\n  s=socket.create_connection((host,port),2); s.close(); results.append([host,port,'OPEN'])\n except OSError:results.append([host,port,'BLOCKED'])\nprint(json.dumps(results))\nassert all(x[2]=='BLOCKED' for x in results)"
    out=guest('alice','python3 -c '+shlex.quote(probe))
    record('cross-user, node, management and Kubernetes API blocked',targets=json.loads(out))
    denied=subprocess.run(ssh_args('bob','true',identity='alice'),text=True,capture_output=True,timeout=30)
    assert denied.returncode!=0 and 'Permission denied' in denied.stderr,denied.stderr
    record('Alice key cannot log into Bob VM')
    marker='acceptance-'+str(int(time.time()))
    guest('alice','echo '+shlex.quote(marker)+' | sudo tee /root/acceptance-marker >/dev/null; sudo sync')
    power('alice','Stopped')
    backup_file=backup('alice')
    restore()
    assert marker in guest('alice-restored','sudo cat /root/acceptance-marker')
    record('offline backup restores onto a fresh PVC and boots with retained data',source='alice-root',restored='alice-restored-root',backup=backup_file)
    power('alice-restored','Stopped')
    power('alice','Running')
    assert marker in guest('alice','sudo cat /root/acceptance-marker')
    record('platform stop/start retains OS modifications')
    # Full Linux poweroff performs systemd service shutdown before VMM exit.
    guest('alice','sudo systemctl poweroff')
    wait_vm('alice','Stopped')
    v=lab.get('cloudvm','alice');p=lab.get('pod',v['status']['podName'])
    assert p['status']['phase']=='Succeeded',p['status']
    time.sleep(4)
    assert lab.get('cloudvm','alice')['status']['phase']=='Stopped'
    record('guest systemd poweroff stays stopped',pod_uid=p['metadata']['uid'])
    power('alice','Running')
    record('acceptance environment left ready',vms=['alice','bob'],restored_vm='Stopped')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['prepare','images','create','ssh','status','stop','start','restart','backup','restore','check'])
    parser.add_argument('name',nargs='?',choices=[*OWNERS,'alice-restored'])
    parser.add_argument('--os',default='ubuntu-24.04',choices=list(CATALOG))
    parser.add_argument('--command')
    a=parser.parse_args()
    if a.action in ('create','ssh','stop','start','restart','backup') and not a.name:parser.error('name required')
    if a.action=='prepare':prepare()
    elif a.action=='images':print(json.dumps(CATALOG,ensure_ascii=False,indent=2))
    elif a.action=='create':create(a.name,a.os)
    elif a.action=='ssh':raise SystemExit(subprocess.call(ssh_args(a.name,a.command)))
    elif a.action=='status':print(lab.kubectl('get','cloudvm,pvc,service'))
    elif a.action=='stop':power(a.name,'Stopped')
    elif a.action=='start':power(a.name,'Running')
    elif a.action=='restart':
        power(a.name,'Stopped')
        power(a.name,'Running')
    elif a.action=='backup':print(backup(a.name))
    elif a.action=='restore':restore()
    elif a.action=='check':check()

if __name__=='__main__':main()
