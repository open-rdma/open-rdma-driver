// Online TX -> ns-3 -> RX co-simulation hub.
//
// Two Verilator/cocotb stages, two Unix domain sockets, one ns-3 process in
// the middle:
//
//   tx (TX RTL)  --tx.sock-->  online-cosim  --rx.sock-->  rx (RX RTL)
//
// The TX stage declares its oracle (the connection, the per-frame table and
// the per-message table, all built from the independent Python models) and
// then drives a virtual-time barrier: ADVANCE(target) hands over every frame
// whose EOF timestamp falls in the window and asks the hub to run the network
// up to `target`; RESULT carries back the frames that met their fate.  The hub
// checks each injection against the declaration before it schedules it, and
// forwards every delivery to the RX stage the moment it happens, so the RX
// replays arrivals while the TX is still running.
//
// What this program adds over Lab 7's socket co-simulation: the oracle is on
// the wire (Lab 7 had none), the RX is fed by the network instead of observing
// it, and the delivery checks the offline flow ran over CSV files -- byte
// identity, one terminal event per frame, per-path FIFO, message reassembly,
// PSN/opcode sequencing and reorder statistics -- run here as the run unfolds.
//
// The hub writes its report only after every check has passed, exactly like
// the offline program: a run that fails leaves no "status": "PASS" behind.

#include "common/four-path-roce.h"
#include "common/socket-protocol.h"

#include <array>
#include <csignal>
#include <cstddef>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <map>
#include <poll.h>
#include <set>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <tuple>

using namespace ns3;

namespace
{

// ---------------------------------------------------------------------------
// Listening sockets (same shape as Lab 7's: one client per link, 0600, and a
// stale socket file is replaced only if it really is a socket).
// ---------------------------------------------------------------------------

class SocketPath
{
  public:
    explicit SocketPath(std::string path)
        : m_path(std::move(path))
    {
    }

    ~SocketPath()
    {
        if (m_owned)
        {
            ::unlink(m_path.c_str());
        }
    }

    const std::string& Get() const
    {
        return m_path;
    }

    void MarkOwned()
    {
        m_owned = true;
    }

  private:
    std::string m_path;
    bool m_owned{false};
};

int
CreateServer(SocketPath& path)
{
    ns3lab::PrepareOutput(path.Get());
    struct stat status
    {
    };
    if (::lstat(path.Get().c_str(), &status) == 0)
    {
        ns3lab::Require(S_ISSOCK(status.st_mode), "refusing to replace a non-socket path");
        ns3lab::Require(::unlink(path.Get().c_str()) == 0, "cannot remove stale socket");
    }
    else
    {
        ns3lab::Require(errno == ENOENT, "cannot inspect socket path");
    }

    const int fd = ::socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0)
    {
        throw std::runtime_error("cannot create Unix socket");
    }
    sockaddr_un address{};
    address.sun_family = AF_UNIX;
    ns3lab::Require(path.Get().size() < sizeof(address.sun_path), "Unix socket path is too long");
    std::memcpy(address.sun_path, path.Get().c_str(), path.Get().size() + 1);
    const auto length = static_cast<socklen_t>(offsetof(sockaddr_un, sun_path) +
                                               path.Get().size() + 1);
    if (::bind(fd, reinterpret_cast<sockaddr*>(&address), length) != 0)
    {
        ::close(fd);
        throw std::runtime_error("cannot bind Unix socket: " + std::string(std::strerror(errno)));
    }
    path.MarkOwned();
    ::chmod(path.Get().c_str(), 0600);
    if (::listen(fd, 1) != 0)
    {
        ::close(fd);
        throw std::runtime_error("cannot listen on Unix socket");
    }
    return fd;
}

int
Accept(int listener, const std::string& role)
{
    const int client = ::accept(listener, nullptr, nullptr);
    if (client < 0)
    {
        throw std::runtime_error("cannot accept the " + role + " connection");
    }
    return client;
}

// True when the peer has closed its end.  The hub has to tell "this stage is
// done and went away" from "there is a message waiting", and it must not turn
// a normal shutdown into a protocol error.
bool
PeerClosed(int fd)
{
    char probe = 0;
    while (true)
    {
        const ssize_t count = ::recv(fd, &probe, 1, MSG_PEEK);
        if (count >= 0)
        {
            return count == 0;
        }
        if (errno != EINTR)
        {
            throw std::runtime_error("cannot probe socket: " + std::string(std::strerror(errno)));
        }
    }
}

// ---------------------------------------------------------------------------
// The declared oracle
// ---------------------------------------------------------------------------

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

// The L2/L3 identity the TX declared, in the transmitter's orientation.
struct Connection
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

struct NetworkIdentity
{
    uint64_t srcMac{0};
    uint32_t srcIp{0};
    uint64_t dstMac{0};
    uint32_t dstIp{0};

    bool operator==(const NetworkIdentity& other) const
    {
        return std::tie(srcMac, srcIp, dstMac, dstIp) ==
               std::tie(other.srcMac, other.srcIp, other.dstMac, other.dstIp);
    }
};

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

bool
SameIdentity(const NetworkIdentity& left, const Connection& right)
{
    return left.srcMac == right.srcMac && left.srcIp == right.srcIp &&
           left.dstMac == right.dstMac && left.dstIp == right.dstIp;
}

struct DeliveryStats
{
    uint64_t delivered{0};
    uint64_t dropped{0};
    uint64_t messagesComplete{0};
};

struct ReorderStats
{
    uint64_t inversions{0};
    uint64_t maxDepth{0};
    uint32_t maxLateFrames{0};
};

uint64_t
ParseDelayPs(const std::string& text)
{
    std::istringstream stream(text);
    ns3::Time parsed;
    stream >> parsed;
    ns3lab::Require(!stream.fail(), "unparsable link delay: " + text);
    return static_cast<uint64_t>(parsed.GetPicoSeconds());
}

class Hub
{
  public:
    Hub(std::string txPath,
        std::string rxPath,
        ns3lab::FourPathRoceNetwork::LinkConfig config,
        bool lossless,
        std::string output,
        std::string pcapPrefix,
        std::string wireTrace)
        : m_txPath(std::move(txPath)),
          m_rxPath(std::move(rxPath)),
          m_config(std::move(config)),
          m_lossless(lossless),
          m_output(std::move(output)),
          m_pcapPrefix(std::move(pcapPrefix)),
          m_network(m_config)
    {
        if (!wireTrace.empty())
        {
            m_wireTrace.open(wireTrace, std::ios::trunc);
            ns3lab::Require(m_wireTrace.good(), "cannot open the wire trace: " + wireTrace);
            // Flushed per message: the interesting runs to look at afterwards
            // are the ones that ended badly, and a run that ends in an
            // uncaught exception never reaches the stream's destructor.
            m_wireTrace.setf(std::ios::unitbuf);
            ns3lab::wire::TraceStream() = &m_wireTrace;
        }
    }

    void Run()
    {
        m_txListener = CreateServer(m_txPath);
        m_rxListener = CreateServer(m_rxPath);
        if (!m_pcapPrefix.empty())
        {
            m_network.EnablePcap(m_pcapPrefix);
        }
        std::cout << "READY " << m_txPath.Get() << ' ' << m_rxPath.Get() << std::endl;
        m_tx = Accept(m_txListener, "TX");
        m_rx = Accept(m_rxListener, "RX");
        if (m_wireTrace.is_open())
        {
            // The trace names descriptors rather than roles, so record which
            // one is which while both are still known.
            m_wireTrace << "# tx=" << m_tx << " rx=" << m_rx << '\n';
            std::cout << "LINK tx=" << m_tx << " rx=" << m_rx << std::endl;
        }
        Handshake(m_tx, "TX", m_txNextRequestId);
        Handshake(m_rx, "RX", m_rxNextRequestId);

        while (m_tx >= 0 || m_rx >= 0)
        {
            std::array<pollfd, 2> fds{};
            std::array<int, 2> roles{};
            std::size_t count = 0;
            if (m_tx >= 0)
            {
                fds[count] = pollfd{m_tx, POLLIN, 0};
                roles[count++] = 0;
            }
            if (m_rx >= 0)
            {
                fds[count] = pollfd{m_rx, POLLIN, 0};
                roles[count++] = 1;
            }
            const int ready = ::poll(fds.data(), count, -1);
            if (ready < 0)
            {
                if (errno == EINTR)
                {
                    continue;
                }
                throw std::runtime_error("poll failed: " + std::string(std::strerror(errno)));
            }
            for (std::size_t index = 0; index < count; ++index)
            {
                if ((fds[index].revents & (POLLIN | POLLHUP | POLLERR)) == 0)
                {
                    continue;
                }
                // A handler may have closed the other link; the poll set is
                // rebuilt on the next pass, so skip an end that is already
                // gone rather than reading from a closed descriptor.
                try
                {
                    if (roles[index] == 0)
                    {
                        if (m_tx >= 0)
                        {
                            ServiceTx();
                        }
                    }
                    else if (m_rx >= 0)
                    {
                        ServiceRx();
                    }
                }
                catch (const std::exception& error)
                {
                    NotifyPeers(error.what());
                    throw;
                }
            }
        }
    }

  private:
    // -- connection setup ---------------------------------------------------

    void Handshake(int fd, const std::string& role, uint32_t& nextRequestId)
    {
        const auto message = ns3lab::wire::ReadMessage(fd);
        ns3lab::Require(message.type == ns3lab::wire::Type::HELLO,
                        "the " + role + " link did not start with HELLO");
        ns3lab::Require(message.requestId == 0, "HELLO must carry request ID 0");
        ns3lab::wire::Reader reader(message.body);
        ns3lab::Require(reader.U32() == 0, "unsupported HELLO flags");
        reader.Finish();
        nextRequestId = 1;

        ns3lab::wire::Writer body;
        body.U32(0); // flags
        body.U16(4); // channels
        body.U16(1); // time unit: picoseconds
        body.U64(ns3::DataRate(m_config.dataRate).GetBitRate());
        body.U32(ns3lab::wire::kMaxFrameBytes);
        body.U32(ns3lab::wire::kMaxBatchFrames);
        for (const auto& delay : m_config.delays)
        {
            body.U64(ParseDelayPs(delay));
        }
        ns3lab::wire::WriteMessage(fd, ns3lab::wire::Type::HELLO_ACK, 0, body.Data());
    }

    // A failed check ends the run; the stages find out why from this rather
    // than from a socket that went quiet.
    void NotifyPeers(const std::string& text) const
    {
        for (const int fd : {m_tx, m_rx})
        {
            if (fd < 0)
            {
                continue;
            }
            try
            {
                ns3lab::wire::WriteError(fd, 0, 1, text);
            }
            catch (const std::exception&)
            {
                // The link is gone; the exit status still carries the failure.
            }
        }
        std::cerr << "online-cosim: " << text << std::endl;
    }

    // -- the TX link --------------------------------------------------------

    void ServiceTx()
    {
        if (PeerClosed(m_tx))
        {
            ns3lab::Require(m_txFinished, "the TX link closed before FINISH");
            ::close(m_tx);
            m_tx = -1;
            return;
        }
        const auto message = ns3lab::wire::ReadMessage(m_tx);
        ns3lab::Require(message.requestId == m_txNextRequestId,
                        "non-contiguous request ID on the TX link");
        ++m_txNextRequestId;
        switch (message.type)
        {
        case ns3lab::wire::Type::DECLARE:
            Declare(message);
            break;
        case ns3lab::wire::Type::ADVANCE:
            Advance(message);
            break;
        case ns3lab::wire::Type::FINISH:
            Finish(message);
            break;
        default:
            throw std::runtime_error("unexpected message type on the TX link");
        }
    }

    // The TX's oracle.  Everything the hub will later check a frame against
    // comes from here, and it is checked for internal consistency first: a
    // declaration that does not describe a gapless PSN stream or a consistent
    // opcode sequence is rejected before it can be used as a reference.
    void Declare(const ns3lab::wire::Message& message)
    {
        ns3lab::Require(!m_declared, "the TX declared its oracle twice");
        ns3lab::wire::Reader reader(message.body);
        m_connection.srcMac = reader.U64();
        m_connection.srcIp = reader.U32();
        m_connection.dstMac = reader.U64();
        m_connection.dstIp = reader.U32();
        const uint32_t frameCount = reader.U32();
        const uint32_t messageCount = reader.U32();
        ns3lab::Require(frameCount > 0 && frameCount <= ns3lab::wire::kMaxBatchFrames,
                        "implausible frame count in DECLARE");
        ns3lab::Require(messageCount > 0 && messageCount <= ns3lab::wire::kMaxBatchFrames,
                        "implausible message count in DECLARE");
        ns3lab::Require(m_connection.srcMac != 0 && m_connection.dstMac != 0,
                        "the declared connection has an empty MAC address");

        for (uint32_t index = 0; index < frameCount; ++index)
        {
            FrameOracle frame;
            frame.frameId = reader.U64();
            frame.messageId = reader.U32();
            frame.packetIndex = reader.U16();
            frame.packetCount = reader.U16();
            frame.requestId = reader.U32();
            frame.qpn = reader.U32();
            frame.psn = reader.U32();
            frame.opcode = reader.U8();
            reader.Reserved(3);
            frame.payloadOffset = reader.U32();
            frame.payloadLength = reader.U32();
            frame.expectedFrame = reader.Bytes(reader.U32());
            ns3lab::Require(!frame.expectedFrame.empty(), "declared frame is empty");
            ns3lab::Require(frame.packetIndex < frame.packetCount,
                            "frame declares a packet index outside its packet count");
            ns3lab::Require(m_frames.emplace(frame.frameId, std::move(frame)).second,
                            "duplicate frame ID in DECLARE");
        }
        for (uint32_t index = 0; index < messageCount; ++index)
        {
            MessageOracle item;
            item.messageId = reader.U32();
            item.requestId = reader.U32();
            item.qpn = reader.U32();
            item.startPsn = reader.U32();
            item.remoteAddress = reader.U64();
            item.remoteKey = reader.U32();
            item.messageLength = reader.U32();
            item.expectedPayload = reader.Bytes(reader.U32());
            ns3lab::Require(item.expectedPayload.size() == item.messageLength,
                            "declared message payload length disagrees with its length field");
            ns3lab::Require(m_messages.emplace(item.messageId, std::move(item)).second,
                            "duplicate message ID in DECLARE");
        }
        reader.Finish();
        m_declared = true;
        ValidateDeclaration();

        ns3lab::wire::WriteMessage(m_tx, ns3lab::wire::Type::DECLARE_ACK, message.requestId, {});
        // The RX has been waiting for exactly this since it connected: the
        // oracle has to be in its hands before the first frame is.
        ns3lab::wire::WriteMessage(m_rx,
                                   ns3lab::wire::Type::DECLARE,
                                   0,
                                   message.body);
    }

    // The checks the offline flow ran over tx-frames.csv/tx-messages.csv once
    // the frames were in hand; here the tables are complete before the first
    // injection, so they can be made up front.
    void ValidateDeclaration() const
    {
        std::map<uint32_t, std::vector<const FrameOracle*>> byMessage;
        for (const auto& [id, frame] : m_frames)
        {
            (void)id;
            ns3lab::Require(m_messages.count(frame.messageId) == 1,
                            "frame references an unknown message");
            byMessage[frame.messageId].push_back(&frame);
        }
        ns3lab::Require(byMessage.size() == m_messages.size(), "message count mismatch");

        for (const auto& [messageId, packets] : byMessage)
        {
            const auto& message = m_messages.at(messageId);
            ns3lab::Require(!packets.empty(), "message has no packets");
            ns3lab::Require(packets.size() == packets.front()->packetCount,
                            "message packet count mismatch");
            for (const auto* packet : packets)
            {
                ns3lab::Require(packet->requestId == message.requestId, "request ID mismatch");
                ns3lab::Require(packet->qpn == message.qpn, "message QPN mismatch");
                ns3lab::Require(packet->psn ==
                                    ((message.startPsn + packet->packetIndex) & ns3lab::kPsnMask),
                                "message PSN sequence mismatch");
                const uint8_t expectedOpcode = packets.size() == 1 ? 0x09
                                               : packet->packetIndex == 0 ? 0x06
                                               : packet->packetIndex + 1 == packets.size() ? 0x08
                                                                                           : 0x07;
                ns3lab::Require(packet->opcode == expectedOpcode,
                                "message opcode sequence mismatch");
            }
        }
    }

    // One virtual-time window: validate every frame against the declaration,
    // schedule it, run the network to the target, then hand the window's
    // verdicts back to the TX and push the arrivals to the RX.
    void Advance(const ns3lab::wire::Message& message)
    {
        ns3lab::Require(m_declared, "ADVANCE arrived before DECLARE");
        ns3lab::wire::Reader reader(message.body);
        const uint64_t targetPs = reader.U64();
        const uint32_t count = reader.U32();
        ns3lab::Require(count <= ns3lab::wire::kMaxBatchFrames, "too many frames in one ADVANCE");
        const uint64_t nowPs = static_cast<uint64_t>(Simulator::Now().GetPicoSeconds());
        ns3lab::Require(targetPs > nowPs, "ADVANCE target is not in the future");

        std::vector<ns3lab::FrameTransmission> batch;
        batch.reserve(count);
        std::tuple<uint64_t, uint64_t> previous{0, 0};
        bool havePrevious = false;
        for (uint32_t index = 0; index < count; ++index)
        {
            ns3lab::FrameTransmission item;
            item.frameId = reader.U64();
            item.timePs = reader.U64();
            item.channel = reader.U8();
            reader.Reserved(3);
            item.frame = reader.Bytes(reader.U32());
            ns3lab::Require(item.frame.size() <= ns3lab::wire::kMaxFrameBytes,
                            "frame is longer than the protocol allows");
            ns3lab::Require(item.channel < 4, "frame channel is out of range");
            const std::tuple<uint64_t, uint64_t> order{item.timePs, item.frameId};
            ns3lab::Require(!havePrevious || order > previous,
                            "frames in one ADVANCE are not strictly ordered");
            previous = order;
            havePrevious = true;
            ns3lab::Require(item.timePs >= nowPs && item.timePs <= targetPs,
                            "frame timestamp is outside the advance window");
            ns3lab::Require(m_injected.insert(item.frameId).second,
                            "frame was injected twice");
            CheckInjection(item);
            m_txOrder[item.frameId] = static_cast<uint32_t>(m_transmissions.size());
            m_transmissions.push_back(item);
            batch.push_back(item);
        }
        reader.Finish();

        for (const auto& item : batch)
        {
            m_network.Schedule(item);
        }
        Simulator::Stop(PicoSeconds(targetPs - nowPs));
        Simulator::Run();
        m_finalTimePs = targetPs;
        ++m_advances;

        // Everything the window produced goes out in both directions: the
        // events to the TX as the RESULT of its barrier, and to the RX as they
        // happened, one message each.
        std::vector<const ns3lab::NetworkEvent*> fresh;
        for (std::size_t index = m_eventsSent; index < m_network.Events().size(); ++index)
        {
            fresh.push_back(&m_network.Events()[index]);
        }
        m_eventsSent = m_network.Events().size();

        ns3lab::wire::Writer body;
        body.U64(targetPs);
        body.U32(static_cast<uint32_t>(fresh.size()));
        for (const auto* event : fresh)
        {
            AppendEvent(body, *event);
        }
        ns3lab::wire::WriteMessage(m_tx, ns3lab::wire::Type::RESULT, message.requestId, body.Data());
        for (const auto* event : fresh)
        {
            ns3lab::wire::Writer single;
            AppendEvent(single, *event);
            ns3lab::wire::WriteMessage(m_rx, ns3lab::wire::Type::EVENT, 0, single.Data());
        }
    }

    // Everything the offline hub checked about a trace record before injecting
    // it, done here per frame as it arrives.
    void CheckInjection(const ns3lab::FrameTransmission& item)
    {
        const auto found = m_frames.find(item.frameId);
        ns3lab::Require(found != m_frames.end(), "frame was never declared");
        const auto& oracle = found->second;
        ns3lab::Require(item.frame == oracle.expectedFrame,
                        "injected frame bytes differ from the declared oracle");
        const auto parsed = ns3lab::ParseRoceFrame(item.frame);
        ns3lab::Require(parsed.qpn == oracle.qpn, "frame QPN differs from the oracle");
        ns3lab::Require(parsed.psn == oracle.psn, "frame PSN differs from the oracle");
        ns3lab::Require(parsed.opcode == oracle.opcode, "frame opcode differs from the oracle");
        ns3lab::Require(parsed.payload.size() == oracle.payloadLength,
                        "frame payload length differs from the oracle");
        ns3lab::Require(SameIdentity(ParseNetworkIdentity(item.frame), m_connection),
                        "frame addresses differ from the declared connection");
        m_channels.insert(item.channel);
    }

    void AppendEvent(ns3lab::wire::Writer& writer, const ns3lab::NetworkEvent& event) const
    {
        const auto order = m_txOrder.find(event.frameId);
        ns3lab::Require(order != m_txOrder.end(), "event for a frame that was never injected");
        writer.U64(event.sequence);
        writer.U64(event.frameId);
        writer.U32(order->second);
        writer.U8(static_cast<uint8_t>(event.kind));
        writer.U8(static_cast<uint8_t>(event.channel));
        writer.U16(0); // reason; the four-path network has one way to drop
        writer.U64(event.timePs);
        writer.U32(static_cast<uint32_t>(event.frame.size()));
        writer.Bytes(event.frame);
    }

    // -- the RX link --------------------------------------------------------

    void ServiceRx()
    {
        if (PeerClosed(m_rx))
        {
            ns3lab::Require(m_rxFinished, "the RX link closed before FINISH");
            ::close(m_rx);
            m_rx = -1;
            return;
        }
        const auto message = ns3lab::wire::ReadMessage(m_rx);
        ns3lab::Require(message.requestId == m_rxNextRequestId,
                        "non-contiguous request ID on the RX link");
        ++m_rxNextRequestId;
        if (message.type == ns3lab::wire::Type::DECLARE_ACK)
        {
            ns3lab::Require(m_declared, "the RX acknowledged a declaration that was never sent");
            m_rxAcked = true;
            return;
        }
        if (message.type == ns3lab::wire::Type::FINISH)
        {
            ns3lab::wire::Reader reader(message.body);
            m_framesIngested = reader.U64();
            reader.Finish();
            ns3lab::Require(m_txFinished, "the RX finished before the network did");
            const uint64_t delivered = m_network.DeliveredFrames();
            ns3lab::Require(m_framesIngested == delivered,
                            "the RX ingested " + std::to_string(m_framesIngested) +
                                " frames, the network delivered " + std::to_string(delivered));
            ns3lab::wire::WriteMessage(m_rx,
                                       ns3lab::wire::Type::FINISH_ACK,
                                       message.requestId,
                                       FinishStatsBody(delivered));
            m_rxFinished = true;
            return;
        }
        throw std::runtime_error("unexpected message type on the RX link");
    }

    uint64_t LastSequence() const
    {
        return m_network.Events().empty() ? 0 : m_network.Events().back().sequence;
    }

    std::vector<uint8_t> FinishStatsBody(uint64_t delivered) const
    {
        ns3lab::wire::Writer writer;
        writer.U64(m_transmissions.size());
        writer.U64(delivered);
        writer.U64(m_network.DroppedFrames());
        writer.U64(LastSequence());
        writer.U64(m_finalTimePs);
        writer.U64(m_reorder.inversions);
        writer.U64(m_reorder.maxDepth);
        writer.U64(m_delivery.messagesComplete);
        uint32_t channels = 0;
        for (const uint32_t channel : m_channels)
        {
            channels |= 1U << channel;
        }
        writer.U32(channels);
        writer.U64(m_framesIngested);
        return writer.Data();
    }

    // -- end of run ---------------------------------------------------------

    void Finish(const ns3lab::wire::Message& message)
    {
        ns3lab::Require(message.body.empty(), "FINISH carries an unexpected body");
        ns3lab::Require(m_declared, "the TX finished without declaring an oracle");
        ns3lab::Require(m_rxAcked, "the RX never acknowledged the declared oracle");
        ns3lab::Require(m_injected.size() == m_frames.size(),
                        "the TX finished without injecting every declared frame");
        const uint64_t decided = m_network.AcceptedFrames() + m_network.DroppedFrames();
        ns3lab::Require(decided == m_transmissions.size(),
                        "only " + std::to_string(decided) + " of " +
                            std::to_string(m_transmissions.size()) +
                            " frames reached the network");
        if (m_lossless)
        {
            ns3lab::Require(m_network.DroppedFrames() == 0, "lossless online run dropped a frame");
        }
        ns3lab::Require(m_channels == std::set<uint32_t>{0, 1, 2, 3},
                        "the run did not cover all four paths (got " +
                            std::to_string(m_channels.size()) + " of 4)");

        m_delivery = ValidateDeliveries();
        m_reorder = AnalyzeReorder();
        m_txFinished = true;
        WriteReport();

        ns3lab::wire::Writer body;
        body.U64(m_transmissions.size());
        body.U64(m_network.DeliveredFrames());
        body.U64(m_network.DroppedFrames());
        body.U64(LastSequence());
        body.U64(m_finalTimePs);
        ns3lab::wire::WriteMessage(m_rx, ns3lab::wire::Type::EOS, 0, body.Data());
        ns3lab::wire::WriteMessage(m_tx,
                                   ns3lab::wire::Type::FINISH_ACK,
                                   message.requestId,
                                   FinishStatsBody(m_network.DeliveredFrames()));

        std::cout << "Online ns-3 " << (m_lossless ? "PASS" : "PASS (lossy mode)")
                  << ": tx_frames=" << m_transmissions.size()
                  << " rx_frames=" << m_delivery.delivered
                  << " messages=" << m_messages.size()
                  << " complete=" << m_delivery.messagesComplete
                  << " reorder_inversions=" << m_reorder.inversions
                  << " dropped=" << m_network.DroppedFrames()
                  << " advances=" << m_advances << std::endl;
    }

    DeliveryStats ValidateDeliveries()
    {
        const auto& events = m_network.Events();
        DeliveryStats stats;
        std::map<uint64_t, const ns3lab::NetworkEvent*> deliveredById;
        // Highest transmit order seen so far on each path, for the FIFO check.
        std::map<uint32_t, uint32_t> lastOrderOnPath;
        for (const auto& event : events)
        {
            const auto* source = &m_transmissions.at(m_txOrder.at(event.frameId));
            ns3lab::Require(event.channel == source->channel, "frame arrived on the wrong path");
            if (event.kind == ns3lab::NetworkEvent::Kind::DROPPED)
            {
                ns3lab::Require(!m_lossless, "lossless online run observed a drop");
                ++stats.dropped;
                continue;
            }
            ++stats.delivered;
            ns3lab::Require(event.frame == source->frame, "network changed frame bytes");
            ns3lab::Require(event.timePs >= source->timePs, "frame arrived before injection");
            ns3lab::Require(deliveredById.emplace(event.frameId, &event).second,
                            "frame has multiple terminal events");
            // A point-to-point link is FIFO: two frames that share a path
            // cannot swap places, however much the four paths reorder traffic
            // against each other.  This is the check that would catch a
            // "network" that was really one queue with the lane numbers
            // reattached afterwards.
            const uint32_t order = m_txOrder.at(event.frameId);
            const auto [it, inserted] = lastOrderOnPath.emplace(event.channel, order);
            if (!inserted)
            {
                ns3lab::Require(order > it->second, "two frames overtook each other on one path");
                it->second = order;
            }
        }
        if (m_lossless)
        {
            ns3lab::Require(deliveredById.size() == m_transmissions.size(),
                            "not every frame was delivered");
        }
        // Every frame the TX emitted has to end somewhere: delivered, or
        // refused at injection.  A frame with no terminal event is one the
        // network swallowed -- or one the TX finished without running the
        // network far enough past the last injection.
        ns3lab::Require(deliveredById.size() + stats.dropped == m_transmissions.size(),
                        "a frame has no terminal event (was the drain window long enough?)");

        std::map<uint32_t, std::vector<const FrameOracle*>> byMessage;
        for (const auto& [id, frame] : m_frames)
        {
            (void)id;
            byMessage[frame.messageId].push_back(&frame);
        }
        for (auto& [messageId, packets] : byMessage)
        {
            const auto& message = m_messages.at(messageId);
            std::sort(packets.begin(), packets.end(), [](const auto* left, const auto* right) {
                return left->packetIndex < right->packetIndex;
            });
            // Reassembly is only meaningful for a message that arrived whole;
            // with drops allowed, an incomplete message is reported rather
            // than failed.
            const bool complete =
                std::all_of(packets.begin(), packets.end(), [&](const FrameOracle* packet) {
                    return deliveredById.count(packet->frameId) == 1;
                });
            if (!complete)
            {
                ns3lab::Require(!m_lossless, "a message is missing frames in a lossless run");
                continue;
            }

            std::vector<uint8_t> payload;
            for (const auto* oracle : packets)
            {
                ns3lab::Require(payload.size() == oracle->payloadOffset,
                                "message payload offset mismatch");
                const auto parsed = ns3lab::ParseRoceFrame(
                    deliveredById.at(oracle->frameId)->frame);
                if (oracle->packetIndex == 0)
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

    ReorderStats AnalyzeReorder() const
    {
        std::map<uint64_t, uint32_t> arrivalOrder;
        for (const auto& event : m_network.Events())
        {
            if (event.kind == ns3lab::NetworkEvent::Kind::DELIVERED)
            {
                arrivalOrder[event.frameId] = static_cast<uint32_t>(arrivalOrder.size());
            }
        }
        std::vector<uint32_t> order;
        order.reserve(arrivalOrder.size());
        for (const auto& item : m_transmissions)
        {
            const auto found = arrivalOrder.find(item.frameId);
            if (found != arrivalOrder.end())
            {
                order.push_back(found->second);
            }
        }

        ReorderStats stats;
        // O(n^2) is fine for the trace sizes these runs produce.
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

    void WriteReport() const
    {
        ns3lab::PrepareOutput(m_output);
        std::ofstream report(m_output);
        if (!report)
        {
            throw std::runtime_error("cannot write report: " + m_output);
        }
        report << "{\n"
               << "  \"lab\": \"openrdma-online-ns3\",\n"
               << "  \"scenario\": \"openrdma-verilator-online-socket\",\n"
               << "  \"transport\": \"unix-socket\",\n"
               << "  \"tx_socket\": \"" << m_txPath.Get() << "\",\n"
               << "  \"rx_socket\": \"" << m_rxPath.Get() << "\",\n"
               << "  \"timestamp_boundary\": \"m3_eof_dequeue_ps\",\n"
               << "  \"windows\": " << m_advances << ",\n"
               << "  \"tx_frames\": " << m_transmissions.size() << ",\n"
               << "  \"rx_frames\": " << m_delivery.delivered << ",\n"
               << "  \"rx_events\": " << m_network.Events().size() << ",\n"
               << "  \"messages\": " << m_messages.size() << ",\n"
               << "  \"messages_complete\": " << m_delivery.messagesComplete << ",\n"
               << "  \"accepted_frames\": " << m_network.AcceptedFrames() << ",\n"
               << "  \"delivered_frames\": " << m_network.DeliveredFrames() << ",\n"
               << "  \"dropped_frames\": " << m_network.DroppedFrames() << ",\n"
               << "  \"lossless\": " << (m_lossless ? "true" : "false") << ",\n"
               << "  \"reorder_inversions\": " << m_reorder.inversions << ",\n"
               << "  \"reorder_max_depth\": " << m_reorder.maxDepth << ",\n"
               << "  \"reordered\": " << (m_reorder.inversions == 0 ? "false" : "true") << ",\n"
               << "  \"final_time_ps\": " << m_finalTimePs << ",\n"
               << "  \"tx\": [";
        for (std::size_t i = 0; i < m_transmissions.size(); ++i)
        {
            if (i)
            {
                report << ',';
            }
            report << "{\"id\":" << m_transmissions[i].frameId
                   << ",\"time_ps\":" << m_transmissions[i].timePs
                   << ",\"channel\":" << m_transmissions[i].channel << '}';
        }
        report << "],\n  \"events\": [";
        const auto& events = m_network.Events();
        for (std::size_t i = 0; i < events.size(); ++i)
        {
            if (i)
            {
                report << ',';
            }
            report << "{\"sequence\":" << events[i].sequence << ",\"id\":" << events[i].frameId
                   << ",\"kind\":\""
                   << (events[i].kind == ns3lab::NetworkEvent::Kind::DELIVERED ? "delivered"
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

    SocketPath m_txPath;
    SocketPath m_rxPath;
    ns3lab::FourPathRoceNetwork::LinkConfig m_config;
    bool m_lossless{true};
    std::string m_output;
    std::string m_pcapPrefix;
    std::ofstream m_wireTrace;
    ns3lab::FourPathRoceNetwork m_network;
    int m_txListener{-1};
    int m_rxListener{-1};
    int m_tx{-1};
    int m_rx{-1};

    uint32_t m_txNextRequestId{1};
    uint32_t m_rxNextRequestId{1};
    bool m_declared{false};
    bool m_rxAcked{false};
    bool m_txFinished{false};
    bool m_rxFinished{false};

    Connection m_connection;
    std::map<uint64_t, FrameOracle> m_frames;
    std::map<uint32_t, MessageOracle> m_messages;
    std::map<uint64_t, uint32_t> m_txOrder;
    std::set<uint64_t> m_injected;
    std::set<uint32_t> m_channels;
    std::vector<ns3lab::FrameTransmission> m_transmissions;
    std::size_t m_eventsSent{0};
    uint64_t m_finalTimePs{0};
    uint64_t m_advances{0};
    uint64_t m_framesIngested{0};
    DeliveryStats m_delivery;
    ReorderStats m_reorder;
};

} // namespace

int
main(int argc, char* argv[])
{
    std::string txSocket = "../results/sockets/tx.sock";
    std::string rxSocket = "../results/sockets/rx.sock";
    std::string output = "../results/online-ns3.json";
    std::string pcapPrefix = "../results/online-ns3";
    std::string wireTrace;
    std::string dataRate = "100Gbps";
    std::string queueSize = "100p";
    bool lossless = true;

    CommandLine cmd(__FILE__);
    cmd.AddValue("txSocket", "Unix socket the TX stage connects to", txSocket);
    cmd.AddValue("rxSocket", "Unix socket the RX stage connects to", rxSocket);
    cmd.AddValue("output", "JSON result path", output);
    cmd.AddValue("pcapPrefix", "Outer point-to-point PCAP prefix (empty disables)", pcapPrefix);
    cmd.AddValue("wireTrace",
                 "Append every protocol message, in order, to this file "
                 "(empty disables); the manual's protocol chapter reads it",
                 wireTrace);
    cmd.AddValue("dataRate", "Per-path link rate", dataRate);
    cmd.AddValue("queueSize", "Per-path DropTail queue size", queueSize);
    cmd.AddValue("lossless",
                 "Fail when the network drops a frame; --lossless=0 allows drops "
                 "(the RX stage cannot replay a lossy run)",
                 lossless);
    cmd.Parse(argc, argv);

    Time::SetResolution(Time::PS);
    std::signal(SIGPIPE, SIG_IGN);

    ns3lab::FourPathRoceNetwork::LinkConfig config;
    config.dataRate = dataRate;
    config.queueSize = queueSize;
    Hub hub(txSocket, rxSocket, config, lossless, output, pcapPrefix, wireTrace);
    hub.Run();
    Simulator::Destroy();
    return 0;
}
