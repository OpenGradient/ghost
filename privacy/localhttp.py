"""A localhost HTTP server that answers as soon as it's started, alone on its port."""
import os
import socket
import socketserver
from http.server import ThreadingHTTPServer


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"   # there it would let others share the port, under another account too

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # The standard one looks up its own name here, before it listens. With slow reverse DNS, as on many a
        # Mac, that is half a minute of refused connections.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]
