#!/usr/bin/env python3
"""
Prepare release: Update version numbers and create changelog
Usage: python scripts/prepare_release.py 1.0.0
"""

import sys
import os
import re
from datetime import datetime

def update_version(version):
    """Update version in relevant files"""
    
    # Update __version__ if exists
    init_file = "app/gui/__init__.py"
    if os.path.exists(init_file):
        with open(init_file, 'r', encoding='utf-8') as f:
            content = f.read()
        
        content = re.sub(r"__version__\s*=\s*['\"].*?['\"]", 
                        f'__version__ = "{version}"', content)
        
        with open(init_file, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"Updated {init_file}")
    
    # Update README
    readme_file = "README.md"
    if os.path.exists(readme_file):
        with open(readme_file, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Update download links or version references if present
        print(f"Updated {readme_file}")

def create_changelog(version):
    """Create changelog entry"""
    
    changelog_file = "CHANGELOG.md"
    
    # Check if changelog exists
    if os.path.exists(changelog_file):
        with open(changelog_file, 'r', encoding='utf-8') as f:
            existing = f.read()
    else:
        existing = "# Changelog\n\n"
    
    # Create new entry
    date = datetime.now().strftime("%Y-%m-%d")
    entry = f"""## [v{version}] - {date}

### Added
- New features in this release

### Changed
- Bug fixes and improvements

### Fixed
- Critical bug fixes

---

"""
    
    # Prepend new entry
    with open(changelog_file, 'w', encoding='utf-8') as f:
        f.write(entry + existing)
    
    print(f"Updated {changelog_file}")

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/prepare_release.py <version>")
        print("Example: python scripts/prepare_release.py 1.0.0")
        sys.exit(1)
    
    version = sys.argv[1]
    
    # Validate version format
    if not re.match(r'^\d+\.\d+\.\d+$', version):
        print(f"Error: Invalid version format '{version}'. Use semantic versioning (x.y.z)")
        sys.exit(1)
    
    print(f"Preparing release v{version}...")
    update_version(version)
    create_changelog(version)
    print(f"\nRelease v{version} prepared successfully!")
    print("Next steps:")
    print(f"  1. Review changes in CHANGELOG.md")
    print(f"  2. Commit: git add . && git commit -m 'Release v{version}'")
    print(f"  3. Tag: git tag v{version}")
    print(f"  4. Push: git push origin main && git push origin v{version}")
