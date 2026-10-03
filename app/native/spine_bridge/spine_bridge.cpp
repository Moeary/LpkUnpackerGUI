#include "spine_bridge.h"

#include <spine/spine.h>

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstring>
#include <string>
#include <vector>

using namespace spine;

// spine-cpp leaves allocation policy to the host application.  The official
// unit-test host supplies this symbol; the bridge supplies the same default
// allocator so the runtime static library is self-contained.
namespace spine {
SpineExtension *getDefaultExtension() {
    static DefaultSpineExtension extension;
    return &extension;
}
}

namespace {

class PageTextureLoader final : public TextureLoader {
public:
    void load(AtlasPage &page, const String &path) override { page.texturePath = path; }
    void unload(void *) override {}
};

struct Bridge {
    PageTextureLoader loader;
    Atlas *atlas = nullptr;
    SkeletonData *data = nullptr;
    Skeleton *skeleton = nullptr;
    AnimationStateData *stateData = nullptr;
    AnimationState *state = nullptr;
    std::string error;
    std::string version;
    std::vector<SpineNativeVertex> vertices;
    std::vector<uint32_t> indices;
    std::vector<SpineNativeBatch> batches;
    std::vector<std::string> pagePaths;
    int animationIndex = -1;
    bool loop = true;
    bool paused = false;
    float duration = 0;
    bool renderDirty = true;

    ~Bridge() { clear(); }

    void clear() {
        delete state;
        state = nullptr;
        delete stateData;
        stateData = nullptr;
        delete skeleton;
        skeleton = nullptr;
        delete data;
        data = nullptr;
        delete atlas;
        atlas = nullptr;
        vertices.clear();
        indices.clear();
        batches.clear();
        pagePaths.clear();
        animationIndex = -1;
        duration = 0;
        renderDirty = true;
        version.clear();
    }

    void setError(const char *message) { error = message ? message : "Unknown Spine native error."; }

    static const char *safe(const String &value) { return value.buffer() ? value.buffer() : ""; }

    void rebuildPages() {
        pagePaths.clear();
        if (!atlas) return;
        Vector<AtlasPage *> &pages = atlas->getPages();
        for (size_t i = 0; i < pages.size(); ++i) {
            pagePaths.emplace_back(safe(pages[i]->texturePath));
        }
    }

    void selectDefaultAnimation() {
        animationIndex = -1;
        duration = 0;
        if (!data || data->getAnimations().size() == 0) {
            renderDirty = true;
            return;
        }
        Vector<Animation *> &animations = data->getAnimations();
        int fallbackDuration = -1;
        for (size_t i = 0; i < animations.size(); ++i) {
            const char *name = safe(animations[i]->getName());
            std::string lower(name);
            std::transform(lower.begin(), lower.end(), lower.begin(), [](unsigned char c) { return (char)std::tolower(c); });
            if (lower == "normal" || lower == "idle") {
                animationIndex = (int)i;
                break;
            }
            if (fallbackDuration < 0 && animations[i]->getDuration() > 0.01f) fallbackDuration = (int)i;
        }
        if (animationIndex < 0) animationIndex = fallbackDuration >= 0 ? fallbackDuration : 0;
        Animation *animation = animations[(size_t)animationIndex];
        duration = animation->getDuration();
        if (state) {
            state->setAnimation(0, animation->getName(), loop);
            state->apply(*skeleton);
        }
        renderDirty = true;
    }

    Animation *animationAt(int index) {
        if (!data || index < 0 || (size_t)index >= data->getAnimations().size()) return nullptr;
        return data->getAnimations()[(size_t)index];
    }

    static void appendAttachment(
        Bridge &bridge, Slot &slot, Attachment *attachment, SkeletonClipping &clipping,
        int32_t pageIndex, int32_t blend, const Color &color,
        const float *world, size_t worldCount, const float *uvs, size_t uvCount,
        const unsigned short *triangles, size_t triangleCount) {
        if (!attachment || pageIndex < 0 || !world || !uvs || !triangles || triangleCount < 3) return;
        std::vector<float> clippedWorld;
        std::vector<float> clippedUvs;
        std::vector<unsigned short> clippedTriangles;
        const float *drawWorld = world;
        const float *drawUvs = uvs;
        const unsigned short *drawTriangles = triangles;
        size_t drawWorldCount = worldCount;
        size_t drawUvCount = uvCount;
        size_t drawTriangleCount = triangleCount;
        if (clipping.isClipping()) {
            Vector<float> inWorld;
            Vector<float> inUvs;
            Vector<unsigned short> inTriangles;
            for (size_t i = 0; i < worldCount; ++i) inWorld.add(world[i]);
            for (size_t i = 0; i < uvCount; ++i) inUvs.add(uvs[i]);
            for (size_t i = 0; i < triangleCount; ++i) inTriangles.add(triangles[i]);
            clipping.clipTriangles(inWorld, inTriangles, inUvs, 2);
            Vector<float> &outWorld = clipping.getClippedVertices();
            Vector<unsigned short> &outTriangles = clipping.getClippedTriangles();
            Vector<float> &outUvs = clipping.getClippedUVs();
            if (outTriangles.size() == 0) return;
            clippedWorld.assign(outWorld.buffer(), outWorld.buffer() + outWorld.size());
            clippedUvs.assign(outUvs.buffer(), outUvs.buffer() + outUvs.size());
            clippedTriangles.assign(outTriangles.buffer(), outTriangles.buffer() + outTriangles.size());
            drawWorld = clippedWorld.data();
            drawUvs = clippedUvs.data();
            drawTriangles = clippedTriangles.data();
            drawWorldCount = clippedWorld.size();
            drawUvCount = clippedUvs.size();
            drawTriangleCount = clippedTriangles.size();
        }
        (void)drawWorldCount;
        (void)drawUvCount;
        SpineNativeBatch batch;
        batch.vertex_offset = (uint32_t)bridge.vertices.size();
        batch.index_offset = (uint32_t)bridge.indices.size();
        batch.page = pageIndex;
        batch.blend = blend;
        for (size_t i = 0; i < drawTriangleCount; ++i) {
            unsigned short index = drawTriangles[i];
            if ((size_t)index * 2 + 1 >= drawWorldCount || (size_t)index * 2 + 1 >= drawUvCount) continue;
            SpineNativeVertex vertex;
            vertex.x = drawWorld[(size_t)index * 2];
            vertex.y = drawWorld[(size_t)index * 2 + 1];
            vertex.u = drawUvs[(size_t)index * 2];
            vertex.v = drawUvs[(size_t)index * 2 + 1];
            vertex.r = color.r;
            vertex.g = color.g;
            vertex.b = color.b;
            vertex.a = color.a;
            bridge.vertices.push_back(vertex);
            bridge.indices.push_back((uint32_t)bridge.vertices.size() - 1);
        }
        batch.vertex_count = (uint32_t)bridge.vertices.size() - batch.vertex_offset;
        batch.index_count = (uint32_t)bridge.indices.size() - batch.index_offset;
        if (batch.index_count > 0) bridge.batches.push_back(batch);
    }

    int render() {
        if (!renderDirty) return (int)batches.size();
        vertices.clear();
        indices.clear();
        batches.clear();
        if (!skeleton || !atlas) {
            renderDirty = false;
            return 0;
        }
        skeleton->updateWorldTransform();
        SkeletonClipping clipping;
        Vector<Slot *> &drawOrder = skeleton->getDrawOrder();
        Vector<AtlasPage *> &pages = atlas->getPages();
        for (size_t slotIndex = 0; slotIndex < drawOrder.size(); ++slotIndex) {
            Slot *slot = drawOrder[slotIndex];
            if (!slot) continue;
            Attachment *attachment = slot->getAttachment();
            if (!attachment) {
                if (clipping.isClipping()) clipping.clipEnd(*slot);
                continue;
            }
            if (attachment->getRTTI().instanceOf(ClippingAttachment::rtti)) {
                clipping.clipStart(*slot, static_cast<ClippingAttachment *>(attachment));
                continue;
            }
            Color color = skeleton->getColor();
            color.r *= slot->getColor().r;
            color.g *= slot->getColor().g;
            color.b *= slot->getColor().b;
            color.a *= slot->getColor().a;
            int blend = (int)slot->getData().getBlendMode();
            int pageIndex = -1;
            if (RegionAttachment *region = dynamic_cast<RegionAttachment *>(attachment)) {
                AtlasRegion *atlasRegion = static_cast<AtlasRegion *>(region->getRendererObject());
                if (!atlasRegion) continue;
                for (size_t p = 0; p < pages.size(); ++p) if (pages[p] == atlasRegion->page) { pageIndex = (int)p; break; }
                float world[8];
                region->computeWorldVertices(slot->getBone(), world, 0, 2);
                Vector<float> &uvVector = region->getUVs();
                unsigned short tri[] = {0, 1, 2, 2, 3, 0};
                Color regionColor = color;
                regionColor.r *= region->getColor().r;
                regionColor.g *= region->getColor().g;
                regionColor.b *= region->getColor().b;
                regionColor.a *= region->getColor().a;
                appendAttachment(*this, *slot, attachment, clipping, pageIndex, blend, regionColor,
                                 world, 8, uvVector.buffer(), uvVector.size(), tri, 6);
            } else if (MeshAttachment *mesh = dynamic_cast<MeshAttachment *>(attachment)) {
                AtlasRegion *atlasRegion = static_cast<AtlasRegion *>(mesh->getRendererObject());
                if (!atlasRegion) continue;
                for (size_t p = 0; p < pages.size(); ++p) if (pages[p] == atlasRegion->page) { pageIndex = (int)p; break; }
                size_t worldCount = mesh->getWorldVerticesLength();
                Vector<float> world;
                world.setSize(worldCount, 0.0f);
                mesh->computeWorldVertices(*slot, 0, worldCount, world, 0, 2);
                Vector<float> &uvVector = mesh->getUVs();
                Vector<unsigned short> &triVector = mesh->getTriangles();
                Color meshColor = color;
                meshColor.r *= mesh->getColor().r;
                meshColor.g *= mesh->getColor().g;
                meshColor.b *= mesh->getColor().b;
                meshColor.a *= mesh->getColor().a;
                appendAttachment(*this, *slot, attachment, clipping, pageIndex, blend, meshColor,
                                 world.buffer(), world.size(), uvVector.buffer(), uvVector.size(),
                                 triVector.buffer(), triVector.size());
            }
            if (clipping.isClipping()) clipping.clipEnd(*slot);
        }
        clipping.clipEnd();
        renderDirty = false;
        return (int)batches.size();
    }
};

static Bridge *asBridge(void *handle) { return static_cast<Bridge *>(handle); }

} // namespace

extern "C" {

void *spine_native_create(void) { return new Bridge(); }

void spine_native_destroy(void *handle) { delete asBridge(handle); }

int spine_native_load_memory(void *handle, const void *skeleton_data, int32_t skeleton_size, int32_t format,
                             const void *atlas_data, int32_t atlas_size, const char *atlas_dir) {
    Bridge *bridge = asBridge(handle);
    if (!bridge) return 0;
    bridge->clear();
    bridge->error.clear();
    if (!skeleton_data || skeleton_size <= 0 || !atlas_data || atlas_size <= 0) {
        bridge->setError("Spine native input is empty.");
        return 0;
    }
    try {
        const char *directory = atlas_dir ? atlas_dir : "";
        bridge->atlas = new Atlas(static_cast<const char *>(atlas_data), atlas_size, directory, &bridge->loader, true);
        if (!bridge->atlas || bridge->atlas->getPages().size() == 0) {
            bridge->setError("Spine atlas contains no texture pages.");
            bridge->clear();
            return 0;
        }
        // Spine's runtime coordinates use a top-left atlas origin.  OpenGL's
        // texture coordinates use a bottom-left origin, so flip the region UVs
        // once before the skeleton attachment data is created.
        bridge->atlas->flipV();
        if (format == 0) {
            SkeletonJson reader(bridge->atlas);
            std::string json(static_cast<const char *>(skeleton_data), (size_t)skeleton_size);
            bridge->data = reader.readSkeletonData(json.c_str());
            if (!bridge->data) {
                bridge->error = Bridge::safe(reader.getError());
            }
        } else {
            SkeletonBinary reader(bridge->atlas);
            bridge->data = reader.readSkeletonData(static_cast<const unsigned char *>(skeleton_data), skeleton_size);
            if (!bridge->data) {
                bridge->error = Bridge::safe(reader.getError());
            }
        }
        if (!bridge->data) {
            if (bridge->error.empty()) bridge->setError("Spine skeleton could not be parsed by this native runtime.");
            bridge->clear();
            return 0;
        }
        bridge->skeleton = new Skeleton(bridge->data);
        bridge->stateData = new AnimationStateData(bridge->data);
        bridge->state = new AnimationState(bridge->stateData);
        bridge->version = Bridge::safe(bridge->data->getVersion());
        bridge->rebuildPages();
        bridge->selectDefaultAnimation();
        bridge->skeleton->updateWorldTransform();
        return 1;
    } catch (...) {
        bridge->setError("Spine native runtime raised an exception while loading the skeleton.");
        bridge->clear();
        return 0;
    }
}

const char *spine_native_last_error(void *handle) { return asBridge(handle) ? asBridge(handle)->error.c_str() : "Invalid Spine native handle."; }
const char *spine_native_skeleton_version(void *handle) { return asBridge(handle) ? asBridge(handle)->version.c_str() : ""; }

int32_t spine_native_animation_count(void *handle) { return asBridge(handle) && asBridge(handle)->data ? (int32_t)asBridge(handle)->data->getAnimations().size() : 0; }
const char *spine_native_animation_name(void *handle, int32_t index) {
    Bridge *b = asBridge(handle); Animation *a = b ? b->animationAt(index) : nullptr; return a ? a->getName().buffer() : "";
}
float spine_native_animation_duration(void *handle, int32_t index) { Bridge *b = asBridge(handle); Animation *a = b ? b->animationAt(index) : nullptr; return a ? a->getDuration() : 0.0f; }
int32_t spine_native_skin_count(void *handle) { return asBridge(handle) && asBridge(handle)->data ? (int32_t)asBridge(handle)->data->getSkins().size() : 0; }
const char *spine_native_skin_name(void *handle, int32_t index) {
    Bridge *b = asBridge(handle); if (!b || !b->data || index < 0 || (size_t)index >= b->data->getSkins().size()) return ""; return b->data->getSkins()[(size_t)index]->getName().buffer();
}
int32_t spine_native_page_count(void *handle) { return asBridge(handle) && asBridge(handle)->atlas ? (int32_t)asBridge(handle)->atlas->getPages().size() : 0; }
SpineNativePageInfo spine_native_page_info(void *handle, int32_t index) {
    SpineNativePageInfo info{"", 0, 0, 0}; Bridge *b = asBridge(handle); if (!b || !b->atlas || index < 0 || (size_t)index >= b->atlas->getPages().size()) return info; AtlasPage *p = b->atlas->getPages()[(size_t)index]; info.path = p->texturePath.buffer(); info.width = p->width; info.height = p->height;
#if defined(SPINE_BRIDGE_RUNTIME_40)
    info.pma = p->pma ? 1 : 0;
#endif
    return info;
}

int spine_native_set_skin(void *handle, const char *name) {
    Bridge *b = asBridge(handle); if (!b || !b->skeleton || !name) return 0; b->skeleton->setSkin(String(name)); b->skeleton->setSlotsToSetupPose(); if (b->state) b->state->apply(*b->skeleton); b->renderDirty = true; return b->skeleton->getSkin() != nullptr;
}
int spine_native_set_animation(void *handle, const char *name, int loop) {
    Bridge *b = asBridge(handle); if (!b || !b->state || !b->skeleton || !name) return 0; Animation *animation = b->data->findAnimation(String(name)); if (!animation) return 0; b->loop = loop != 0; b->duration = animation->getDuration(); b->state->setAnimation(0, animation->getName(), b->loop); b->state->apply(*b->skeleton); b->renderDirty = true; return 1;
}
int spine_native_set_loop(void *handle, int loop) { Bridge *b = asBridge(handle); if (!b || !b->state || !b->skeleton) return 0; b->loop = loop != 0; TrackEntry *entry = b->state->getCurrent(0); if (entry) { entry->setLoop(b->loop); float trackTime = std::max(0.0f, entry->getTrackTime()); if (b->duration > 0.0f) trackTime = b->loop ? std::fmod(trackTime, b->duration) : std::min(trackTime, b->duration); entry->setTrackTime(trackTime); b->state->apply(*b->skeleton); b->skeleton->updateWorldTransform(); } b->renderDirty = true; return 1; }
int spine_native_set_paused(void *handle, int paused) { Bridge *b = asBridge(handle); if (!b) return 0; b->paused = paused != 0; return 1; }
int spine_native_set_time(void *handle, float time) { Bridge *b = asBridge(handle); if (!b || !b->state || !b->skeleton) return 0; if (!std::isfinite(time)) time = 0.0f; if (b->duration > 0) time = b->loop ? std::fmod(std::max(0.0f, time), b->duration) : std::min(std::max(0.0f, time), b->duration); else time = std::max(0.0f, time); TrackEntry *entry = b->state->getCurrent(0); if (!entry) return 0; entry->setTrackTime(time); b->state->apply(*b->skeleton); b->skeleton->updateWorldTransform(); b->renderDirty = true; return 1; }
int spine_native_reset_pose(void *handle) { Bridge *b = asBridge(handle); if (!b || !b->skeleton || !b->state) return 0; b->skeleton->setToSetupPose(); b->state->clearTracks(); b->selectDefaultAnimation(); return 1; }
float spine_native_time(void *handle) { Bridge *b = asBridge(handle); TrackEntry *entry = b && b->state ? b->state->getCurrent(0) : nullptr; if (!entry) return 0.0f; float time = std::max(0.0f, entry->getTrackTime()); if (b->duration > 0.0f) time = b->loop ? std::fmod(time, b->duration) : std::min(time, b->duration); return time; }
float spine_native_duration(void *handle) { return asBridge(handle) ? asBridge(handle)->duration : 0.0f; }

void spine_native_update(void *handle, float delta) {
    Bridge *b = asBridge(handle); if (!b || !b->state || !b->skeleton || b->paused) return;
    float advance = std::isfinite(delta) ? std::max(0.0f, delta) : 0.0f;
    TrackEntry *entry = b->state->getCurrent(0);
    if (!b->loop && entry && b->duration > 0.0f) {
        float current = std::max(0.0f, entry->getTrackTime());
        if (current >= b->duration) {
            entry->setTrackTime(b->duration);
            b->state->apply(*b->skeleton);
            b->skeleton->updateWorldTransform();
            b->renderDirty = true;
            return;
        }
        advance = std::min(advance, b->duration - current);
    }
    b->state->update(advance);
    b->state->apply(*b->skeleton);
    entry = b->state->getCurrent(0);
    if (!b->loop && entry && b->duration > 0.0f && entry->getTrackTime() > b->duration) entry->setTrackTime(b->duration);
    b->skeleton->updateWorldTransform();
    b->renderDirty = true;
}
int spine_native_required_sizes(void *handle, int32_t *vertices, int32_t *indices, int32_t *batches) { Bridge *b = asBridge(handle); if (!b) return 0; b->render(); if (vertices) *vertices = (int32_t)b->vertices.size(); if (indices) *indices = (int32_t)b->indices.size(); if (batches) *batches = (int32_t)b->batches.size(); return 1; }
int spine_native_render(void *handle, SpineNativeVertex *vertices, int32_t vertex_capacity, uint32_t *indices, int32_t index_capacity, SpineNativeBatch *batches, int32_t batch_capacity) {
    Bridge *b = asBridge(handle); if (!b) return -1; b->render(); if ((int32_t)b->vertices.size() > vertex_capacity || (int32_t)b->indices.size() > index_capacity || (int32_t)b->batches.size() > batch_capacity) return -1; if (!b->vertices.empty() && vertices) std::memcpy(vertices, b->vertices.data(), b->vertices.size() * sizeof(SpineNativeVertex)); if (!b->indices.empty() && indices) std::memcpy(indices, b->indices.data(), b->indices.size() * sizeof(uint32_t)); if (!b->batches.empty() && batches) std::memcpy(batches, b->batches.data(), b->batches.size() * sizeof(SpineNativeBatch)); return (int)b->batches.size();
}
int spine_native_bounds(void *handle, float *x, float *y, float *width, float *height) { Bridge *b = asBridge(handle); if (!b || !b->skeleton) return 0; Vector<float> buffer; float bx=0, by=0, bw=0, bh=0; b->skeleton->getBounds(bx, by, bw, bh, buffer); if (x) *x=bx; if (y) *y=by; if (width) *width=bw; if (height) *height=bh; return 1; }

} // extern "C"
