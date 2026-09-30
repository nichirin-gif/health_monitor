import socket
import psutil
import time
import json
import sys

SERVER_IP = "10.0.0.5"
SERVER_PORT = 5000

node = sys.argv[1] if len(sys.argv) > 1 else "Server-1"

client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

while True:
    cpu = psutil.cpu_percent(interval=1)
    memory = psutil.virtual_memory().percent
    processes = len(psutil.pids())

    net = psutil.net_io_counters()

    health = {
        "node": node,
        "cpu": cpu,
        "memory": memory,
        "processes": processes,
        "bytes_sent": net.bytes_sent,
        "bytes_received": net.bytes_recv,
        "timestamp": time.time()
    }

    message = json.dumps(health)

    client.sendto(message.encode(), (SERVER_IP, SERVER_PORT))

    print(f"Sent: {message}")

    time.sleep(2)
