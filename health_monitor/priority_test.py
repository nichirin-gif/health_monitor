import heapq

queue = []

normal = (2, 1, "NORMAL")
warning = (1, 2, "WARNING")
critical = (0, 3, "CRITICAL")

heapq.heappush(queue, normal)
heapq.heappush(queue, warning)
heapq.heappush(queue, critical)

print("Processing order:")

while queue:
    priority, timestamp, name = heapq.heappop(queue)
    print(name)

