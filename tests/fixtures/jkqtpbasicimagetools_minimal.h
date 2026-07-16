#pragma once

#include <iostream>

#define JKQTP_TRACE(message) do { customTrace(message); } while (false)

template <typename Pixel, int Channels>
inline void convertImage(const Pixel* source, Pixel* target, int count) {
    if (!source || !target) {
        qWarning() << "invalid image buffer" << count;
        LOG(ERROR) << "image conversion failed";
        std::cerr << "conversion error" << std::endl;
        JKQTP_TRACE("conversion rejected");
        return;
    }

    for (int index = 0; index < count; ++index) {
        target[index * Channels] = source[index];
    }
}
