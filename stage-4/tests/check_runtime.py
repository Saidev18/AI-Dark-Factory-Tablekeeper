"""Container smoke checks; stdout contains no service state or credentials."""
import hashlib
import json
import pathlib
import socket
import sys
import time
from urllib.request import urlopen

port = int(sys.argv[1])
started = time.monotonic()
while True:
    try:
        with urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
            assert response.status == 200 and json.load(response) == {"status": "ok"}
        break
    except OSError:
        if time.monotonic() - started > 60:
            raise
        time.sleep(0.05)
print(f"health on PORT={port}: {time.monotonic() - started:.3f}s")
routes = pathlib.Path("/proc/net/route").read_text().splitlines()[1:]
assert not any(line.split()[1] == "00000000" for line in routes), routes
for address in ("1.1.1.1", "8.8.8.8"):
    try:
        socket.create_connection((address, 443), timeout=0.5).close()
    except OSError:
        pass
    else:
        raise AssertionError("Outbound access unexpectedly available")
print("no default route; outbound socket attempts fail")
from zoneinfo import ZoneInfo
for zone in ("Europe/Berlin", "America/New_York"):
    ZoneInfo(zone)
print("both IANA zones available without networking")
print("server_sha256=" + hashlib.sha256(pathlib.Path("/app/server.py").read_bytes()).hexdigest())
