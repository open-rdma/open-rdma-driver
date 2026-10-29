#ifndef NS3_LAB_COMMON_H
#define NS3_LAB_COMMON_H

#include "ns3/core-module.h"
#include "ns3/internet-module.h"
#include "ns3/network-module.h"

#include <algorithm>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <limits>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace ns3lab
{

constexpr uint32_t kHeaderMagic = 0x4e53334c; // "NS3L"

class LabHeader : public ns3::Header
{
  public:
    LabHeader() = default;

    LabHeader(uint32_t messageId, uint32_t sequence, uint32_t channel, uint64_t txTimeNs)
        : m_messageId(messageId),
          m_sequence(sequence),
          m_channel(channel),
          m_txTimeNs(txTimeNs)
    {
    }

    static ns3::TypeId GetTypeId()
    {
        static ns3::TypeId tid = ns3::TypeId("ns3lab::LabHeader")
                                     .SetParent<ns3::Header>()
                                     .AddConstructor<LabHeader>();
        return tid;
    }

    ns3::TypeId GetInstanceTypeId() const override
    {
        return GetTypeId();
    }

    uint32_t GetSerializedSize() const override
    {
        return 24;
    }

    void Serialize(ns3::Buffer::Iterator start) const override
    {
        start.WriteHtonU32(kHeaderMagic);
        start.WriteHtonU32(m_messageId);
        start.WriteHtonU32(m_sequence);
        start.WriteHtonU32(m_channel);
        start.WriteHtonU64(m_txTimeNs);
    }

    uint32_t Deserialize(ns3::Buffer::Iterator start) override
    {
        m_magic = start.ReadNtohU32();
        m_messageId = start.ReadNtohU32();
        m_sequence = start.ReadNtohU32();
        m_channel = start.ReadNtohU32();
        m_txTimeNs = start.ReadNtohU64();
        return GetSerializedSize();
    }

    void Print(std::ostream& os) const override
    {
        os << "message=" << m_messageId << " sequence=" << m_sequence
           << " channel=" << m_channel << " tx=" << m_txTimeNs << "ns";
    }

    bool IsValid() const
    {
        return m_magic == kHeaderMagic;
    }

    uint32_t GetMessageId() const
    {
        return m_messageId;
    }

    uint32_t GetSequence() const
    {
        return m_sequence;
    }

    uint32_t GetChannel() const
    {
        return m_channel;
    }

    uint64_t GetTxTimeNs() const
    {
        return m_txTimeNs;
    }

  private:
    uint32_t m_magic{kHeaderMagic};
    uint32_t m_messageId{0};
    uint32_t m_sequence{0};
    uint32_t m_channel{0};
    uint64_t m_txTimeNs{0};
};

NS_OBJECT_ENSURE_REGISTERED(LabHeader);

inline std::vector<uint8_t>
MakePayload(uint32_t sequence, std::size_t size)
{
    std::vector<uint8_t> payload(size);
    for (std::size_t i = 0; i < size; ++i)
    {
        payload[i] = static_cast<uint8_t>((sequence * 17 + i * 31) & 0xff);
    }
    return payload;
}

inline bool
PayloadMatches(const ns3::Ptr<ns3::Packet>& packet, uint32_t sequence)
{
    std::vector<uint8_t> actual(packet->GetSize());
    packet->CopyData(actual.data(), actual.size());
    return actual == MakePayload(sequence, actual.size());
}

struct PacketRecord
{
    uint32_t sequence{0};
    uint32_t channel{0};
    uint32_t bytes{0};
    int64_t txTimeNs{0};
    int64_t rxTimeNs{0};
};

class UdpCollector
{
  public:
    void Attach(const ns3::Ptr<ns3::Node>& node, uint16_t port)
    {
        m_socket = ns3::Socket::CreateSocket(node, ns3::UdpSocketFactory::GetTypeId());
        const auto local = ns3::InetSocketAddress(ns3::Ipv4Address::GetAny(), port);
        if (m_socket->Bind(local) != 0)
        {
            throw std::runtime_error("failed to bind UDP collector");
        }
        m_socket->SetRecvCallback(ns3::MakeCallback(&UdpCollector::HandleRead, this));
    }

    const std::vector<PacketRecord>& Records() const
    {
        return m_records;
    }

    const std::vector<uint32_t>& ArrivalOrder() const
    {
        return m_arrivalOrder;
    }

    uint32_t PayloadErrors() const
    {
        return m_payloadErrors;
    }

    uint32_t InvalidHeaders() const
    {
        return m_invalidHeaders;
    }

    uint32_t DuplicatePackets() const
    {
        return m_duplicatePackets;
    }

    uint32_t ReorderedPackets() const
    {
        return m_reorderedPackets;
    }

    uint32_t MaxReorderDepth() const
    {
        return m_maxReorderDepth;
    }

    uint64_t ReceivedBytes() const
    {
        return m_receivedBytes;
    }

    double MeanDelayNs() const
    {
        if (m_records.empty())
        {
            return 0.0;
        }
        long double sum = 0;
        for (const auto& record : m_records)
        {
            sum += record.rxTimeNs - record.txTimeNs;
        }
        return static_cast<double>(sum / m_records.size());
    }

    double ThroughputGbps() const
    {
        if (m_records.size() < 2)
        {
            return 0.0;
        }
        const auto spanNs = m_records.back().rxTimeNs - m_records.front().rxTimeNs;
        if (spanNs <= 0)
        {
            return 0.0;
        }
        return static_cast<double>(m_receivedBytes * 8) / static_cast<double>(spanNs);
    }

    std::vector<uint32_t> MissingSequences(uint32_t packetCount) const
    {
        std::vector<uint32_t> missing;
        for (uint32_t sequence = 0; sequence < packetCount; ++sequence)
        {
            if (!m_seen.contains(sequence))
            {
                missing.push_back(sequence);
            }
        }
        return missing;
    }

  private:
    void HandleRead(ns3::Ptr<ns3::Socket> socket)
    {
        while (auto packet = socket->Recv())
        {
            const uint32_t packetBytes = packet->GetSize();
            LabHeader header;
            if (packet->RemoveHeader(header) != header.GetSerializedSize() || !header.IsValid())
            {
                ++m_invalidHeaders;
                continue;
            }

            const uint32_t sequence = header.GetSequence();
            if (!PayloadMatches(packet, sequence))
            {
                ++m_payloadErrors;
            }
            if (!m_seen.insert(sequence).second)
            {
                ++m_duplicatePackets;
            }

            if (m_haveHighest && sequence < m_highestSequence)
            {
                ++m_reorderedPackets;
                m_maxReorderDepth = std::max(m_maxReorderDepth, m_highestSequence - sequence);
            }
            m_highestSequence = m_haveHighest ? std::max(m_highestSequence, sequence) : sequence;
            m_haveHighest = true;

            const auto nowNs = ns3::Simulator::Now().GetNanoSeconds();
            m_records.push_back(PacketRecord{sequence,
                                             header.GetChannel(),
                                             packetBytes,
                                             static_cast<int64_t>(header.GetTxTimeNs()),
                                             nowNs});
            m_arrivalOrder.push_back(sequence);
            m_receivedBytes += packetBytes;
        }
    }

    ns3::Ptr<ns3::Socket> m_socket;
    std::vector<PacketRecord> m_records;
    std::vector<uint32_t> m_arrivalOrder;
    std::set<uint32_t> m_seen;
    uint32_t m_payloadErrors{0};
    uint32_t m_invalidHeaders{0};
    uint32_t m_duplicatePackets{0};
    uint32_t m_reorderedPackets{0};
    uint32_t m_maxReorderDepth{0};
    uint32_t m_highestSequence{0};
    uint64_t m_receivedBytes{0};
    bool m_haveHighest{false};
};

class UdpSequenceSource : public ns3::Application
{
  public:
    void Configure(std::vector<ns3::Address> destinations,
                   uint32_t packetCount,
                   uint32_t packetSize,
                   ns3::Time interval,
                   uint32_t messageId = 1)
    {
        if (destinations.empty())
        {
            throw std::invalid_argument("at least one destination is required");
        }
        if (packetSize < LabHeader().GetSerializedSize())
        {
            throw std::invalid_argument("packetSize is smaller than LabHeader");
        }
        m_destinations = std::move(destinations);
        m_packetCount = packetCount;
        m_packetSize = packetSize;
        m_interval = interval;
        m_messageId = messageId;
    }

    uint32_t SentPackets() const
    {
        return m_sentPackets;
    }

    uint64_t SentBytes() const
    {
        return static_cast<uint64_t>(m_sentPackets) * m_packetSize;
    }

  private:
    void StartApplication() override
    {
        m_socket = ns3::Socket::CreateSocket(GetNode(), ns3::UdpSocketFactory::GetTypeId());
        m_socket->Bind();
        m_sendEvent = ns3::Simulator::ScheduleNow(&UdpSequenceSource::SendNext, this);
    }

    void StopApplication() override
    {
        ns3::Simulator::Cancel(m_sendEvent);
        if (m_socket)
        {
            m_socket->Close();
        }
    }

    void SendNext()
    {
        if (m_sentPackets >= m_packetCount)
        {
            return;
        }

        const uint32_t sequence = m_sentPackets;
        const uint32_t channel = sequence % m_destinations.size();
        const uint32_t bodySize = m_packetSize - LabHeader().GetSerializedSize();
        const auto payload = MakePayload(sequence, bodySize);
        auto packet = ns3::Create<ns3::Packet>(payload.data(), payload.size());
        packet->AddHeader(LabHeader(m_messageId,
                                    sequence,
                                    channel,
                                    ns3::Simulator::Now().GetNanoSeconds()));

        const int sent = m_socket->SendTo(packet, 0, m_destinations[channel]);
        if (sent < 0)
        {
            throw std::runtime_error("UDP send failed");
        }
        ++m_sentPackets;

        if (m_sentPackets < m_packetCount)
        {
            m_sendEvent = ns3::Simulator::Schedule(m_interval,
                                                   &UdpSequenceSource::SendNext,
                                                   this);
        }
    }

    ns3::Ptr<ns3::Socket> m_socket;
    ns3::EventId m_sendEvent;
    std::vector<ns3::Address> m_destinations;
    uint32_t m_packetCount{0};
    uint32_t m_packetSize{0};
    uint32_t m_messageId{0};
    uint32_t m_sentPackets{0};
    ns3::Time m_interval{ns3::NanoSeconds(1)};
};

inline ns3::Time
IntervalForRate(uint32_t packetSize, const ns3::DataRate& rate)
{
    const uint64_t bits = static_cast<uint64_t>(packetSize) * 8;
    const double seconds = static_cast<double>(bits) / rate.GetBitRate();
    return ns3::Seconds(seconds);
}

inline void
PrepareOutput(const std::string& path)
{
    const std::filesystem::path output(path);
    if (output.has_parent_path())
    {
        std::filesystem::create_directories(output.parent_path());
    }
}

inline std::string
JsonArray(const std::vector<uint32_t>& values)
{
    std::ostringstream stream;
    stream << '[';
    for (std::size_t i = 0; i < values.size(); ++i)
    {
        if (i != 0)
        {
            stream << ',';
        }
        stream << values[i];
    }
    stream << ']';
    return stream.str();
}

inline void
Require(bool condition, const std::string& message)
{
    if (!condition)
    {
        throw std::runtime_error(message);
    }
}

} // namespace ns3lab

#endif // NS3_LAB_COMMON_H
