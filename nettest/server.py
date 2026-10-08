"""Minimal plain-TCP echo server for diagnosing host-to-host connectivity.

No TLS, no cert pinning, no app logic -- just a raw socket bind/listen/accept.
Run on the COLLECTOR (host) machine:

    python nettest\\server.py 0.0.0.0 43199
    python nettest\\server.py 10.0.4.138 43199

Try both the all-interfaces bind and the specific detected LAN IP bind to see
if that distinction matters.
"""
import socket
import sys


def main() -> None:
    bind_address = sys.argv[1] if len(sys.argv) > 1 else "0.0.0.0"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 43199

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((bind_address, port))
        server.listen(1)
        print(f"Listening on {bind_address}:{port} ... (Ctrl+C to stop)")
        while True:
            conn, addr = server.accept()
            with conn:
                print(f"Connection from {addr}")
                data = conn.recv(1024)
                print(f"Received: {data!r}")
                conn.sendall(b"ack: " + data)


if __name__ == "__main__":
    main()
