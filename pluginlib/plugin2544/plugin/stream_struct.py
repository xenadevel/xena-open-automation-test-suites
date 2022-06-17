import asyncio
from typing import List, Optional, TYPE_CHECKING
from pydantic import BaseModel
from xoa_driver import utils, misc, enums
from ..model import TestConfiguration, HwModifier
from .common import gen_macaddress
from .data_model import (
    AddressCollection,
    RXTableData,
    StreamOffset,
)
from .learning import add_address_refresh_entry
from .statistics import (
    DelayCounter,
    DelayData,
    StreamCounter,
    StreamStatisticData,
)
from ..utils.field import MacAddress, IPv4Address, IPv6Address
from ..utils import constants as const, protocol_segments as ps

if TYPE_CHECKING:
    from .structure import PortStruct


class PRStatistic(BaseModel):
    rx_stream_counter: StreamCounter
    latency: DelayData
    jitter: DelayData
    fcs: int
    loss_frames: int


class PRStream:
    def __init__(self, tx_port: "PortStruct", rx_port: "PortStruct", tpld_id):
        self._tx_port = tx_port
        self._tpldid = tpld_id
        self._rx_port = rx_port
        self._rx = self._rx_port.port_statistic.rx.access_tpld(tpld_id)
        self._statistic: PRStatistic
        
    @property
    def rx_port(self) -> "PortStruct":
        return self._rx_port

    @property
    def statistic(self) -> PRStatistic:
        return self._statistic

    async def query(self):
        rx_frames, error, ji, latency, fcs = await utils.apply(
            self._rx.traffic.get(),
            self._rx.errors.get(),
            self._rx.jitter.get(),
            self._rx.latency.get(),
            self._rx_port.port_statistic.rx.extra.get(),
        )
        self._statistic = PRStatistic(
            rx_stream_counter=StreamCounter(
                frames=rx_frames.packet_count_since_cleared,
                bps=rx_frames.bit_count_last_sec,
                pps=rx_frames.packet_count_last_sec,
                bytes_count=rx_frames.byte_count_since_cleared,
            ),
            fcs=fcs.fcs_error_count,
            loss_frames=error.non_incre_seq_event_count,
            latency=DelayData(
                minimum=latency.min_val,
                average=latency.avg_val,
                maximum=latency.max_val,
            ),
            jitter=DelayData(
                counter_type=const.CounterType.JITTER,
                minimum=ji.min_val,
                average=ji.avg_val,
                maximum=ji.max_val,
            ),
        )

    def update_rx_port_statistic(self, burst_frames:int=0):
        self._rx_port.statistic.add_rx(self.statistic.rx_stream_counter)
        self._rx_port.statistic.add_latency(self.statistic.latency)
        self._rx_port.statistic.add_jitter(self.statistic.jitter)
        self._rx_port.statistic.add_extra(self.statistic.fcs)
        self._rx_port.statistic.add_burst_frames(burst_frames)


class StreamStruct:
    def __init__(
        self,
        tx_port: "PortStruct",
        rx_ports: List["PortStruct"],
        stream_id: int,
        tpldid: int,
        arp_mac: Optional[MacAddress] = None,
        stream_offset: Optional["StreamOffset"] = None,
    ):
        self._tx_port: "PortStruct" = tx_port
        self._rx_ports: List["PortStruct"] = rx_ports
        self._stream_id: int = stream_id
        self._tpldid: int = tpldid
        self._arp_mac: Optional[MacAddress] = arp_mac
        self._stream: misc.GenuineStream
        self._flow_creation_type: const.FlowCreationType
        self._addr_coll: AddressCollection
        self._packet_header: bytearray
        self._stream_offset = stream_offset
        self._tx_frames: StreamCounter
        self._rx_frames: StreamCounter
        self._loss_frames: int = 0
        self._packet_limit: int = 0
        self._pr_streams = [
            PRStream(self._tx_port, port, self._tpldid) for port in self._rx_ports
        ]
        self._best_result: StreamStatisticData

    def is_rx_port(self, peer_struct: "PortStruct"):
        return True if peer_struct in self._rx_ports else False

    @property
    def rx_port(self) -> "PortStruct":
        if self._flow_creation_type.is_stream_based:
            return self._rx_ports[0]
        else:
            return self._tx_port

    @property
    def latency(self) -> DelayCounter:
        la = DelayCounter()
        for pr_stream in self._pr_streams:
            la.update(pr_stream.statistic.latency)
        return la

    @property
    def jitter(self) -> DelayCounter:
        ji = DelayCounter(counter_type=const.CounterType.JITTER)
        for pr_stream in self._pr_streams:
            ji.update(pr_stream.statistic.jitter)
        return ji

    @property
    def hw_modifiers(self) -> List["HwModifier"]:
        if self._flow_creation_type.is_stream_based:
            return [
                modifier
                for header_segment in self._tx_port.port_conf.profile.header_segments
                for modifier in header_segment.hw_modifiers
            ]
        else:
            modifier_range = self._tx_port.properties.get_modifier_range(self._stream_id)
            return [
                HwModifier(
                    field_name="Dst MAC addr",
                    offset=4,
                    mask="0x00FF0000",
                    start_value=modifier_range[0],
                    stop_value=modifier_range[1],
                )
            ]

    @property
    def best_result(self) -> StreamStatisticData:
        return self._best_result

    def set_best_result(self):  # Only for stream based used
        self._best_result = StreamStatisticData(
            tx_counter=self.tx_frames,
            rx_counter=self.rx_frames,
            latency=self._pr_streams[0].statistic.latency,
            jitter=self._pr_streams[0].statistic.jitter,
            addr_coll=self._addr_coll,
            fcs=self._pr_streams[0].statistic.fcs,
            loss_frames=self._pr_streams[0].statistic.loss_frames,
        )

    def aggregate(self):
        self._best_result.calculate(self._tx_port, self.rx_port)

    async def configure(self, test_conf: "TestConfiguration") -> None:
        stream = await self._tx_port.create_stream()
        self._stream = stream
        self._flow_creation_type = test_conf.flow_creation_type
        self._addr_coll = await get_address_collection(
            self._tx_port,
            self.rx_port,
            test_conf.mac_base_address,
            self._stream_offset,
        )
        await utils.apply(
            self._stream.enable.set(enums.OnOffWithSuppress.ON),
            self._stream.packet.header.protocol.set(
                self._tx_port.port_conf.profile.segment_id_list
            ),
            self._stream.payload.content.set(
                test_conf.payload_type.to_xmp(), f"0x{test_conf.payload_pattern}"
            ),
            self._stream.tpld_id.set(test_payload_identifier=self._tpldid),
            self._stream.insert_packets_checksum.set(enums.OnOff.ON),
        )
        await self.set_packet_header()
        await self.setup_modifier()
        self.init_rx_tables(
            test_conf.arp_refresh_enabled, test_conf.use_gateway_mac_as_dmac
        )

    def init_rx_tables(self, arp_refresh_enabled: bool, use_gateway_mac_as_dmac: bool):
        if not arp_refresh_enabled or not self._tx_port.protocol_version.is_l3:
            return
        if self._stream_offset:
            if self._tx_port.protocol_version.is_ipv4:
                dst_addr = self._addr_coll.dst_ipv4_addr
                self.rx_port.properties.arp_trunks.add(
                    RXTableData(dst_addr, self._addr_coll.dmac)
                )
            else:
                dst_addr = self._addr_coll.dst_ipv6_addr
                self.rx_port.properties.ndp_trunks.add(
                    RXTableData(dst_addr, self._addr_coll.dmac)
                )
            add_address_refresh_entry(
                self.rx_port,
                dst_addr,
                self._addr_coll.dmac,
            )
        else:
            add_address_refresh_entry(self.rx_port, None, None)

        if use_gateway_mac_as_dmac:
            add_address_refresh_entry(
                self._tx_port,
                None,
                None,
            )

    @property
    def tx_frames(self) -> StreamCounter:
        return self._tx_frames

    @property
    def rx_frames(self) -> StreamCounter:
        return self._rx_frames

    async def query(self):
        tx_frames = await self._tx_port._port.statistics.tx.obtain_from_stream(
            self._stream_id
        ).get()
        await asyncio.gather(*[pr_stream.query() for pr_stream in self._pr_streams])
        self._tx_frames = StreamCounter(
            frames=tx_frames.packet_count_since_cleared,
            bps=tx_frames.bit_count_last_sec,
            pps=tx_frames.packet_count_last_sec,
        )
        self._rx_frames = StreamCounter()
        self._loss_frames = 0
        for pr_stream in self._pr_streams:
            self._rx_frames.update(pr_stream.statistic.rx_stream_counter)
            self._loss_frames += pr_stream.statistic.loss_frames
            pr_stream.update_rx_port_statistic(self._packet_limit)
        self.update_tx_port_statistic()

    def update_tx_port_statistic(self):
        self._tx_port.statistic.add_tx(self._tx_frames)
        self._tx_port.statistic.add_burst_frames(self._packet_limit)
        self._tx_port.statistic.add_loss(
            self._tx_frames.frames, self.rx_frames.frames, self._loss_frames
        )

    async def set_packet_header(self):
        packet_header_list = bytearray()
        # Insert all configured header segments in order
        segment_index = 0
        for segment in self._tx_port.port_conf.profile.header_segments:
            segment_type = segment.segment_type
            if (
                segment_type == const.SegmentType.TCP
                and self._tx_port.capabilities.can_tcp_checksum
            ):
                segment_type = const.SegmentType.TCPCHECK
            patched_value = ps.get_segment_value(segment, segment_index, self._addr_coll)
            real_value = ps.calculate_checksum(
                segment, ps.DEFAULT_SEGMENT_DIC, patched_value
            )

            packet_header_list += real_value
            segment_index += 1

        self._packet_header = packet_header_list
        await self._stream.packet.header.data.set(f"0x{bytes(self._packet_header).hex()}")

    async def setup_modifier(self) -> None:
        tokens = []
        modifiers = self._stream.packet.header.modifiers
        await modifiers.configure(len(self.hw_modifiers))
        for mid, hw_modifier in enumerate(self.hw_modifiers):
            modifier = modifiers.obtain(mid)
            tokens.append(
                modifier.specification.set(
                    position=hw_modifier.position,
                    mask=hw_modifier.mask,
                    action=hw_modifier.action.to_xmp(),
                    repetition=hw_modifier.repeat_count,
                )
            )
            tokens.append(
                modifier.range.set(
                    min_val=hw_modifier.start_value,
                    step=hw_modifier.step_value,
                    max_val=hw_modifier.stop_value,
                )
            )
        await utils.apply(*tokens)

    async def set_packet_size(
        self, packet_size_type: enums.LengthType, min_size: int, max_size: int
    ) -> None:
        await self._stream.packet.length.set(packet_size_type, min_size, max_size)

    async def set_l2bps_rate(self, rate: int):
        await self._stream.rate.l2bps.set(rate)

    async def set_frame_limit(self, frame_count: int) -> None:
        self._packet_limit = frame_count
        await self._stream.packet.limit.set(frame_count)


async def get_address_collection(
    port_struct: "PortStruct",
    peer_struct: "PortStruct",
    mac_base_address: str,
    stream_offset: Optional[StreamOffset] = None,
) -> "AddressCollection":
    if stream_offset:
        return AddressCollection(
            smac=gen_macaddress(mac_base_address, stream_offset.tx_offset),
            dmac=gen_macaddress(mac_base_address, stream_offset.rx_offset),
            src_ipv4_addr=IPv4Address(
                port_struct.port_conf.ipv4_properties.network[stream_offset.tx_offset]
            ),
            dst_ipv4_addr=IPv4Address(
                peer_struct.port_conf.ipv4_properties.network[stream_offset.rx_offset]
            ),
            src_ipv6_addr=IPv6Address(
                port_struct.port_conf.ipv6_properties.network[stream_offset.tx_offset]
            ),
            dst_ipv6_addr=IPv6Address(
                peer_struct.port_conf.ipv6_properties.network[stream_offset.rx_offset]
            ),
        )
    else:
        return AddressCollection(
            smac=await port_struct.get_mac_address(),
            dmac=await peer_struct.get_mac_address(),
            src_ipv4_addr=port_struct.port_conf.ipv4_properties.address,
            dst_ipv4_addr=peer_struct.port_conf.ipv4_properties.dst_addr,
            src_ipv6_addr=port_struct.port_conf.ipv6_properties.address,
            dst_ipv6_addr=peer_struct.port_conf.ipv6_properties.dst_addr,
        )
