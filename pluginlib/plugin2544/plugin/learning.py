import math
from typing import TYPE_CHECKING, Iterator, List, Optional, Tuple, Union
from pluginlib.plugin2544.plugin.data_model import ArpRefreshData
from pluginlib.plugin2544.utils import exceptions, constants as const
from xoa_driver import misc, enums, utils
from ..utils.field import NonNegativeDecimal

from ..utils.scheduler import schedule

from .setup_source_port_rates import setup_source_port_rates
from ..utils.field import IPv4Address, IPv6Address
from ..utils.packet import ARPPacket, MacAddress, NDPPacket
import asyncio
from ..utils.field import NonNegativeDecimal

from .setup_source_port_rates import setup_source_port_rates


if TYPE_CHECKING:
    from pluginlib.plugin2544.plugin.test_resource import ResourceManager
    from .structure import PortStruct


def get_dest_ip_modifier_addr_range(
    port_struct: "PortStruct",
) -> Optional[range]:
    header_segments = port_struct.port_conf.profile.header_segments
    flag = False
    addr_range = None
    for header_segment in header_segments:
        if header_segment.segment_type in (const.SegmentType.IP, const.SegmentType.IPV6):
            flag = True
        for modifier in header_segment.hw_modifiers:
            if modifier.field_name in ["Dest IP Addr", "Dest IPv6 Addr"]:
                addr_range = range(
                    modifier.start_value,
                    modifier.stop_value + 1,
                    modifier.step_value,
                )
        if flag:
            break
    return addr_range


def add_address_refresh_entry(
    port_struct: "PortStruct",
    source_ip: Union["IPv4Address", "IPv6Address", None],
    source_mac: Union["MacAddress", None],
) -> None:  # AddAddressRefreshEntry
    """ARP REFRESH STEP 1: generate address_refresh_data_set"""
    # is_ipv4 = port_struct.port_conf.profile.protocol_version.is_ipv4
    addr_range = get_dest_ip_modifier_addr_range(port_struct)
    port_struct.properties.address_refresh_data_set.add(
        ArpRefreshData(source_ip, source_mac, addr_range)
    )


def get_bytes_from_macaddress(dmac: "MacAddress") -> Iterator[str]:
    for i in range(0, len(dmac), 3):
        yield dmac[i : i + 2]


def get_link_local_uci_ipv6address(dmac: "MacAddress") -> str:
    b = get_bytes_from_macaddress(dmac)
    return f"FE80000000000000{int(next(b)) | 2 }{next(b)}{next(b)}FFFE{next(b)}{next(b)}{next(b)}"


def get_address_list(
    source_ip: Union["IPv4Address", "IPv6Address"],
    addr_range: Optional[range],
) -> List[Union["IPv4Address", "IPv6Address"]]:
    if not addr_range:
        return [source_ip]
    source_ip_list = []
    for i in addr_range:
        if isinstance(source_ip, IPv4Address):
            splitter = "."
            typing = IPv4Address
        else:
            splitter = ":"
            typing = IPv6Address

        addr = str(source_ip).split(splitter)
        addr[-1] = str(i)
        addr_str = splitter.join(addr)
        source_ip_list.append(typing(addr_str))

    return source_ip_list


async def get_address_learning_packet(
    port_struct: "PortStruct",
    arp_refresh_data: ArpRefreshData,
    use_gateway=False,
) -> List[str]:  # GetAddressLearningPacket
    """ARP REFRESH STEP 2: generate learning packet according to address_refresh_data_set"""
    dmac = MacAddress("FF:FF:FF:FF:FF:FF")
    gateway = port_struct.port_conf.ip_properties.gateway
    sender_ip = port_struct.port_conf.ip_properties.address
    if use_gateway and not gateway.is_empty:
        gwmac = port_struct.port_conf.ip_gateway_mac_address
        if not gwmac.is_empty:
            dmac = gwmac
    smac = (
        await port_struct.get_mac_address()
        if not arp_refresh_data.source_mac or arp_refresh_data.source_mac.is_empty
        else arp_refresh_data.source_mac
    )
    source_ip = (
        sender_ip
        if not arp_refresh_data.source_ip or arp_refresh_data.source_ip.is_empty
        else arp_refresh_data.source_ip
    )
    source_ip_list = get_address_list(source_ip, arp_refresh_data.addr_range)
    packet_list = []
    for source_ip in source_ip_list:
        if port_struct.protocol_version.is_ipv4:
            destination_ip = sender_ip if gateway.is_empty else gateway
            packet = ARPPacket(
                smac=smac,
                source_ip=IPv4Address(source_ip),
                destination_ip=IPv4Address(destination_ip),
                dmac=dmac,
            ).make_arp_packet()

        else:
            destination_ip = get_link_local_uci_ipv6address(dmac)
            packet = NDPPacket(
                smac=smac,
                source_ip=IPv6Address(source_ip),
                destination_ip=IPv6Address(destination_ip),
                dmac=dmac,
            ).make_ndp_packet()
        packet_list.append(packet)
    return packet_list


async def setup_address_refresh(
    resources: "ResourceManager",
) -> List[Tuple["misc.Token", bool]]:  # SetupAddressRefresh
    address_refresh_tokens: List[Tuple["misc.Token", bool]] = []
    for port_struct in resources.port_structs:
        arp_data_set = port_struct.properties.address_refresh_data_set
        for arp_data in arp_data_set:
            packet_list = await get_address_learning_packet(
                port_struct,
                arp_data,
                resources.test_conf.use_gateway_mac_as_dmac,
            )
            is_rx_only = (
                port_struct.port_conf.is_rx_port
                and not port_struct.port_conf.is_tx_port
            )
            for packet in packet_list:
                address_refresh_tokens.append(
                    (port_struct.port.tx_single_pkt.send.set(packet), is_rx_only)
                )
    return address_refresh_tokens


async def setup_address_arp_refresh(
    resources: "ResourceManager",
) -> "AddressRefreshHandler":  # SetupAddressArpRefresh
    # if test_conf.multi_stream_config.enable_multi_stream:
    #     await setup_multi_stream_address_arp_refresh(control_ports, stream_lists)
    # else:
    #     setup_normal_address_arp_refresh(control_ports)
    # gateway_arp_refresh(control_ports, test_conf)
    address_refresh_tokens = await setup_address_refresh(resources)
    return AddressRefreshHandler(
        address_refresh_tokens, resources.test_conf.arp_refresh_period_second
    )


class AddressRefreshHandler:
    """set packet interval and return batch"""

    def __init__(
        self,
        address_refresh_tokens: List[Tuple["misc.Token", bool]],
        refresh_period: "NonNegativeDecimal",
    ) -> None:
        self.index = 0
        self.refresh_burst_size = 1
        self.tokens: List["misc.Token"] = []
        self.address_refresh_tokens: List[
            Tuple["misc.Token", bool]
        ] = address_refresh_tokens
        self.interval = 0.0  # unit: second
        self.refresh_period = refresh_period
        self.state = const.TestState.L3_LEARNING

    def get_batch(self) -> List["misc.Token"]:
        packet_list = []
        if self.index >= len(self.tokens):
            self.index = 0
        for i in range(self.refresh_burst_size):
            if self.index < len(self.tokens):
                packet_list.append(self.tokens[self.index])
                self.index += 1
        return packet_list

    def _calc_refresh_time_interval(
        self, refresh_tokens: List["misc.Token"]
    ) -> None:  # CalcRefreshTimerInternal
        total_refresh_count = len(refresh_tokens)
        if total_refresh_count > 0:
            self.refresh_burst_size = 1
            interval = math.floor(self.refresh_period / total_refresh_count)
            if interval < const.MIN_REFRESH_TIMER_INTERNAL:
                self.refresh_burst_size = math.ceil(
                    const.MIN_REFRESH_TIMER_INTERNAL / interval
                )
                interval = const.MIN_REFRESH_TIMER_INTERNAL
            self.interval = interval / 1000.0  # ms -> second

    def set_current_state(self, state: "const.TestState") -> "AddressRefreshHandler":
        self.state = state
        if self.state == const.TestState.L3_LEARNING:
            self.tokens = [
                refresh_token[0] for refresh_token in self.address_refresh_tokens
            ]
        else:
            self.tokens = [
                refresh_token[0]
                for refresh_token in self.address_refresh_tokens
                if refresh_token[1]
            ]
        self._calc_refresh_time_interval(self.tokens)
        return self


async def generate_l3_learning_packets(
    count: int,
    resources: "ResourceManager",
    address_refresh_handler: "AddressRefreshHandler",
) -> bool:
    tokens = address_refresh_handler.get_batch()
    await utils.apply(*tokens)

    return not resources.test_running()


async def send_l3_learning_packets(
    resources: "ResourceManager",
    address_refresh_handler: "AddressRefreshHandler",
) -> None:
    await schedule(
        address_refresh_handler.interval,
        "s",
        generate_l3_learning_packets,
        resources,
        address_refresh_handler,
    )


async def schedule_arp_refresh(
    resources: "ResourceManager",
    address_refresh_handler: Optional["AddressRefreshHandler"],
    state: const.TestState = const.TestState.RUNNING_TEST,
):
    # arp refresh jobs
    if address_refresh_handler:
        address_refresh_handler.set_current_state(state)
        if address_refresh_handler.tokens:
            await send_l3_learning_packets(resources, address_refresh_handler)


async def add_L3_learning_preamble_steps(
    resources: "ResourceManager",
    current_packet_size: NonNegativeDecimal,
    address_refresh_handler:Optional["AddressRefreshHandler"] = None,
) -> None:  # AddL3LearningPreambleSteps
    if not address_refresh_handler:
        return
    address_refresh_handler.set_current_state(const.TestState.L3_LEARNING)
    resources.set_rate(resources.test_conf.learning_rate_pct)
    await setup_source_port_rates(resources, current_packet_size)
    await resources.set_tx_time_limit(resources.test_conf.learning_duration_second * 1000)

    await resources.start_traffic()
    await asyncio.gather(*address_refresh_handler.tokens)
    await schedule_arp_refresh(
        resources, address_refresh_handler, const.TestState.L3_LEARNING
    )
    while resources.test_running():
        await resources.query_traffic_status()
        await asyncio.sleep(1)
    await resources.set_tx_time_limit(0)


async def add_flow_based_learning_preamble_steps(
    resources: "ResourceManager",
    current_packet_size: NonNegativeDecimal,
) -> None:  # AddFlowBasedLearningPreambleSteps
    if not resources.test_conf.use_flow_based_learning_preamble:
        return
    resources.set_rate(resources.test_conf.learning_rate_pct)
    await setup_source_port_rates(resources, current_packet_size)
    await resources.set_frame_limit(resources.test_conf.flow_based_learning_frame_count)
    await resources.start_traffic()
    while resources.test_running():
        await resources.query_traffic_status()
        await asyncio.sleep(0.1)
    await asyncio.sleep(resources.test_conf.delay_after_flow_based_learning_ms / 1000)
    await resources.set_frame_limit(0)  # clear packet limit

async def mac_learning(port_struct: "PortStruct", mac_learning_frame_count: int) -> None:
    if not port_struct.port_conf.is_rx_port:
        return
    dest_mac = "FFFFFFFFFFFF"
    four_f = "FFFF"
    paddings = "00" * 118
    mac_address = await port_struct.get_mac_address()
    own_mac = mac_address.to_hexstring()
    hex_data = f"{dest_mac}{own_mac}{four_f}{paddings}"
    packet = f"0x{hex_data}"
    max_cap = port_struct.port.info.capabilities.max_xmit_one_packet_length
    cur_length = len(hex_data) // 2
    if cur_length > max_cap:
        raise exceptions.PacketLengthExceed(cur_length, max_cap)
    for _ in range(mac_learning_frame_count):
        await port_struct.send_packet(packet)# P_XMITONE
        await asyncio.sleep(1)

async def add_mac_learning_steps(
    resources: "ResourceManager",
    require_mode: "const.MACLearningMode",
) -> None:
    if require_mode != resources.test_conf.mac_learning_mode:
        return
    for port_struct in resources.port_structs:
        await mac_learning(port_struct, resources.test_conf.mac_learning_frame_count)