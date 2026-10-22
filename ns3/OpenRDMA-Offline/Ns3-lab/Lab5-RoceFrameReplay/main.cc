#include "../common/lab-common.h"

#include "ns3/point-to-point-module.h"

#include <array>
#include <fstream>
#include <iostream>
#include <map>
#include <tuple>

using namespace ns3;

namespace
{

constexpr uint32_t kPsnMask = 0x00ffffff;
constexpr uint16_t kIpv4EtherType = 0x0800;

struct TraceRecord
{
    uint64_t timeNs{0};
    uint32_t channel{0};
    std::vector<uint8_t> frame;
};

struct ParsedRocePacket
{
    uint32_t channel{0};
    uint8_t opcode{0};
    uint32_t qpn{0};
    uint32_t psn{0};
    uint32_t rkey{0};
    uint64_t remoteAddress{0};
    uint32_t messageLength{0};
    std::vector<uint8_t> payload;
};

uint16_t
Read16(const std::vector<uint8_t>& bytes, std::size_t offset)
{
    return (static_cast<uint16_t>(bytes.at(offset)) << 8) | bytes.at(offset + 1);
}

uint32_t
Read24(const std::vector<uint8_t>& bytes, std::size_t offset)
{
    return (static_cast<uint32_t>(bytes.at(offset)) << 16) |
           (static_cast<uint32_t>(bytes.at(offset + 1)) << 8) | bytes.at(offset + 2);
}

uint32_t
Read32(const std::vector<uint8_t>& bytes, std::size_t offset)
{
    return (static_cast<uint32_t>(Read16(bytes, offset)) << 16) | Read16(bytes, offset + 2);
}

uint64_t
Read64(const std::vector<uint8_t>& bytes, std::size_t offset)
{
    return (static_cast<uint64_t>(Read32(bytes, offset)) << 32) | Read32(bytes, offset + 4);
}

uint16_t
Ipv4Checksum(const std::vector<uint8_t>& bytes, std::size_t offset)
{
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

std::vector<uint8_t>
DecodeHex(const std::string& text)
{
    ns3lab::Require(text.size() % 2 == 0, "frame hex string has odd length");
    std::vector<uint8_t> bytes;
    bytes.reserve(text.size() / 2);
    for (std::size_t i = 0; i < text.size(); i += 2)
    {
        const auto value = std::stoul(text.substr(i, 2), nullptr, 16);
        bytes.push_back(static_cast<uint8_t>(value));
    }
    return bytes;
}

std::vector<TraceRecord>
ReadTrace(const std::string& path)
{
    std::ifstream stream(path);
    if (!stream)
    {
        throw std::runtime_error("cannot open trace: " + path);
    }

    std::vector<TraceRecord> records;
    std::string line;
    std::getline(stream, line); // CSV header
    while (std::getline(stream, line))
    {
        if (!line.empty() && line.back() == '\r')
        {
            line.pop_back();
        }
        if (line.empty() || line.front() == '#')
        {
            continue;
        }
        const auto firstComma = line.find(',');
        const auto secondComma = line.find(',', firstComma + 1);
        ns3lab::Require(firstComma != std::string::npos && secondComma != std::string::npos,
                        "malformed trace row");
        records.push_back(TraceRecord{std::stoull(line.substr(0, firstComma)),
                                      static_cast<uint32_t>(std::stoul(
                                          line.substr(firstComma + 1,
                                                      secondComma - firstComma - 1))),
                                      DecodeHex(line.substr(secondComma + 1))});
    }
    ns3lab::Require(!records.empty(), "trace contains no frames");
    return records;
}

ParsedRocePacket
ParseFrame(const std::vector<uint8_t>& frame, uint32_t channel)
{
    ns3lab::Require(frame.size() >= 54, "frame is shorter than Ethernet/IP/UDP/BTH");
    ns3lab::Require(Read16(frame, 12) == kIpv4EtherType, "frame is not IPv4");
    ns3lab::Require(frame[14] == 0x45, "only a 20-byte IPv4 header is supported");
    ns3lab::Require(frame[23] == 17, "IPv4 payload is not UDP");
    ns3lab::Require(Ipv4Checksum(frame, 14) == 0, "bad IPv4 checksum");
    ns3lab::Require(static_cast<std::size_t>(Read16(frame, 16)) + 14 == frame.size(), "IPv4 total length mismatch");
    ns3lab::Require(Read16(frame, 36) == 4791, "UDP destination is not RoCEv2 port 4791");
    ns3lab::Require(static_cast<std::size_t>(Read16(frame, 38)) + 34 == frame.size(), "UDP length mismatch");

    ParsedRocePacket packet;
    packet.channel = channel;
    packet.opcode = frame[42];
    packet.qpn = Read24(frame, 46);
    packet.psn = Read24(frame, 50);

    const bool firstLayout = packet.opcode == 0x06 || packet.opcode == 0x09;
    const std::size_t payloadOffset = firstLayout ? 70 : 54;
    ns3lab::Require(frame.size() >= payloadOffset, "truncated RETH header");
    if (firstLayout)
    {
        // OpenRDMA-flow currently emits RKEY, remote address, then message length.
        packet.rkey = Read32(frame, 54);
        packet.remoteAddress = Read64(frame, 58);
        packet.messageLength = Read32(frame, 66);
    }
    packet.payload.assign(frame.begin() + payloadOffset, frame.end());
    ns3lab::Require(!packet.payload.empty(), "zero-length RDMA payload is unsupported");
    return packet;
}

class RoceSink
{
  public:
    RoceSink() = default;

    void Configure(uint32_t channel, std::vector<ParsedRocePacket>* packets)
    {
        m_channel = channel;
        m_packets = packets;
    }

    void Receive(Ptr<NetDevice>,
                 Ptr<const Packet> packet,
                 uint16_t,
                 const Address&,
                 const Address&,
                 NetDevice::PacketType)
    {
        std::vector<uint8_t> bytes(packet->GetSize());
        packet->CopyData(bytes.data(), bytes.size());
        m_packets->push_back(ParseFrame(bytes, m_channel));
    }

  private:
    uint32_t m_channel{0};
    std::vector<ParsedRocePacket>* m_packets{nullptr};
};

void
Transmit(Ptr<NetDevice> device, Address peer, const TraceRecord* record, uint32_t* sent)
{
    auto packet = Create<Packet>(record->frame.data(), record->frame.size());
    ns3lab::Require(device->Send(packet, peer, kIpv4EtherType), "NetDevice rejected replay frame");
    ++*sent;
}

uint32_t
PsnDistance(uint32_t start, uint32_t current)
{
    return (current - start) & kPsnMask;
}

uint32_t
ValidateMessages(const std::vector<ParsedRocePacket>& received)
{
    std::map<uint32_t, std::vector<ParsedRocePacket>> byQpn;
    for (const auto& packet : received)
    {
        byQpn[packet.qpn].push_back(packet);
    }

    for (auto& [qpn, packets] : byQpn)
    {
        const auto first = std::find_if(packets.begin(), packets.end(), [](const auto& packet) {
            return packet.opcode == 0x06 || packet.opcode == 0x09;
        });
        ns3lab::Require(first != packets.end(), "message has no FIRST/ONLY packet");
        const uint32_t startPsn = first->psn;
        const uint32_t expectedLength = first->messageLength;

        std::sort(packets.begin(), packets.end(), [startPsn](const auto& left, const auto& right) {
            return PsnDistance(startPsn, left.psn) < PsnDistance(startPsn, right.psn);
        });

        std::vector<uint8_t> message;
        for (std::size_t index = 0; index < packets.size(); ++index)
        {
            ns3lab::Require(PsnDistance(startPsn, packets[index].psn) == index,
                            "message contains a missing or duplicate PSN");
            if (packets.size() == 1)
            {
                ns3lab::Require(packets[index].opcode == 0x09, "single-packet message is not ONLY");
            }
            else if (index == 0)
            {
                ns3lab::Require(packets[index].opcode == 0x06, "message does not start with FIRST");
            }
            else if (index + 1 == packets.size())
            {
                ns3lab::Require(packets[index].opcode == 0x08, "message does not end with LAST");
            }
            else
            {
                ns3lab::Require(packets[index].opcode == 0x07, "interior packet is not MIDDLE");
            }
            message.insert(message.end(), packets[index].payload.begin(), packets[index].payload.end());
        }

        ns3lab::Require(message.size() == expectedLength, "reassembled message length mismatch");
        for (std::size_t offset = 0; offset < message.size(); ++offset)
        {
            const auto expected = static_cast<uint8_t>((qpn * 29 + offset * 7) & 0xff);
            ns3lab::Require(message[offset] == expected, "reassembled payload mismatch");
        }
    }
    return byQpn.size();
}

} // namespace

int
main(int argc, char* argv[])
{
    std::string tracePath = "../Lab5-RoceFrameReplay/test-data/m3-trace.csv";
    std::string output = "../results/lab5.json";
    std::string pcapPrefix = "../results/lab5";

    CommandLine cmd(__FILE__);
    cmd.AddValue("trace", "CSV frame trace to replay", tracePath);
    cmd.AddValue("output", "JSON result path", output);
    cmd.AddValue("pcapPrefix", "Outer point-to-point PCAP prefix", pcapPrefix);
    cmd.Parse(argc, argv);

    auto trace = ReadTrace(tracePath);
    NodeContainer endpoints;
    endpoints.Create(2);
    auto sender = endpoints.Get(0);
    auto receiver = endpoints.Get(1);

    const std::array<std::string, 4> delays = {"200ns", "400ns", "800ns", "2us"};
    std::array<NetDeviceContainer, 4> paths;
    std::array<RoceSink, 4> sinks;
    std::vector<ParsedRocePacket> received;

    for (uint32_t channel = 0; channel < paths.size(); ++channel)
    {
        PointToPointHelper helper;
        helper.SetDeviceAttribute("DataRate", StringValue("100Gbps"));
        helper.SetChannelAttribute("Delay", StringValue(delays[channel]));
        paths[channel] = helper.Install(sender, receiver);
        sinks[channel].Configure(channel, &received);
        receiver->RegisterProtocolHandler(MakeCallback(&RoceSink::Receive, &sinks[channel]),
                                          kIpv4EtherType,
                                          paths[channel].Get(1));
        helper.EnablePcap(pcapPrefix + "-ch" + std::to_string(channel),
                          paths[channel].Get(0),
                          false);
    }

    uint32_t sent = 0;
    for (const auto& record : trace)
    {
        ns3lab::Require(record.channel < paths.size(), "trace channel is out of range");
        Simulator::Schedule(MicroSeconds(10) + NanoSeconds(record.timeNs),
                            &Transmit,
                            paths[record.channel].Get(0),
                            paths[record.channel].Get(1)->GetAddress(),
                            &record,
                            &sent);
    }

    Simulator::Stop(MilliSeconds(1));
    Simulator::Run();

    ns3lab::Require(sent == trace.size(), "not every trace frame was transmitted");
    ns3lab::Require(received.size() == trace.size(), "lossless replay dropped a frame");

    std::map<std::pair<uint32_t, uint32_t>, uint32_t> expectedChannels;
    for (const auto& record : trace)
    {
        const auto parsed = ParseFrame(record.frame, record.channel);
        expectedChannels[{parsed.qpn, parsed.psn}] = record.channel;
    }
    for (const auto& packet : received)
    {
        ns3lab::Require(expectedChannels.at({packet.qpn, packet.psn}) == packet.channel,
                        "frame arrived through the wrong channel");
    }
    const uint32_t messages = ValidateMessages(received);

    ns3lab::PrepareOutput(output);
    std::ofstream report(output);
    report << "{\n"
           << "  \"lab\": 5,\n"
           << "  \"scenario\": \"openrdma-rocev2-frame-replay\",\n"
           << "  \"tx_frames\": " << sent << ",\n"
           << "  \"rx_frames\": " << received.size() << ",\n"
           << "  \"reassembled_messages\": " << messages << ",\n"
           << "  \"arrival\": [";
    for (std::size_t i = 0; i < received.size(); ++i)
    {
        if (i != 0)
        {
            report << ',';
        }
        report << "{\"channel\":" << received[i].channel << ",\"qpn\":" << received[i].qpn
               << ",\"psn\":" << received[i].psn << ",\"opcode\":"
               << static_cast<uint32_t>(received[i].opcode) << '}';
    }
    report << "],\n"
           << "  \"icrc_modeled\": false,\n"
           << "  \"status\": \"PASS\"\n"
           << "}\n";

    std::cout << "Lab 5 PASS: frames=" << received.size() << " messages=" << messages << '\n';
    Simulator::Destroy();
    return 0;
}
