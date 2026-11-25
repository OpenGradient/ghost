"""A localhost HTTP server that answers as soon as it's started."""
import socketserver
from http.server import ThreadingHTTPServer


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # The standard one looks up its own name here, before it listens. With slow reverse DNS, as on many a
        # Mac, that is half a minute of refused connections.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]
