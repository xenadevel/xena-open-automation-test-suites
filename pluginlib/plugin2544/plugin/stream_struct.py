import asyncio
from dataclasses import dataclass
from decimal import Decimal
from typing import List, Optional, TYPE_CHECKING

from pluginlib.plugin2544.model import TestConfiguration, HwModifier
from pluginlib.plugin2544.plugin.common import gen_macaddress
from pluginlib.plugin2544.plugin.data_model import (
    PortMax,
    RXTableData,
    StreamOffset,
)
from pluginlib.plugin2544.plugin.learning import (
    add_address_refresh_entry,
)
from pluginlib.plugin2544.plugin.statistics import (
    AvgMinMax,
    CounterType,
    DelayCounter,
    DelayData,
    StreamCounter,
)

from pluginlib.plugin2544.utils.field import MacAddress, IPv4Address, IPv6Address
from pluginlib.plugin2544.utils import constants as const, protocol_segments as ps
from xoa_driver import utils, ports as xoa_ports, misc, enums

if TYPE_CHECKING:
    from pluginlib.plugin2544.plugin.structure import PortStruct


class PRStream:
    def __init__(self, tx_port: "PortStruct", rx_port: "PortStruct", tpld_id):
        self._tx_port = tx_port
        self._tpldid = tpld_id
        self._rx_port = rx_port
        self._rx = self._rx_port.port.statistics.rx.access_tpld(tpld_id)
        self.max = PortMax()
        self.latency: DelayData
        self.jitter: DelayData

    async def query(self,packet_size: Decimal, duration: Decimal, is_final: bool = False):
        rx_frames, error, ji, latency, fcs = await utils.apply(
            self._rx.traffic.get(),
            self._rx.errors.get(),
            self._rx.jitter.get(),
            self._rx.latency.get(),
            self._rx_port.port.statistics.rx.extra.get(),
        )
        self.rx_frames = StreamCounter(
            frame_size=packet_size,
            duration=duration,
            is_final=is_final,
            frames=rx_frames.packet_count_since_cleared,
            bps=rx_frames.bit_count_last_sec,
            pps=rx_frames.packet_count_last_sec,
        )
        self._rx_port.statistic.add_rx(self.rx_frames)
        # la = DelayCounter(latency.min_val, latency.avg_val, latency.max_val)
        self.latency = DelayData(
            minimum=latency.min_val, total=latency.avg_val, maximun=latency.max_val
        )
        self._rx_port.statistic.add_latency(self.latency)
        self.jitter = DelayData(
            counter_type=CounterType.JITTER,
            minimum=ji.min_val,
            total=ji.avg_val,
            maximun=ji.max_val,
        )
        # ji = DelayCounter(ji.min_val, ji.avg_val, ji.max_val)
        self._rx_port.statistic.add_jitter(self.jitter)
        self._rx_port.statistic.add_extra(fcs.fcs_error_count)
        self._tx_port.statistic.add_loss(error.non_incre_seq_event_count)
        self.max.update_bps(rx_frames.bit_count_last_sec)
        self.max.update_pps(rx_frames.packet_count_last_sec)


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
        self.tx_port: "PortStruct" = tx_port
        self._rx_ports: List["PortStruct"] = rx_ports
        self.stream_id: int = stream_id
        self.tpldid: int = tpldid
        self.arp_mac: Optional[MacAddress] = arp_mac
        self.stream: misc.GenuineStream
        # self.modifiers: List[HwModifier] = []
        self.packet_header: bytearray
        self.stream_offset = stream_offset
        self.pr_streams = [
            PRStream(self.tx_port, port, self.tpldid) for port in self._rx_ports
        ]

    def is_rx_port(self, peer_struct: "PortStruct"):
        return True if peer_struct in self._rx_ports else False

    @property
    def rx_port(self) -> "PortStruct":
        if self.flow_creation_type.is_stream_based:
            return self._rx_ports[0]
        else:
            return self.tx_port

    @property
    def latency(self) -> AvgMinMax:
        latency_data = [
            pr_stream.latency
            for pr_stream in self.pr_streams
            if pr_stream.latency.is_valid
        ]
        total = sum([data.total for data in latency_data])
        min_val = min([data.minimum for data in latency_data])
        max_val = max([data.maximum for data in latency_data])
        avg_val = Decimal(str(total)) / Decimal(len(latency_data))
        return AvgMinMax(minimum=min_val, maximun=max_val, avg=avg_val)

    @property
    def jitter(self) -> AvgMinMax:
        jitter_data = [
            pr_stream.jitter
            for pr_stream in self.pr_streams
            if pr_stream.jitter.is_valid
        ]
        total = sum([data.total for data in jitter_data])
        min_val = min([data.minimum for data in jitter_data])
        max_val = max([data.maximum for data in jitter_data])
        avg_val = Decimal(str(total)) / Decimal(len(jitter_data))
        return AvgMinMax(minimum=min_val, maximun=max_val, avg=avg_val)

    @property
    def hw_modifiers(self) -> List["HwModifier"]:
        if self.flow_creation_type.is_stream_based:
            return [
                modifier
                for header_segment in self.tx_port.port_conf.profile.header_segments
                for modifier in header_segment.hw_modifiers
            ]
        else:
            modifier_range = self.tx_port.properties.get_modifier_range(self.stream_id)
            return [
                HwModifier(
                    field_name="Dst MAC addr",
                    offset=4,
                    mask="0x00FF0000",
                    start_value=modifier_range[0],
                    stop_value=modifier_range[1],
                )
            ]

    async def configure(self, test_conf: "TestConfiguration") -> None:
        stream = await self.tx_port.port.streams.create()
        self.stream = stream
        self.flow_creation_type = test_conf.flow_creation_type
        self.addr_coll = await get_address_collection(
            self.tx_port,
            self.rx_port,
            test_conf.mac_base_address,
            self.stream_offset,
        )
        await utils.apply(
            self.stream.enable.set(enums.OnOffWithSuppress.ON),
            self.stream.packet.header.protocol.set(
                self.tx_port.port_conf.profile.header_segment_id_list
            ),
            self.stream.payload.content.set(
                test_conf.payload_type.to_xmp(), f"0x{test_conf.payload_pattern}"
            ),
            self.stream.tpld_id.set(test_payload_identifier=self.tpldid),
            self.stream.insert_packets_checksum.set(enums.OnOff.ON),
        )
        await self.set_packet_header()
        await self.setup_modifier()
        self.init_rx_tables(
            test_conf.arp_refresh_enabled, test_conf.use_gateway_mac_as_dmac
        )

    def init_rx_tables(self, arp_refresh_enabled: bool, use_gateway_mac_as_dmac: bool):
        if not arp_refresh_enabled or not self.tx_port.protocol_version.is_l3:
            return
        if self.stream_offset:
            if self.tx_port.protocol_version.is_ipv4:
                dst_addr = self.addr_coll.dst_ipv4_addr
                self.rx_port.properties.arp_trunks.add(
                    RXTableData(dst_addr, self.addr_coll.dmac)
                )
            else:
                dst_addr = self.addr_coll.dst_ipv6_addr
                self.rx_port.properties.ndp_trunks.add(
                    RXTableData(dst_addr, self.addr_coll.dmac)
                )
            add_address_refresh_entry(
                self.rx_port,
                dst_addr,
                self.addr_coll.dmac,
            )
        else:
            add_address_refresh_entry(self.rx_port, None, None)

        if use_gateway_mac_as_dmac:
            add_address_refresh_entry(
                self.tx_port,
                None,
                None,
            )

    @property
    def tx_frames(self) -> StreamCounter:
        return self._tx_frames

    @property
    def rx_frames(self) -> StreamCounter:
        total_rx_frames = StreamCounter()
        for pr_stream in self.pr_streams:
            total_rx_frames.update(pr_stream.rx_frames)
        return total_rx_frames

    async def query(
        self, packet_size: Decimal, duration: Decimal, is_final: bool = False
    ):
        tx_frames = await self.tx_port.port.statistics.tx.obtain_from_stream(
            self.stream_id
        ).get()
        await asyncio.gather(*[pr_stream.query(packet_size, duration, is_final) for pr_stream in self.pr_streams])
        self._tx_frames = StreamCounter(
            frame_size=packet_size,
            duration=duration,
            is_final=is_final,
            interframe_gap=self.tx_port.port_conf.inter_frame_gap,
            frames=tx_frames.packet_count_since_cleared,
            bps=tx_frames.bit_count_last_sec,
            pps=tx_frames.packet_count_last_sec,
        )
        self.tx_port.statistic.add_tx(self._tx_frames)

    async def set_packet_header(self):
        packet_header_list = bytearray()
        # Insert all configured header segments in order
        segment_index = 0
        for segment in self.tx_port.port_conf.profile.header_segments:
            segment_type = segment.segment_type
            if (
                segment_type == const.SegmentType.TCP
                and self.tx_port.port.info.capabilities.can_tcp_checksum
            ):
                segment_type = const.SegmentType.TCPCHECK
            patched_value = ps.get_segment_value(segment, segment_index, self.addr_coll)
            real_value = ps.calculate_checksum(
                segment, ps.DEFAULT_SEGMENT_DIC, patched_value
            )

            packet_header_list += real_value
            segment_index += 1

        self.packet_header = packet_header_list
        await self.stream.packet.header.data.set(f"0x{bytes(self.packet_header).hex()}")

    async def setup_modifier(self) -> None:
        tokens = []
        modifiers = self.stream.packet.header.modifiers
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
        await self.stream.packet.length.set(packet_size_type, min_size, max_size)

    async def set_l2bps_rate(self, rate: int):
        await self.stream.rate.l2bps.set(rate)

    async def set_frame_limit(self, frame_count: int) -> None:
        await self.stream.packet.limit.set(frame_count)


@dataclass
class AddressCollection:
    smac: MacAddress
    dmac: MacAddress
    src_ipv4_addr: IPv4Address
    dst_ipv4_addr: IPv4Address
    src_ipv6_addr: IPv6Address
    dst_ipv6_addr: IPv6Address


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