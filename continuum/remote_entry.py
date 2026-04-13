"""Entry point for background remote server process.

Called by `continuum remote start --background` via:
    python -m continuum.remote_entry [--tunnel] [--token TOKEN] [--port PORT] [--host HOST]
"""
import argparse
from .remote_server import run_remote, run_remote_with_tunnel

parser = argparse.ArgumentParser()
parser.add_argument("--tunnel", action="store_true")
parser.add_argument("--token",  default=None)
parser.add_argument("--port",   type=int, default=8766)
parser.add_argument("--host",   default="127.0.0.1")
args = parser.parse_args()

if args.tunnel:
    run_remote_with_tunnel(host=args.host, port=args.port, token=args.token)
else:
    run_remote(host=args.host, port=args.port, token=args.token)
