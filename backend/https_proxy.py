"""Minimal TLS-to-TCP proxy so mobile browsers get HTTPS for geolocation."""

import asyncio
import ssl
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CERT = ROOT / "data" / "certs" / "cert.pem"
KEY = ROOT / "data" / "certs" / "key.pem"
LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 3443
TARGET_HOST = "127.0.0.1"
TARGET_PORT = 3000


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(65536):
            writer.write(chunk)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except (OSError, asyncio.CancelledError):
        pass


async def _handle(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
    peer = client_writer.get_extra_info("peername")
    print(f"Incoming TLS connection from {peer}", flush=True)
    try:
        upstream_reader, upstream_writer = await asyncio.open_connection(TARGET_HOST, TARGET_PORT)
    except OSError as exc:
        print(f"Upstream connect failed: {exc}", flush=True)
        client_writer.close()
        return
    try:
        await asyncio.gather(
            _pipe(client_reader, upstream_writer),
            _pipe(upstream_reader, client_writer),
        )
    finally:
        upstream_writer.close()
        client_writer.close()


async def main() -> None:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(CERT), keyfile=str(KEY))
    server = await asyncio.start_server(_handle, LISTEN_HOST, LISTEN_PORT, ssl=ctx)
    print(f"HTTPS proxy listening on https://{LISTEN_HOST}:{LISTEN_PORT} -> http://{TARGET_HOST}:{TARGET_PORT}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
