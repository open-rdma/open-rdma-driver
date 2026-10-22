// Offline TX -> ns-3 -> RX interconnect.
//
// The TX Verilator/cocotb run writes a frame trace (frame_id, eof_time_ps,
// channel, frame_hex), a per-frame/per-message oracle and the declared L2/L3
// connection.  This program replays the trace over a four-path point-to-point
// network and writes the arrival-ordered receive trace that the RX
// Verilator/cocotb run consumes.
//
//   tx-trace.csv --[four-path ns-3]--> rx-trace.csv
//
// The timestamp boundary on the transmit side is the M3 EOF dequeue, so the
// network boundary is store-and-forward: a frame enters the network only once
// its last beat has been taken out of the channel buffer.

#include "common/four-path-roce.h"

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
    uint32_t channel{0};
};

// The L2/L3 identity the TX declared, in the transmitter's orientation.
struct Connection
{
    uint64_t srcMac{0};
    uint32_t srcIp{0};
    uint64_t dstMac{0};
    uint32_t dstIp{0};
};

// The same identity as carried by one frame.
struct NetworkIdentity
{
    uint64_t srcMac{0};
    uint32_t srcIp{0};
    uint64_t dstMac{0};
    uint32_t dstIp{0};
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
                               "expected_frame_hex",
                               "channel"});
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
        item.channel = std::stoul(fields[11]);
        ns3lab::Require(result.emplace(item.frameId, std::move(item)).second,
                        "duplicate frame-oracle ID");
    }
    return result;
}

Connection
ReadConnection(const std::string& path)
{
    const auto rows = ReadCsv(path, {"src_mac", "src_ip", "dst_mac", "dst_ip"});
    // The CSV is written with one row; extra rows would make "the declared
    // connection" ambiguous, so refuse them instead of silently picking one.
    ns3lab::Require(rows.size() == 1, "connection file must hold exactly one row");
    const auto& fields = rows.front();
    Connection connection;
    connection.srcMac = std::stoull(fields[0], nullptr, 16);
    connection.srcIp = static_cast<uint32_t>(std::stoul(fields[1], nullptr, 16));
    connection.dstMac = std::stoull(fields[2], nullptr, 16);
    connection.dstIp = static_cast<uint32_t>(std::stoul(fields[3], nullptr, 16));
    return connection;
}

NetworkIdentity
ParseNetworkIdentity(const std::vector<uint8_t>& frame)
{
    ns3lab::Require(frame.size() >= 34, "frame is too short to carry a network identity");
    NetworkIdentity identity;
    identity.dstMac = (static_cast<uint64_t>(ns3lab::Read16(frame, 0)) << 32) |
                      ns3lab::Read32(frame, 2);
    identity.srcMac = (static_cast<uint64_t>(ns3lab::Read16(frame, 6)) << 32) |
                      ns3lab::Read32(frame, 8);
    identity.srcIp = ns3lab::Read32(frame, 26);
    identity.dstIp = ns3lab::Read32(frame, 30);
    return identity;
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
              const std::map<uint32_t, MessageOracle>& messages,
              const Connection& connection)
{
    ns3lab::Require(trace.size() == frames.size(), "trace/frame-oracle count mismatch");
    std::set<uint32_t> channels;
    for (const auto& record : trace)
    {
        ns3lab::Require(record.channel < 4, "trace channel is out of range");
        const auto& oracle = frames.at(record.frameId);
        ns3lab::Require(record.frame == oracle.expectedFrame,
                        "trace bytes differ from the Verilator oracle");
        // The traffic has to be spread over the paths, and the trace has to
        // still agree with what the TX stage recorded about its own scheduler.
        // Both columns come from the TX run, so a trace whose channels were
        // flattened on the way here (or a workload that only ever used one
        // path) cannot pass unnoticed.
        ns3lab::Require(record.channel == oracle.channel,
                        "trace channel differs from the TX frame oracle");
        channels.insert(record.channel);
        ns3lab::Require(messages.count(oracle.messageId) == 1,
                        "frame references an unknown message");
        const auto parsed = ns3lab::ParseRoceFrame(record.frame);
        ns3lab::Require(parsed.qpn == oracle.qpn, "frame QPN differs from oracle");
        ns3lab::Require(parsed.psn == oracle.psn, "frame PSN differs from oracle");
        ns3lab::Require(parsed.opcode == oracle.opcode, "frame opcode differs from oracle");
        ns3lab::Require(parsed.payload.size() == oracle.payloadLength,
                        "frame payload length differs from oracle");

        // Address filtering is the RX's job, but a frame that does not carry
        // the declared connection has already gone wrong here.
        const auto identity = ParseNetworkIdentity(record.frame);
        ns3lab::Require(identity.srcMac == connection.srcMac &&
                            identity.dstMac == connection.dstMac &&
                            identity.srcIp == connection.srcIp &&
                            identity.dstIp == connection.dstIp,
                        "frame addresses differ from the declared connection");
    }
    ns3lab::Require(channels == std::set<uint32_t>{0, 1, 2, 3},
                    "the trace does not cover all four paths (got " +
                        std::to_string(channels.size()) + " of 4)");
}

// What the network actually did with the trace, once drops are allowed.
struct DeliveryStats
{
    uint64_t delivered{0};
    uint64_t dropped{0};
    uint64_t messagesComplete{0};
};

DeliveryStats
ValidateDeliveries(const std::vector<TraceRecord>& trace,
                   const std::map<uint64_t, FrameOracle>& frames,
                   const std::map<uint32_t, MessageOracle>& messages,
                   const std::vector<ns3lab::NetworkEvent>& events,
                   bool lossless)
{
    std::map<uint64_t, const TraceRecord*> traceById;
    std::map<uint64_t, uint32_t> traceOrder;
    for (std::size_t i = 0; i < trace.size(); ++i)
    {
        traceById[trace[i].frameId] = &trace[i];
        traceOrder[trace[i].frameId] = static_cast<uint32_t>(i);
    }

    DeliveryStats stats;
    std::map<uint64_t, const ns3lab::NetworkEvent*> deliveredById;
    // Highest transmit order seen so far on each path, for the FIFO check.
    std::map<uint32_t, uint32_t> lastOrderOnPath;
    for (const auto& event : events)
    {
        const auto* source = traceById.at(event.frameId);
        ns3lab::Require(event.channel == source->channel, "frame arrived on the wrong path");
        if (event.kind == ns3lab::NetworkEvent::Kind::DROPPED)
        {
            ns3lab::Require(!lossless, "lossless offline run observed a drop");
            ++stats.dropped;
            continue;
        }
        ++stats.delivered;
        ns3lab::Require(event.frame == source->frame, "network changed frame bytes");
        ns3lab::Require(event.timePs >= source->timePs, "frame arrived before injection");
        ns3lab::Require(deliveredById.emplace(event.frameId, &event).second,
                        "frame has multiple terminal events");
        // A point-to-point link is FIFO: two frames that share a path cannot
        // swap places, however much the four paths reorder traffic against
        // each other.  This is the check that would catch a "network" that was
        // really one queue with the lane numbers reattached afterwards.
        const uint32_t order = traceOrder.at(event.frameId);
        const auto [it, inserted] = lastOrderOnPath.emplace(event.channel, order);
        if (!inserted)
        {
            ns3lab::Require(order > it->second, "two frames overtook each other on one path");
            it->second = order;
        }
    }
    if (lossless)
    {
        ns3lab::Require(deliveredById.size() == trace.size(), "not every frame was delivered");
    }
    // Every frame the TX emitted has to end somewhere: delivered, or refused at
    // injection.  A frame with no terminal event is one the network swallowed,
    // which is what an in-link queue drop would look like from here.
    ns3lab::Require(deliveredById.size() + stats.dropped == trace.size(),
                    "a frame has no terminal event");

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

        // Reassembly is only meaningful for a message that arrived whole; with
        // drops allowed, an incomplete message is reported rather than failed.
        const bool complete =
            std::all_of(packets.begin(), packets.end(), [&](const FrameOracle* packet) {
                return deliveredById.count(packet->frameId) == 1;
            });
        if (!complete)
        {
            ns3lab::Require(!lossless, "a message is missing frames in a lossless run");
            continue;
        }

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
            const auto parsed =
                ns3lab::ParseRoceFrame(deliveredById.at(oracle.frameId)->frame);
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
        ++stats.messagesComplete;
    }
    return stats;
}

// The RX replay cares about the order in which the four lanes see traffic, so
// the receive trace is written in arrival order and keeps the transmit order
// as a separate column.  Dropped frames are not written: they never reached the
// receiver, and handing them to the replay would invent traffic.
void
WriteRxTrace(const std::string& path,
             const std::vector<TraceRecord>& trace,
             const std::vector<ns3lab::NetworkEvent>& events)
{
    ns3lab::PrepareOutput(path);
    std::ofstream stream(path);
    if (!stream)
    {
        throw std::runtime_error("cannot write RX trace: " + path);
    }
    std::map<uint64_t, uint32_t> txOrder;
    for (std::size_t i = 0; i < trace.size(); ++i)
    {
        txOrder[trace[i].frameId] = static_cast<uint32_t>(i);
    }

    stream << "sequence,frame_id,tx_order,channel,arrival_time_ps,frame_hex\n";
    uint64_t sequence = 0;
    for (const auto& event : events)
    {
        if (event.kind != ns3lab::NetworkEvent::Kind::DELIVERED)
        {
            continue;
        }
        stream << sequence++ << ',' << event.frameId << ',' << txOrder.at(event.frameId) << ','
               << event.channel << ',' << event.timePs << ',' << ns3lab::EncodeHex(event.frame)
               << '\n';
    }
}

struct ReorderStats
{
    uint64_t inversions{0};
    uint64_t maxDepth{0};
    uint32_t maxLateFrames{0};
};

// Counts pairwise inversions between transmit order and arrival order, i.e. how
// often a later frame overtakes an earlier one.  Frames that never arrived have
// no place in either order and are skipped, so the number stays meaningful when
// drops are allowed.
ReorderStats
AnalyzeReorder(const std::vector<TraceRecord>& trace,
               const std::vector<ns3lab::NetworkEvent>& events)
{
    std::map<uint64_t, uint32_t> arrivalOrder;
    for (const auto& event : events)
    {
        if (event.kind == ns3lab::NetworkEvent::Kind::DELIVERED)
        {
            arrivalOrder[event.frameId] = static_cast<uint32_t>(arrivalOrder.size());
        }
    }

    std::vector<uint32_t> order;
    order.reserve(arrivalOrder.size());
    for (const auto& item : trace)
    {
        const auto found = arrivalOrder.find(item.frameId);
        if (found != arrivalOrder.end())
        {
            order.push_back(found->second);
        }
    }

    ReorderStats stats;
    // O(n^2) is fine for the trace sizes these offline runs produce.
    for (std::size_t i = 0; i < order.size(); ++i)
    {
        uint32_t late = 0;
        for (std::size_t j = i + 1; j < order.size(); ++j)
        {
            if (order[j] < order[i])
            {
                ++stats.inversions;
                ++late;
            }
        }
        stats.maxLateFrames = std::max(stats.maxLateFrames, late);
    }
    // Reorder depth: how far a frame travels backwards in arrival order.
    for (std::size_t i = 0; i < order.size(); ++i)
    {
        for (std::size_t j = i + 1; j < order.size(); ++j)
        {
            if (order[j] < order[i])
            {
                stats.maxDepth = std::max<uint64_t>(stats.maxDepth, order[i] - order[j]);
            }
        }
    }
    return stats;
}

void
WriteReport(const std::string& output,
            const std::vector<TraceRecord>& trace,
            const std::map<uint32_t, MessageOracle>& messages,
            const std::vector<ns3lab::NetworkEvent>& events,
            const ns3lab::FourPathRoceNetwork& network,
            const ReorderStats& stats,
            const DeliveryStats& delivery,
            bool lossless)
{
    ns3lab::PrepareOutput(output);
    std::ofstream report(output);
    report << "{\n"
           << "  \"lab\": \"openrdma-offline-ns3\",\n"
           << "  \"scenario\": \"openrdma-verilator-offline-trace\",\n"
           << "  \"timestamp_boundary\": \"m3_eof_dequeue_ps\",\n"
           << "  \"tx_frames\": " << trace.size() << ",\n"
           << "  \"rx_frames\": " << delivery.delivered << ",\n"
           << "  \"rx_events\": " << events.size() << ",\n"
           << "  \"messages\": " << messages.size() << ",\n"
           << "  \"messages_complete\": " << delivery.messagesComplete << ",\n"
           << "  \"accepted_frames\": " << network.AcceptedFrames() << ",\n"
           << "  \"delivered_frames\": " << network.DeliveredFrames() << ",\n"
           << "  \"dropped_frames\": " << network.DroppedFrames() << ",\n"
           << "  \"lossless\": " << (lossless ? "true" : "false") << ",\n"
           << "  \"reorder_inversions\": " << stats.inversions << ",\n"
           << "  \"reorder_max_depth\": " << stats.maxDepth << ",\n"
           << "  \"reordered\": " << (stats.inversions == 0 ? "false" : "true") << ",\n"
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
        report << "{\"sequence\":" << events[i].sequence << ",\"id\":" << events[i].frameId
               << ",\"kind\":\"" << (events[i].kind == ns3lab::NetworkEvent::Kind::DELIVERED
                                         ? "delivered"
                                         : "dropped")
               << "\",\"channel\":" << events[i].channel
               << ",\"time_ps\":" << events[i].timePs << '}';
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
    std::string txTracePath = "../results/tx-trace.csv";
    std::string frameOraclePath = "../results/tx-frames.csv";
    std::string messageOraclePath = "../results/tx-messages.csv";
    std::string connectionPath = "../results/tx-connection.csv";
    std::string rxTracePath = "../results/rx-trace.csv";
    std::string output = "../results/ns3-offline.json";
    std::string pcapPrefix = "../results/ns3-offline";
    std::string dataRate = "100Gbps";
    std::string queueSize = "100p";
    bool lossless = true;

    CommandLine cmd(__FILE__);
    cmd.AddValue("txTrace", "Verilator TX frame trace", txTracePath);
    cmd.AddValue("frameOracle", "Per-frame reference oracle", frameOraclePath);
    cmd.AddValue("messageOracle", "Per-message reference oracle", messageOraclePath);
    cmd.AddValue("connection", "Declared L2/L3 identity of the connection", connectionPath);
    cmd.AddValue("rxTrace", "Arrival-ordered RX trace to write", rxTracePath);
    cmd.AddValue("output", "JSON result path", output);
    cmd.AddValue("pcapPrefix", "Outer point-to-point PCAP prefix", pcapPrefix);
    cmd.AddValue("dataRate", "Per-path link rate", dataRate);
    cmd.AddValue("queueSize", "Per-path DropTail queue size", queueSize);
    cmd.AddValue("lossless",
                 "Fail when the network drops a frame; --lossless=0 allows drops "
                 "(the RX replay stage still expects a lossless trace)",
                 lossless);
    cmd.Parse(argc, argv);

    Time::SetResolution(Time::PS);
    const auto trace = ReadTrace(txTracePath);
    const auto frameOracle = ReadFrameOracle(frameOraclePath);
    const auto messageOracle = ReadMessageOracle(messageOraclePath);
    const auto connection = ReadConnection(connectionPath);
    ValidateTrace(trace, frameOracle, messageOracle, connection);

    ns3lab::FourPathRoceNetwork::LinkConfig config;
    config.dataRate = dataRate;
    config.queueSize = queueSize;
    ns3lab::FourPathRoceNetwork network(config);
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

    const uint64_t stopPs = trace.back().timePs + network.DrainGuardPs(transmissions);
    Simulator::Stop(PicoSeconds(stopPs));
    Simulator::Run();

    // Frames are refused at injection when a path's queue is full, so with
    // drops allowed the invariant is that every scheduled frame got a verdict,
    // not that every one was accepted.
    const uint64_t decided = network.AcceptedFrames() + network.DroppedFrames();
    ns3lab::Require(decided == trace.size(),
                    "only " + std::to_string(decided) + " of " + std::to_string(trace.size()) +
                        " frames reached the network");
    if (lossless)
    {
        ns3lab::Require(network.DroppedFrames() == 0, "lossless offline run dropped a frame");
    }
    const auto delivery =
        ValidateDeliveries(trace, frameOracle, messageOracle, network.Events(), lossless);

    const auto stats = AnalyzeReorder(trace, network.Events());
    WriteRxTrace(rxTracePath, trace, network.Events());
    WriteReport(output, trace, messageOracle, network.Events(), network, stats, delivery, lossless);

    std::cout << "Offline ns-3 " << (lossless ? "PASS" : "PASS (lossy mode)")
              << ": tx_frames=" << trace.size() << " rx_frames=" << delivery.delivered
              << " messages=" << messageOracle.size()
              << " complete=" << delivery.messagesComplete
              << " reorder_inversions=" << stats.inversions
              << " dropped=" << network.DroppedFrames() << '\n';
    Simulator::Destroy();
    return 0;
}
