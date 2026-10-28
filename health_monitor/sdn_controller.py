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
  osken-manager --ofp-tcp-listen-port 6633 sdn_controller.py --verbose
"""

import json

from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER
from os_ken.controller.handler import set_ev_cls
from os_ken.ofproto import ofproto_v1_3
from os_ken.lib.packet import packet, ethernet
from os_ken.lib import hub
from webob import Request, Response

NOTIFY_INSTANCE_NAME = "health_monitor_api"

RATE_LIMIT_KBPS = 500  # cap applied to non-critical hosts when one node is CRITICAL
METER_ID = 1


class HealthMonitorController(app_manager.OSKenApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mac_to_port = {}
        self.datapaths = {}
        self.critical_nodes = set()
        self.failed_nodes = set()
        self.ip_to_port = {}

        api = NotifyController(None, None, {NOTIFY_INSTANCE_NAME: self})

        def notify_wsgi(environ, start_response):
            req = Request(environ)
            if req.path_info != "/notify":
                response = Response(status=404, body=b"not found")
            elif req.remote_addr not in ("127.0.0.1", "10.0.0.5"):
                response = Response(status=403, body=b"monitor access only")
            elif req.method != "POST":
                response = Response(status=405, body=b"POST required")
            else:
                response = api.notify(req)
            return response(environ, start_response)

        self.http_server = hub.WSGIServer(("0.0.0.0", 8080), notify_wsgi)
        self.threads.append(hub.spawn(self.http_server.serve_forever))
        self.logger.info("Health API listening on http://127.0.0.1:8080/notify")

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
            command=(ofproto.OFPFC_DELETE_STRICT if priority is not None else ofproto.OFPFC_DELETE),
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
        dp = self._primary_datapath()
        if dp is not None:
            match = dp.ofproto_parser.OFPMatch(
                eth_type=0x0800, ipv4_src=ip, ipv4_dst="10.0.0.5",
                ip_proto=17, udp_dst=5000)
            self.add_flow(dp, 110, match,
                          [dp.ofproto_parser.OFPActionOutput(dp.ofproto.OFPP_NORMAL)])
            self._refresh_rates()
        self.logger.info("REGISTER: %s", ip)

    # ---------- dynamic SDN reactions ----------

    def _primary_datapath(self):
        if not self.datapaths:
            return None
        return next(iter(self.datapaths.values()))

    def _refresh_rates(self):
        dp = self._primary_datapath()
        if dp is None:
            return
        parser, ofp = dp.ofproto_parser, dp.ofproto
        if self.critical_nodes:
            self._ensure_meter(dp, METER_ID, RATE_LIMIT_KBPS)
        for ip in self.ip_to_port:
            match = parser.OFPMatch(eth_type=0x0800, ipv4_src=ip)
            self.remove_flow(dp, match, priority=10)
            if self.critical_nodes and ip not in self.critical_nodes:
                instructions = [parser.OFPInstructionMeter(METER_ID),
                    parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS,
                        [parser.OFPActionOutput(ofp.OFPP_NORMAL)])]
                dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=10,
                    match=match, instructions=instructions))

    def handle_critical(self, node_ip):
        self.critical_nodes.add(node_ip)
        self._refresh_rates()
        self.logger.info("CRITICAL: %s; critical hosts: %s",
                         node_ip, sorted(self.critical_nodes))

    def handle_recovered_critical(self, node_ip):
        self.critical_nodes.discard(node_ip)
        self._refresh_rates()
        self.logger.info("RECOVERED (critical): %s", node_ip)

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
        if getattr(self, "_meter_datapath", None) is datapath:
            return
        self._meter_datapath = datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        bands = [parser.OFPMeterBandDrop(rate=kbps, burst_size=100)]
        req = parser.OFPMeterMod(datapath=datapath,
                                  command=ofproto.OFPMC_ADD,
                                  flags=ofproto.OFPMF_KBPS,
                                  meter_id=meter_id, bands=bands)
        datapath.send_msg(req)


class NotifyController:
    """REST API used by monitor.py to push health conditions in."""

    def __init__(self, req, link, data, **config):
        self.app = data[NOTIFY_INSTANCE_NAME]

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
            self.app.register_ip_port(node_ip, body.get("port"))
        else:
            return Response(status=400, body=b"unknown condition")

        return Response(status=200, body=b"ok")
