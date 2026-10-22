#include "../common/lab-common.h"

#include "ns3/point-to-point-module.h"

#include <fstream>
#include <iostream>

using namespace ns3;

namespace
{

uint64_t
LossSignature(const std::vector<uint32_t>& missing)
{
    uint64_t hash = 1469598103934665603ULL;
    for (const auto sequence : missing)
    {
        hash ^= sequence;
        hash *= 1099511628211ULL;
    }
    return hash;
}

} // namespace

int
main(int argc, char* argv[])
{
    double lossRate = 0.08;
    uint32_t packetCount = 1000;
    uint32_t packetSize = 512;
    uint32_t seed = 20260914;
    uint64_t run = 1;
    std::string output = "../results/lab3.json";

    CommandLine cmd(__FILE__);
    cmd.AddValue("lossRate", "Independent receive-side packet loss probability", lossRate);
    cmd.AddValue("packetCount", "Number of packets to send", packetCount);
    cmd.AddValue("packetSize", "UDP payload size, including the lab header", packetSize);
    cmd.AddValue("seed", "ns-3 random seed", seed);
    cmd.AddValue("run", "Independent run number", run);
    cmd.AddValue("output", "JSON result path", output);
    cmd.Parse(argc, argv);

    ns3lab::Require(lossRate >= 0.0 && lossRate <= 1.0, "lossRate must be in [0, 1]");

    NodeContainer nodes;
    nodes.Create(2);

    PointToPointHelper link;
    link.SetDeviceAttribute("DataRate", StringValue("100Gbps"));
    link.SetChannelAttribute("Delay", StringValue("500ns"));
    const auto devices = link.Install(nodes);

    auto errorModel = CreateObject<RateErrorModel>();
    errorModel->SetAttribute("ErrorUnit", EnumValue(RateErrorModel::ERROR_UNIT_PACKET));
    errorModel->SetAttribute("ErrorRate", DoubleValue(lossRate));
    errorModel->AssignStreams(7);
    RngSeedManager::SetSeed(seed);
    RngSeedManager::SetRun(run);
    devices.Get(1)->SetAttribute("ReceiveErrorModel", PointerValue(errorModel));

    InternetStackHelper internet;
    internet.Install(nodes);

    Ipv4AddressHelper addresses;
    addresses.SetBase("10.3.0.0", "255.255.255.0");
    const auto interfaces = addresses.Assign(devices);

    constexpr uint16_t port = 9000;
    ns3lab::UdpCollector collector;
    collector.Attach(nodes.Get(1), port);

    auto source = CreateObject<ns3lab::UdpSequenceSource>();
    source->Configure({InetSocketAddress(interfaces.GetAddress(1), port)},
                      packetCount,
                      packetSize,
                      NanoSeconds(500));
    nodes.Get(0)->AddApplication(source);
    source->SetStartTime(MicroSeconds(10));
    source->SetStopTime(MilliSeconds(2));

    Simulator::Stop(MilliSeconds(3));
    Simulator::Run();

    const auto missing = collector.MissingSequences(packetCount);
    ns3lab::Require(source->SentPackets() == packetCount, "sender did not emit every packet");
    ns3lab::Require(collector.Records().size() + missing.size() == packetCount,
                    "received and missing packet counts do not add up");
    ns3lab::Require(collector.PayloadErrors() == 0, "payload validation failed");
    ns3lab::Require(collector.DuplicatePackets() == 0, "duplicate packet received");
    if (lossRate == 0.0)
    {
        ns3lab::Require(missing.empty(), "zero loss rate still dropped packets");
    }
    else if (packetCount >= 100)
    {
        ns3lab::Require(!missing.empty(), "loss experiment produced no dropped packets");
    }

    ns3lab::PrepareOutput(output);
    std::ofstream report(output);
    report << "{\n"
           << "  \"lab\": 3,\n"
           << "  \"scenario\": \"controlled-loss\",\n"
           << "  \"seed\": " << seed << ",\n"
           << "  \"run\": " << run << ",\n"
           << "  \"loss_rate\": " << lossRate << ",\n"
           << "  \"tx_packets\": " << packetCount << ",\n"
           << "  \"rx_packets\": " << collector.Records().size() << ",\n"
           << "  \"lost_packets\": " << missing.size() << ",\n"
           << "  \"loss_signature\": \"0x" << std::hex << LossSignature(missing)
           << std::dec << "\",\n"
           << "  \"missing_sequences\": " << ns3lab::JsonArray(missing) << ",\n"
           << "  \"status\": \"PASS\"\n"
           << "}\n";

    std::cout << "Lab 3 PASS: seed=" << seed << " run=" << run
              << " lost=" << missing.size() << " signature=0x" << std::hex
              << LossSignature(missing) << std::dec << '\n';
    Simulator::Destroy();
    return 0;
}
