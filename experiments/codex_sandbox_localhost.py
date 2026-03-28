import socket

HOST = "127.0.0.1"
PORT = 9000
MESSAGE = "hello from python\n"

with socket.create_connection((HOST, PORT)) as s:
    s.sendall(MESSAGE.encode("utf-8"))
