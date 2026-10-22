#include "../common/lab-common.h"

#include "ns3/flow-monitor-helper.h"
#include "ns3/point-to-point-module.h"
#include "ns3/traffic-control-module.h"

#include <fstream>
#include <iostream>
#include <vector>

using namespace ns3;

namespace
{

class QueueProbe
{
  public:
    void DepthChanged(uint32_t, uint32_t newDepth)
    {
        m_maxDepth = std::max(m_maxDepth, newDepth);
        m_samples.emplace_back(Simulator::Now().GetNanoSeconds(), newDepth);
    }

    void PacketDropped(Ptr<const QueueDiscItem>)
    {
        ++m_drops;
    }

    uint32_t MaxDepth() const
    {
        return m_maxDepth;
    }

    uint32_t Drops() const
    {
        return m_drops;
    }

    void WriteCsv(const std::string& path) const
    {
        ns3lab::PrepareOutput(path);
        std::ofstream csv(path);
        csv << "time_ns,packets_in_queue\n";
        for (const auto& [timeNs, depth] : m_samples)
        {
            csv << timeNs << ',' << depth << '\n';
        }
    }

  private:
    uint32_t m_maxDepth{0};
    uint32_t m_drops{0};
    std::vector<std::pair<int64_t, uint32_t>> m_samples;
};

} // namespace

int
main(int argc, char* argv[])
{
    std::string offeredRate = "40Gbps";
    std::string bottleneckRate = "25Gbps";
    std::string output = "../results/lab2.json";
    std::string queueTrace = "../results/lab2-queue.csv";
    uint32_t packetSize = 1200;
    uint32_t packetCount = 2000;
    uint32_t queuePackets = 32;

    CommandLine cmd(__FILE__);
    cmd.AddValue("offeredRate", "Application offered rate", offeredRate);
    cmd.AddValue("bottleneckRate", "Router-to-receiver link rate", bottleneckRate);
    cmd.AddValue("packetSize", "UDP payload size, including the lab header", packetSize);
    cmd.AddValue("packetCount", "Number of packets to offer", packetCount);
    cmd.AddValue("queuePackets", "Bottleneck device queue capacity", queuePackets);
    cmd.AddValue("output", "JSON result path", output);
    cmd.AddValue("queueTrace", "Queue depth CSV path", queueTrace);
    cmd.Parse(argc, argv);

    NodeContainer nodes;
    nodes.Create(3);
    auto sender = nodes.Get(0);
    auto router = nodes.Get(1);
    auto receiver = nodes.Get(2);

    PointToPointHelper fastLink;
    fastLink.SetDeviceAttribute("DataRate", StringValue("100Gbps"));
    fastLink.SetChannelAttribute("Delay", StringValue("100ns"));
    const auto fastDevices = fastLink.Install(sender, router);

    PointToPointHelper slowLink;
    slowLink.SetDeviceAttribute("DataRate", StringValue(bottleneckRate));
    slowLink.SetChannelAttribute("Delay", StringValue("1us"));
    slowLink.SetQueue("ns3::DropTailQueue", "MaxSize", StringValue("1p"));
    const auto slowDevices = slowLink.Install(router, receiver);

    InternetStackHelper internet;
    internet.Install(nodes);

    Ipv4AddressHelper addresses;
    addresses.SetBase("10.2.1.0", "255.255.255.0");
    addresses.Assign(fastDevices);
    addresses.SetBase("10.2.2.0", "255.255.255.0");
    const auto receiverInterfaces = addresses.Assign(slowDevices);
    Ipv4GlobalRoutingHelper::PopulateRoutingTables();

    TrafficControlHelper trafficControl;
    trafficControl.Uninstall(slowDevices.Get(0));
    trafficControl.SetRootQueueDisc("ns3::FifoQueueDisc",
                                    "MaxSize",
                                    StringValue(std::to_string(queuePackets) + "p"));
    const auto queueDiscs = trafficControl.Install(slowDevices.Get(0));
    auto queueDisc = queueDiscs.Get(0);
    QueueProbe queueProbe;
    queueDisc->TraceConnectWithoutContext("PacketsInQueue",
                                          MakeCallback(&QueueProbe::DepthChanged, &queueProbe));
    queueDisc->TraceConnectWithoutContext("Drop",
                                          MakeCallback(&QueueProbe::PacketDropped, &queueProbe));

    constexpr uint16_t port = 9000;
    ns3lab::UdpCollector collector;
    collector.Attach(receiver, port);

    auto source = CreateObject<ns3lab::UdpSequenceSource>();
    source->Configure({InetSocketAddress(receiverInterfaces.GetAddress(1), port)},
                      packetCount,
                      packetSize,
                      ns3lab::IntervalForRate(packetSize, DataRate(offeredRate)));
    sender->AddApplication(source);
    source->SetStartTime(MicroSeconds(10));
    source->SetStopTime(MilliSeconds(2));

    FlowMonitorHelper flowHelper;
    auto flowMonitor = flowHelper.InstallAll();

    Simulator::Stop(MilliSeconds(3));
    Simulator::Run();
    flowMonitor->CheckForLostPackets();

    const uint32_t received = collector.Records().size();
    const uint32_t dropped = queueProbe.Drops();
    const uint32_t flowLost = [&flowMonitor]() {
        uint32_t count = 0;
        for (const auto& [id, stat] : flowMonitor->GetFlowStats())
        {
            (void)id;
            count += stat.lostPackets;
        }
        return count;
    }();

    ns3lab::Require(source->SentPackets() == packetCount, "sender did not offer every packet");
    ns3lab::Require(dropped > 0, "the configured overload did not fill the queue");
    ns3lab::Require(received + dropped == packetCount, "packet accounting is not conserved");
    ns3lab::Require(queueProbe.MaxDepth() <= queuePackets, "queue exceeded its configured limit");
    ns3lab::Require(collector.PayloadErrors() == 0, "payload validation failed");
    ns3lab::Require(collector.ThroughputGbps() <= DataRate(bottleneckRate).GetBitRate() / 1.0e9 * 1.01,
                    "measured goodput exceeds the bottleneck rate");

    queueProbe.WriteCsv(queueTrace);
    ns3lab::PrepareOutput(output);
    std::ofstream report(output);
    report << std::fixed << std::setprecision(3)
           << "{\n"
           << "  \"lab\": 2,\n"
           << "  \"scenario\": \"bottleneck-queue\",\n"
           << "  \"offered_rate\": \"" << offeredRate << "\",\n"
           << "  \"bottleneck_rate\": \"" << bottleneckRate << "\",\n"
           << "  \"tx_packets\": " << packetCount << ",\n"
           << "  \"rx_packets\": " << received << ",\n"
           << "  \"queue_disc_drops\": " << dropped << ",\n"
           << "  \"flow_monitor_lost_packets\": " << flowLost << ",\n"
           << "  \"max_queue_packets\": " << queueProbe.MaxDepth() << ",\n"
           << "  \"goodput_gbps\": " << collector.ThroughputGbps() << ",\n"
           << "  \"status\": \"PASS\"\n"
           << "}\n";

    std::cout << "Lab 2 PASS: received=" << received << " dropped=" << dropped
              << " max_queue=" << queueProbe.MaxDepth() << '\n';
    Simulator::Destroy();
    return 0;
}
