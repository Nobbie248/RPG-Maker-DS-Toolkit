#pragma once

#include <cmath>
#include <limits>
#include <type_traits>

template<typename T>
inline typename std::enable_if_t<std::is_floating_point_v<T>, bool>
fEqual(T x, T y, int count = 1)
{
    const T difference = std::abs(x - y);
    const T tolerance = count * std::numeric_limits<T>::epsilon();
    return difference <= tolerance * std::abs(x) &&
           difference <= tolerance * std::abs(y);
}
