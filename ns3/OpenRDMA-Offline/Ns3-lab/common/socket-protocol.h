#ifndef NS3_LAB_SOCKET_PROTOCOL_H
#define NS3_LAB_SOCKET_PROTOCOL_H

#include "../common/lab-common.h"

#include <cerrno>
#include <cstdint>
#include <cstring>
#include <stdexcept>
#include <string>
#include <sys/socket.h>
#include <unistd.h>
#include <vector>

namespace ns3lab::wire
{

constexpr uint32_t kMagic = 0x4e533743; // "NS7C"
constexpr uint16_t kVersion = 1;
constexpr uint32_t kMaxMessageBytes = 16 * 1024 * 1024;
constexpr uint32_t kMaxFrameBytes = 65535;
constexpr uint32_t kMaxBatchFrames = 4096;

enum class Type : uint16_t
{
    HELLO = 1,
    HELLO_ACK = 2,
    ADVANCE = 3,
    RESULT = 4,
    FINISH = 5,
    FINISH_ACK = 6,
    ERROR = 7,
};

class Writer
{
  public:
    void U8(uint8_t value)
    {
        m_data.push_back(value);
    }

    void U16(uint16_t value)
    {
        U8(static_cast<uint8_t>(value >> 8));
        U8(static_cast<uint8_t>(value));
    }

    void U32(uint32_t value)
    {
        U16(static_cast<uint16_t>(value >> 16));
        U16(static_cast<uint16_t>(value));
    }

    void U64(uint64_t value)
    {
        U32(static_cast<uint32_t>(value >> 32));
        U32(static_cast<uint32_t>(value));
    }

    void Bytes(const std::vector<uint8_t>& value)
    {
        m_data.insert(m_data.end(), value.begin(), value.end());
    }

    void Bytes(const std::string& value)
    {
        m_data.insert(m_data.end(), value.begin(), value.end());
    }

    const std::vector<uint8_t>& Data() const
    {
        return m_data;
    }

  private:
    std::vector<uint8_t> m_data;
};

class Reader
{
  public:
    explicit Reader(const std::vector<uint8_t>& data)
        : m_data(data)
    {
    }

    uint8_t U8()
    {
        Need(1);
        return m_data[m_offset++];
    }

    uint16_t U16()
    {
        return (static_cast<uint16_t>(U8()) << 8) | U8();
    }

    uint32_t U32()
    {
        return (static_cast<uint32_t>(U16()) << 16) | U16();
    }

    uint64_t U64()
    {
        return (static_cast<uint64_t>(U32()) << 32) | U32();
    }

    std::vector<uint8_t> Bytes(std::size_t size)
    {
        Need(size);
        std::vector<uint8_t> result(m_data.begin() + m_offset,
                                    m_data.begin() + m_offset + size);
        m_offset += size;
        return result;
    }

    void Reserved(std::size_t size)
    {
        const auto bytes = Bytes(size);
        Require(std::all_of(bytes.begin(), bytes.end(), [](uint8_t value) { return value == 0; }),
                "nonzero reserved protocol field");
    }

    void Finish() const
    {
        Require(m_offset == m_data.size(), "trailing bytes in protocol message");
    }

  private:
    void Need(std::size_t size) const
    {
        Require(size <= m_data.size() - m_offset, "truncated protocol message");
    }

    const std::vector<uint8_t>& m_data;
    std::size_t m_offset{0};
};

struct Message
{
    Type type{Type::ERROR};
    uint32_t requestId{0};
    std::vector<uint8_t> body;
};

inline void
ReadExact(int fd, void* destination, std::size_t size)
{
    auto* bytes = static_cast<uint8_t*>(destination);
    std::size_t offset = 0;
    while (offset < size)
    {
        const ssize_t count = ::recv(fd, bytes + offset, size - offset, 0);
        if (count > 0)
        {
            offset += static_cast<std::size_t>(count);
            continue;
        }
        if (count == 0)
        {
            throw std::runtime_error("socket closed before protocol message completed");
        }
        if (errno == EINTR)
        {
            continue;
        }
        throw std::runtime_error("socket read failed: " + std::string(std::strerror(errno)));
    }
}

inline void
WriteAll(int fd, const void* source, std::size_t size)
{
    const auto* bytes = static_cast<const uint8_t*>(source);
    std::size_t offset = 0;
    while (offset < size)
    {
        const ssize_t count = ::send(fd, bytes + offset, size - offset, MSG_NOSIGNAL);
        if (count > 0)
        {
            offset += static_cast<std::size_t>(count);
            continue;
        }
        if (count == 0)
        {
            throw std::runtime_error("socket closed during write");
        }
        if (errno == EINTR)
        {
            continue;
        }
        throw std::runtime_error("socket write failed: " + std::string(std::strerror(errno)));
    }
}

inline Message
ReadMessage(int fd)
{
    std::array<uint8_t, 4> prefix{};
    ReadExact(fd, prefix.data(), prefix.size());
    const uint32_t length = (static_cast<uint32_t>(prefix[0]) << 24) |
                            (static_cast<uint32_t>(prefix[1]) << 16) |
                            (static_cast<uint32_t>(prefix[2]) << 8) | prefix[3];
    Require(length >= 12 && length <= kMaxMessageBytes, "invalid protocol message length");
    std::vector<uint8_t> payload(length);
    ReadExact(fd, payload.data(), payload.size());
    Reader reader(payload);
    Require(reader.U32() == kMagic, "bad protocol magic");
    Require(reader.U16() == kVersion, "unsupported protocol version");
    const auto type = static_cast<Type>(reader.U16());
    const uint32_t requestId = reader.U32();
    return Message{type, requestId, reader.Bytes(length - 12)};
}

inline void
WriteMessage(int fd, Type type, uint32_t requestId, const std::vector<uint8_t>& body)
{
    Writer payload;
    payload.U32(kMagic);
    payload.U16(kVersion);
    payload.U16(static_cast<uint16_t>(type));
    payload.U32(requestId);
    payload.Bytes(body);
    Require(payload.Data().size() <= kMaxMessageBytes, "protocol message is too large");

    Writer message;
    message.U32(static_cast<uint32_t>(payload.Data().size()));
    message.Bytes(payload.Data());
    WriteAll(fd, message.Data().data(), message.Data().size());
}

inline void
WriteError(int fd, uint32_t requestId, uint32_t code, const std::string& text)
{
    Writer body;
    body.U32(code);
    body.U32(static_cast<uint32_t>(text.size()));
    body.Bytes(text);
    WriteMessage(fd, Type::ERROR, requestId, body.Data());
}

} // namespace ns3lab::wire

#endif
