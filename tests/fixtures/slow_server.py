# Simulated dev server: slow startup, prints its URL, and logs a runtime error on GET /boom.
import http.server, sys, time
port = int(sys.argv[1]) if len(sys.argv) > 1 else 4310
delay = float(sys.argv[2]) if len(sys.argv) > 2 else 3
print("compiling...", flush=True)
for i in range(int(delay * 2)):
    time.sleep(0.5); print(f"  compiled {i+1} modules", flush=True)
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/boom":
            print("Error: Cannot read properties of undefined (reading 'id') at /api/boom", flush=True)
            self.send_response(500); self.end_headers(); return
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *a): print("GET", self.path, flush=True)
s = http.server.HTTPServer(("127.0.0.1", port), H)
print(f"  ➜  Local:   http://localhost:{port}/", flush=True)
s.serve_forever()
