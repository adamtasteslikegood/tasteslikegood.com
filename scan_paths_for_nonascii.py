#!/usr/bin/env python3
"""
Script to scan all file paths in the repository for non-ASCII characters.
Generates a markdown report: repowidepathscannonascii.md
"""

import os
from pathlib import Path


def is_ascii_path(path_str):
    """Check if a path string contains only ASCII characters."""
    try:
        path_str.encode('ascii')
        return True
    except UnicodeEncodeError:
        return False


def scan_repository_paths(root_dir):
    """
    Scan all file paths in the repository for non-ASCII characters.
    
    Args:
        root_dir: The root directory of the repository
        
    Returns:
        A tuple of (all_paths, non_ascii_paths)
    """
    all_paths = []
    non_ascii_paths = []
    
    # Walk through the entire repository
    for dirpath, dirnames, filenames in os.walk(root_dir):
        # Skip .git directory
        if '.git' in dirnames:
            dirnames.remove('.git')
        
        # Get relative path from root
        rel_dirpath = os.path.relpath(dirpath, root_dir)
        
        # Check directory path
        if rel_dirpath != '.':
            all_paths.append(rel_dirpath)
            if not is_ascii_path(rel_dirpath):
                non_ascii_paths.append(('directory', rel_dirpath))
        
        # Check each file in this directory
        for filename in filenames:
            rel_filepath = os.path.join(rel_dirpath, filename)
            if rel_dirpath == '.':
                rel_filepath = filename
            
            all_paths.append(rel_filepath)
            if not is_ascii_path(rel_filepath):
                non_ascii_paths.append(('file', rel_filepath))
    
    return all_paths, non_ascii_paths


def generate_report(all_paths, non_ascii_paths, output_file):
    """
    Generate a markdown report of the scan results.
    
    Args:
        all_paths: List of all paths scanned
        non_ascii_paths: List of tuples (type, path) with non-ASCII characters
        output_file: Path to the output markdown file
    """
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write("# Repository-Wide Path ASCII Scan Report\n\n")
        
        f.write(f"**Scan Date:** {Path().absolute()}\n\n")
        f.write(f"**Total Paths Scanned:** {len(all_paths)}\n\n")
        f.write(f"**Paths with Non-ASCII Characters:** {len(non_ascii_paths)}\n\n")
        
        if non_ascii_paths:
            f.write("## Paths Containing Non-ASCII Characters\n\n")
            f.write("The following paths contain non-ASCII (non-standard) characters:\n\n")
            
            # Separate by type
            dirs = [p for t, p in non_ascii_paths if t == 'directory']
            files = [p for t, p in non_ascii_paths if t == 'file']
            
            if dirs:
                f.write("### Directories\n\n")
                for path in dirs:
                    # Show both the path and hex representation of non-ASCII chars
                    non_ascii_chars = [c for c in path if ord(c) > 127]
                    f.write(f"- `{path}`\n")
                    if non_ascii_chars:
                        f.write(f"  - Non-ASCII characters: {', '.join(repr(c) for c in non_ascii_chars)}\n")
                f.write("\n")
            
            if files:
                f.write("### Files\n\n")
                for path in files:
                    # Show both the path and hex representation of non-ASCII chars
                    non_ascii_chars = [c for c in path if ord(c) > 127]
                    f.write(f"- `{path}`\n")
                    if non_ascii_chars:
                        f.write(f"  - Non-ASCII characters: {', '.join(repr(c) for c in non_ascii_chars)}\n")
                f.write("\n")
            
            f.write("## Recommendations\n\n")
            f.write("Non-ASCII characters in file paths can cause issues with:\n")
            f.write("- Cross-platform compatibility (Windows, macOS, Linux)\n")
            f.write("- Version control systems\n")
            f.write("- Build and deployment tools\n")
            f.write("- Shell scripts and command-line operations\n\n")
            f.write("Consider renaming these paths to use only ASCII characters (A-Z, a-z, 0-9, hyphens, underscores).\n")
        else:
            f.write("## Results\n\n")
            f.write("✅ **All paths in the repository contain only ASCII characters.**\n\n")
            f.write("This is excellent for cross-platform compatibility and tooling support.\n")


def main():
    """Main function to run the path scan."""
    # Get the repository root directory
    script_dir = Path(__file__).parent.absolute()
    repo_root = script_dir
    
    print(f"Scanning repository at: {repo_root}")
    print("This may take a moment for large repositories...\n")
    
    # Scan all paths
    all_paths, non_ascii_paths = scan_repository_paths(repo_root)
    
    print(f"Total paths scanned: {len(all_paths)}")
    print(f"Paths with non-ASCII characters: {len(non_ascii_paths)}")
    
    if non_ascii_paths:
        print("\nNon-ASCII paths found:")
        for path_type, path in non_ascii_paths:
            print(f"  [{path_type}] {path}")
    
    # Generate the report
    output_file = repo_root / "repowidepathscannonascii.md"
    generate_report(all_paths, non_ascii_paths, output_file)
    
    print(f"\n✅ Report generated: {output_file}")


if __name__ == "__main__":
    main()
