"""Benchmark-only TCP forwarding; pinned to the same CPU as the VMM."""
import asyncio
async def connection(r,w):
    try:
        gr,gw=await asyncio.wait_for(asyncio.open_connection('192.0.2.2',8080),3)
        async def pipe(src,dst):
            try:
                while data:=await src.read(65536):dst.write(data);await dst.drain()
            except (OSError,ConnectionError):pass
            finally:dst.close()
        await asyncio.gather(pipe(r,gw),pipe(gr,w))
    except (OSError,asyncio.TimeoutError):w.close()
async def main():
    server=await asyncio.start_server(connection,'0.0.0.0',8080)
    async with server:await server.serve_forever()
asyncio.run(main())
