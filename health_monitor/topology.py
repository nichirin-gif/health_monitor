from mininet.net import Mininet
from mininet.topo import Topo
from mininet.cli import CLI
from mininet.log import setLogLevel
from mininet.node import OVSSwitch, RemoteController

CONTROLLER_IP = "127.0.0.1"
CONTROLLER_PORT = 6633


class HealthMonitorTopo(Topo):

    def build(self):
        switch = self.addSwitch('s1', protocols='OpenFlow13')

        for i in range(1, 5):
            host = self.addHost(f'h{i}', ip=f'10.0.0.{i}/24')
            self.addLink(host, switch)

        monitor = self.addHost('h5', ip='10.0.0.5/24')
        self.addLink(monitor, switch)


def run():
    topo = HealthMonitorTopo()

    controller = RemoteController(
        'c0', ip=CONTROLLER_IP, port=CONTROLLER_PORT
    )

    net = Mininet(
        topo=topo,
        switch=OVSSwitch,
        controller=controller,
        autoSetMacs=True
    )

    net.start()
    net.get('s1').cmd('ip addr replace 10.0.0.254/24 dev s1')
    net.get('s1').cmd('ip link set s1 up')

    for sw in net.switches:
        sw.cmd(f'ovs-vsctl set bridge {sw.name} protocols=OpenFlow13')

    print("\n*** Health Monitoring Network Started (SDN mode) ***")
    print("h1-h4 = Monitoring Agents")
    print("h5    = Central Monitor")
    print("s1    = OVS switch (OpenFlow 1.3, remote controller)")
    print(f"c0    = OS-Ken controller @ {CONTROLLER_IP}:{CONTROLLER_PORT}")
    print()
    print("Start the OS-Ken controller BEFORE running this script:")
    print("  osken-manager --ofp-tcp-listen-port 6633 "
          "sdn_controller.py --verbose")
    print()

    CLI(net)

    net.stop()


if __name__ == '__main__':
    setLogLevel('info')
    run()
