import socket
import queue
import json
import heapq
import itertools
import threading
import time
import urllib.request
import urllib.error

HOST = "0.0.0.0"
PORT = 5000

CPU_WARNING = 60
CPU_CRITICAL = 80

MEMORY_WARNING = 75
MEMORY_CRITICAL = 90

FAILURE_TIMEOUT = 12

CONTROLLER_URL = "http://10.0.0.254:8080/notify"
CONTROLLER_TIMEOUT = 0.5

server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
server.bind((HOST, PORT))

priority_queue = []
queue_lock = threading.Lock()
# heapq falls back to comparing the next tuple element on a tie; a
# monotonically increasing counter breaks (priority, timestamp) ties
# before heapq ever has to compare the health dict.
_seq = itertools.count()

last_seen = {}
node_ip = {}
failed_nodes = set()
critical_nodes = set()
node_lock = threading.Lock()

print(f"Monitoring server listening on UDP port {PORT}...")


controller_events = queue.Queue()

def notify_controller(ip, condition):
    controller_events.put((ip, condition))

def controller_worker():
    while True:
        _send_notification(*controller_events.get())

def _send_notification(ip, condition):
    """Best-effort notification to the Ryu SDN controller. The UDP
    health-monitoring loop must keep working even if the controller
    or its REST API is unreachable."""
    payload = json.dumps({"ip": ip, "condition": condition}).encode()
    request = urllib.request.Request(
        CONTROLLER_URL, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=CONTROLLER_TIMEOUT):
            pass
    except (urllib.error.URLError, OSError) as exc:
        print("Controller notification failed:", condition, ip, exc)


def receive_reports():
    while True:
        data, address = server.recvfrom(4096)

        try:
            health = json.loads(data.decode())
            assert isinstance(health, dict)
            assert isinstance(health["node"], str) and health["node"]
            assert all(isinstance(health[k], (int, float))
                       for k in ("cpu", "memory", "timestamp"))
        except (ValueError, KeyError, TypeError, AssertionError):
            print("Ignored invalid report from", address)
            continue

        node = health["node"]
        cpu = health["cpu"]
        memory = health["memory"]
        ip = address[0]

        with node_lock:
            last_seen[node] = time.time()

            if node not in node_ip:
                node_ip[node] = ip
                notify_controller(ip, "REGISTER")

            if node in failed_nodes:
                failed_nodes.remove(node)
                print(f"RECOVERED: {node} is sending reports again")
                notify_controller(ip, "RECOVERED_FAILURE")

        if cpu >= CPU_CRITICAL or memory >= MEMORY_CRITICAL:
            priority = 0
            priority_name = "CRITICAL"
        elif cpu >= CPU_WARNING or memory >= MEMORY_WARNING:
            priority = 1
            priority_name = "WARNING"
        else:
            priority = 2
            priority_name = "NORMAL"

        with node_lock:
            if priority_name == "CRITICAL" and node not in critical_nodes:
                critical_nodes.add(node)
                notify_controller(ip, "CRITICAL")
            elif priority_name != "CRITICAL" and node in critical_nodes:
                critical_nodes.remove(node)
                notify_controller(ip, "RECOVERED_CRITICAL")

        with queue_lock:
            heapq.heappush(
                priority_queue,
                (priority, health["timestamp"], next(_seq), health, priority_name)
            )

        print(f"Received {priority_name} report from {node}")


def process_reports():
    while True:
        time.sleep(0.05)

        with queue_lock:
            if priority_queue:
                _, _, _, report, report_priority = heapq.heappop(priority_queue)
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
                        ip = node_ip.get(node)
                        if ip:
                            notify_controller(ip, "FAILURE")


receiver_thread = threading.Thread(target=receive_reports)
processor_thread = threading.Thread(target=process_reports)
failure_thread = threading.Thread(target=check_failures)

threading.Thread(target=controller_worker, daemon=True).start()
receiver_thread.start()
processor_thread.start()
failure_thread.start()

receiver_thread.join()
processor_thread.join()
failure_thread.join()
