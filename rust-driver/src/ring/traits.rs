/// Abstraction over low-level CSR access.
///
/// CSR failures are unrecoverable (hardware disconnected, simulator crashed),
/// so implementations panic rather than returning errors.
pub(crate) trait DeviceAdaptor: Clone {
    fn read_csr(&self, addr: usize) -> u32;
    fn write_csr(&self, addr: usize, data: u32);
}

/// Compile-time description of a ring.
pub(crate) trait RingSpec {
    const ELEMENT_NUM_EXP: u8;

    const ELEMENT_NUM: u32 = 1 << Self::ELEMENT_NUM_EXP;

    #[inline]
    fn element_num() -> u32 {
        Self::ELEMENT_NUM
    }

    #[inline]
    fn element_num_usize() -> usize {
        Self::ELEMENT_NUM as usize
    }

    fn csr_base(&self) -> usize;

    type Element: Sized;
}

/// Marker trait for Device → Host rings (card produces, host consumes)
///
/// This trait is automatically implemented for all `RingSpec` types whose
/// `Element` implements `FromRingBytes`.
pub(crate) trait RingSpecToHost: RingSpec
where
    <Self as RingSpec>::Element: FromRingBytes,
{
}

/// Marker trait for Host → Device rings (host produces, card consumes)
///
/// This trait is automatically implemented for all `RingSpec` types whose
/// `Element` implements `ToRingBytes`.
pub(crate) trait RingSpecToCard: RingSpec
where
    <Self as RingSpec>::Element: ToRingBytes,
{
}

// ============================================================================
// Ring Buffer Element Traits
// ============================================================================

/// Serialization trait for ring buffer elements (used by ProducerRing, Host → Device).
///
/// Types implementing this trait can be written to a producer ring buffer.
/// The trait uses an associated type to define the byte representation.
pub(crate) trait ToRingBytes: Copy {
    /// The byte-level representation used in DMA buffer
    type Bytes: Copy;

    /// Serialize this element to bytes for DMA transfer
    /// TODO 也许需要写成 fn to_bytes(&self) -> &[Self::Bytes] 的形式更好
    fn to_bytes(&self) -> Self::Bytes;
}

/// Deserialization trait for ring buffer elements (used by ConsumerRing, Device → Host).
///
/// Types implementing this trait can be read from a consumer ring buffer.
/// Supports both single and multi-descriptor scenarios through slice parameter.
pub(crate) trait FromRingBytes: Sized {
    /// The byte-level representation used in DMA buffer
    type Bytes: Copy;

    /// Deserialize from one or more descriptors
    ///
    /// # Parameters
    /// - `bytes`: Descriptor slice
    ///   - `bytes.len() == 1`: Single descriptor (CmdQueue, WRITE, ACK)
    ///   - `bytes.len() == 2`: Double descriptor (READ, NAK, SendQueue)
    ///
    /// # Returns
    /// - `Some(Self)`: Successfully deserialized
    /// - `None`: Deserialization failed (invalid format or insufficient length)
    fn from_bytes(bytes: &[Self::Bytes]) -> Option<Self>;

    /// Check if the first descriptor is valid (bit 31.7)
    ///
    /// This typically checks the valid bit set by hardware.
    fn is_valid(bytes: &Self::Bytes) -> bool;

    /// Check if the first descriptor has a next descriptor (bit 31.6)
    ///
    /// Returns `false` by default. Override for types that support chaining.
    fn has_next(bytes: &Self::Bytes) -> bool {
        false
    }
}

// ============================================================================
// Default Implementations for Raw Bytes
// ============================================================================

// /// Zero-cost passthrough implementation for raw byte arrays (Producer side)
// impl ToRingBytes for [u8; 32] {
//     type Bytes = [u8; 32];

//     #[inline]
//     fn to_bytes(&self) -> Self::Bytes {
//         *self
//     }
// }

// /// Raw byte implementation with hardware descriptor validation (Consumer side)
// impl FromRingBytes for [u8; 32] {
//     type Bytes = [u8; 32];

//     #[inline]
//     fn from_bytes(bytes: &[Self::Bytes]) -> Option<Self> {
//         bytes.first().copied()
//     }

//     #[inline]
//     fn is_valid(bytes: &Self::Bytes) -> bool {
//         // Valid bit is the highest bit (bit 7) of the last byte
//         bytes[31] >> 7 == 1
//     }

//     #[inline]
//     fn has_next(bytes: &Self::Bytes) -> bool {
//         // Has-next bit is bit 6 of the last byte
//         (bytes[31] >> 6) & 1 == 1
//     }
// }

// ============================================================================
// Legacy Descriptor Traits (for compatibility with existing code)
// ============================================================================

/// TODO: Migrate to ToRingBytes and remove this trait
pub(crate) trait DescSerialize {
    fn serialize(&self) -> [u8; 32];
}

pub(crate) trait DescDeserialize {
    fn deserialize(d: [u8; 32]) -> Self;
}
