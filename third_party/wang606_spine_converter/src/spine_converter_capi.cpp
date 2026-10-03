#include "spine_converter_capi.h"

#include <algorithm>
#include <cctype>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <regex>
#include <stdexcept>
#include <string>
#include <vector>

#include "SkeletonData.h"

namespace {

enum class SpineVersion {
    Version35 = 0,
    Version36 = 1,
    Version37 = 2,
    Version38 = 3,
    Version40 = 4,
    Version41 = 5,
    Version42 = 6,
    Invalid = -1,
};

enum class FileFormat { Json, Skel };

constexpr const char* kSourceCommit = "5ecb2139b0a1af266974f95abeec6bb8562d1249";
constexpr const char* kAbiVersion = "1";

std::filesystem::path nativePath(const std::string& value) {
    return std::filesystem::u8path(value);
}

bool atLeast(SpineVersion value, SpineVersion target) {
    return static_cast<int>(value) >= static_cast<int>(target);
}

bool atMost(SpineVersion value, SpineVersion target) {
    return static_cast<int>(value) <= static_cast<int>(target);
}

std::string lower(std::string value) {
    std::transform(value.begin(), value.end(), value.begin(), [](unsigned char c) {
        return static_cast<char>(std::tolower(c));
    });
    return value;
}

bool hasSuffix(const std::string& value, const std::string& suffix) {
    const std::string normalized = lower(value);
    return normalized.size() >= suffix.size() &&
           normalized.compare(normalized.size() - suffix.size(), suffix.size(), suffix) == 0;
}

SpineVersion versionFromFamily(const std::string& family) {
    if (family == "3.5") return SpineVersion::Version35;
    if (family == "3.6") return SpineVersion::Version36;
    if (family == "3.7") return SpineVersion::Version37;
    if (family == "3.8") return SpineVersion::Version38;
    if (family == "4.0") return SpineVersion::Version40;
    if (family == "4.1") return SpineVersion::Version41;
    if (family == "4.2") return SpineVersion::Version42;
    return SpineVersion::Invalid;
}

SpineVersion parseVersion(const std::string& version) {
    std::smatch match;
    if (!std::regex_match(version, match, std::regex(R"(^([0-9]+)\.([0-9]+)\.([0-9]+)$)"))) {
        return SpineVersion::Invalid;
    }
    return versionFromFamily(match[1].str() + "." + match[2].str());
}

SpineVersion detectVersion(const std::string& input) {
    std::ifstream stream(nativePath(input), std::ios::binary);
    if (!stream) {
        throw std::runtime_error("cannot open input file");
    }
    constexpr std::size_t headerSize = 4096;
    std::vector<char> buffer(headerSize, '\0');
    stream.read(buffer.data(), static_cast<std::streamsize>(buffer.size()));
    const std::string header(buffer.data(), static_cast<std::size_t>(stream.gcount()));
    std::smatch match;
    if (!std::regex_search(header, match, std::regex(R"(([0-9]+)\.([0-9]+)\.([0-9]+))"))) {
        return SpineVersion::Invalid;
    }
    return versionFromFamily(match[1].str() + "." + match[2].str());
}

FileFormat inputFormatFor(const std::string& path) {
    const std::string normalized = lower(path);
    if (hasSuffix(normalized, ".json")) return FileFormat::Json;
    if (normalized.find(".skel") != std::string::npos) return FileFormat::Skel;
    throw std::runtime_error("input file must have .json or .skel extension");
}

FileFormat outputFormatFor(const std::string& format) {
    const std::string normalized = lower(format);
    if (normalized == "json" || normalized == ".json") return FileFormat::Json;
    if (normalized == "skel" || normalized == ".skel") return FileFormat::Skel;
    throw std::runtime_error("output format must be json or skel");
}

SkeletonData readData(const std::string& input, FileFormat format, SpineVersion version) {
    if (format == FileFormat::Json) {
        std::ifstream stream(nativePath(input));
        if (!stream) throw std::runtime_error("cannot open input JSON file");
        Json json;
        stream >> json;
        switch (version) {
            case SpineVersion::Version35: return spine35::readJsonData(json);
            case SpineVersion::Version36: return spine36::readJsonData(json);
            case SpineVersion::Version37: return spine37::readJsonData(json);
            case SpineVersion::Version38: return spine38::readJsonData(json);
            case SpineVersion::Version40: return spine40::readJsonData(json);
            case SpineVersion::Version41: return spine41::readJsonData(json);
            case SpineVersion::Version42: return spine42::readJsonData(json);
            default: throw std::runtime_error("unsupported input Spine version");
        }
    }

    std::ifstream stream(nativePath(input), std::ios::binary);
    if (!stream) throw std::runtime_error("cannot open input SKEL file");
    Binary binary((std::istreambuf_iterator<char>(stream)), std::istreambuf_iterator<char>());
    switch (version) {
        case SpineVersion::Version35: return spine35::readBinaryData(binary);
        case SpineVersion::Version36: return spine36::readBinaryData(binary);
        case SpineVersion::Version37: return spine37::readBinaryData(binary);
        case SpineVersion::Version38: return spine38::readBinaryData(binary);
        case SpineVersion::Version40: return spine40::readBinaryData(binary);
        case SpineVersion::Version41: return spine41::readBinaryData(binary);
        case SpineVersion::Version42: return spine42::readBinaryData(binary);
        default: throw std::runtime_error("unsupported input Spine version");
    }
}

template <typename Writer>
void writeData(const std::string& output, FileFormat format, SkeletonData& data, Writer writer) {
    if (format == FileFormat::Skel) {
        const Binary bytes = writer.writeBinary(data);
        std::ofstream stream(nativePath(output), std::ios::binary);
        if (!stream) throw std::runtime_error("cannot create output SKEL file");
        stream.write(reinterpret_cast<const char*>(bytes.data()), static_cast<std::streamsize>(bytes.size()));
        return;
    }
    const Json json = writer.writeJson(data);
    std::ofstream stream(nativePath(output));
    if (!stream) throw std::runtime_error("cannot create output JSON file");
    stream << dumpJson(json);
    if (!stream) throw std::runtime_error("cannot write output JSON file");
}

struct Writer35 {
    static Binary writeBinary(SkeletonData& data) { return spine35::writeBinaryData(data); }
    static Json writeJson(SkeletonData& data) { return spine35::writeJsonData(data); }
};
struct Writer36 {
    static Binary writeBinary(SkeletonData& data) { return spine36::writeBinaryData(data); }
    static Json writeJson(SkeletonData& data) { return spine36::writeJsonData(data); }
};
struct Writer37 {
    static Binary writeBinary(SkeletonData& data) { return spine37::writeBinaryData(data); }
    static Json writeJson(SkeletonData& data) { return spine37::writeJsonData(data); }
};
struct Writer38 {
    static Binary writeBinary(SkeletonData& data) { return spine38::writeBinaryData(data); }
    static Json writeJson(SkeletonData& data) { return spine38::writeJsonData(data); }
};
struct Writer40 {
    static Binary writeBinary(SkeletonData& data) { return spine40::writeBinaryData(data); }
    static Json writeJson(SkeletonData& data) { return spine40::writeJsonData(data); }
};
struct Writer41 {
    static Binary writeBinary(SkeletonData& data) { return spine41::writeBinaryData(data); }
    static Json writeJson(SkeletonData& data) { return spine41::writeJsonData(data); }
};
struct Writer42 {
    static Binary writeBinary(SkeletonData& data) { return spine42::writeBinaryData(data); }
    static Json writeJson(SkeletonData& data) { return spine42::writeJsonData(data); }
};

void writeDataForVersion(const std::string& output, FileFormat format, SkeletonData& data, SpineVersion version) {
    switch (version) {
        case SpineVersion::Version35: writeData(output, format, data, Writer35{}); return;
        case SpineVersion::Version36: writeData(output, format, data, Writer36{}); return;
        case SpineVersion::Version37: writeData(output, format, data, Writer37{}); return;
        case SpineVersion::Version38: writeData(output, format, data, Writer38{}); return;
        case SpineVersion::Version40: writeData(output, format, data, Writer40{}); return;
        case SpineVersion::Version41: writeData(output, format, data, Writer41{}); return;
        case SpineVersion::Version42: writeData(output, format, data, Writer42{}); return;
        default: throw std::runtime_error("unsupported output Spine version");
    }
}

void convert(const std::string& input, const std::string& output, const std::string& target,
             const std::string& outputFormat, bool removeCurveOption) {
    if (input.empty() || output.empty()) throw std::runtime_error("input and output paths are required");
    const SpineVersion inputVersion = detectVersion(input);
    if (inputVersion == SpineVersion::Invalid) throw std::runtime_error("could not detect input Spine version");
    const SpineVersion outputVersion = parseVersion(target);
    if (outputVersion == SpineVersion::Invalid) {
        throw std::runtime_error("target version must be a complete supported x.y.z value");
    }

    const FileFormat inputFormat = inputFormatFor(input);
    const FileFormat outputFormatValue = outputFormatFor(outputFormat);
    SkeletonData data = readData(input, inputFormat, inputVersion);
    data.version = target;

    if (atLeast(inputVersion, SpineVersion::Version40) && atMost(outputVersion, SpineVersion::Version38)) {
        convertSpacingMode4xTo3x(data);
        convertRotateTimeline4xTo3x(data);
        if (removeCurveOption) removeCurve(data); else convertCurve4xTo3x(data);
    }
    if (atMost(inputVersion, SpineVersion::Version38) && atLeast(outputVersion, SpineVersion::Version40)) {
        convertRotateTimeline3xTo4x(data);
        if (removeCurveOption) removeCurve(data); else convertCurve3xTo4x(data);
    }
    if (atLeast(inputVersion, SpineVersion::Version42) && atMost(outputVersion, SpineVersion::Version41)) {
        convertOrder42ToBelow(data);
    }

    writeDataForVersion(output, outputFormatValue, data, outputVersion);
}

void setError(char* buffer, std::size_t capacity, const std::string& message) {
    if (!buffer || capacity == 0) return;
    const std::size_t length = std::min(capacity - 1, message.size());
    std::memcpy(buffer, message.data(), length);
    buffer[length] = '\0';
}

} // namespace

extern "C" SPINE_CONVERTER_API int spine_converter_convert(
    const char* input_file,
    const char* output_file,
    const char* target_version,
    const char* output_format,
    int remove_curve,
    char* error_buffer,
    std::size_t error_buffer_size) {
    try {
        convert(input_file ? input_file : "", output_file ? output_file : "",
                target_version ? target_version : "", output_format ? output_format : "",
                remove_curve != 0);
        setError(error_buffer, error_buffer_size, "");
        return 0;
    } catch (const std::exception& error) {
        setError(error_buffer, error_buffer_size, error.what());
        return 1;
    } catch (...) {
        setError(error_buffer, error_buffer_size, "unknown native converter error");
        return 2;
    }
}

extern "C" SPINE_CONVERTER_API const char* spine_converter_source_commit() {
    return kSourceCommit;
}

extern "C" SPINE_CONVERTER_API const char* spine_converter_abi_version() {
    return kAbiVersion;
}
