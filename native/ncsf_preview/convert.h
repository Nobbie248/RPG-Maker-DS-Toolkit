#pragma once

#include <type_traits>

class ConvertFuncs
{
public:
    template<typename T>
    static constexpr typename std::enable_if_t<std::is_enum_v<T>, std::underlying_type_t<T>>
    ToIntegral(const T &value)
    {
        return static_cast<std::underlying_type_t<T>>(value);
    }
};
