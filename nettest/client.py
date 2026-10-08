"""Minimal plain-TCP echo client for diagnosing host-to-host connectivity.

No TLS, no cert pinning, no app logic -- just connect/send/recv.
Run on the WATCHER (remote) machine:

    python nettest\\client.py 10.0.4.138 43199 hello
"""
import socket
import sys


def main() -> None:
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 43199
    message = sys.argv[3] if len(sys.argv) > 3 else "hello"

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        print(f"Connecting to {host}:{port} ...")
        client.connect((host, port))
        client.sendall(message.encode())
        reply = client.recv(1024)
        print(f"Reply: {reply!r}")


if __name__ == "__main__":
    main()
