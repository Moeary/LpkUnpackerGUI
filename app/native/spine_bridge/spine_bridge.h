#pragma once

/* A deliberately small C ABI around the official spine-cpp runtime.
 *
 * The runtime itself is compiled separately for each supported Spine family
 * (3.8 and 4.0).  Python never dereferences a C++ object; it only consumes
 * these POD buffers, which keeps the ctypes boundary stable across the two
 * runtime builds.
 */
#include <stdint.h>

#if defined(_WIN32)
#  if defined(SPINE_BRIDGE_BUILD)
#    define SPINE_BRIDGE_API __declspec(dllexport)
#  else
#    define SPINE_BRIDGE_API __declspec(dllimport)
#  endif
#else
#  define SPINE_BRIDGE_API __attribute__((visibility("default")))
#endif

#ifdef __cplusplus
extern "C" {
#endif

typedef struct SpineNativeVertex {
    float x, y;
    float u, v;
    float r, g, b, a;
} SpineNativeVertex;

typedef struct SpineNativeBatch {
    uint32_t vertex_offset;
    uint32_t vertex_count;
    uint32_t index_offset;
    uint32_t index_count;
    int32_t page;
    int32_t blend;
} SpineNativeBatch;

typedef struct SpineNativePageInfo {
    const char *path;
    int32_t width;
    int32_t height;
    int32_t pma;
} SpineNativePageInfo;

SPINE_BRIDGE_API void *spine_native_create(void);
SPINE_BRIDGE_API void spine_native_destroy(void *handle);

/* format: 0 = JSON, 1 = binary .skel.  atlas_data is the complete text of
 * one atlas file and atlas_dir is the directory used to resolve page paths. */
SPINE_BRIDGE_API int spine_native_load_memory(
    void *handle,
    const void *skeleton_data,
    int32_t skeleton_size,
    int32_t format,
    const void *atlas_data,
    int32_t atlas_size,
    const char *atlas_dir);
SPINE_BRIDGE_API const char *spine_native_last_error(void *handle);
SPINE_BRIDGE_API const char *spine_native_skeleton_version(void *handle);

SPINE_BRIDGE_API int32_t spine_native_animation_count(void *handle);
SPINE_BRIDGE_API const char *spine_native_animation_name(void *handle, int32_t index);
SPINE_BRIDGE_API float spine_native_animation_duration(void *handle, int32_t index);
SPINE_BRIDGE_API int32_t spine_native_skin_count(void *handle);
SPINE_BRIDGE_API const char *spine_native_skin_name(void *handle, int32_t index);
SPINE_BRIDGE_API int32_t spine_native_page_count(void *handle);
SPINE_BRIDGE_API SpineNativePageInfo spine_native_page_info(void *handle, int32_t index);

SPINE_BRIDGE_API int spine_native_set_skin(void *handle, const char *name);
SPINE_BRIDGE_API int spine_native_set_animation(void *handle, const char *name, int loop);
SPINE_BRIDGE_API int spine_native_set_loop(void *handle, int loop);
SPINE_BRIDGE_API int spine_native_set_paused(void *handle, int paused);
SPINE_BRIDGE_API int spine_native_set_time(void *handle, float time);
SPINE_BRIDGE_API int spine_native_reset_pose(void *handle);
SPINE_BRIDGE_API float spine_native_time(void *handle);
SPINE_BRIDGE_API float spine_native_duration(void *handle);

SPINE_BRIDGE_API void spine_native_update(void *handle, float delta);
SPINE_BRIDGE_API int spine_native_required_sizes(void *handle, int32_t *vertices, int32_t *indices, int32_t *batches);
SPINE_BRIDGE_API int spine_native_render(
    void *handle,
    SpineNativeVertex *vertices,
    int32_t vertex_capacity,
    uint32_t *indices,
    int32_t index_capacity,
    SpineNativeBatch *batches,
    int32_t batch_capacity);
SPINE_BRIDGE_API int spine_native_bounds(void *handle, float *x, float *y, float *width, float *height);

#ifdef __cplusplus
}
#endif
