use std::net::Ipv4Addr;

/// Maximum number of bits used to represent a PSN.
pub(crate) const MAX_PSN_SIZE_BITS: usize = 24;
/// Maximum size of the PSN window. This represents the maximum number outstanding PSNs.
pub(crate) const MAX_PSN_WINDOW: usize = 1 << (MAX_PSN_SIZE_BITS - 1);
/// Bit mask used to extract the PSN value from a 32-bit number.
pub(crate) const PSN_MASK: u32 = (1 << MAX_PSN_SIZE_BITS) - 1;

/// Maximum number of bits used to represent a MSN.
pub(crate) const MAX_MSN_SIZE_BITS: usize = 16;
/// Maximum size of the PSN window. This represents the maximum number outstanding PSNs.
pub(crate) const MAX_MSN_WINDOW: usize = 1 << (MAX_MSN_SIZE_BITS - 1);

pub(crate) const MAX_QP_CNT: usize = 1024;
pub(crate) const QPN_KEY_PART_WIDTH: u32 = 8;
pub(crate) const QPN_IDX_PART_WIDTH: u32 = 32 - QPN_KEY_PART_WIDTH;

pub(crate) const MAX_CQ_CNT: usize = 1024;

/// Maximum number of outstanding send work requests (WRs) that can be posted to a Queue Pair (QP).
pub(crate) const MAX_SEND_WR: usize = 0x8000;

pub(crate) const TEST_CARD_IP_ADDRESS: u32 = 0x1122_330A;

// TODO: implement ARP MAC resolution
pub(crate) const CARD_MAC_ADDRESS: u64 = 0xAABB_CCDD_EE0A;
pub(crate) const CARD_MAC_ADDRESS_OCTETS: [u8; 6] = [0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0x0A];

pub(crate) const MAX_PD_CNT: usize = 256;

/// (Max) size of a single WR chunk
// pub(crate) const WR_CHUNK_SIZE: u32 = 0x4000;
pub(crate) const WR_CHUNK_SIZE: u32 = 0x10000;

/// Ack timeout config
pub(crate) const DEFAULT_INIT_RETRY_COUNT: usize = 5;
pub(crate) const DEFAULT_TIMEOUT_CHECK_DURATION: u8 = 8;
// 这对吗？太大了吧
pub(crate) const DEFAULT_LOCAL_ACK_TIMEOUT: u8 = 50;

pub(crate) const POST_RECV_TCP_LOOP_BACK_SERVER_ADDRESS: Ipv4Addr = Ipv4Addr::new(127, 0, 0, 1);
pub(crate) const POST_RECV_TCP_LOOP_BACK_CLIENT_ADDRESS: Ipv4Addr = Ipv4Addr::new(127, 0, 0, 2);

pub(crate) const BLUE_RDMA_SYSFS_PATH: &str = "/sys/class/infiniband/bluerdma0";
pub(crate) const BLUE_RDMA_NETDEV_INTERFACE_NAME: &str = "blue0";

pub(crate) const U_DMA_BUF_CLASS_PATH: &str = "/sys/class/u-dma-buf/udmabuf0";

pub(crate) const PAGE_SIZE_2MB: usize = 1 << 21;

pub(crate) const MAX_MR_CNT: usize = 8192;
pub(crate) const LR_KEY_KEY_PART_WIDTH: u32 = 8;
pub(crate) const LR_KEY_IDX_PART_WIDTH: u32 = 32 - LR_KEY_KEY_PART_WIDTH;
/// Maximum number of entries in the secodn stage table
pub(crate) const PGT_LEN: usize = 0x20000;

pub(crate) const VENDER_ID: u16 = 0x1172;
pub(crate) const DEVICE_ID: u16 = 0x0000;
pub(crate) const PCI_SYSFS_BUS_PATH: &str = "/sys/bus/pci/devices";
