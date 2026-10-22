#include "../common/lab-common.h"

#include "ns3/point-to-point-module.h"

#include <fstream>
#include <iostream>

using namespace ns3;

int
main(int argc, char* argv[])
{
    std::string dataRate = "100Gbps";
    std::string delay = "1us";
    std::string output = "../results/lab1.json";
    std::string pcapPrefix = "../results/lab1";
    uint32_t packetSize = 1024;
    uint32_t packetCount = 100;
    uint64_t intervalNs = 1000;

    CommandLine cmd(__FILE__);
    cmd.AddValue("dataRate", "Point-to-point link rate", dataRate);
    cmd.AddValue("delay", "One-way propagation delay", delay);
    cmd.AddValue("packetSize", "UDP payload size, including the lab header", packetSize);
    cmd.AddValue("packetCount", "Number of packets to send", packetCount);
    cmd.AddValue("intervalNs", "Time between packets in nanoseconds", intervalNs);
    cmd.AddValue("output", "JSON result path", output);
    cmd.AddValue("pcapPrefix", "PCAP file prefix", pcapPrefix);
    cmd.Parse(argc, argv);

    NodeContainer nodes;
    nodes.Create(2);

    PointToPointHelper link;
    link.SetDeviceAttribute("DataRate", StringValue(dataRate));
    link.SetChannelAttribute("Delay", StringValue(delay));
    const NetDeviceContainer devices = link.Install(nodes);

    InternetStackHelper internet;
    internet.Install(nodes);

    Ipv4AddressHelper addresses;
    addresses.SetBase("10.1.0.0", "255.255.255.0");
    const Ipv4InterfaceContainer interfaces = addresses.Assign(devices);

    constexpr uint16_t port = 9000;
    ns3lab::UdpCollector collector;
    collector.Attach(nodes.Get(1), port);

    auto source = CreateObject<ns3lab::UdpSequenceSource>();
    source->Configure({InetSocketAddress(interfaces.GetAddress(1), port)},
                      packetCount,
                      packetSize,
                      NanoSeconds(intervalNs));
    nodes.Get(0)->AddApplication(source);
    source->SetStartTime(MicroSeconds(1));
    source->SetStopTime(MilliSeconds(5));

    ns3lab::PrepareOutput(output);
    ns3lab::PrepareOutput(pcapPrefix);
    link.EnablePcapAll(pcapPrefix, false);

    Simulator::Stop(MilliSeconds(6));
    Simulator::Run();

    const auto missing = collector.MissingSequences(packetCount);
    const auto propagation = Time(delay).GetNanoSeconds();
    const auto serialization =
        static_cast<int64_t>((static_cast<long double>(packetSize) * 8.0L * 1.0e9L) /
                             DataRate(dataRate).GetBitRate());

    ns3lab::Require(source->SentPackets() == packetCount, "sender did not emit every packet");
    ns3lab::Require(collector.Records().size() == packetCount, "receiver packet count mismatch");
    ns3lab::Require(missing.empty(), "the lossless link lost a packet");
    ns3lab::Require(collector.PayloadErrors() == 0, "payload validation failed");
    ns3lab::Require(collector.DuplicatePackets() == 0, "duplicate packet received");
    ns3lab::Require(collector.ReorderedPackets() == 0, "single path reordered packets");
    ns3lab::Require(collector.MeanDelayNs() >= propagation + serialization,
                    "measured delay is below the physical lower bound");

    std::ofstream report(output);
    report << std::fixed << std::setprecision(3)
           << "{\n"
           << "  \"lab\": 1,\n"
           << "  \"scenario\": \"point-to-point\",\n"
           << "  \"data_rate\": \"" << dataRate << "\",\n"
           << "  \"propagation_delay\": \"" << delay << "\",\n"
           << "  \"tx_packets\": " << source->SentPackets() << ",\n"
           << "  \"rx_packets\": " << collector.Records().size() << ",\n"
           << "  \"payload_errors\": " << collector.PayloadErrors() << ",\n"
           << "  \"mean_delay_ns\": " << collector.MeanDelayNs() << ",\n"
           << "  \"serialization_lower_bound_ns\": " << serialization << ",\n"
           << "  \"status\": \"PASS\"\n"
           << "}\n";

    std::cout << "Lab 1 PASS: " << packetCount << " packets, mean delay "
              << collector.MeanDelayNs() << " ns\n";
    Simulator::Destroy();
    return 0;
}
