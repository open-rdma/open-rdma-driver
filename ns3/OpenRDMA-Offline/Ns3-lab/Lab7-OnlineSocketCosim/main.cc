#include "../common/four-path-roce.h"
#include "../common/socket-protocol.h"

#include <csignal>
#include <cstddef>
#include <filesystem>
#include <iostream>
#include <set>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>

using namespace ns3;

namespace
{

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

class Server
{
  public:
    explicit Server(int client)
        : m_client(client)
    {
    }

    void Run()
    {
        Hello();
        while (true)
        {
            const auto message = ns3lab::wire::ReadMessage(m_client);
            ns3lab::Require(message.requestId == m_nextRequestId,
                            "non-contiguous protocol request ID");
            ++m_nextRequestId;
            if (message.type == ns3lab::wire::Type::ADVANCE)
            {
                Advance(message);
            }
            else if (message.type == ns3lab::wire::Type::FINISH)
            {
                Finish(message);
                return;
            }
            else
            {
                throw std::runtime_error("unexpected protocol message type");
            }
        }
    }

  private:
    void Hello()
    {
        const auto message = ns3lab::wire::ReadMessage(m_client);
        ns3lab::Require(message.type == ns3lab::wire::Type::HELLO,
                        "first protocol message is not HELLO");
        ns3lab::Require(message.requestId == 0, "HELLO request ID must be zero");
        ns3lab::wire::Reader reader(message.body);
        ns3lab::Require(reader.U32() == 0, "unsupported HELLO flags");
        reader.Finish();

        ns3lab::wire::Writer body;
        body.U32(0);                    // server flags
        body.U16(4);                    // channels
        body.U16(1);                    // timestamp unit: picoseconds
        body.U64(100'000'000'000ULL);   // link bit rate
        body.U32(ns3lab::wire::kMaxFrameBytes);
        body.U32(ns3lab::wire::kMaxBatchFrames);
        for (uint64_t delay : {200'000ULL, 400'000ULL, 800'000ULL, 2'000'000ULL})
        {
            body.U64(delay);
        }
        ns3lab::wire::WriteMessage(
            m_client, ns3lab::wire::Type::HELLO_ACK, message.requestId, body.Data());
        m_nextRequestId = 1;
    }

    void Advance(const ns3lab::wire::Message& message)
    {
        ns3lab::wire::Reader reader(message.body);
        const uint64_t targetPs = reader.U64();
        const uint32_t count = reader.U32();
        ns3lab::Require(count <= ns3lab::wire::kMaxBatchFrames, "ADVANCE batch is too large");
        const uint64_t nowPs = static_cast<uint64_t>(Simulator::Now().GetPicoSeconds());
        ns3lab::Require(targetPs > nowPs, "ADVANCE target must move time forward");

        std::vector<ns3lab::FrameTransmission> frames;
        frames.reserve(count);
        std::pair<uint64_t, uint64_t> previous{0, 0};
        bool first = true;
        for (uint32_t index = 0; index < count; ++index)
        {
            ns3lab::FrameTransmission frame;
            frame.frameId = reader.U64();
            frame.timePs = reader.U64();
            frame.channel = reader.U8();
            reader.Reserved(3);
            const uint32_t frameLength = reader.U32();
            ns3lab::Require(frameLength > 0 && frameLength <= ns3lab::wire::kMaxFrameBytes,
                            "invalid frame length");
            frame.frame = reader.Bytes(frameLength);
            ns3lab::Require(frame.channel < 4, "frame channel is out of range");
            ns3lab::Require(frame.timePs >= nowPs && frame.timePs <= targetPs,
                            "frame timestamp is outside the ADVANCE window");
            ns3lab::Require(m_frameIds.insert(frame.frameId).second, "duplicate frame ID");
            const auto order = std::make_pair(frame.timePs, frame.frameId);
            ns3lab::Require(first || previous < order, "ADVANCE frames are not sorted");
            previous = order;
            first = false;
            ns3lab::ParseRoceFrame(frame.frame);
            frames.push_back(std::move(frame));
        }
        reader.Finish();

        for (const auto& frame : frames)
        {
            m_network.Schedule(frame);
            m_outstanding.insert(frame.frameId);
        }
        m_submitted += frames.size();
        Simulator::Stop(PicoSeconds(targetPs - nowPs));
        Simulator::Run();

        ns3lab::wire::Writer body;
        body.U64(static_cast<uint64_t>(Simulator::Now().GetPicoSeconds()));
        const auto& allEvents = m_network.Events();
        body.U32(static_cast<uint32_t>(allEvents.size() - m_eventCursor));
        for (; m_eventCursor < allEvents.size(); ++m_eventCursor)
        {
            const auto& event = allEvents[m_eventCursor];
            ns3lab::Require(m_outstanding.erase(event.frameId) == 1,
                            "terminal event has no outstanding frame");
            body.U64(event.sequence);
            body.U64(event.frameId);
            body.U8(static_cast<uint8_t>(event.kind));
            body.U8(static_cast<uint8_t>(event.channel));
            body.U16(event.kind == ns3lab::NetworkEvent::Kind::DROPPED ? 1 : 0);
            body.U64(event.timePs);
            body.U32(static_cast<uint32_t>(event.frame.size()));
            body.Bytes(event.frame);
        }
        ns3lab::wire::WriteMessage(
            m_client, ns3lab::wire::Type::RESULT, message.requestId, body.Data());
    }

    void Finish(const ns3lab::wire::Message& message)
    {
        ns3lab::wire::Reader reader(message.body);
        reader.Finish();
        ns3lab::Require(m_outstanding.empty(), "FINISH received with frames still in flight");
        ns3lab::wire::Writer body;
        body.U64(m_submitted);
        body.U64(m_network.DeliveredFrames());
        body.U64(m_network.DroppedFrames());
        body.U64(m_network.Events().empty() ? 0 : m_network.Events().back().sequence);
        body.U64(static_cast<uint64_t>(Simulator::Now().GetPicoSeconds()));
        ns3lab::wire::WriteMessage(
            m_client, ns3lab::wire::Type::FINISH_ACK, message.requestId, body.Data());
    }

    int m_client{-1};
    uint32_t m_nextRequestId{1};
    ns3lab::FourPathRoceNetwork m_network;
    std::set<uint64_t> m_frameIds;
    std::set<uint64_t> m_outstanding;
    std::size_t m_eventCursor{0};
    uint64_t m_submitted{0};
};

} // namespace

int
main(int argc, char* argv[])
{
    std::string socketPath = "../results/lab7.sock";
    CommandLine cmd(__FILE__);
    cmd.AddValue("socket", "Unix-domain socket path", socketPath);
    cmd.Parse(argc, argv);

    std::signal(SIGPIPE, SIG_IGN);
    Time::SetResolution(Time::PS);
    SocketPath path(socketPath);
    int serverFd = -1;
    int clientFd = -1;
    try
    {
        serverFd = CreateServer(path);
        std::cout << "READY " << path.Get() << std::endl;
        clientFd = ::accept(serverFd, nullptr, nullptr);
        if (clientFd < 0)
        {
            throw std::runtime_error("accept failed");
        }
        Server(clientFd).Run();
        ::close(clientFd);
        ::close(serverFd);
        Simulator::Destroy();
        std::cout << "Lab 7 server PASS" << std::endl;
        return 0;
    }
    catch (const std::exception& error)
    {
        if (clientFd >= 0)
        {
            try
            {
                ns3lab::wire::WriteError(clientFd, 0, 1, error.what());
            }
            catch (...)
            {
            }
            ::close(clientFd);
        }
        if (serverFd >= 0)
        {
            ::close(serverFd);
        }
        Simulator::Destroy();
        std::cerr << "Lab 7 server ERROR: " << error.what() << std::endl;
        return 1;
    }
}
