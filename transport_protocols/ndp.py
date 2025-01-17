#!/usr/bin/env python3

from scapy.all import *
from scapy.all import IP, Ether
from nanoPU_sim import * # Note the cyclic dependency here!
from headers import *
from sim_utils import *
import operator

NDP_PROTO = 0x99

class NDP(Packet):
    name = "NDP"
    fields_desc = [
        FlagsField("flags", 0, 8, ["DATA", "ACK", "NACK", "PULL",
                                   "CHOP", "F1", "F2", "F3"]),
        ShortField("src_context", 0),
        ShortField("dst_context", 0),
        ShortField("tx_msg_id", 0),
        ShortField("msg_len", 0),
        ShortField("pkt_offset", 0), # or should this be byte offset? Or should the header include both?
        ShortField("pull_offset", 0),
        XBitField("_pad17bytes",0,17*8)
    ]
    def mysummary(self):
        return self.sprintf("flags=%flags%, msg_len=%msg_len%, pkt_offset=%pkt_offset%, pull_offset=%pull_offset%")

bind_layers(IP, NDP, proto=NDP_PROTO)
#####
# Programmable Elements
#####

class IngressPipe(object):
    """P4 programmable ingress pipeline"""
    def __init__(self, net_queue, assemble_queue):
        self.env = Simulator.env
        self.logger = Logger()
        self.net_queue = net_queue
        self.assemble_queue = assemble_queue

        # Programmer-defined state to track credit for each message {rx_msg_id => credit}
        self.credit = {} #Credit is the Pull Offset in NDP

        self.env.process(self.start())

    @staticmethod
    def init_params():
        pass

    def log(self, msg):
        self.logger.log("IngressPipe: {}".format(msg))

    ########
    # Methods to wire up events/externs
    ########

    def init_getRxMsgInfo(self, getRxMsgInfo):
        self.getRxMsgInfo = getRxMsgInfo

    def init_deliveredEvent(self, deliveredEvent):
        self.deliveredEvent = deliveredEvent

    def init_creditToBtxEvent(self, creditToBtxEvent):
        self.creditToBtxEvent = creditToBtxEvent

    def init_ctrlPktEvent(self, ctrlPktEvent):
        self.ctrlPktEvent = ctrlPktEvent

    def start(self):
        """Receive and process packets from the network
        """
        while not Simulator.complete:
            # wait for a pkt from the network
            pkt = yield self.net_queue.get()
            self.log('Received pkt: src={} dst={} src_context={} dst_context={} pkt_offset={} pull_offset= {} flags={}'.format(pkt[IP].src,
                                                                                                                               pkt[IP].dst,
                                                                                                                               pkt[NDP].src_context,
                                                                                                                               pkt[NDP].dst_context,
                                                                                                                               pkt[NDP].pkt_offset,
                                                                                                                               pkt[NDP].pull_offset,
                                                                                                                               pkt[NDP].flags))

            # defaults
            tx_msg_id = pkt[NDP].tx_msg_id
            pkt_offset = pkt[NDP].pkt_offset
            msg_len = pkt[NDP].msg_len

            if pkt[NDP].flags.DATA:
                self.log('Processing data pkt')
                # defaults for generating control pkts
                genACK = False
                genNACK = False
                genPULL = False
                dst_ip = pkt[IP].src
                dst_context = pkt[NDP].src_context
                src_context = pkt[NDP].dst_context
                rx_msg_id, ack_no, isNewMsg, isNewPkt = self.getRxMsgInfo(pkt[IP].src,
                                                                          pkt[NDP].src_context,
                                                                          pkt[NDP].tx_msg_id,
                                                                          pkt[NDP].msg_len,
                                                                          pkt[NDP].pkt_offset)
                # NOTE: ack_no is the current acknowledgement number before
                #       processing this incoming data packet because this
                #       packet has not updated the received_bitmap in the
                #       assembly buffer yet.
                pull_offset_diff = 0
                if pkt[NDP].flags.CHOP:
                    self.log('Processing chopped data pkt')
                    # send NACK and PULL
                    genNACK = True
                    genPULL = True

                else:
                    # process DATA pkt
                    genACK = True
                    # TODO: No need to generate new PULL pkt if this was the
                    #       last packet of the msg
                    #       (ie, if ack_no > compute_num_pkts(msg_len))
                    # if( ack_no + Simulator.rtt_pkts <= pkt[NDP].msg_len):
                    genPULL = True

                    data = (ReassembleMeta(rx_msg_id,
                                           pkt[IP].src,
                                           pkt[NDP].src_context,
                                           pkt[NDP].tx_msg_id,
                                           pkt[NDP].msg_len,
                                           pkt[NDP].pkt_offset),
                            pkt[NDP].payload)
                    self.assemble_queue.put(data)
                    pull_offset_diff = 1

                # compute pull_offset with a PRAW extern
                pull_offset = 0
                if isNewMsg:
                    self.credit[rx_msg_id] = Simulator.rtt_pkts + pull_offset_diff
                    pull_offset = self.credit[rx_msg_id]
                else:
                    self.credit[rx_msg_id] += pull_offset_diff
                    pull_offset = self.credit[rx_msg_id]

                # fire event to generate control pkt(s)
                # TODO: Instead of providing some arguments to the packet
                #       generator, we should provide the exact transport layer
                #       header because we want the fixed function packet generator
                #       to be able to generate packets for any transport protocol
                #       that programmer deploys.
                self.ctrlPktEvent(genACK, genNACK, genPULL, dst_ip,
                                  dst_context, src_context, tx_msg_id,
                                  msg_len, pkt_offset, pull_offset)
            else:
                self.log('Processing {} for tx_msg_id: {}, pkt {}'.format(pkt[NDP].flags,
                                                                          tx_msg_id,
                                                                          pkt[NDP].pkt_offset if pkt[NDP].flags!="PULL" else pkt[NDP].pull_offset))
                # control pkt for msg being transmitted
                if pkt[NDP].flags.ACK:
                    # fire event to mark pkt as delivered
                    isInterval = False
                    self.deliveredEvent(tx_msg_id, pkt_offset, isInterval, msg_len)
                if pkt[NDP].flags.PULL or pkt[NDP].flags.NACK:
                    # mark pkt for rtx for NACK
                    rtx_pkt = pkt_offset if pkt[NDP].flags.NACK else None
                    # update credit for PULL
                    credit = pkt[NDP].pull_offset if pkt[NDP].flags.PULL else None
                    self.creditToBtxEvent(tx_msg_id, rtx_pkt = rtx_pkt, new_credit = credit,
                                          opCode = 'write', compVal = credit,
                                          relOp = operator.gt)

class EgressPipe(object):
    """P4 programmable egress pipeline"""
    def __init__(self, net_queue, arbiter_queue):
        self.env = Simulator.env
        self.logger = Logger()
        self.net_queue = net_queue
        self.arbiter_queue = arbiter_queue
        self.env.process(self.start())

    @staticmethod
    def init_params():
        pass

    def log(self, msg):
        self.logger.log('EgressPipe: {}'.format(msg))

    def start(self):
        """Receive and process packets
        """
        while not Simulator.complete:
            # wait for a pkt from the arbiter
            (meta, pkt) = yield self.arbiter_queue.get()
            eth = Ether(dst=SWITCH_MAC, src=NIC_MAC)
            ip = IP(dst=meta.dst_ip, src=NIC_IP_TX)
            if meta.is_data:
                self.log('Processing data pkt')
                # add Ethernet/IP/NDP headers
                pkt = eth/ip/NDP(flags="DATA",
                                 src_context=meta.src_context,
                                 dst_context=meta.dst_context,
                                 tx_msg_id=meta.tx_msg_id,
                                 msg_len=meta.msg_len,
                                 pkt_offset=meta.pkt_offset)/pkt
            else:
                self.log('Processing control pkt: {}'.format(pkt[NDP].flags))
                # add Ethernet/IP headers to control pkts
                pkt = eth/ip/pkt

            packetization_delay = len(pkt)*8/Simulator.tx_link_rate
            yield self.env.timeout(packetization_delay)
            # send pkt into network
            self.net_queue.put(pkt)
            self.log('Transmitted pkt: src={} dst={} src_context={} dst_context={} pkt_offset={} pull_offset= {} flags={}'.format(pkt[IP].src,
                                                                                                                               pkt[IP].dst,
                                                                                                                               pkt[NDP].src_context,
                                                                                                                               pkt[NDP].dst_context,
                                                                                                                               pkt[NDP].pkt_offset,
                                                                                                                               pkt[NDP].pull_offset,
                                                                                                                               pkt[NDP].flags))


class PktGen(object):
    """Generate control packets"""
    def __init__(self, arbiter_queue):
        self.env = Simulator.env
        self.logger = Logger()
        self.arbiter_queue = arbiter_queue
        self.pacer_queue = simpy.Store(self.env)
        self.pacer_lastTxTime = - (Simulator.max_pkt_len+len(Ether()/IP()/NDP()))*8/Simulator.rx_link_rate
        self.env.process(self.start_pacer())

    @staticmethod
    def init_params():
        pass

    def log(self, msg):
        self.logger.log('PktGen: {}'.format(msg))

    def ctrlPktEvent(self, genACK, genNACK, genPULL, dst_ip, dst_context,
                     src_context, tx_msg_id, msg_len, pkt_offset, pull_offset):
        self.log('Processing ctrlPktEvent, genACK: {}, genNACK: {}, genPULL: {}'.format(genACK, genNACK, genPULL))
        # generate control pkt
        meta = EgressMeta(is_data=False, dst_ip=dst_ip)
        if genPULL:
            inter_packet_time = (Simulator.max_pkt_len+len(Ether()/IP()/NDP()))*8/Simulator.rx_link_rate # ns
            txTime = self.pacer_lastTxTime + inter_packet_time
            now = self.env.now
            if( now < txTime ):
                delay = txTime - now
                self.pacer_lastTxTime = txTime
            else:
                delay = 0
                self.pacer_lastTxTime = now

            ndp = NDP(flags="PULL",
                      src_context=src_context,
                      dst_context=dst_context,
                      tx_msg_id=tx_msg_id,
                      msg_len=msg_len,
                      pkt_offset = pkt_offset,
                      pull_offset=pull_offset)

            if genACK and delay == 0:
                # We can combine PULL and ACKs
                ndp.flags |= "ACK"
                genACK = False # Don't generate ACK again for this event

            if genNACK and delay == 0:
                # We can combine PULL and NACKs
                ndp.flags |= "NACK"
                genNACK = False # Don't generate NACK again for this event

            self.pacer_queue.put((meta, ndp, delay))

        if genACK:
            ndp_ack = NDP(flags="ACK",
                      src_context=src_context,
                      dst_context=dst_context,
                      tx_msg_id=tx_msg_id,
                      msg_len=msg_len,
                      pkt_offset=pkt_offset)
            self.arbiter_queue.put((meta, ndp_ack))
        if genNACK:
            ndp_nack = NDP(flags="NACK",
                      src_context=src_context,
                      dst_context=dst_context,
                      tx_msg_id=tx_msg_id,
                      msg_len=msg_len,
                      pkt_offset=pkt_offset)
            self.arbiter_queue.put((meta, ndp_nack))

    def start_pacer(self):
        """Start pacing generated PULL pkts
        """
        while not Simulator.complete:
            meta, pkt, delay = yield self.pacer_queue.get()
            data = (meta, pkt)

            yield self.env.timeout(delay)

            self.log('Pacer is releasing a PULL pkt')
            self.arbiter_queue.put(data)

class NetworkPkt(object):
    """A small wrapper class around scapy pkts to add priority"""
    def __init__(self, pkt, priority):
        self.pkt = pkt
        self.priority = priority

    def __lt__(self, other):
        """Highest priority element is the one with the smallest priority value"""
        return self.priority < other.priority

class Network(object):
    """The network delays each pkt. It may also drop or trim data pkts.
    """
    def __init__(self, rx_queue, tx_queue):
        self.env = Simulator.env
        self.logger = Logger()
        # rxQueue is used to receive pkts (EgressPipe output)
        self.rx_queue = rx_queue
        # txQueue is where to put outgoing pkts (IngressPipe input)
        self.tx_queue = tx_queue
        # TOR queue
        self.tor_queue = simpy.PriorityStore(self.env)

        # Count the number of DATA packets on switch
        self.data_pkt_counter = 0

        self.env.process(self.start_rx())
        self.env.process(self.start_tx())

    @staticmethod
    def init_params():
        Network.data_pkt_delay_dist = DistGenerator('data_pkt_delay')
        Network.ctrl_pkt_delay_dist = DistGenerator('ctrl_pkt_delay')
        Network.data_pkt_trim_prob = Simulator.config['data_pkt_drop_prob'].next()

    def log(self, msg):
        self.logger.log('Network: {}'.format(msg))

    def forward_data(self, pkt):
        delay = Network.data_pkt_delay_dist.next()
        self.log('Forwarding data pkt with delay {}'.format(delay))
        yield self.env.timeout(delay)
        self.tor_queue.put(NetworkPkt(pkt, priority=1))

    def forward_ctrl(self, pkt):
        delay = Network.ctrl_pkt_delay_dist.next()
        self.log('Forwarding control pkt ({}) with delay {}'.format(pkt[NDP].flags, delay))
        yield self.env.timeout(delay)
        self.tor_queue.put(NetworkPkt(pkt, priority=0))

    def start_rx(self):
        """Start receiving messages"""
        while not Simulator.complete:
            # Wait to receive a pkt
            pkt = yield self.rx_queue.get()
            self.log('Received pkt: src={} dst={} src_context={} dst_context={} pkt_offset={} pull_offset={} flags={}'.format(pkt[IP].src,
                                                                                                                               pkt[IP].dst,
                                                                                                                               pkt[NDP].src_context,
                                                                                                                               pkt[NDP].dst_context,
                                                                                                                               pkt[NDP].pkt_offset,
                                                                                                                               pkt[NDP].pull_offset,
                                                                                                                               pkt[NDP].flags))
            Simulator.network_pkts.append(pkt)
            if pkt[NDP].flags.DATA:
                self.data_pkt_counter += 1
                # if random.random() < Network.data_pkt_trim_prob:
                # Istead of random drops, drop every (1 / Network.data_pkt_trim_prob)-th packet
                if Network.data_pkt_trim_prob > 0 and self.data_pkt_counter % (1 / Network.data_pkt_trim_prob) == 0:
                    self.log('Trimming data pkt')
                    # trim pkt
                    pkt[NDP].flags.CHOP = True
                    if len(pkt) > 64:
                        pkt = Ether(str(pkt)[0:64])
                    self.env.process(self.forward_ctrl(pkt))
                else:
                    self.env.process(self.forward_data(pkt))
            else:
                self.env.process(self.forward_ctrl(pkt))

    def start_tx(self):
        """Start transmitting pkts from the TOR queue to the TX queue"""
        while not Simulator.complete:
            net_pkt = yield self.tor_queue.get()
            pkt = net_pkt.pkt
            self.log('Transmitting pkt: src={} dst={} src_context={} dst_context={} pkt_offset={} pull_offset={} flags={}'.format(pkt[IP].src,
                                                                                                                               pkt[IP].dst,
                                                                                                                               pkt[NDP].src_context,
                                                                                                                               pkt[NDP].dst_context,
                                                                                                                               pkt[NDP].pkt_offset,
                                                                                                                               pkt[NDP].pull_offset,
                                                                                                                               pkt[NDP].flags))
            # delay based on pkt length and link rate
            delay = len(pkt)*8/Simulator.rx_link_rate
            yield self.env.timeout(delay)

            self.tx_queue.put(pkt)
