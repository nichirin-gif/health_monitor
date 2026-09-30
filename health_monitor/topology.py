from mininet.net import Mininet
from mininet.topo import Topo
from mininet.cli import CLI
from mininet.log import setLogLevel
from mininet.node import OVSBridge


class HealthMonitorTopo(Topo):

    def build(self):
        switch = self.addSwitch('s1')

        for i in range(1, 5):
            host = self.addHost(f'h{i}')
            self.addLink(host, switch)

        monitor = self.addHost('h5')
        self.addLink(monitor, switch)


def run():
    topo = HealthMonitorTopo()

    net = Mininet(
        topo=topo,
        switch=OVSBridge,
        controller=None
    )

    net.start()

    print("\n*** Health Monitoring Network Started ***")
    print("h1-h4 = Monitoring Agents")
    print("h5 = Central Monitor")
    print("s1 = Network Switch")
    print()

    CLI(net)

    net.stop()


if __name__ == '__main__':
    setLogLevel('info')
    run()
