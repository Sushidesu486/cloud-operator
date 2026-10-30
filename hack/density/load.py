"""Open-loop HTTP driver and a controlled 250 ms remote AI API simulator."""
import asyncio,json,math,sys,time,resource
soft,hard=resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE,(min(65536,hard),hard))

async def mock(r,w):
    try:
        await asyncio.wait_for(r.readuntil(b'\r\n\r\n'),10)
        await asyncio.sleep(.250)
        body=json.dumps({'result':'x'*16384}).encode()
        w.write(b'HTTP/1.1 200 OK\r\nContent-Length: '+str(len(body)).encode()+b'\r\nConnection: close\r\n\r\n'+body);await w.drain()
    except (OSError,asyncio.TimeoutError,asyncio.IncompleteReadError):pass
    finally:w.close()

async def serve():
    server=await asyncio.start_server(mock,'0.0.0.0',9090)
    async with server:await server.serve_forever()

async def load(config):
    hosts=config['hosts'];rate=config['rps'];duration=config['seconds'];path=config['path']
    latency=[];errors=[];schedule_lag=[];tasks=set();per_host={h:0 for h in hosts}
    start=time.monotonic();limit=2000
    async def one(host,planned):
        actual=time.monotonic();schedule_lag.append((actual-planned)*1000)
        try:
            async def exchange():
                r,w=await asyncio.open_connection(host,8080)
                try:
                    w.write(('GET '+path+' HTTP/1.1\r\nHost: bench\r\nConnection: close\r\n\r\n').encode());await w.drain()
                    response=await r.read()
                    if not response.startswith(b'HTTP/1.1 200 '):raise ValueError('HTTP failure')
                    header,body=response.split(b'\r\n\r\n',1)
                    length=next(int(x.split(b':',1)[1]) for x in header.split(b'\r\n') if x.lower().startswith(b'content-length:'))
                    if len(body)!=length:raise ValueError('truncated response')
                finally:w.close();await w.wait_closed()
            await asyncio.wait_for(exchange(),5)
            latency.append((time.monotonic()-actual)*1000);per_host[host]+=1
        except Exception as e:errors.append(type(e).__name__+(':'+str(e.errno) if isinstance(e,OSError) and e.errno else ''))
    offered=int(rate*duration)
    for i in range(offered):
        planned=start+i/rate
        await asyncio.sleep(max(0,planned-time.monotonic()))
        if len(tasks)>=limit:errors.append('driver_inflight_limit');continue
        t=asyncio.create_task(one(hosts[i%len(hosts)],planned));tasks.add(t);t.add_done_callback(tasks.discard)
    if tasks:await asyncio.gather(*tasks)
    elapsed=time.monotonic()-start
    latency.sort();schedule_lag.sort()
    def pct(a,p):return a[min(len(a)-1,math.ceil(len(a)*p)-1)] if a else None
    print(json.dumps({'driverFDLimit':resource.getrlimit(resource.RLIMIT_NOFILE)[0],'path':path,'vmCount':len(hosts),'offeredRps':rate,'scheduledSeconds':duration,'elapsedSeconds':elapsed,'requests':offered,'success':len(latency),'errors':len(errors),'errorTypes':{x:errors.count(x) for x in set(errors)},'successfulRps':len(latency)/elapsed,'latencyMs':{'p50':pct(latency,.5),'p95':pct(latency,.95),'p99':pct(latency,.99),'max':max(latency) if latency else None},'driverLagP99Ms':pct(schedule_lag,.99),'perHostSuccess':per_host}),flush=True)

if sys.argv[1]=='serve':asyncio.run(serve())
else:asyncio.run(load(json.loads(sys.argv[2])))
