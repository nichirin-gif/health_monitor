# Remote System Health Monitoring over SDN

A Python/UDP health-monitoring application running in Mininet, with an OS-Ken
OpenFlow 1.3 controller that changes switch rules in response to health events.

The working setup was tested on an Ubuntu 26.04.1 ARM64 VM with Python 3.14.4,
Mininet, Open vSwitch, iperf 2, and OS-Ken 3.1.1.

## Architecture

- `h1`–`h4` (`10.0.0.1`–`10.0.0.4`): agents reporting CPU, memory, process count,
  and network I/O counters approximately every three seconds.
- `h5` (`10.0.0.5`): central UDP monitor on port 5000, with a priority queue,
  threshold classification, and missing-report detection.
- `s1`: Open vSwitch using OpenFlow 1.3, connected to the controller on
  `127.0.0.1:6633` in the VM's main network namespace.
- `10.0.0.254`: an address assigned to the switch's internal interface so h5
  can reach the controller's HTTP endpoint at `http://10.0.0.254:8080/notify`.

The controller and Mininet run inside the same Ubuntu VM. This is a
single-switch topology; its dynamic policies are source-traffic filtering and
rate limiting. It does not provide alternate-path rerouting.

## Files

| File | Purpose |
|---|---|
| `agent.py` | Collects metrics with psutil and sends JSON over UDP; retries after send errors |
| `monitor.py` | Receives and validates reports, processes alerts, detects missing reports, and queues HTTP notifications |
| `sdn_controller.py` | OS-Ken learning switch, HTTP notification API, meter rules, and failure/recovery rules |
| `topology.py` | Creates the five hosts, switch, remote-controller connection, and management address |
| `priority_test.py` | Standalone demonstration of heap-based priority ordering |
| `perf_eval.py` | Separate network-only comparison script; not the source of the functional-test results below |

## Install

For a first-time checkout:

```bash
git clone https://github.com/nichirin-gif/health_monitor.git ~/health_monitor_sdn
```

If the repository is already downloaded, use the existing copy. The commands
below assume its scripts are in `~/health_monitor_sdn/health_monitor`.

```bash
sudo apt update
sudo apt install -y mininet openvswitch-switch iperf python3-pip python3-venv python3-psutil
cd ~/health_monitor_sdn/health_monitor
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install "os-ken==3.1.1" psutil
.venv/bin/osken-manager --version
```

Expected version: `osken-manager 3.1.1`. Keep this version pinned: the project
uses its manager command. The current controller imports `os_ken`; installing
Ryu or only changing a command name is not the setup used here.

The Ubuntu psutil package is needed because the Mininet commands below use
system `python3`. The virtual environment supplies the controller dependencies.

## Start the controller and network

In terminal 1:

```bash
cd ~/health_monitor_sdn/health_monitor
.venv/bin/osken-manager --ofp-tcp-listen-port 6633 sdn_controller.py --verbose
```

Leave it running. Startup should show the health API listening; after Mininet
starts, the controller should log that the switch connected and a table-miss
flow was installed. Eventlet warnings appeared during the tested setup; check
connectivity and any actual tracebacks when diagnosing startup problems.

In terminal 2:

```bash
cd ~/health_monitor_sdn/health_monitor
sudo python3 topology.py
```

The following commands go at the `mininet>` prompt. Run each line separately;
do not include the prompt itself when pasting commands.

```text
pingall
h5 ping -c 2 10.0.0.254
```

Both connectivity checks should have zero packet loss.

## Start monitoring

Run each line separately at `mininet>`:

```text
h5 python3 -u monitor.py > /tmp/health-monitor.log 2>&1 < /dev/null &
h1 python3 -u agent.py Server-1 > /tmp/health-agent-1.log 2>&1 < /dev/null &
h2 python3 -u agent.py Server-2 > /tmp/health-agent-2.log 2>&1 < /dev/null &
h3 python3 -u agent.py Server-3 > /tmp/health-agent-3.log 2>&1 < /dev/null &
h4 python3 -u agent.py Server-4 > /tmp/health-agent-4.log 2>&1 < /dev/null &
```

Wait about five seconds, then inspect the monitor:

```text
h5 tail -n 50 /tmp/health-monitor.log
```

The controller should log registration of `10.0.0.1` through `10.0.0.4`.
Avoid launching duplicate agents or a second monitor in the same session.

### Classification and timing

| Metric | Warning | Critical |
|---|---:|---:|
| CPU | 60% | 80% |
| Memory | 75% | 90% |

Either metric can raise a report's severity. The application queue processes
CRITICAL before WARNING before NORMAL when reports are queued together.

A host that has reported at least once is marked failed after more than
12 seconds of silence; the checker runs every two seconds, so detection is
normally around 12–14 seconds after the last report. A never-started agent
is not detected by this mechanism.

The logged alert latency is elapsed time from the agent's report timestamp
to application processing. It includes delivery and scheduling/queueing time;
it is not an isolated measurement of network propagation delay.

## SDN policies

| Flow priority | Policy |
|---:|---|
| 110 | Allow registered agents' IPv4 UDP health reports to h5 port 5000 using NORMAL forwarding |
| 100 | Drop other IPv4 packets whose source is a host declared failed |
| 10 | Apply meter 1 to other IPv4 traffic from registered, non-critical agents while a critical event is active |
| 1 | Learned forwarding rules, with a 20-second idle timeout |
| 0 | Send unmatched packets to the controller |

Health-report exceptions allow returning agents to report even while their
failure drop rule exists. Recovery removes the priority-100 rule.

Meter 1 is configured at **500 kbps**, shared by the matching non-critical
traffic. It is not a separate 500 kbps allowance for each host. All registered
agents' health reports bypass this meter through priority 110. Multiple
critical agents remain exempt from the priority-10 rules.

When the last critical event clears, the priority-10 rules are removed. The
unused meter configuration may remain installed.

HTTP notifications run through a separate queue so network timeouts do not
block the UDP receive loop. Notification failures are logged. Delivery is
best-effort: there is no automatic notification retry or state replay after a
controller restart. For a clean demo restart, restart the controller, topology,
monitor, and agents together.

## Failure and automatic-recovery demo

With all agents and the monitor running, enter at `mininet>`:

```text
link s1 h1 down
```

Wait 15 seconds. Then run these separately:

```text
h1 tail -n 3 /tmp/health-agent-1.log
h5 grep -E 'FAILURE|RECOVERED|Controller notification failed' /tmp/health-monitor.log
sh ovs-ofctl -O OpenFlow13 dump-flows s1
```

The agent should print send-error retry messages; the monitor should declare
Server-1 failed; the switch should contain a priority-100 source drop rule.

Restore the link:

```text
link s1 h1 up
```

Wait about five seconds, then check:

```text
h5 grep -E 'FAILURE|RECOVERED|Controller notification failed' /tmp/health-monitor.log | tail -n 4
sh ovs-ofctl -O OpenFlow13 dump-flows s1 | grep -E 'priority=(100|110)'
```

Server-1 should recover without restarting its patched agent. Its priority-100
rule should disappear, while the four priority-110 health-report rules remain.

## Simulated critical-event and traffic demo

The following test directly injects a controller event. It demonstrates the SDN
reaction; it does not demonstrate a real CPU threshold crossing through the
agent and monitor.

With all four agents registered and otherwise non-critical, enter:

```text
h5 python3 -c 'import urllib.request,json; r=urllib.request.urlopen(urllib.request.Request("http://10.0.0.254:8080/notify",data=json.dumps({"ip":"10.0.0.1","condition":"CRITICAL"}).encode(),headers={"Content-Type":"application/json"})); print(r.status,r.read().decode())'
```

Expected response: `200 ok`. Check the rules and meter separately:

```text
sh ovs-ofctl -O OpenFlow13 dump-flows s1 | grep 'priority=10,'
sh ovs-ofctl -O OpenFlow13 dump-meters s1
```

Expect three priority-10 meter rules for h2–h4, plus meter 1 with a 500 kbps drop
band. Then start the UDP test server and run the client, one command at a time:

```text
h5 iperf -s -u -p 5050 > /tmp/health-iperf.log 2>&1 < /dev/null &
h2 iperf -c 10.0.0.5 -u -p 5050 -b 2M -t 5
```

Use the receiver's **Server Report** to assess delivered throughput; the sender
can continue offering about 2 Mbps while the meter drops excess traffic.

Clear the simulated event:

```text
h5 python3 -c 'import urllib.request,json; r=urllib.request.urlopen(urllib.request.Request("http://10.0.0.254:8080/notify",data=json.dumps({"ip":"10.0.0.1","condition":"RECOVERED_CRITICAL"}).encode(),headers={"Content-Type":"application/json"})); print(r.status,r.read().decode())'
```

Repeat the identical client test:

```text
h2 iperf -c 10.0.0.5 -u -p 5050 -b 2M -t 5
```

### Observed functional-test results

These are individual five-second UDP runs from the tested VM, not repeated
benchmark averages or guarantees for other machines.

| State | Receiver throughput | Receiver packet-loss count |
|---|---:|---:|
| Simulated critical policy active | 583 Kbits/sec | 646 / 895, approximately 72.2% |
| Simulated critical policy cleared | 2.10 Mbits/sec | 0 / 894 |

The limited run printed `(0%)` despite its 646/895 loss count; that percentage
was inconsistent, so the count is reported here. The short-run 583 Kbits/sec
measurement is above the configured 500 kbps; startup effects and measurement
windows may contribute. A longer, repeated study is needed to characterize
steady-state enforcement accurately.

## Stop the demo

Optionally stop the test server first, at `mininet>`:

```text
h5 pkill -f '^iperf -s -u -p 5050$'
```

Then type `exit` at `mininet>`. Press Ctrl+C in the controller terminal.
The source files remain saved.

## Scope and remaining work

- Mininet hosts share the VM's kernel and CPU/memory resources. Their psutil
  CPU, memory, and process measurements are VM-wide rather than independent
  physical-server measurements; network namespaces provide the network isolation.
- UDP reports are best-effort; missing reports indicate agent/network silence,
  not proof that a physical server has failed.
- The topology has no alternate route, configured shared-link bottleneck, or
  demonstrated multipath recovery. Metering reduces matching background traffic;
  the tests do not establish an improvement in critical-report latency.
- Functional checks covered connectivity, four-agent reporting, failure/recovery,
  and a simulated critical controller event with a short throughput comparison.
  Full scalability, sustained-load, and real CPU-critical end-to-end evaluation
  were not performed.
- `priority_test.py` can demonstrate queue ordering independently.
  `perf_eval.py` is a separate network-only comparison script that does not start
  the monitoring application or trigger its health events. Review and adapt it
  before using it for a formal evaluation; the results above came from the manual
  commands documented here.
