#ifndef NS3_LAB_FOUR_PATH_ROCE_H
#define NS3_LAB_FOUR_PATH_ROCE_H

#include "roce-frame.h"

#include "ns3/data-rate.h"
#include "ns3/point-to-point-module.h"

#include <array>
#include <cstdint>
#include <string>
#include <vector>

namespace ns3lab
{

struct FrameTransmission
{
    uint64_t frameId{0};
    uint64_t timePs{0};
    uint32_t channel{0};
    std::vector<uint8_t> frame;
};

struct NetworkEvent
{
    enum class Kind : uint8_t
    {
        DELIVERED = 1,
        DROPPED = 2,
    };

    uint64_t sequence{0};
    uint64_t frameId{0};
    Kind kind{Kind::DELIVERED};
    uint32_t channel{0};
    uint64_t timePs{0};
    std::vector<uint8_t> frame;
};

class FrameIdTag : public ns3::Tag
{
  public:
    FrameIdTag() = default;
    explicit FrameIdTag(uint64_t value)
        : m_value(value)
    {
    }

    static ns3::TypeId GetTypeId()
    {
        static ns3::TypeId tid = ns3::TypeId("ns3lab::FrameIdTag")
                                     .SetParent<ns3::Tag>()
                                     .AddConstructor<FrameIdTag>();
        return tid;
    }

    ns3::TypeId GetInstanceTypeId() const override
    {
        return GetTypeId();
    }

    uint32_t GetSerializedSize() const override
    {
        return 8;
    }

    void Serialize(ns3::TagBuffer buffer) const override
    {
        buffer.WriteU64(m_value);
    }

    void Deserialize(ns3::TagBuffer buffer) override
    {
        m_value = buffer.ReadU64();
    }

    void Print(std::ostream& stream) const override
    {
        stream << m_value;
    }

    uint64_t Get() const
    {
        return m_value;
    }

  private:
    uint64_t m_value{0};
};

class FourPathRoceNetwork
{
  public:
    struct LinkConfig
    {
        std::string dataRate{"100Gbps"};
        std::string queueSize{"100p"};
        std::array<std::string, 4> delays{"200ns", "400ns", "800ns", "2us"};
    };

    FourPathRoceNetwork() : FourPathRoceNetwork(LinkConfig{})
    {
    }

    explicit FourPathRoceNetwork(const LinkConfig& config) : m_config(config)
    {
        m_nodes.Create(2);
        for (uint32_t channel = 0; channel < m_paths.size(); ++channel)
        {
            ns3::PointToPointHelper helper;
            helper.SetDeviceAttribute("DataRate", ns3::StringValue(config.dataRate));
            helper.SetQueue("ns3::DropTailQueue<Packet>",
                            "MaxSize",
                            ns3::StringValue(config.queueSize));
            helper.SetChannelAttribute("Delay", ns3::StringValue(config.delays[channel]));
            m_paths[channel] = helper.Install(m_nodes);
            m_sinks[channel].owner = this;
            m_sinks[channel].channel = channel;
            m_nodes.Get(1)->RegisterProtocolHandler(
                ns3::MakeCallback(&ChannelSink::Receive, &m_sinks[channel]),
                kIpv4EtherType,
                m_paths[channel].Get(1));
        }
    }

    void EnablePcap(const std::string& prefix)
    {
        for (uint32_t channel = 0; channel < m_paths.size(); ++channel)
        {
            ns3::PointToPointHelper helper;
            helper.EnablePcap(prefix + "-ch" + std::to_string(channel),
                              m_paths[channel].Get(0),
                              false);
        }
    }

    void Schedule(const FrameTransmission& transmission)
    {
        Require(transmission.channel < m_paths.size(), "frame channel is out of range");
        const uint64_t now = static_cast<uint64_t>(ns3::Simulator::Now().GetPicoSeconds());
        Require(transmission.timePs >= now, "cannot schedule a frame in the past");
        ns3::Simulator::Schedule(ns3::PicoSeconds(transmission.timePs - now),
                                 &FourPathRoceNetwork::Send,
                                 this,
                                 transmission);
    }

    const std::vector<NetworkEvent>& Events() const
    {
        return m_events;
    }

    uint64_t AcceptedFrames() const
    {
        return m_accepted;
    }

    uint64_t DeliveredFrames() const
    {
        return m_delivered;
    }

    uint64_t DroppedFrames() const
    {
        return m_dropped;
    }

    // How long the simulator must run past the last injection for every frame
    // to have met its fate.  This follows the *configured* rate and delays: a
    // guard pinned to the default 100 Gbps stops the simulator while slow links
    // are still serializing, which used to look like frames vanishing.
    uint64_t DrainGuardPs(const std::vector<FrameTransmission>& transmissions) const
    {
        const uint64_t linkBps = ns3::DataRate(m_config.dataRate).GetBitRate();
        Require(linkBps > 0, "link data rate must be positive");
        uint64_t maxDelayPs = 0;
        for (const auto& delay : m_config.delays)
        {
            std::istringstream stream(delay);
            ns3::Time parsed;
            stream >> parsed;
            Require(!stream.fail(), "unparsable link delay: " + delay);
            maxDelayPs = std::max(maxDelayPs, static_cast<uint64_t>(parsed.GetPicoSeconds()));
        }

        std::array<uint64_t, 4> serialized{};
        for (const auto& item : transmissions)
        {
            Require(item.channel < serialized.size(), "frame channel is out of range");
            const uint64_t bits = (item.frame.size() + 2) * 8ULL;
            serialized[item.channel] +=
                (bits * 1'000'000'000'000ULL + linkBps - 1) / linkBps;
        }
        return maxDelayPs + *std::max_element(serialized.begin(), serialized.end()) + 1'000;
    }

  private:
    struct ChannelSink
    {
        FourPathRoceNetwork* owner{nullptr};
        uint32_t channel{0};

        void Receive(ns3::Ptr<ns3::NetDevice>,
                     ns3::Ptr<const ns3::Packet> packet,
                     uint16_t,
                     const ns3::Address&,
                     const ns3::Address&,
                     ns3::NetDevice::PacketType)
        {
            owner->ReceiveFrame(channel, packet);
        }
    };

    void Send(FrameTransmission transmission)
    {
        auto packet = ns3::Create<ns3::Packet>(transmission.frame.data(), transmission.frame.size());
        packet->AddPacketTag(FrameIdTag(transmission.frameId));
        const bool accepted = m_paths[transmission.channel].Get(0)->Send(
            packet,
            m_paths[transmission.channel].Get(1)->GetAddress(),
            kIpv4EtherType);
        if (accepted)
        {
            ++m_accepted;
            return;
        }
        ++m_dropped;
        m_events.push_back(NetworkEvent{m_nextSequence++,
                                        transmission.frameId,
                                        NetworkEvent::Kind::DROPPED,
                                        transmission.channel,
                                        static_cast<uint64_t>(
                                            ns3::Simulator::Now().GetPicoSeconds()),
                                        std::move(transmission.frame)});
    }

    void ReceiveFrame(uint32_t channel, ns3::Ptr<const ns3::Packet> packet)
    {
        FrameIdTag tag;
        Require(packet->PeekPacketTag(tag), "received frame has no frame ID tag");
        std::vector<uint8_t> bytes(packet->GetSize());
        packet->CopyData(bytes.data(), bytes.size());
        ++m_delivered;
        m_events.push_back(NetworkEvent{m_nextSequence++,
                                        tag.Get(),
                                        NetworkEvent::Kind::DELIVERED,
                                        channel,
                                        static_cast<uint64_t>(
                                            ns3::Simulator::Now().GetPicoSeconds()),
                                        std::move(bytes)});
    }

    ns3::NodeContainer m_nodes;
    LinkConfig m_config;
    std::array<ns3::NetDeviceContainer, 4> m_paths;
    std::array<ChannelSink, 4> m_sinks;
    std::vector<NetworkEvent> m_events;
    uint64_t m_nextSequence{0};
    uint64_t m_accepted{0};
    uint64_t m_delivered{0};
    uint64_t m_dropped{0};
};

} // namespace ns3lab

#endif
