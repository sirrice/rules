#!/usr/bin/env python3
"""
Static file server for the Rules Browser.
Sets COOP/COEP headers required for DuckDB WASM SharedArrayBuffer support.

Usage: python3 serve.py [port]
"""
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler

class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
        super().end_headers()
    def log_message(self, fmt, *args):
        print(f"  {self.address_string()} {fmt % args}")

port = int(sys.argv[1]) if len(sys.argv) > 1 else 5001
print(f"Rules Browser → http://localhost:{port}")
HTTPServer(("", port), Handler).serve_forever()
