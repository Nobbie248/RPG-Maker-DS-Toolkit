#include <algorithm>
#include <bitset>
#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

#include "SSEQPlayer/Player.h"
#include "SSEQPlayer/SDAT.h"
#include "SSEQPlayer/common.h"
#include "SSEQPlayer/consts.h"

static std::vector<std::uint8_t> read_file(const std::string &path)
{
    std::ifstream input(path, std::ios::binary | std::ios::ate);
    if (!input)
        throw std::runtime_error("Could not open SDAT: " + path);
    const auto size = input.tellg();
    if (size <= 0)
        throw std::runtime_error("SDAT is empty");
    input.seekg(0);
    std::vector<std::uint8_t> data(static_cast<std::size_t>(size));
    if (!input.read(reinterpret_cast<char *>(data.data()), size))
        throw std::runtime_error("Could not read SDAT");
    return data;
}

static std::int16_t clamp16(std::int32_t sample)
{
    return static_cast<std::int16_t>(std::clamp(sample, -32768, 32767));
}

int main(int argc, char **argv)
{
    if (argc != 6)
    {
        std::cerr << "usage: ncsf_preview SDAT SEQUENCE SECONDS SAMPLE_RATE OUTPUT_PCM\n";
        return 2;
    }
    try
    {
        auto data = read_file(argv[1]);
        const auto sequence = static_cast<std::uint32_t>(std::stoul(argv[2]));
        const double seconds = std::stod(argv[3]);
        const auto sample_rate = static_cast<std::uint32_t>(std::stoul(argv[4]));
        if (seconds <= 0.0 || seconds > 600.0 || sample_rate < 8000 || sample_rate > 192000)
            throw std::runtime_error("Invalid render duration or sample rate");

        PseudoFile file;
        file.data = &data;
        SDAT sdat(file, sequence);
        Player player;
        player.allowedChannels = std::bitset<16>(sdat.player.channelMask);
        player.sseqVol = Cnv_Scale(sdat.sseq->info.vol);
        player.sampleRate = sample_rate;
        // Match in_ncsf's default high-quality 16-point sinc interpolation.
        player.interpolation = Interpolation::Sinc;
        if (!player.Setup(sdat.sseq.get()))
            throw std::runtime_error("Could not initialize SSEQ player");
        player.Timer();

        const auto frames = static_cast<std::uint64_t>(seconds * sample_rate);
        std::ofstream output(argv[5], std::ios::binary);
        if (!output)
            throw std::runtime_error("Could not create PCM output");

        const double seconds_per_sample = 1.0 / sample_rate;
        double playback_time = 0.0;
        double next_clock = SecondsPerClockCycle;
        for (std::uint64_t frame = 0; frame < frames; ++frame)
        {
            playback_time += seconds_per_sample;
            std::int32_t left = 0;
            std::int32_t right = 0;
            for (int channel_index = 0; channel_index < 16; ++channel_index)
            {
                Channel &channel = player.channels[channel_index];
                if (channel.state <= ChannelState::None)
                    continue;
                std::int32_t sample = channel.GenerateSample();
                channel.IncrementSample();
                std::uint8_t shift = channel.reg.volumeDiv;
                if (shift == 3)
                    shift = 4;
                sample = muldiv7(sample, channel.reg.volumeMul) >> shift;
                left += muldiv7(sample, 127 - channel.reg.panning);
                right += muldiv7(sample, channel.reg.panning);
            }
            const std::int16_t stereo[2] = {clamp16(left), clamp16(right)};
            output.write(reinterpret_cast<const char *>(stereo), sizeof(stereo));
            while (playback_time > next_clock)
            {
                player.Timer();
                next_clock += SecondsPerClockCycle;
            }
        }
        if (!output)
            throw std::runtime_error("Could not finish PCM output");
        return 0;
    }
    catch (const std::exception &error)
    {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
