"""Pinned tool package metadata used by the optional tool installer.

The manifest intentionally contains only artifacts whose source, version and
digest are known.  It is data rather than download logic so the native Spine
builder can register its own output contract without making the settings page
know how C++ compilation works.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolPackageManifest:
    """One version-pinned package or a licensed/manual runtime source."""

    package_id: str
    display_name: str
    version: str
    install_dir: str
    artifact_url: str | None = None
    artifact_sha256: str | None = None
    artifact_size: int | None = None
    artifact_filename: str | None = None
    expected_files: tuple[str, ...] = ()
    entrypoint: str | None = None
    license_name: str = ""
    license_url: str | None = None
    # Relative path copied into the installed runtime root when a source
    # package carries a redistributable license document.
    license_file: str | None = None
    source_url: str | None = None
    source_commit: str | None = None
    official_download_url: str | None = None
    distribution: str = "archive"
    build_provider: str | None = None
    runtime_family: str | None = None
    notes: str = ""

    @property
    def is_pinned_archive(self) -> bool:
        return bool(self.artifact_url and self.artifact_sha256)

    @property
    def requires_builder(self) -> bool:
        return self.distribution == "source_build"

    @property
    def is_manual_license_download(self) -> bool:
        return self.distribution == "licensed_manual"

    def as_dict(self) -> dict[str, object]:
        """Serialize provenance for an installed package manifest."""

        return {
            "package_id": self.package_id,
            "display_name": self.display_name,
            "version": self.version,
            "install_dir": self.install_dir,
            "artifact_url": self.artifact_url,
            "artifact_sha256": self.artifact_sha256,
            "artifact_size": self.artifact_size,
            "artifact_filename": self.artifact_filename,
            "expected_files": list(self.expected_files),
            "entrypoint": self.entrypoint,
            "license_name": self.license_name,
            "license_url": self.license_url,
            "license_file": self.license_file,
            "source_url": self.source_url,
            "source_commit": self.source_commit,
            "official_download_url": self.official_download_url,
            "distribution": self.distribution,
            "build_provider": self.build_provider,
            "runtimeFamily": self.runtime_family,
            "notes": self.notes,
        }


ASSETSTUDIO_MANIFEST = ToolPackageManifest(
    package_id="assetstudio_cli",
    display_name="AssetStudioModCLI",
    version="0.19.0",
    install_dir="AssetStudioCLI",
    artifact_url=(
        "https://github.com/aelurum/AssetStudioMod/releases/download/"
        "v0.19.0/AssetStudioModCLI_net472_win32_64.zip"
    ),
    artifact_sha256="cad5b9e2084dda85007012f94c6d3ad1944ba153796c4e80027792d0ba7be285",
    artifact_size=10_992_121,
    artifact_filename="AssetStudioModCLI_net472_win32_64.zip",
    expected_files=("AssetStudioModCLI_net472_win32_64/AssetStudioModCLI.exe",),
    entrypoint="AssetStudioModCLI_net472_win32_64/AssetStudioModCLI.exe",
    license_name="MIT License",
    license_url="https://raw.githubusercontent.com/aelurum/AssetStudioMod/AssetStudioMod/LICENSE",
    source_url="https://github.com/aelurum/AssetStudioMod/releases/tag/v0.19.0",
    notes="The archive is the official AssetStudioMod v0.19.0 CLI release.",
)


SPINE_NATIVE_MANIFEST = ToolPackageManifest(
    package_id="spine_native",
    display_name="Spine native runtime",
    version="3.8.75",
    install_dir="spine_native/3.8.75",
    # Pin both the commit and the generated archive digest.  A branch URL is
    # unsuitable here: GitHub can regenerate it after the manifest is
    # released, while the digest must remain a reproducible build input.
    artifact_url=(
        "https://codeload.github.com/EsotericSoftware/spine-runtimes/zip/"
        "c0699e23a0c8799710323bdf0e076e18f6ba41a2"
    ),
    artifact_sha256="a1e130bd8a01f06aedc52887b37cc61ca7cddb7ba9e842bf870b5756f2598ffe",
    artifact_size=90_707_479,
    artifact_filename="spine-runtimes-c0699e23a0c8799710323bdf0e076e18f6ba41a2.zip",
    source_url="https://github.com/EsotericSoftware/spine-runtimes/commit/c0699e23a0c8799710323bdf0e076e18f6ba41a2",
    source_commit="c0699e23a0c8799710323bdf0e076e18f6ba41a2",
    license_name="Spine Runtimes License Agreement",
    license_url=(
        "https://github.com/EsotericSoftware/spine-runtimes/blob/"
        "c0699e23a0c8799710323bdf0e076e18f6ba41a2/LICENSE"
    ),
    license_file="LICENSE",
    distribution="source_build",
    build_provider="app.core.spine_embed.build_native_runtime",
    runtime_family="3.8",
    notes=(
        "This is a pinned source archive for the spine_embed native builder; "
        "it is not the browser spine-ts runtime and is never installed as web assets."
    ),
)


SPINE_NATIVE_40_MANIFEST = ToolPackageManifest(
    package_id="spine_native_4_0",
    display_name="Spine native runtime 4.0",
    version="4.0",
    install_dir="spine_native/4.0",
    artifact_url=(
        "https://codeload.github.com/EsotericSoftware/spine-runtimes/zip/"
        "425ce416bb218b28caeec47b317aa57cd7140375"
    ),
    artifact_sha256="b273c58cf318c0018415d55c926d1ead9362e59d3db7e51d1a4e86f53a9af3a6",
    artifact_size=84_355_288,
    artifact_filename="spine-runtimes-425ce416bb218b28caeec47b317aa57cd7140375.zip",
    source_url="https://github.com/EsotericSoftware/spine-runtimes/commit/425ce416bb218b28caeec47b317aa57cd7140375",
    source_commit="425ce416bb218b28caeec47b317aa57cd7140375",
    license_name="Spine Runtimes License Agreement",
    license_url=(
        "https://github.com/EsotericSoftware/spine-runtimes/blob/"
        "425ce416bb218b28caeec47b317aa57cd7140375/LICENSE"
    ),
    license_file="LICENSE",
    distribution="source_build",
    build_provider="app.core.spine_embed.build_native_runtime",
    runtime_family="4.0",
    notes=(
        "This is a pinned 4.0 source archive for the spine_embed native builder; "
        "it is not the browser spine-ts runtime and is never installed as web assets."
    ),
)


CUBISM_CORE_MANIFEST = ToolPackageManifest(
    package_id="cubism_core",
    display_name="Live2D Cubism Core",
    version="official-sdk",
    install_dir="CubismCore",
    expected_files=("Live2DCubismCore.dll",),
    entrypoint="Live2DCubismCore.dll",
    license_name="Live2D Proprietary Software License Agreement",
    license_url="https://www.live2d.com/eula/live2d-proprietary-software-license-agreement_en.html",
    official_download_url="https://www.live2d.com/en/sdk/download/native/",
    distribution="licensed_manual",
    notes=(
        "Cubism Core is closed-source and is distributed inside the official "
        "Live2D SDK package. The installer never fabricates a public archive URL."
    ),
)


TOOLCHAIN_MANIFEST: dict[str, ToolPackageManifest] = {
    item.package_id: item
    for item in (
        ASSETSTUDIO_MANIFEST,
        SPINE_NATIVE_MANIFEST,
        SPINE_NATIVE_40_MANIFEST,
        CUBISM_CORE_MANIFEST,
    )
}


def register_tool_package_manifest(manifest: ToolPackageManifest) -> None:
    """Register a builder-provided manifest without changing built-ins."""

    if not isinstance(manifest, ToolPackageManifest) or not manifest.package_id.strip():
        raise ValueError("A tool package manifest with a package_id is required.")
    TOOLCHAIN_MANIFEST[manifest.package_id] = manifest


def get_tool_package_manifest(package_id: str) -> ToolPackageManifest:
    """Return a pinned manifest or raise a clear unknown-package error."""

    key = str(package_id or "").strip().casefold()
    for package_key, manifest in TOOLCHAIN_MANIFEST.items():
        if package_key.casefold() == key:
            return manifest
    available = ", ".join(sorted(TOOLCHAIN_MANIFEST))
    raise KeyError(f"Unknown tool package {package_id!r}; available packages: {available}")


__all__ = [
    "ASSETSTUDIO_MANIFEST",
    "CUBISM_CORE_MANIFEST",
    "SPINE_NATIVE_MANIFEST",
    "SPINE_NATIVE_40_MANIFEST",
    "TOOLCHAIN_MANIFEST",
    "ToolPackageManifest",
    "get_tool_package_manifest",
    "register_tool_package_manifest",
]
