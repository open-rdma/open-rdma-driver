#ifndef NS3_LAB_ROCE_FRAME_H
#define NS3_LAB_ROCE_FRAME_H

#include "lab-common.h"

#include <cctype>
#include <cstdint>
#include <string>
#include <vector>

namespace ns3lab
{

constexpr uint32_t kPsnMask = 0x00ffffff;
constexpr uint16_t kIpv4EtherType = 0x0800;

struct ParsedRocePacket
{
    uint8_t opcode{0};
    uint32_t qpn{0};
    uint32_t psn{0};
    uint32_t rkey{0};
    uint64_t remoteAddress{0};
    uint32_t messageLength{0};
    std::size_t payloadOffset{0};
    std::vector<uint8_t> payload;
};

inline uint16_t
Read16(const std::vector<uint8_t>& bytes, std::size_t offset)
{
    Require(offset + 2 <= bytes.size(), "truncated 16-bit field");
    return (static_cast<uint16_t>(bytes[offset]) << 8) | bytes[offset + 1];
}

inline uint32_t
Read24(const std::vector<uint8_t>& bytes, std::size_t offset)
{
    Require(offset + 3 <= bytes.size(), "truncated 24-bit field");
    return (static_cast<uint32_t>(bytes[offset]) << 16) |
           (static_cast<uint32_t>(bytes[offset + 1]) << 8) | bytes[offset + 2];
}

inline uint32_t
Read32(const std::vector<uint8_t>& bytes, std::size_t offset)
{
    return (static_cast<uint32_t>(Read16(bytes, offset)) << 16) | Read16(bytes, offset + 2);
}

inline uint64_t
Read64(const std::vector<uint8_t>& bytes, std::size_t offset)
{
    return (static_cast<uint64_t>(Read32(bytes, offset)) << 32) | Read32(bytes, offset + 4);
}

inline uint16_t
Ipv4Checksum(const std::vector<uint8_t>& bytes, std::size_t offset)
{
    Require(offset + 20 <= bytes.size(), "truncated IPv4 header");
    uint32_t sum = 0;
    for (std::size_t i = 0; i < 20; i += 2)
    {
        sum += Read16(bytes, offset + i);
    }
    while (sum >> 16)
    {
        sum = (sum & 0xffff) + (sum >> 16);
    }
    return static_cast<uint16_t>(~sum);
}

inline uint8_t
HexNibble(char value)
{
    if (value >= '0' && value <= '9')
    {
        return static_cast<uint8_t>(value - '0');
    }
    const char lower = static_cast<char>(std::tolower(static_cast<unsigned char>(value)));
    Require(lower >= 'a' && lower <= 'f', "frame contains a non-hex character");
    return static_cast<uint8_t>(lower - 'a' + 10);
}

inline std::vector<uint8_t>
DecodeHex(const std::string& text)
{
    Require(text.size() % 2 == 0, "frame hex string has odd length");
    std::vector<uint8_t> bytes;
    bytes.reserve(text.size() / 2);
    for (std::size_t i = 0; i < text.size(); i += 2)
    {
        bytes.push_back(static_cast<uint8_t>((HexNibble(text[i]) << 4) | HexNibble(text[i + 1])));
    }
    return bytes;
}

inline std::string
EncodeHex(const std::vector<uint8_t>& bytes)
{
    static constexpr char kHex[] = "0123456789abcdef";
    std::string text;
    text.resize(bytes.size() * 2);
    for (std::size_t i = 0; i < bytes.size(); ++i)
    {
        text[2 * i] = kHex[bytes[i] >> 4];
        text[2 * i + 1] = kHex[bytes[i] & 0xf];
    }
    return text;
}

inline ParsedRocePacket
ParseRoceFrame(const std::vector<uint8_t>& frame)
{
    Require(frame.size() >= 54, "frame is shorter than Ethernet/IP/UDP/BTH");
    Require(Read16(frame, 12) == kIpv4EtherType, "frame is not IPv4");
    Require(frame[14] == 0x45, "only a 20-byte IPv4 header is supported");
    Require((Read16(frame, 20) & 0x1fff) == 0, "fragmented IPv4 is unsupported");
    Require(frame[23] == 17, "IPv4 payload is not UDP");
    Require(Ipv4Checksum(frame, 14) == 0, "bad IPv4 checksum");
    Require(static_cast<std::size_t>(Read16(frame, 16)) + 14 == frame.size(),
            "IPv4 total length mismatch");
    Require(Read16(frame, 36) == 4791, "UDP destination is not RoCEv2 port 4791");
    Require(static_cast<std::size_t>(Read16(frame, 38)) + 34 == frame.size(),
            "UDP length mismatch");

    ParsedRocePacket packet;
    packet.opcode = frame[42];
    Require(packet.opcode >= 0x06 && packet.opcode <= 0x09,
            "unsupported BTH opcode");
    packet.qpn = Read24(frame, 46);
    packet.psn = Read24(frame, 50);

    const bool firstLayout = packet.opcode == 0x06 || packet.opcode == 0x09;
    packet.payloadOffset = firstLayout ? 70 : 54;
    Require(frame.size() >= packet.payloadOffset, "truncated RETH header");
    if (firstLayout)
    {
        // This order follows the current OpenRDMA M3 implementation.
        packet.rkey = Read32(frame, 54);
        packet.remoteAddress = Read64(frame, 58);
        packet.messageLength = Read32(frame, 66);
    }
    packet.payload.assign(frame.begin() + packet.payloadOffset, frame.end());
    Require(!packet.payload.empty(), "zero-length RDMA payload is unsupported");
    return packet;
}

inline uint32_t
PsnDistance(uint32_t start, uint32_t current)
{
    return (current - start) & kPsnMask;
}

} // namespace ns3lab

#endif
