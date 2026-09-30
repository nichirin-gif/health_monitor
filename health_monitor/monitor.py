import socket
import json
import heapq
import threading
import time

HOST = "0.0.0.0"
PORT = 5000

CPU_WARNING = 60
CPU_CRITICAL = 80

MEMORY_WARNING = 75
MEMORY_CRITICAL = 90

FAILURE_TIMEOUT = 12

server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
server.bind((HOST, PORT))

priority_queue = []
queue_lock = threading.Lock()

last_seen = {}
failed_nodes = set()
node_lock = threading.Lock()

print(f"Monitoring server listening on UDP port {PORT}...")


def receive_reports():
    while True:
        data, address = server.recvfrom(4096)

        health = json.loads(data.decode())

        node = health["node"]
        cpu = health["cpu"]
        memory = health["memory"]

        with node_lock:
            last_seen[node] = time.time()

            if node in failed_nodes:
                failed_nodes.remove(node)
                print(f"RECOVERED: {node} is sending reports again")

        if cpu >= CPU_CRITICAL or memory >= MEMORY_CRITICAL:
            priority = 0
            priority_name = "CRITICAL"
        elif cpu >= CPU_WARNING or memory >= MEMORY_WARNING:
            priority = 1
            priority_name = "WARNING"
        else:
            priority = 2
            priority_name = "NORMAL"

        with queue_lock:
            heapq.heappush(
                priority_queue,
                (priority, health["timestamp"], health, priority_name)
            )

        print(f"Received {priority_name} report from {node}")


def process_reports():
    while True:
        time.sleep(3)

        with queue_lock:
            if priority_queue:
                _, _, report, report_priority = heapq.heappop(priority_queue)
            else:
                report = None

        if report:
            latency = time.time() - report["timestamp"]

            print("----- Processing Report -----")
            print(f"Node: {report['node']}")
            print(f"CPU: {report['cpu']}%")
            print(f"Memory: {report['memory']}%")
            print(f"Priority: {report_priority}")
            print(f"Alert latency: {latency:.3f} seconds")
            print()


def check_failures():
    while True:
        time.sleep(2)

        current_time = time.time()

        with node_lock:
            for node, last_time in list(last_seen.items()):
                if current_time - last_time > FAILURE_TIMEOUT:
                    if node not in failed_nodes:
                        failed_nodes.add(node)
                        print(f"🚨 FAILURE DETECTED: {node} is not responding")


receiver_thread = threading.Thread(target=receive_reports)
processor_thread = threading.Thread(target=process_reports)
failure_thread = threading.Thread(target=check_failures)

receiver_thread.start()
processor_thread.start()
failure_thread.start()

receiver_thread.join()
processor_thread.join()
failure_thread.join()
