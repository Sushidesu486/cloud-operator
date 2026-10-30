#!/usr/bin/env python3
"""Bounded 1/10/20 VM density experiment; separate disposable namespace."""
import argparse,concurrent.futures,copy,datetime,json,shlex,subprocess,time
from pathlib import Path
import lab

HERE=lab.HERE
NS='cloud-density-lab';TAG='density-20261030';ASSETS=lab.REMOTE+'/assets'
SCRIPTS=HERE/'hack/density';LOCAL=lab.LOCAL/'density';LOCAL.mkdir(exist_ok=True)
REPORT=HERE/'evidence/density-results.json'
ORIGINAL_NS=lab.NS
lab.NS=NS;lab.TAG=TAG
CPU=63

def apply(obj):
    lab.owned(obj['kind'],obj['metadata']['name']);lab.apply(obj)

def wait(desc,fn,seconds=300):
    print('Waiting: '+desc,flush=True);return lab.wait_for(desc,fn,seconds)

def ready(name):
    p=lab.get('pod',name)
    if p and p['status'].get('phase')=='Failed':raise RuntimeError('Pod failed: '+name)
    return p if p and any(c['type']=='Ready' and c['status']=='True' for c in p['status'].get('conditions',[])) else None

def pvc(name,source=None):
    obj=lab.resource('v1','PersistentVolumeClaim',name,spec={'accessModes':['ReadWriteOnce'],'volumeMode':'Block','storageClassName':'ceph-block','resources':{'requests':{'storage':'8Gi'}}})
    if source:obj['spec']['dataSource']={'kind':'PersistentVolumeClaim','name':source}
    if not lab.owned('pvc',name):lab.kubectl('create','-f','-',data=obj)

def prepare():
    apply({'apiVersion':'v1','kind':'Namespace','metadata':{'name':NS,'labels':{lab.LABEL:TAG}}})
    apply(lab.resource('v1','ResourceQuota','budget',spec={'hard':{'pods':'24','requests.cpu':'5','limits.cpu':'46','requests.memory':'18Gi','limits.memory':'20Gi','persistentvolumeclaims':'21','requests.storage':'168Gi'}}))
    apply(lab.resource('networking.k8s.io/v1','NetworkPolicy','deny-all',spec={'podSelector':{},'policyTypes':['Ingress','Egress'],'ingress':[],'egress':[]}))
    apply(lab.resource('networking.k8s.io/v1','NetworkPolicy','benchmark-only',spec={'podSelector':{},'policyTypes':['Ingress','Egress'],
        'ingress':[{'from':[{'podSelector':{}}],'ports':[{'port':8080,'protocol':'TCP'},{'port':9090,'protocol':'TCP'}]}],
        'egress':[{'to':[{'podSelector':{}}],'ports':[{'port':8080,'protocol':'TCP'},{'port':9090,'protocol':'TCP'}]}]}))
    scripts={p.name:p.read_text() for p in SCRIPTS.glob('*.py')}
    apply(lab.resource('v1','ConfigMap','scripts',data=scripts))
    load=lab.resource('v1','Pod','loadgen',spec={'restartPolicy':'Never','automountServiceAccountToken':False,
      'nodeSelector':{'kubernetes.io/hostname':'m601.clusters.zjusct.io'},
      'containers':[{'name':'loadgen','image':lab.IMAGE,'command':['python3','/scripts/load.py','serve'],
       'resources':{'requests':{'cpu':'1','memory':'256Mi'},'limits':{'cpu':'2','memory':'512Mi'}},
       'volumeMounts':[{'name':'scripts','mountPath':'/scripts','readOnly':True}],
       'readinessProbe':{'tcpSocket':{'port':9090},'periodSeconds':2}}],
      'volumes':[{'name':'scripts','configMap':{'name':'scripts'}}]})
    if not lab.owned('pod','loadgen'):lab.kubectl('create','-f','-',data=load)
    load=wait('load generator on m601',lambda:ready('loadgen'))
    upstream=load['status']['podIP']
    pvc('template')
    filename=json.loads((lab.LOCAL/'backup-alice.json').read_text())['file']
    # Restore the acceptance backup into a separate density-only template, then
    # add the controlled workload and regenerate per-boot identities on clones.
    script="""import os,pathlib,subprocess
subprocess.run(['python3','/assets/volume-transfer.py'],check=True)
r=pathlib.Path('/work/root');r.mkdir()
subprocess.run(['mount','/dev/vm-root',str(r)],check=True)
try:
 (r/'etc/machine-id').write_text('')
 subprocess.run(['cp','/scripts/guest.py',str(r/'usr/local/sbin/density-guest.py')],check=True)
 (r/'etc/systemd/system/density.service').write_text('[Unit]\\nAfter=network.target\\n[Service]\\nEnvironment=UPSTREAM='+os.environ['UPSTREAM']+'\\nExecStart=/usr/bin/python3 /usr/local/sbin/density-guest.py\\nRestart=on-failure\\n[Install]\\nWantedBy=multi-user.target\\n')
 subprocess.run(['systemctl','--root='+str(r),'enable','density.service'],check=True)
 subprocess.run(['sync'],check=True)
finally:subprocess.run(['umount',str(r)],check=True)
"""
    job=lab.resource('batch/v1','Job','initialize-template',spec={'backoffLimit':0,'template':{'spec':{
      'restartPolicy':'Never','automountServiceAccountToken':False,'nodeSelector':{'kubernetes.io/hostname':lab.NODE},
      'containers':[{'name':'initialize','image':lab.IMAGE,'command':['python3','-c',script],
        'securityContext':{'privileged':True},'env':[{'name':'TRANSFER_MODE','value':'restore'},{'name':'BACKUP_NAME','value':filename},{'name':'UPSTREAM','value':upstream}],
        'resources':{'requests':{'cpu':'1','memory':'256Mi'},'limits':{'cpu':'2','memory':'512Mi'}},
        'volumeMounts':[{'name':'assets','mountPath':'/assets','readOnly':True},{'name':'backup','mountPath':'/backup','readOnly':True},{'name':'scripts','mountPath':'/scripts','readOnly':True},{'name':'work','mountPath':'/work'}],
        'volumeDevices':[{'name':'root','devicePath':'/dev/vm-root'}]}],
      'volumes':[{'name':'assets','hostPath':{'path':ASSETS,'type':'Directory'}},{'name':'backup','hostPath':{'path':lab.REMOTE+'/backups','type':'Directory'}},{'name':'scripts','configMap':{'name':'scripts'}},{'name':'work','emptyDir':{}},{'name':'root','persistentVolumeClaim':{'claimName':'template'}}]}}})
    if not lab.owned('job','initialize-template'):lab.kubectl('create','-f','-',data=job)
    def done():
        for c in lab.get('job','initialize-template').get('status',{}).get('conditions',[]):
            if c['type']=='Failed' and c['status']=='True':raise RuntimeError(lab.kubectl('logs','job/initialize-template','--tail=30'))
            if c['type']=='Complete' and c['status']=='True':return True
    wait('density template restore',done,600)
    # Save a copy of the current accepted Pod shape; no mutation of CloudVM or its Pods.
    raw=lab.ssh('kubectl -n '+ORIGINAL_NS+' get pods -l cloud.zjusct.io/owner=alice -o json')
    candidates=[p for p in lab.decode(raw)['items'] if p['status']['phase']=='Running']
    (LOCAL/'pod-template.json').write_text(json.dumps(candidates[0]))
    print('Density template ready; existing acceptance VMs retained',flush=True)

def add_vm(i):
    name='vm-%02d'%i;pvc(name+'-root','template')
    base=json.loads((LOCAL/'pod-template.json').read_text())
    p=lab.resource('v1','Pod',name,spec=base['spec'])
    p['metadata']['labels'].update({'density-role':'vm','cloud.zjusct.io/owner':'density'})
    s=p['spec'];s.pop('nodeName',None);s.pop('priority',None)
    for c in s['containers']:
        c['command']=['/assets/busybox','taskset','-c',str(CPU),'/assets/cloud-launcher','run']
        cfg=json.loads(c['env'][0]['value']);cfg['memoryMiB']=512;c['env'][0]['value']=json.dumps(cfg)
        c['readinessProbe']['exec']['command']=['/assets/busybox','taskset','-c',str(CPU),'/assets/cloud-launcher','ready']
        c['resources']['requests']['cpu']='10m';c['resources']['limits']['cpu']='1'
        c['resources']['requests']['memory']='640Mi';c['resources']['limits']['memory']='640Mi'
    for v in s['volumes']:
        if v['name']=='root':v['persistentVolumeClaim']['claimName']=name+'-root'
    # Count the forwarding process in the same physical CPU budget.
    s['containers'].append({'name':'relay','image':lab.IMAGE,'command':['/assets/busybox','taskset','-c',str(CPU),'python3','/scripts/relay.py'],
      'resources':{'requests':{'cpu':'5m','memory':'32Mi'},'limits':{'cpu':'1','memory':'64Mi'}},
      'volumeMounts':[{'name':'scripts','mountPath':'/scripts','readOnly':True},{'name':'assets','mountPath':'/assets','readOnly':True}]})
    s['volumes'].append({'name':'scripts','configMap':{'name':'scripts'}})
    if not lab.owned('pod',name):lab.kubectl('create','-f','-',data=p)

def snapshot():
    # Read only the selected Pod cgroups on the host; sampling adds no exec
    # processes to the measured containers.
    script='''import subprocess,json,time,pathlib
ns=%r
pods=json.loads(subprocess.check_output(['kubectl','-n',ns,'get','pod','-l','density-role=vm','-o','json']))['items']
data={}
for pod in pods:
 uid=pod['metadata']['uid'].replace('-','_')
 base=pathlib.Path('/sys/fs/cgroup/kubepods.slice/kubepods-burstable.slice')/('kubepods-burstable-pod'+uid+'.slice')
 for c in pod['status']['containerStatuses']:
  cid=c['containerID'].split('://')[1]
  path=base/('cri-containerd-'+cid+'.scope')
  out={'time':time.monotonic()}
  for name in ['cpu.stat','memory.stat','memory.events']:
   out[name]={l.split()[0]:int(l.split()[1]) for l in (path/name).read_text().splitlines()}
  out['memory.current']=int((path/'memory.current').read_text())
  affinities=[]
  for pid in ' '.join(f.read_text() for f in path.rglob('cgroup.procs')).split():
   try:affinities.append(next(l for l in pathlib.Path('/proc',pid,'status').read_text().splitlines() if l.startswith('Cpus_allowed_list')))
   except (FileNotFoundError,StopIteration):pass
  out['affinity']='; '.join(sorted(set(affinities)))
  data[pod['metadata']['name']+'/'+c['name']]=out
print(json.dumps({'time':time.time(),'containers':data,'hostProcStat':pathlib.Path('/proc/stat').read_text()}))
'''%NS
    return lab.decode(lab.ssh('python3 -',script,timeout=45))


def metrics(a,b):
    cpu=0;throttle=0;periods=0;throttled=0;rss=0;mem=0;oom=0
    for name,y in b['containers'].items():
        x=a['containers'][name];dt=y['time']-x['time']
        cpu+=(y['cpu.stat']['usage_usec']-x['cpu.stat']['usage_usec'])/1e6/dt
        throttle+=(y['cpu.stat'].get('throttled_usec',0)-x['cpu.stat'].get('throttled_usec',0))/1e6
        periods+=y['cpu.stat'].get('nr_periods',0)-x['cpu.stat'].get('nr_periods',0)
        throttled+=y['cpu.stat'].get('nr_throttled',0)-x['cpu.stat'].get('nr_throttled',0)
        mem+=y['memory.current'];rss+=y['memory.stat']['anon'];oom+=y['memory.events']['oom_kill']-x['memory.events']['oom_kill']
    def core(s,n):return list(map(int,next(l for l in s.splitlines() if l.startswith('cpu%d '%n)).split()[1:]))
    per_cpu={}
    for n in [CPU,CPU-32]:
        delta=[v-u for u,v in zip(core(a['hostProcStat'],n),core(b['hostProcStat'],n))]
        per_cpu[str(n)]=100*(1-(delta[3]+delta[4])/sum(delta[:8]))
    return {'cpuCores':cpu,'memoryCurrentMiB':mem/2**20,'anonymousMiB':rss/2**20,'throttledSeconds':throttle,'throttledPeriods':throttled,'periods':periods,'oomKills':oom,'hostCpuBusyPercent':per_cpu,'affinity':sorted(set(v['affinity'] for v in b['containers'].values()))}

def run():
    report={'startedAt':datetime.datetime.now().astimezone().isoformat(),'configuration':{'cpuAffinity':CPU,'vmVCPUs':1,'guestMemoryMiB':512,'rootGiB':8,'guestOS':'Ubuntu 24.04','loadNode':'m601.clusters.zjusct.io','vmNode':lab.NODE,'upstreamWaitMs':250,'agentCPUms':1,'responseBytes':{'web':4096,'mockAPI':16384},'readinessPeriodSeconds':2},'stages':[]}
    def save():REPORT.write_text(json.dumps(report,indent=2)+'\n')
    existing=0
    for count in [1,10,20]:
        started=time.monotonic()
        for i in range(existing+1,count+1):add_vm(i)
        for i in range(1,count+1):wait('VM %d ready'%i,lambda i=i:ready('vm-%02d'%i),600)
        pods=lab.decode(lab.kubectl('get','pod','-l','density-role=vm','-o','json'))['items']
        hosts=[p['status']['podIP'] for p in sorted(pods,key=lambda x:x['metadata']['name'])]
        stage={'vmCount':count,'scaleUpSeconds':time.monotonic()-started,'tests':[]};report['stages'].append(stage);save()
        # Warm the application and let boot activity settle.
        config={'hosts':hosts,'path':'/web','rps':count,'seconds':5}
        lab.ssh(shlex.join(['kubectl','-n',NS,'exec','loadgen','--','python3','/scripts/load.py','run',json.dumps(config)]),timeout=30)
        time.sleep(10)
        before=snapshot();time.sleep(20);after=snapshot()
        stage['idle']=metrics(before,after);save();print('IDLE '+str(count)+' '+json.dumps(stage['idle']),flush=True)
        for path,rps in [('/web',100),('/web',500),('/agent',40),('/agent',100),('/agent',300)]:
            before=snapshot()
            config={'hosts':hosts,'path':path,'rps':rps,'seconds':20}
            raw=lab.ssh(shlex.join(['kubectl','-n',NS,'exec','loadgen','--','python3','/scripts/load.py','run',json.dumps(config)]),timeout=60)
            result=lab.decode(raw);after=snapshot();result['resources']=metrics(before,after)
            stage['tests'].append(result);save();print('RESULT '+json.dumps(result),flush=True)
        existing=count
    report['completedAt']=datetime.datetime.now().astimezone().isoformat();save()

def stop_benchmark_pods(names):
    pods=lab.decode(lab.kubectl('get','pods','-o','json'))['items']
    selected=[p for p in pods if p['metadata']['name'] in names]
    if any(p['metadata'].get('labels',{}).get(lab.LABEL)!=TAG for p in selected):
        raise RuntimeError('Refusing to stop an unowned Pod')
    # Python PID 1 handles SIGINT. Stop benchmark helpers before normal Pod
    # deletion so guest shutdown can finish without waiting for helper SIGKILL.
    commands=[]
    for p in selected:
        if p['status']['phase']!='Running':continue
        for c in p['spec']['containers']:
            if c['name'] in ('relay','loadgen'):
                commands.append(shlex.join(['kubectl','-n',NS,'exec',p['metadata']['name'],'-c',c['name'],'--','python3','-c','import os,signal; os.kill(1,signal.SIGINT)']))
    def signal(cmd):
        try:lab.ssh(cmd,timeout=15)
        except RuntimeError:pass
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:list(pool.map(signal,commands))
    if selected:lab.kubectl('delete','pod',*[p['metadata']['name'] for p in selected],'--wait=false')
    wait('benchmark Pod termination',lambda:not any(p['metadata']['name'] in names for p in lab.decode(lab.kubectl('get','pods','-o','json'))['items']),120)


def recheck():
    report=json.loads(REPORT.read_text())
    if 'completedAt' not in report:raise RuntimeError('Finish the main matrix first')
    pods=lab.decode(lab.kubectl('get','pods','-l','density-role=vm','-o','json'))['items']
    stop_benchmark_pods([p['metadata']['name'] for p in pods if p['metadata']['name']!='vm-01'])
    p=wait('single VM baseline',lambda:ready('vm-01'))
    before=snapshot()
    config={'hosts':[p['status']['podIP']],'path':'/web','rps':100,'seconds':40}
    raw=lab.ssh(shlex.join(['kubectl','-n',NS,'exec','loadgen','--','python3','/scripts/load.py','run',json.dumps(config)]),timeout=60)
    result=lab.decode(raw);result['resources']=metrics(before,snapshot())
    report['singleVMBaselineRecheck']=result
    REPORT.write_text(json.dumps(report,indent=2)+'\n')
    print('RECHECK '+json.dumps(result),flush=True)


def cleanup():
    if lab.owned('namespace',NS):
        pods=lab.decode(lab.kubectl('get','pods','-o','json'))['items']
        stop_benchmark_pods([p['metadata']['name'] for p in pods if p['status']['phase']=='Running'])
        lab.kubectl('delete','namespace',NS,'--wait=false')
        wait('density namespace deletion',lambda:not lab.get('namespace',NS),180)
    print('Disposable density resources removed; acceptance namespace retained',flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['prepare','run','recheck','cleanup']);a=p.parse_args()
    {'prepare':prepare,'run':run,'recheck':recheck,'cleanup':cleanup}[a.action]()
