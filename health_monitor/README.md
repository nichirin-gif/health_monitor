# Remote System Health Monitoring over SDN

UDP-based health monitoring (CPU/memory/process/network stats) extended with
a Ryu SDN control plane on top of the existing Mininet/OVS topology.

## Layers

| Layer | File | Role |
|---|---|---|
| Application | `agent.py` | collects host stats via `psutil`, sends JSON over UDP |
| Application | `monitor.py` | UDP server, priority queue (heapq), failure detection, notifies the SDN controller on CRITICAL/FAILURE events |
| Control plane | `sdn_controller.py` | Ryu app: OpenFlow 1.3 learning switch + REST `/notify` endpoint + dynamic flow/meter changes |
| Data plane | `topology.py` | Mininet topology: h1-h4 (agents) + h5 (monitor) on OVS switch `s1`, connected to the Ryu controller |
| Evaluation | `perf_eval.py` | compares static (no controller) vs SDN-controlled network: latency, loss, throughput, recovery time |
| Test | `priority_test.py` | standalone demo of the heap-based alert ordering |

## 1. Install dependencies (Ubuntu)

```bash
sudo apt update
sudo apt install -y mininet openvswitch-switch iperf python3-pip
pip3 install psutil ryu
```

If `pip3 install ryu` fails on newer Python (Ryu is unmaintained upstream),
use the maintained fork instead:

```bash
pip3 install os-ken
# then run with: osken-manager instead of ryu-manager (same CLI/API)
```

## 2. Start the SDN controller

```bash
cd health_monitor
ryu-manager --ofp-tcp-listen-port 6633 sdn_controller.py --verbose
```

This starts:
- the OpenFlow server on TCP 6633 (Mininet's `RemoteController` connects here)
- a REST API on `http://127.0.0.1:8080/notify` (used by `monitor.py`)

## 3. Start the Mininet topology

In a second terminal:

```bash
sudo python3 topology.py
```

This brings up `h1..h4` (agents), `h5` (monitor) and `s1` (OVS, OpenFlow 1.3),
connected to the Ryu controller. Verify basic connectivity first:

```
mininet> pingall
```

You should see 0% loss — this confirms the controller is installing table-miss
and learning-switch flows correctly before any monitoring traffic runs.

## 4. Run the monitoring application

Inside the Mininet CLI:

```
mininet> h5 python3 monitor.py &
mininet> h1 python3 agent.py Server-1 &
mininet> h2 python3 agent.py Server-2 &
mininet> h3 python3 agent.py Server-3 &
mininet> h4 python3 agent.py Server-4 &
```

`monitor.py` behaves exactly as before (UDP server, priority queue,
failure detection) but now also POSTs a small JSON event to the controller's
`/notify` endpoint whenever:
- a node's CPU/memory crosses into CRITICAL (`condition: "CRITICAL"`)
- a CRITICAL node recovers (`condition: "RECOVERED_CRITICAL"`)
- a node is declared failed (silent for 12s) (`condition: "FAILURE"`)
- a failed node starts reporting again (`condition: "RECOVERED_FAILURE"`)

These notifications never block the UDP loop — `notify_controller()` uses a
short timeout and silently ignores failures, so the existing monitoring
behavior is unaffected if the controller is down.

## 5. Dynamic SDN behavior

`sdn_controller.py` reacts to those events by changing the flow tables, not
just logging them:

- **CRITICAL** → installs an OpenFlow meter (`METER_ID=1`, rate-limited to
  `RATE_LIMIT_KBPS`) and attaches it to every *other* host's flows, so the
  critical node's reports get comparatively more of the shared link.
  Removed again on `RECOVERED_CRITICAL`.
- **FAILURE** → installs a high-priority drop rule matching the failed
  host's IP, isolating it from the network. Removed again on
  `RECOVERED_FAILURE`.

Watch the controller's `--verbose` log to see these flow mods happen live,
and confirm with:

```bash
sudo ovs-ofctl dump-flows s1 -O OpenFlow13
```

## 6. Challenging scenario: simulated node/service failure

Force a node offline mid-test to trigger the failure path end-to-end:

```
mininet> h1 python3 agent.py Server-1 &
mininet> h1 ifconfig h1-eth0 down
```

Within `FAILURE_TIMEOUT` (12s) `monitor.py` should print
`🚨 FAILURE DETECTED: Server-1 is not responding` and the controller log
should show the isolating drop rule being installed. Bring it back with:

```
mininet> h1 ifconfig h1-eth0 up
```

and confirm the `RECOVERED_FAILURE` flow removal in the controller log.

## 7. Performance evaluation

```bash
sudo python3 perf_eval.py
```

This runs the same topology twice — once with `controller=None` (today's
original behavior) and once with the Ryu controller — and reports average
ping RTT, packet loss, iperf throughput, and link-recovery time for both, so
the SDN run can be compared directly against the pre-SDN baseline.

## Known limitation

The topology is a single switch / single broadcast domain, so there is no
alternate path to reroute traffic onto during a failure — the dynamic
response here is isolation (drop) + rate-limiting (meter) rather than
path-based rerouting. A multi-switch topology would be required to
demonstrate true rerouting; that is a natural extension but was kept out of
scope to stay consistent with the existing `topology.py` design.
