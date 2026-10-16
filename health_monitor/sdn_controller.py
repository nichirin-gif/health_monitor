"""
Ryu SDN controller for the health-monitoring project.

Responsibilities:
  1. Baseline L2 learning-switch forwarding over OpenFlow 1.3, so the
     network behaves exactly like the old standalone OVS bridge until
     a condition changes that.
  2. A small REST endpoint (/notify) that monitor.py calls whenever it
     classifies a report as CRITICAL or declares/clears a node failure.
  3. Dynamic flow-table changes in response to those notifications:
       - CRITICAL  -> rate-limit (meter) all other hosts' traffic so the
                      critical node's UDP reports get priority on the
                      shared switch.
       - RECOVERED -> remove that meter.
       - FAILURE   -> install a drop rule isolating the failed host's
                      port (filtering / failure containment).
       - RECOVERED -> remove the drop rule.

Run with:
  ryu-manager --ofp-tcp-listen-port 6633 sdn_controller.py --verbose
"""

import json

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet
from ryu.app.wsgi import WSGIApplication, ControllerBase, route
from ryu.lib import ofctl_v1_3
from webob import Response

NOTIFY_INSTANCE_NAME = "health_monitor_api"

RATE_LIMIT_KBPS = 500  # cap applied to non-critical hosts when one node is CRITICAL
METER_ID = 1


class HealthMonitorController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {"wsgi": WSGIApplication}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mac_to_port = {}
        self.datapaths = {}
        self.critical_nodes = set()
        self.failed_nodes = set()
        self.ip_to_port = {}

        wsgi = kwargs["wsgi"]
        wsgi.register(NotifyController, {NOTIFY_INSTANCE_NAME: self})

    # ---------- switch setup ----------

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        self.datapaths[datapath.id] = datapath

        # table-miss -> send to controller
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                           ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)

        self.logger.info("Switch %s connected, table-miss flow installed",
                          datapath.id)

    def add_flow(self, datapath, priority, match, actions,
                 buffer_id=None, idle_timeout=0, hard_timeout=0):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS,
                                              actions)]
        if buffer_id:
            mod = parser.OFPFlowMod(datapath=datapath, buffer_id=buffer_id,
                                     priority=priority, match=match,
                                     instructions=inst,
                                     idle_timeout=idle_timeout,
                                     hard_timeout=hard_timeout)
        else:
            mod = parser.OFPFlowMod(datapath=datapath, priority=priority,
                                     match=match, instructions=inst,
                                     idle_timeout=idle_timeout,
                                     hard_timeout=hard_timeout)
        datapath.send_msg(mod)

    def remove_flow(self, datapath, match, priority=None):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        mod = parser.OFPFlowMod(
            datapath=datapath,
            command=ofproto.OFPFC_DELETE,
            out_port=ofproto.OFPP_ANY,
            out_group=ofproto.OFPG_ANY,
            match=match,
            priority=priority if priority is not None else 0,
        )
        datapath.send_msg(mod)

    # ---------- baseline learning-switch forwarding ----------

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match["in_port"]

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]
        dst = eth.dst
        src = eth.src
        dpid = datapath.id

        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][src] = in_port

        if dst in self.mac_to_port[dpid]:
            out_port = self.mac_to_port[dpid][dst]
        else:
            out_port = ofproto.OFPP_FLOOD

        actions = [parser.OFPActionOutput(out_port)]

        if out_port != ofproto.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port, eth_dst=dst)
            self.add_flow(datapath, 1, match, actions,
                          buffer_id=msg.buffer_id, idle_timeout=20)
            if msg.buffer_id != ofproto.OFP_NO_BUFFER:
                return

        data = None
        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data

        out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                   in_port=in_port, actions=actions, data=data)
        datapath.send_msg(out)

    def register_ip_port(self, ip, port, dpid=None):
        self.ip_to_port[ip] = port

    # ---------- dynamic SDN reactions ----------

    def _primary_datapath(self):
        if not self.datapaths:
            return None
        return next(iter(self.datapaths.values()))

    def handle_critical(self, node_ip):
        datapath = self._primary_datapath()
        if datapath is None:
            self.logger.warning("No switch connected yet, cannot react")
            return

        if node_ip in self.critical_nodes:
            return
        self.critical_nodes.add(node_ip)

        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        self._ensure_meter(datapath, METER_ID, RATE_LIMIT_KBPS)

        # Rate-limit everyone except the critical node so its reports
        # get through with less contention on the shared link.
        for host_ip, _ in list(self.ip_to_port.items()):
            if host_ip == node_ip:
                continue
            match = parser.OFPMatch(eth_type=0x0800, ipv4_src=host_ip)
            inst = [parser.OFPInstructionMeter(METER_ID),
                    parser.OFPInstructionActions(
                        ofproto.OFPIT_APPLY_ACTIONS,
                        [parser.OFPActionOutput(ofproto.OFPP_NORMAL)])]
            mod = parser.OFPFlowMod(datapath=datapath, priority=10,
                                     match=match, instructions=inst)
            datapath.send_msg(mod)

        self.logger.info(
            "CRITICAL on %s: rate-limiting other hosts to %s kbps",
            node_ip, RATE_LIMIT_KBPS)

    def handle_recovered_critical(self, node_ip):
        datapath = self._primary_datapath()
        if datapath is None or node_ip not in self.critical_nodes:
            return
        self.critical_nodes.discard(node_ip)

        parser = datapath.ofproto_parser
        for host_ip in list(self.ip_to_port.keys()):
            if host_ip == node_ip:
                continue
            match = parser.OFPMatch(eth_type=0x0800, ipv4_src=host_ip)
            self.remove_flow(datapath, match, priority=10)

        self.logger.info("RECOVERED (critical) %s: rate-limit removed",
                          node_ip)

    def handle_failure(self, node_ip):
        datapath = self._primary_datapath()
        if datapath is None:
            return
        if node_ip in self.failed_nodes:
            return
        self.failed_nodes.add(node_ip)

        parser = datapath.ofproto_parser
        match = parser.OFPMatch(eth_type=0x0800, ipv4_src=node_ip)
        # highest priority, empty instruction list == drop
        self.add_flow(datapath, 100, match, [])

        self.logger.info("FAILURE on %s: isolating host (drop rule)",
                          node_ip)

    def handle_recovered_failure(self, node_ip):
        datapath = self._primary_datapath()
        if datapath is None or node_ip not in self.failed_nodes:
            return
        self.failed_nodes.discard(node_ip)

        parser = datapath.ofproto_parser
        match = parser.OFPMatch(eth_type=0x0800, ipv4_src=node_ip)
        self.remove_flow(datapath, match, priority=100)

        self.logger.info("RECOVERED (failure) %s: drop rule removed",
                          node_ip)

    def _ensure_meter(self, datapath, meter_id, kbps):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        bands = [parser.OFPMeterBandDrop(rate=kbps, burst_size=100)]
        req = parser.OFPMeterMod(datapath=datapath,
                                  command=ofproto.OFPMC_ADD,
                                  flags=ofproto.OFPMF_KBPS,
                                  meter_id=meter_id, bands=bands)
        datapath.send_msg(req)


class NotifyController(ControllerBase):
    """REST API used by monitor.py to push health conditions in."""

    def __init__(self, req, link, data, **config):
        super().__init__(req, link, data, **config)
        self.app = data[NOTIFY_INSTANCE_NAME]

    @route("notify", "/notify", methods=["POST"])
    def notify(self, req, **kwargs):
        try:
            body = json.loads(req.body.decode())
        except (ValueError, UnicodeDecodeError):
            return Response(status=400, body=b"invalid json")

        node_ip = body.get("ip")
        condition = body.get("condition")

        if not node_ip or not condition:
            return Response(status=400, body=b"missing ip or condition")

        if condition == "CRITICAL":
            self.app.handle_critical(node_ip)
        elif condition == "RECOVERED_CRITICAL":
            self.app.handle_recovered_critical(node_ip)
        elif condition == "FAILURE":
            self.app.handle_failure(node_ip)
        elif condition == "RECOVERED_FAILURE":
            self.app.handle_recovered_failure(node_ip)
        elif condition == "REGISTER":
            port = body.get("port")
            if port is not None:
                self.app.register_ip_port(node_ip, port)
        else:
            return Response(status=400, body=b"unknown condition")

        return Response(status=200, body=b"ok")
