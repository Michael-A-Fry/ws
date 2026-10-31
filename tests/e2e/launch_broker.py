"""Test launcher: a broker that may use plain http to 127.0.0.1, so the end-to-end test can use a local stub upstream.

The real `ws broker serve` has no such switch. This runs the installed copy of ws (PYTHONPATH=/opt/ws/lib) with
the same store and registry as the service, on its own socket.

usage: launch_broker.py SOCKET GROUP
"""

import signal
import sys

from ws import broker

b = broker.Broker.from_env(sys.argv[1], sys.argv[2], 0, 3600, allow_plain_http=True)
signal.signal(signal.SIGTERM, lambda *_: b.request_stop())
b.start()
print("ready", flush=True)
b._stopped.wait()
