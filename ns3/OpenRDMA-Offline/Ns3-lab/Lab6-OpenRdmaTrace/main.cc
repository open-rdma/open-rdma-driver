#include "../common/four-path-roce.h"

#include <fstream>
#include <iostream>
#include <map>
#include <set>
#include <tuple>

using namespace ns3;

namespace
{

struct TraceRecord : ns3lab::FrameTransmission
{
};

struct FrameOracle
{
    uint64_t frameId{0};
    uint32_t messageId{0};
    uint32_t packetIndex{0};
    uint32_t packetCount{0};
    uint32_t requestId{0};
    uint32_t qpn{0};
    uint32_t psn{0};
    uint8_t opcode{0};
    uint32_t payloadOffset{0};
    uint32_t payloadLength{0};
    std::vector<uint8_t> expectedFrame;
};

struct MessageOracle
{
    uint32_t messageId{0};
    uint32_t requestId{0};
    uint32_t qpn{0};
    uint32_t startPsn{0};
    uint64_t remoteAddress{0};
    uint32_t remoteKey{0};
    uint32_t messageLength{0};
    std::vector<uint8_t> expectedPayload;
};

std::vector<std::string>
Split(const std::string& line)
{
    std::vector<std::string> fields;
    std::size_t begin = 0;
    while (true)
    {
        const auto comma = line.find(',', begin);
        fields.push_back(line.substr(begin, comma - begin));
        if (comma == std::string::npos)
        {
            return fields;
        }
        begin = comma + 1;
    }
}

std::vector<std::vector<std::string>>
ReadCsv(const std::string& path, const std::vector<std::string>& expectedHeader)
{
    std::ifstream stream(path);
    if (!stream)
    {
        throw std::runtime_error("cannot open CSV: " + path);
    }
    std::string line;
    ns3lab::Require(static_cast<bool>(std::getline(stream, line)), "empty CSV: " + path);
    if (!line.empty() && line.back() == '\r')
    {
        line.pop_back();
    }
    ns3lab::Require(Split(line) == expectedHeader, "unexpected CSV header: " + path);

    std::vector<std::vector<std::string>> rows;
    uint32_t lineNumber = 1;
    while (std::getline(stream, line))
    {
        ++lineNumber;
        if (!line.empty() && line.back() == '\r')
        {
            line.pop_back();
        }
        if (line.empty() || line.front() == '#')
        {
            continue;
        }
        auto fields = Split(line);
        ns3lab::Require(fields.size() == expectedHeader.size(),
                        path + ":" + std::to_string(lineNumber) + ": wrong field count");
        rows.push_back(std::move(fields));
    }
    ns3lab::Require(!rows.empty(), "CSV contains no records: " + path);
    return rows;
}

std::vector<TraceRecord>
ReadTrace(const std::string& path)
{
    const auto rows = ReadCsv(path, {"frame_id", "eof_time_ps", "channel", "frame_hex"});
    std::vector<TraceRecord> records;
    std::set<uint64_t> ids;
    for (const auto& fields : rows)
    {
        TraceRecord record;
        record.frameId = std::stoull(fields[0]);
        record.timePs = std::stoull(fields[1]);
        record.channel = std::stoul(fields[2]);
        record.frame = ns3lab::DecodeHex(fields[3]);
        ns3lab::Require(ids.insert(record.frameId).second, "duplicate trace frame ID");
        records.push_back(std::move(record));
    }
    std::sort(records.begin(), records.end(), [](const auto& left, const auto& right) {
        return std::tie(left.timePs, left.frameId) < std::tie(right.timePs, right.frameId);
    });
    return records;
}

std::map<uint64_t, FrameOracle>
ReadFrameOracle(const std::string& path)
{
    const auto rows = ReadCsv(path,
                              {"frame_id",
                               "message_id",
                               "packet_index",
                               "packet_count",
                               "request_id",
                               "qpn",
                               "psn",
                               "opcode",
                               "payload_offset",
                               "payload_length",
                               "expected_frame_hex"});
    std::map<uint64_t, FrameOracle> result;
    for (const auto& fields : rows)
    {
        FrameOracle item;
        item.frameId = std::stoull(fields[0]);
        item.messageId = std::stoul(fields[1]);
        item.packetIndex = std::stoul(fields[2]);
        item.packetCount = std::stoul(fields[3]);
        item.requestId = std::stoul(fields[4]);
        item.qpn = std::stoul(fields[5]);
        item.psn = std::stoul(fields[6]);
        item.opcode = static_cast<uint8_t>(std::stoul(fields[7]));
        item.payloadOffset = std::stoul(fields[8]);
        item.payloadLength = std::stoul(fields[9]);
        item.expectedFrame = ns3lab::DecodeHex(fields[10]);
        ns3lab::Require(result.emplace(item.frameId, std::move(item)).second,
                        "duplicate frame-oracle ID");
    }
    return result;
}

std::map<uint32_t, MessageOracle>
ReadMessageOracle(const std::string& path)
{
    const auto rows = ReadCsv(path,
                              {"message_id",
                               "request_id",
                               "qpn",
                               "start_psn",
                               "remote_addr",
                               "remote_key",
                               "message_length",
                               "expected_payload_hex"});
    std::map<uint32_t, MessageOracle> result;
    for (const auto& fields : rows)
    {
        MessageOracle item;
        item.messageId = std::stoul(fields[0]);
        item.requestId = std::stoul(fields[1]);
        item.qpn = std::stoul(fields[2]);
        item.startPsn = std::stoul(fields[3]);
        item.remoteAddress = std::stoull(fields[4]);
        item.remoteKey = std::stoul(fields[5]);
        item.messageLength = std::stoul(fields[6]);
        item.expectedPayload = ns3lab::DecodeHex(fields[7]);
        ns3lab::Require(result.emplace(item.messageId, std::move(item)).second,
                        "duplicate message-oracle ID");
    }
    return result;
}

void
ValidateTrace(const std::vector<TraceRecord>& trace,
              const std::map<uint64_t, FrameOracle>& frames,
              const std::map<uint32_t, MessageOracle>& messages)
{
    ns3lab::Require(trace.size() == frames.size(), "trace/frame-oracle count mismatch");
    for (const auto& record : trace)
    {
        ns3lab::Require(record.channel < 4, "trace channel is out of range");
        const auto& oracle = frames.at(record.frameId);
        ns3lab::Require(record.frame == oracle.expectedFrame,
                        "trace bytes differ from the Verilator oracle");
        ns3lab::Require(messages.count(oracle.messageId) == 1,
                        "frame references an unknown message");
        const auto parsed = ns3lab::ParseRoceFrame(record.frame);
        ns3lab::Require(parsed.qpn == oracle.qpn, "frame QPN differs from oracle");
        ns3lab::Require(parsed.psn == oracle.psn, "frame PSN differs from oracle");
        ns3lab::Require(parsed.opcode == oracle.opcode, "frame opcode differs from oracle");
        ns3lab::Require(parsed.payload.size() == oracle.payloadLength,
                        "frame payload length differs from oracle");
    }
}

void
ValidateDeliveries(const std::vector<TraceRecord>& trace,
                   const std::map<uint64_t, FrameOracle>& frames,
                   const std::map<uint32_t, MessageOracle>& messages,
                   const std::vector<ns3lab::NetworkEvent>& events)
{
    std::map<uint64_t, const TraceRecord*> traceById;
    for (const auto& item : trace)
    {
        traceById[item.frameId] = &item;
    }

    std::map<uint64_t, const ns3lab::NetworkEvent*> eventById;
    for (const auto& event : events)
    {
        ns3lab::Require(event.kind == ns3lab::NetworkEvent::Kind::DELIVERED,
                        "lossless Lab 6 observed a drop");
        const auto* source = traceById.at(event.frameId);
        ns3lab::Require(event.channel == source->channel, "frame arrived on the wrong path");
        ns3lab::Require(event.frame == source->frame, "network changed frame bytes");
        ns3lab::Require(event.timePs >= source->timePs, "frame arrived before injection");
        ns3lab::Require(eventById.emplace(event.frameId, &event).second,
                        "frame has multiple terminal events");
    }
    ns3lab::Require(eventById.size() == trace.size(), "not every frame was delivered");

    std::map<uint32_t, std::vector<const FrameOracle*>> byMessage;
    for (const auto& [id, frame] : frames)
    {
        (void)id;
        byMessage[frame.messageId].push_back(&frame);
    }
    ns3lab::Require(byMessage.size() == messages.size(), "message count mismatch");

    for (auto& [messageId, packets] : byMessage)
    {
        const auto& message = messages.at(messageId);
        std::sort(packets.begin(), packets.end(), [](const auto* left, const auto* right) {
            return left->packetIndex < right->packetIndex;
        });
        ns3lab::Require(!packets.empty(), "message has no packets");
        ns3lab::Require(packets.size() == packets.front()->packetCount,
                        "message packet count mismatch");

        std::vector<uint8_t> payload;
        for (std::size_t index = 0; index < packets.size(); ++index)
        {
            const auto& oracle = *packets[index];
            ns3lab::Require(oracle.packetIndex == index, "non-contiguous packet index");
            ns3lab::Require(oracle.requestId == message.requestId, "request ID mismatch");
            ns3lab::Require(oracle.qpn == message.qpn, "message QPN mismatch");
            ns3lab::Require(oracle.psn == ((message.startPsn + index) & ns3lab::kPsnMask),
                            "message PSN sequence mismatch");

            const uint8_t expectedOpcode = packets.size() == 1   ? 0x09
                                           : index == 0          ? 0x06
                                           : index + 1 == packets.size() ? 0x08
                                                                          : 0x07;
            ns3lab::Require(oracle.opcode == expectedOpcode, "message opcode sequence mismatch");
            ns3lab::Require(payload.size() == oracle.payloadOffset,
                            "message payload offset mismatch");
            const auto parsed = ns3lab::ParseRoceFrame(eventById.at(oracle.frameId)->frame);
            if (index == 0)
            {
                ns3lab::Require(parsed.messageLength == message.messageLength,
                                "RETH message length mismatch");
                ns3lab::Require(parsed.remoteAddress == message.remoteAddress,
                                "RETH remote address mismatch");
                ns3lab::Require(parsed.rkey == message.remoteKey, "RETH RKEY mismatch");
            }
            payload.insert(payload.end(), parsed.payload.begin(), parsed.payload.end());
        }
        ns3lab::Require(payload.size() == message.messageLength, "message length mismatch");
        ns3lab::Require(payload == message.expectedPayload, "reassembled payload mismatch");
    }
}

void
WriteReport(const std::string& output,
            const std::vector<TraceRecord>& trace,
            const std::map<uint32_t, MessageOracle>& messages,
            const std::vector<ns3lab::NetworkEvent>& events)
{
    ns3lab::PrepareOutput(output);
    std::ofstream report(output);
    report << "{\n"
           << "  \"lab\": 6,\n"
           << "  \"scenario\": \"openrdma-verilator-offline-trace\",\n"
           << "  \"timestamp_boundary\": \"m3_eof_dequeue_ps\",\n"
           << "  \"tx_frames\": " << trace.size() << ",\n"
           << "  \"rx_frames\": " << events.size() << ",\n"
           << "  \"messages\": " << messages.size() << ",\n"
           << "  \"tx\": [";
    for (std::size_t i = 0; i < trace.size(); ++i)
    {
        if (i)
        {
            report << ',';
        }
        report << "{\"id\":" << trace[i].frameId << ",\"time_ps\":" << trace[i].timePs
               << ",\"channel\":" << trace[i].channel << '}';
    }
    report << "],\n  \"events\": [";
    for (std::size_t i = 0; i < events.size(); ++i)
    {
        if (i)
        {
            report << ',';
        }
        report << "{\"sequence\":" << events[i].sequence << ",\"id\":"
               << events[i].frameId << ",\"kind\":\"delivered\",\"channel\":"
               << events[i].channel << ",\"time_ps\":" << events[i].timePs << '}';
    }
    report << "],\n"
           << "  \"icrc_modeled\": false,\n"
           << "  \"fcs_modeled\": false,\n"
           << "  \"status\": \"PASS\"\n"
           << "}\n";
}

} // namespace

int
main(int argc, char* argv[])
{
    std::string tracePath = "../results/lab6-trace.csv";
    std::string frameOraclePath = "../results/lab6-frames.csv";
    std::string messageOraclePath = "../results/lab6-messages.csv";
    std::string output = "../results/lab6.json";
    std::string pcapPrefix = "../results/lab6";

    CommandLine cmd(__FILE__);
    cmd.AddValue("trace", "Verilator frame trace", tracePath);
    cmd.AddValue("frameOracle", "Per-frame reference oracle", frameOraclePath);
    cmd.AddValue("messageOracle", "Per-message reference oracle", messageOraclePath);
    cmd.AddValue("output", "JSON result path", output);
    cmd.AddValue("pcapPrefix", "Outer point-to-point PCAP prefix", pcapPrefix);
    cmd.Parse(argc, argv);

    Time::SetResolution(Time::PS);
    const auto trace = ReadTrace(tracePath);
    const auto frameOracle = ReadFrameOracle(frameOraclePath);
    const auto messageOracle = ReadMessageOracle(messageOraclePath);
    ValidateTrace(trace, frameOracle, messageOracle);

    ns3lab::FourPathRoceNetwork network;
    if (!pcapPrefix.empty())
    {
        network.EnablePcap(pcapPrefix);
    }
    std::vector<ns3lab::FrameTransmission> transmissions;
    transmissions.reserve(trace.size());
    for (const auto& item : trace)
    {
        transmissions.push_back(item);
        network.Schedule(item);
    }

    const uint64_t stopPs = trace.back().timePs +
                            ns3lab::FourPathRoceNetwork::DrainGuardPs(transmissions);
    Simulator::Stop(PicoSeconds(stopPs));
    Simulator::Run();

    ns3lab::Require(network.AcceptedFrames() == trace.size(), "not every frame was accepted");
    ns3lab::Require(network.DroppedFrames() == 0, "lossless Lab 6 dropped a frame");
    ValidateDeliveries(trace, frameOracle, messageOracle, network.Events());
    WriteReport(output, trace, messageOracle, network.Events());

    std::cout << "Lab 6 PASS: frames=" << trace.size() << " messages=" << messageOracle.size()
              << '\n';
    Simulator::Destroy();
    return 0;
}
