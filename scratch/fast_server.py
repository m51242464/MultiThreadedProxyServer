import http.server
import socketserver

class FastHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.send_header("Content-length", "2")
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        return # Disable logging for performance

# Allow address reuse
socketserver.TCPServer.allow_reuse_address = True
with socketserver.ThreadingTCPServer(("", 9090), FastHandler) as httpd:
    print("Fast server running on port 9090")
    httpd.serve_forever()
