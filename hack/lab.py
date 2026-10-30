"""Explicit opt-in m600 integration experiment. No Argo/production objects.

prepare: creates the labelled lab namespace and, if absent, its test CRD.
check: runs the namespaced operator and real Firecracker lifecycle checks.
cleanup: deletes ONLY this labelled disposable namespace/CRD; leaves remote files.
Requires SSH m600, admin kubectl only for bootstrap/checks, and prebuilt binaries.
"""
import argparse
import datetime
import json
from pathlib import Path
import shlex
import subprocess
import time

HERE = Path(__file__).resolve().parents[1]
NS = 'cloud-operator-lab'
LABEL = 'cloud-lab.zjusct.io/experiment'
TAG = 'operator-20261030'
REMOTE = '/var/tmp/zjusct-cloud-operator-lab-20261030'
SOURCE = '/var/tmp/zjusct-fc-scheduler-lab-20261030/assets'
IMAGE = 'harbor.clusters.zjusct.io/quay.io/ceph/ceph@sha256:af0c5903e901e329adabe219dfc8d0c3efc1f05102a753902f33ee16c26b6cee'
NODE = 'm600.clusters.zjusct.io'
CRD = 'cloudvms.cloud.zjusct.io'
LOCAL = HERE / '.local'
LOCAL.mkdir(exist_ok=True, mode=0o700)

def ssh(command, data=None, timeout=45):
    result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', 'm600', command],
                            input=data, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(result.stderr[-2000:] + result.stdout[-2000:])
    return result.stdout

def kubectl(*args, data=None):
    return ssh(shlex.join(['kubectl', '--request-timeout=20s', '-n', NS, *args]),
               data if isinstance(data, str) else None if data is None else json.dumps(data))

def decode(raw):
    # SSH login banners can precede the JSON response.
    start = raw.find('{')
    if start < 0:
        return None
    return json.loads(raw[start:])

def get(kind, name):
    return decode(kubectl('get', kind, name, '--ignore-not-found', '-o', 'json'))

def owned(kind, name):
    obj = get(kind, name)
    if obj and obj['metadata'].get('labels', {}).get(LABEL) != TAG:
        raise RuntimeError(f'{kind}/{name} is not owned by this experiment')
    return obj

def resource(api, kind, name, **fields):
    return {'apiVersion': api, 'kind': kind,
            'metadata': {'name': name, 'namespace': NS, 'labels': {LABEL: TAG}}, **fields}

def apply(obj):
    kubectl('apply', '--server-side', '--field-manager=cloud-operator-lab', '-f', '-', data=obj)

def prepare(launcher):
    owned('namespace', NS)
    owned('crd', CRD)
    ssh('mkdir -p '+shlex.quote(REMOTE+'/assets')+' && chmod 700 '+shlex.quote(REMOTE))
    for src, dst in [(HERE/'bin/cloud-operator', REMOTE+'/cloud-operator'),
                     (launcher/'bin/cloud-launcher', REMOTE+'/assets/cloud-launcher'),
                     (launcher/'bin/cloud-guest-helper', REMOTE+'/assets/cloud-guest-helper'),
                     (launcher/'dev/build-root.py', REMOTE+'/assets/build-root.py')]:
        subprocess.run(['scp', str(src), 'm600:'+dst], check=True, capture_output=True)
    ssh(shlex.join(['cp', SOURCE+'/firecracker', SOURCE+'/jailer', SOURCE+'/vmlinux', SOURCE+'/busybox', REMOTE+'/assets/']))
    # Convert generated YAML using kubectl locally on m600; avoid an extra Python YAML dependency.
    crd = decode(kubectl('create', '--dry-run=client', '--validate=false', '-f', '-', '-o', 'json',
                         data=(HERE/'config/crd/cloud.zjusct.io_cloudvms.yaml').read_text()))
    crd['metadata'].setdefault('labels', {})[LABEL] = TAG
    apply(crd)
    kubectl('wait', '--for=condition=Established', 'crd/'+CRD, '--timeout=20s')
    objects = [
        {'apiVersion':'v1', 'kind':'Namespace', 'metadata':{'name':NS, 'labels':{LABEL:TAG}}},
        resource('networking.k8s.io/v1', 'NetworkPolicy', 'deny-all', spec={
            'podSelector':{}, 'policyTypes':['Ingress','Egress'], 'ingress':[], 'egress':[]}),
        resource('v1', 'ResourceQuota', 'budget', spec={'hard':{
            'pods':'3', 'requests.cpu':'3', 'limits.cpu':'3', 'requests.memory':'2Gi', 'limits.memory':'2Gi',
            'persistentvolumeclaims':'1', 'requests.storage':'256Mi', 'count/cloudvms.cloud.zjusct.io':'2'}}),
        resource('v1', 'ServiceAccount', 'operator'),
        resource('rbac.authorization.k8s.io/v1', 'Role', 'operator', rules=[
            {'apiGroups':['cloud.zjusct.io'], 'resources':['cloudvms'], 'verbs':['get','list','watch']},
            {'apiGroups':['cloud.zjusct.io'], 'resources':['cloudvms/status'], 'verbs':['get','patch','update']},
            {'apiGroups':[''], 'resources':['pods'], 'verbs':['get','list','watch','create','delete']},
            {'apiGroups':[''], 'resources':['persistentvolumeclaims'], 'verbs':['get','list','watch','update','patch']},
            {'apiGroups':[''], 'resources':['events'], 'verbs':['create','patch']},
            {'apiGroups':['coordination.k8s.io'], 'resources':['leases'], 'verbs':['get','list','watch','create','update','patch']},
        ]),
        resource('rbac.authorization.k8s.io/v1', 'RoleBinding', 'operator',
            roleRef={'apiGroup':'rbac.authorization.k8s.io','kind':'Role','name':'operator'},
            subjects=[{'kind':'ServiceAccount','name':'operator','namespace':NS}]),
    ]
    for obj in objects: apply(obj)
    if not get('pvc','alice-root'):
        pvc = resource('v1','PersistentVolumeClaim','alice-root', spec={
            'accessModes':['ReadWriteOnce'], 'volumeMode':'Block', 'storageClassName':'ceph-block',
            'resources':{'requests':{'storage':'256Mi'}}})
        pvc['metadata']['labels']['cloud.zjusct.io/owner']='alice'
        kubectl('create','-f','-',data=pvc)
    else:
        owned('pvc','alice-root')
    if not get('job','initialize-root'):
        # Initialization is a separate explicit job, never part of launcher startup.
        job=resource('batch/v1','Job','initialize-root',spec={'backoffLimit':0,'template':{'spec':{
            'restartPolicy':'Never','automountServiceAccountToken':False,
            'nodeSelector':{'kubernetes.io/hostname':NODE},
            'containers':[{'name':'initialize','image':IMAGE,'command':['python3','/assets/build-root.py'],
                'resources':{'requests':{'cpu':'1','memory':'256Mi'},'limits':{'cpu':'1','memory':'256Mi'}},
                'volumeMounts':[{'name':'assets','mountPath':'/assets','readOnly':True}],
                'volumeDevices':[{'name':'root','devicePath':'/dev/vm-root'}]}],
            'volumes':[{'name':'assets','hostPath':{'path':REMOTE+'/assets','type':'Directory'}},
                       {'name':'root','persistentVolumeClaim':{'claimName':'alice-root'}}]}}})
        kubectl('create','-f','-',data=job)
    # The namespace token is short-lived and never printed or stored in either repository.
    script=f'''import json,subprocess,os
c=json.loads(subprocess.check_output(['kubectl','config','view','--raw','--minify','--flatten','-o','json']))
t=subprocess.check_output(['kubectl','-n','{NS}','create','token','operator','--duration=2h'],text=True).strip()
c['users']=[{{'name':'operator','user':{{'token':t}}}}]
c['contexts']=[{{'name':'lab','context':{{'cluster':c['clusters'][0]['name'],'user':'operator','namespace':'{NS}'}}}}]
c['current-context']='lab'
p='{REMOTE}/client.json'
fd=os.open(p,os.O_CREAT|os.O_TRUNC|os.O_WRONLY,0o600)
with os.fdopen(fd,'w') as f:json.dump(c,f)
os.chmod(p,0o600)
'''
    ssh('python3 -',script)
    print('Prepared isolated namespace, test CRD, short-lived credentials and root initialization job',flush=True)

def wait_for(description, predicate, seconds=90):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        value=predicate()
        if value:return value
        time.sleep(2)
    raise RuntimeError('Timed out: '+description)

def stop_operator():
    script=f'''import os,signal
p='{REMOTE}/operator.pid'
if os.path.exists(p):
 pid=int(open(p).read())
 try:
  args=open('/proc/%d/cmdline'%pid,'rb').read().split(b'\\0')
  if b'{REMOTE}/cloud-operator' in args:os.kill(pid,signal.SIGTERM)
 except FileNotFoundError:pass
'''
    ssh('python3 -',script)

def check():
    assert owned('namespace',NS) and owned('crd',CRD)
    wait_for('root image initialization',lambda:any(c.get('type')=='Complete' and c.get('status')=='True'
             for c in get('job','initialize-root').get('status',{}).get('conditions',[])))
    stop_operator()
    cmd=shlex.join(['env','KUBECONFIG='+REMOTE+'/client.json',REMOTE+'/cloud-operator',
        '--namespace='+NS,'--development','--node='+NODE,'--assets='+REMOTE+'/assets','--runner-image='+IMAGE])
    log=(LOCAL/'operator.log').open('w')
    proc=subprocess.Popen(['ssh','-o','BatchMode=yes','m600',
        'echo $$ > '+shlex.quote(REMOTE+'/operator.pid')+'; exec '+cmd],stdout=log,stderr=subprocess.STDOUT,text=True)
    report={'time':datetime.datetime.now().astimezone().isoformat(),'checks':[]}
    evidence=HERE/'evidence';evidence.mkdir(exist_ok=True)
    def record(name,**data):
        report['checks'].append({'name':name,'passed':True,**data})
        (evidence/'live-results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        print(name,flush=True)
    def phase(name):
        vm=get('cloudvm','alice')
        return vm if vm and vm.get('status',{}).get('phase')==name else None
    def console(pod,data):
        kubectl('exec','-i',pod,'--','/assets/cloud-launcher','console-input',data=data)
    try:
        if get('cloudvm','alice'):raise RuntimeError('check requires a fresh CloudVM; cleanup and prepare for a new experiment')
        vm=resource('cloud.zjusct.io/v1alpha1','CloudVM','alice',spec={
            'owner':'alice','rootVolumeClaim':'alice-root','vcpus':1,'memoryMiB':128,'powerState':'Running','bootID':1})
        kubectl('create','-f','-',data=vm)
        running=wait_for('first boot',lambda:phase('Running'));pod=running['status']['podName']
        logs=wait_for('guest root boot',lambda:(out if 'CLOUD_GUEST_READY' in (out:=kubectl('logs',pod,'--tail=80')) else None))
        p=get('pod',pod)
        assert p['spec']['nodeName']==NODE and p['spec']['schedulerName']=='default-scheduler'
        assert p['spec']['restartPolicy']=='Never'
        record('operator creates scheduled Firecracker Pod',pod_uid=p['metadata']['uid'],node=p['spec']['nodeName'])
        console(pod,'echo retained-by-cloud > /root/user-marker; sync; echo CLOUD_MARKER_WRITTEN\n')
        wait_for('root write',lambda:'CLOUD_MARKER_WRITTEN' in kubectl('logs',pod,'--tail=30'))
        # Preserve the exit result before Kubernetes removes the stopped Pod.
        stop_log=LOCAL/'requested-stop.log'
        with stop_log.open('w') as stream:
            follower=subprocess.Popen(['ssh','-o','BatchMode=yes','m600',
                shlex.join(['kubectl','-n',NS,'logs','-f',pod,'--tail=100'])],stdout=stream,stderr=subprocess.STDOUT)
            try:
                wait_for('log stream',lambda:'CLOUD_GUEST_READY' in stop_log.read_text(),seconds=20)
                kubectl('patch','cloudvm','alice','--type=merge','-p',json.dumps({'spec':{'powerState':'Stopped'}}))
                wait_for('requested stop',lambda:phase('Stopped'))
                follower.wait(timeout=15)
            finally:
                if follower.poll() is None:follower.terminate()
        assert '"reason":"RequestedStop","exitCode":0' in stop_log.read_text(),stop_log.read_text()[-1200:]
        assert get('pvc','alice-root')['status']['phase']=='Bound'
        record('requested stop exits cleanly and retains root PVC',launcher_reason='RequestedStop',exit_code=0)
        kubectl('patch','cloudvm','alice','--type=merge','-p',json.dumps({'spec':{'powerState':'Running','bootID':2}}))
        running=wait_for('second boot',lambda:phase('Running'));pod=running['status']['podName']
        logs=wait_for('persistent root marker',lambda:(out if 'CLOUD_PERSISTENCE retained-by-cloud' in (out:=kubectl('logs',pod,'--tail=80')) else None))
        assert 'CLOUD_GUEST_READY boot=2' in logs
        record('new boot preserves the actual root filesystem',pod_uid=get('pod',pod)['metadata']['uid'])
        console(pod,'poweroff\n')
        stopped=wait_for('guest shutdown',lambda:phase('Stopped'))
        assert stopped['status']['reason']=='GuestExited'
        uid=get('pod',pod)['metadata']['uid']
        # Observe across several reconciles; successful exit must not trigger a reboot.
        for _ in range(3):
            time.sleep(3)
            assert phase('Stopped') and get('pod',pod)['metadata']['uid']==uid
        record('guest shutdown stays stopped across reconciles',pod_uid=uid)
        try:
            kubectl('patch','cloudvm','alice','--type=merge','-p',json.dumps({'spec':{'rootVolumeClaim':'other-root'}}))
        except RuntimeError as err:
            assert 'immutable' in str(err)
        else:raise AssertionError('CRD accepted root volume mutation')
        record('CRD rejects root volume reassignment')
        pvc_uid=get('pvc','alice-root')['metadata']['uid']
        kubectl('delete','cloudvm','alice','--wait=true')
        assert get('pvc','alice-root')['metadata']['uid']==pvc_uid
        record('deleting VM retains its independently owned root PVC',pvc_uid=pvc_uid)
    finally:
        stop_operator()
        try:proc.wait(timeout=15)
        except subprocess.TimeoutExpired:proc.terminate()
        log.close()

def cleanup():
    stop_operator()
    ns=owned('namespace',NS)
    crd=owned('crd',CRD)
    if crd:
        all_vms=decode(kubectl('get','cloudvms','--all-namespaces','-o','json'))['items']
        if any(x['metadata']['namespace']!=NS for x in all_vms):
            raise RuntimeError('CloudVM CRD is in use outside the experiment; refusing cleanup')
    if ns:
        kubectl('delete','namespace',NS,'--wait=false')
        wait_for('lab namespace deletion',lambda:not get('namespace',NS),seconds=90)
    if crd:kubectl('delete','crd',CRD,'--wait=true')
    ssh('python3 -',f"from pathlib import Path\nfor name in ['client.json','operator.pid']:\n Path('{REMOTE}',name).unlink(missing_ok=True)\n")
    print('Disposable namespace and experiment-owned CRD removed; source/evidence retained')

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['prepare','check','cleanup'])
    parser.add_argument('--launcher',type=Path,default=HERE.parent/'cloud-launcher')
    args=parser.parse_args()
    if args.action=='prepare':prepare(args.launcher.resolve())
    elif args.action=='check':check()
    else:cleanup()
