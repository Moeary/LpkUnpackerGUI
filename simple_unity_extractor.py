import os
import sys
import argparse
from typing import List

# Setup path to include Core
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

try:
    from Core.image_extractor import ImageExtractor
    import UnityPy
except ImportError as e:
    print(f"Error: Required modules not found. {e}")
    print("Please install requirements: pip install UnityPy pillow")
    sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="Extract textures from Unity asset files.")
    parser.add_argument("input", nargs="+", help="Input files or folders")
    parser.add_argument("-o", "--output", default="output/unity_images", help="Output directory")
    
    args = parser.parse_args()
    
    output_dir = os.path.abspath(args.output)
    os.makedirs(output_dir, exist_ok=True)
    
    files_to_process = []
    for item in args.input:
        abs_item = os.path.abspath(item)
        if os.path.isfile(abs_item):
            files_to_process.append(abs_item)
        elif os.path.isdir(abs_item):
            for root, dirs, files in os.walk(abs_item):
                for filename in files:
                    # Unity files often have no extension
                    if '.' not in filename:
                        files_to_process.append(os.path.join(root, filename))
    
    if not files_to_process:
        print("No files found to process.")
        return

    print(f"Found {len(files_to_process)} potential Unity files.")
    
    total_extracted = 0
    total_skipped = 0
    
    for i, file_path in enumerate(files_to_process):
        print(f"[{i+1}/{len(files_to_process)}] Processing: {os.path.basename(file_path)}")
        
        # Check if it's a Unity file
        file_type = ImageExtractor.detect_file_type(file_path)
        if file_type == "UNITY":
            try:
                extractor = ImageExtractor(file_path)
                extracted, skipped = extractor.extract_images(output_dir)
                total_extracted += extracted
                total_skipped += skipped
            except Exception as e:
                print(f"  Error: {e}")
        else:
            print("  Not a Unity file, skipping.")
            
    print("\nExtraction Complete!")
    print(f"Total extracted: {total_extracted}")
    print(f"Total skipped:   {total_skipped}")
    print(f"Output saved to: {output_dir}")

if __name__ == "__main__":
    main()
