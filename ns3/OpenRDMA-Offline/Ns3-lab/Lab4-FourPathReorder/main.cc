#include "../common/lab-common.h"

#include "ns3/point-to-point-module.h"

#include <array>
#include <fstream>
#include <iostream>

using namespace ns3;

int
main(int argc, char* argv[])
{
    uint32_t packetCount = 80;
    uint32_t packetSize = 256;
    uint64_t intervalNs = 100;
    std::string output = "../results/lab4.json";
    std::string pcapPrefix = "../results/lab4";

    CommandLine cmd(__FILE__);
    cmd.AddValue("packetCount", "Number of packets to distribute", packetCount);
    cmd.AddValue("packetSize", "UDP payload size, including the lab header", packetSize);
    cmd.AddValue("intervalNs", "Global packet spacing", intervalNs);
    cmd.AddValue("output", "JSON result path", output);
    cmd.AddValue("pcapPrefix", "PCAP file prefix", pcapPrefix);
    cmd.Parse(argc, argv);

    NodeContainer endpoints;
    endpoints.Create(2);
    auto sender = endpoints.Get(0);
    auto receiver = endpoints.Get(1);

    InternetStackHelper internet;
    internet.Install(endpoints);

    const std::array<std::string, 4> delays = {"200ns", "400ns", "800ns", "2us"};
    std::vector<Address> destinations;
    std::array<NetDeviceContainer, 4> pathDevices;

    for (uint32_t channel = 0; channel < delays.size(); ++channel)
    {
        PointToPointHelper path;
        path.SetDeviceAttribute("DataRate", StringValue("100Gbps"));
        path.SetChannelAttribute("Delay", StringValue(delays[channel]));
        pathDevices[channel] = path.Install(sender, receiver);

        Ipv4AddressHelper addresses;
        const std::string network = "10.4." + std::to_string(channel + 1) + ".0";
        addresses.SetBase(network.c_str(), "255.255.255.252");
        const auto interfaces = addresses.Assign(pathDevices[channel]);
        destinations.emplace_back(InetSocketAddress(interfaces.GetAddress(1), 9000));

        path.EnablePcap(pcapPrefix + "-ch" + std::to_string(channel),
                        pathDevices[channel].Get(0),
                        false);
    }

    ns3lab::UdpCollector collector;
    collector.Attach(receiver, 9000);

    auto source = CreateObject<ns3lab::UdpSequenceSource>();
    source->Configure(destinations, packetCount, packetSize, NanoSeconds(intervalNs));
    sender->AddApplication(source);
    source->SetStartTime(MicroSeconds(10));
    source->SetStopTime(MilliSeconds(1));

    Simulator::Stop(MilliSeconds(2));
    Simulator::Run();

    const auto missing = collector.MissingSequences(packetCount);
    std::array<uint32_t, 4> channelCounts{};
    std::array<int64_t, 4> previousSequence{};
    previousSequence.fill(-1);

    for (const auto& record : collector.Records())
    {
        ns3lab::Require(record.channel < channelCounts.size(), "invalid channel in packet header");
        ns3lab::Require(record.sequence % 4 == record.channel, "packet arrived on the wrong path");
        ns3lab::Require(static_cast<int64_t>(record.sequence) > previousSequence[record.channel],
                        "a path reordered its own packet stream");
        previousSequence[record.channel] = record.sequence;
        ++channelCounts[record.channel];
    }

    ns3lab::Require(source->SentPackets() == packetCount, "sender did not emit every packet");
    ns3lab::Require(missing.empty(), "four-path lossless network lost a packet");
    ns3lab::Require(collector.PayloadErrors() == 0, "payload validation failed");
    ns3lab::Require(collector.ReorderedPackets() > 0, "path delay spread produced no global reorder");
    ns3lab::Require(collector.MaxReorderDepth() > 0, "maximum reorder depth was not measured");

    ns3lab::PrepareOutput(output);
    std::ofstream report(output);
    report << "{\n"
           << "  \"lab\": 4,\n"
           << "  \"scenario\": \"four-path-reorder\",\n"
           << "  \"path_delays\": [\"200ns\",\"400ns\",\"800ns\",\"2us\"],\n"
           << "  \"tx_packets\": " << packetCount << ",\n"
           << "  \"rx_packets\": " << collector.Records().size() << ",\n"
           << "  \"reordered_packets\": " << collector.ReorderedPackets() << ",\n"
           << "  \"max_reorder_depth\": " << collector.MaxReorderDepth() << ",\n"
           << "  \"channel_counts\": [" << channelCounts[0] << ',' << channelCounts[1]
           << ',' << channelCounts[2] << ',' << channelCounts[3] << "],\n"
           << "  \"arrival_order\": " << ns3lab::JsonArray(collector.ArrivalOrder()) << ",\n"
           << "  \"status\": \"PASS\"\n"
           << "}\n";

    std::cout << "Lab 4 PASS: reordered=" << collector.ReorderedPackets()
              << " max_depth=" << collector.MaxReorderDepth() << '\n';
    Simulator::Destroy();
    return 0;
}
