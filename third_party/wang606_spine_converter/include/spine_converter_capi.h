#pragma once

#include <cstddef>

#if defined(_WIN32) && defined(SPINE_CONVERTER_BUILD)
#  define SPINE_CONVERTER_API __declspec(dllexport)
#elif defined(_WIN32)
#  define SPINE_CONVERTER_API __declspec(dllimport)
#else
#  define SPINE_CONVERTER_API
#endif

// Stable C ABI used by app/core/spine_converter.py.  All strings are UTF-8.
// The function returns 0 on success and a non-zero error code on failure.
// error_buffer may be null when no diagnostic is needed.
extern "C" {
SPINE_CONVERTER_API int spine_converter_convert(
    const char* input_file,
    const char* output_file,
    const char* target_version,
    const char* output_format,
    int remove_curve,
    char* error_buffer,
    std::size_t error_buffer_size);

SPINE_CONVERTER_API const char* spine_converter_source_commit();
SPINE_CONVERTER_API const char* spine_converter_abi_version();
}
