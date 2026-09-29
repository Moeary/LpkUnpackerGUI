cmake_minimum_required(VERSION 3.15)
project(LpkSpineConverterNative LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

macro(add_spine_c version)
    file(GLOB SPINE_C
        "spine/spine-c-${version}/include/spine/*.h"
        "spine/spine-c-${version}/src/spine/*.c")
    add_library(spine_c_${version} STATIC ${SPINE_C})
    target_include_directories(spine_c_${version} PRIVATE "spine/spine-c-${version}/include")
endmacro()

macro(add_spine_cpp version)
    file(GLOB SPINE_CPP
        "spine/spine-cpp-${version}/include/spine/*.h"
        "spine/spine-cpp-${version}/src/spine/*.cpp")
    add_library(spine_cpp_${version} STATIC ${SPINE_CPP})
    target_include_directories(spine_cpp_${version} PRIVATE "spine/spine-cpp-${version}/include")
endmacro()

add_spine_c(21)
add_spine_c(35)
add_spine_c(36)
add_spine_cpp(37)
add_spine_cpp(38)
add_spine_cpp(40)
add_spine_cpp(41)
add_spine_cpp(42)

set(CONVERTER_SOURCES
    "include/json.hpp"
    "src/common.cpp"
    "src/CurveConverter.cpp"
    "src/OrderConverter.cpp"
    "src/RotateTimelineConverter.cpp"
    "src/SpacingModeConverter.cpp"
    "src/SkeletonData35BinaryReader.cpp"
    "src/SkeletonData35BinaryWriter.cpp"
    "src/SkeletonData35JsonReader.cpp"
    "src/SkeletonData35JsonWriter.cpp"
    "src/SkeletonData36BinaryReader.cpp"
    "src/SkeletonData36BinaryWriter.cpp"
    "src/SkeletonData36JsonReader.cpp"
    "src/SkeletonData36JsonWriter.cpp"
    "src/SkeletonData37BinaryReader.cpp"
    "src/SkeletonData37BinaryWriter.cpp"
    "src/SkeletonData37JsonReader.cpp"
    "src/SkeletonData37JsonWriter.cpp"
    "src/SkeletonData38BinaryReader.cpp"
    "src/SkeletonData38BinaryWriter.cpp"
    "src/SkeletonData38JsonReader.cpp"
    "src/SkeletonData38JsonWriter.cpp"
    "src/SkeletonData40BinaryReader.cpp"
    "src/SkeletonData40BinaryWriter.cpp"
    "src/SkeletonData40JsonReader.cpp"
    "src/SkeletonData40JsonWriter.cpp"
    "src/SkeletonData41BinaryReader.cpp"
    "src/SkeletonData41BinaryWriter.cpp"
    "src/SkeletonData41JsonReader.cpp"
    "src/SkeletonData41JsonWriter.cpp"
    "src/SkeletonData42BinaryReader.cpp"
    "src/SkeletonData42BinaryWriter.cpp"
    "src/SkeletonData42JsonReader.cpp"
    "src/SkeletonData42JsonWriter.cpp"
)

# The application loads this library through ctypes.  The upstream CLI and
# atlas executable are intentionally not build dependencies of this target.
add_library(lpk_spine_converter SHARED
    ${CONVERTER_SOURCES}
    "src/spine_converter_capi.cpp"
)
target_compile_definitions(lpk_spine_converter PRIVATE SPINE_CONVERTER_BUILD)
target_include_directories(lpk_spine_converter PUBLIC "include")
set_target_properties(lpk_spine_converter PROPERTIES OUTPUT_NAME "lpk_spine_converter" PREFIX "")
if (MINGW)
    # Keep the installed DLL self-contained on normal Windows machines.
    target_link_options(lpk_spine_converter PRIVATE -static-libgcc -static-libstdc++)
endif()

