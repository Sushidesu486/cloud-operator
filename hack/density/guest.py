"""Small asyncio web app and a remote-API-bound agent simulation."""
import asyncio,json,os,time
UPSTREAM=os.environ['UPSTREAM']
WEB=json.dumps({'message':'hello','data':'x'*4096}).encode()

async def request(reader,writer):
    try:
        head=await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'),10)
        path=head.split(b' ')[1]
        if path==b'/agent':
            r,w=await asyncio.open_connection(UPSTREAM,9090)
            w.write(b'GET /completion HTTP/1.1\r\nHost: mock\r\nConnection: close\r\n\r\n');await w.drain()
            response=await asyncio.wait_for(r.read(),10);w.close();await w.wait_closed()
            data=json.loads(response.split(b'\r\n\r\n',1)[1])
            # One millisecond of actual process CPU per agent request, plus JSON/network work.
            end=time.process_time()+0.001
            while time.process_time()<end:pass
            body=json.dumps({'result':data['result'][:100],'ok':True}).encode()
        else:body=WEB
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: '+str(len(body)).encode()+b'\r\nConnection: close\r\n\r\n'+body)
        await writer.drain()
    except (OSError,ValueError,asyncio.IncompleteReadError,asyncio.TimeoutError):pass
    finally:writer.close()

async def main():
    server=await asyncio.start_server(request,'0.0.0.0',8080)
    async with server:await server.serve_forever()
asyncio.run(main())
