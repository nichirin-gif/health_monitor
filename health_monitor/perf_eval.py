"""
Performance evaluation: static (non-SDN) OVS vs Ryu-controlled SDN.

Must be run as root on the Mininet host (sudo python3 perf_eval.py).
For the SDN run, start the controller first:
    ryu-manager --ofp-tcp-listen-port 6633 sdn_controller.py --verbose

Measures, for each mode:
  - latency:      average ping RTT, h1 -> h5
  - packet loss:  ping loss %, h1 -> h5
  - throughput:   iperf TCP bandwidth, h1 -> h5
  - recovery time: seconds between killing a host's interface and the
                    switch's flow table (SDN) / MAC table (static)
                    forwarding to it again once it's restored

Results are printed as a simple comparison table.
"""

import re
import time

from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController
from mininet.log import setLogLevel

from topology import HealthMonitorTopo, CONTROLLER_IP, CONTROLLER_PORT


def run_static():
    topo = HealthMonitorTopo()
    net = Mininet(topo=topo, switch=OVSSwitch, controller=None,
                   autoSetMacs=True)
    net.start()
    for sw in net.switches:
        sw.cmd(f'ovs-vsctl set bridge {sw.name} protocols=OpenFlow13')
        sw.cmd(f'ovs-vsctl set-fail-mode {sw.name} standalone')
    results = measure(net, label="STATIC (no controller)")
    net.stop()
    return results


def run_sdn():
    topo = HealthMonitorTopo()
    controller = RemoteController('c0', ip=CONTROLLER_IP, port=CONTROLLER_PORT)
    net = Mininet(topo=topo, switch=OVSSwitch, controller=controller,
                   autoSetMacs=True)
    net.start()
    for sw in net.switches:
        sw.cmd(f'ovs-vsctl set bridge {sw.name} protocols=OpenFlow13')
    time.sleep(2)  # let the controller install table-miss flows
    results = measure(net, label="SDN (Ryu controller)")
    net.stop()
    return results


def measure(net, label):
    h1 = net.get('h1')
    h5 = net.get('h5')

    print(f"\n=== {label} ===")

    ping_out = h1.cmd(f'ping -c 20 -i 0.2 {h5.IP()}')
    loss_match = re.search(r'(\d+)% packet loss', ping_out)
    rtt_match = re.search(
        r'= [\d.]+/([\d.]+)/[\d.]+/[\d.]+ ms', ping_out)
    loss = float(loss_match.group(1)) if loss_match else None
    avg_rtt = float(rtt_match.group(1)) if rtt_match else None

    h5.cmd('iperf -s -p 5050 &')
    time.sleep(1)
    iperf_out = h1.cmd(f'iperf -c {h5.IP()} -p 5050 -t 5')
    h5.cmd('kill %iperf')
    bw_match = re.search(r'([\d.]+ [KMG]bits/sec)', iperf_out)
    throughput = bw_match.group(1) if bw_match else "n/a"

    recovery_time = measure_recovery(net, h1, h5)

    print(f"Avg RTT:        {avg_rtt} ms")
    print(f"Packet loss:    {loss}%")
    print(f"Throughput:     {throughput}")
    print(f"Recovery time:  {recovery_time:.3f} s")

    return {
        "label": label,
        "avg_rtt_ms": avg_rtt,
        "loss_pct": loss,
        "throughput": throughput,
        "recovery_s": recovery_time,
    }


def measure_recovery(net, h1, h5):
    """Bring h5's link down, then back up, and time how long until
    pings succeed again."""
    link = net.linksBetween(h5, net.get('s1'))[0]
    link.intf1.ifconfig('down')
    time.sleep(1)

    link.intf1.ifconfig('up')
    start = time.time()

    for _ in range(50):
        result = h1.cmd(f'ping -c 1 -W 1 {h5.IP()}')
        if ', 0% packet loss' in result:
            return time.time() - start
        time.sleep(0.2)

    return -1.0  # did not recover within timeout


def print_comparison(static_results, sdn_results):
    print("\n=== Comparison: Static vs SDN ===")
    print(f"{'Metric':<16}{'Static':<20}{'SDN':<20}")
    print(f"{'Avg RTT (ms)':<16}{static_results['avg_rtt_ms']!s:<20}"
          f"{sdn_results['avg_rtt_ms']!s:<20}")
    print(f"{'Loss (%)':<16}{static_results['loss_pct']!s:<20}"
          f"{sdn_results['loss_pct']!s:<20}")
    print(f"{'Throughput':<16}{static_results['throughput']!s:<20}"
          f"{sdn_results['throughput']!s:<20}")
    print(f"{'Recovery (s)':<16}{static_results['recovery_s']!s:<20}"
          f"{sdn_results['recovery_s']!s:<20}")


if __name__ == '__main__':
    setLogLevel('info')
    static_results = run_static()
    print("\nStart the Ryu controller now, then press Enter to continue "
          "with the SDN measurement run...")
    input()
    sdn_results = run_sdn()
    print_comparison(static_results, sdn_results)
