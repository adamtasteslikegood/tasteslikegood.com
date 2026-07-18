#!/usr/bin/env python3
"""
Check for and optionally fix non-ASCII characters in file and directory names.

This script scans a repository for files and directories with non-ASCII characters
in their names, logs the findings, and optionally renames them to ASCII-safe names.
"""

import os
import sys
import argparse
import re
import unicodedata
from datetime import datetime


def normalize_filename(filename):
    """
    Convert a filename with non-ASCII characters to ASCII-safe format.

    Args:
        filename: The original filename

    Returns:
        A normalized ASCII-safe filename
    """
    # First, try to decompose and remove accents
    nfd = unicodedata.normalize("NFD", filename)
    ascii_filename = "".join(char for char in nfd if unicodedata.category(char) != "Mn")

    # Replace any remaining non-ASCII characters with underscores
    ascii_filename = re.sub(r"[^\x00-\x7F]+", "_", ascii_filename)

    # Clean up multiple underscores
    ascii_filename = re.sub(r"_+", "_", ascii_filename)

    return ascii_filename


def is_ascii(text):
    """Check if text contains only ASCII characters."""
    try:
        text.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def find_non_ascii_paths(root_dir, exclude_dirs=None):
    """
    Find all files and directories with non-ASCII characters in their names.

    Args:
        root_dir: Root directory to scan
        exclude_dirs: Set of directory names to exclude (e.g., {'.git', 'node_modules'})

    Returns:
        List of tuples (full_path, name, is_directory)
    """
    if exclude_dirs is None:
        exclude_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv"}

    non_ascii_paths = []

    for root, dirs, files in os.walk(root_dir):
        # Remove excluded directories from dirs list to prevent walking into them
        dirs[:] = [d for d in dirs if d not in exclude_dirs]

        # Check directory names
        for dir_name in dirs:
            if not is_ascii(dir_name):
                full_path = os.path.join(root, dir_name)
                non_ascii_paths.append((full_path, dir_name, True))

        # Check file names
        for file_name in files:
            if not is_ascii(file_name):
                full_path = os.path.join(root, file_name)
                non_ascii_paths.append((full_path, file_name, False))

    return non_ascii_paths


def log_findings(non_ascii_paths, log_file, action_taken="detected"):
    """
    Log findings to a file.

    Args:
        non_ascii_paths: List of tuples (full_path, name, is_directory)
        log_file: Path to log file
        action_taken: Description of action taken ('detected', 'renamed', or 'dry-run')

    Returns:
        None
    """
    timestamp = datetime.now().isoformat()

    with open(log_file, "a", encoding="utf-8") as f:
        f.write(f"\n{'='*80}\n")
        f.write(f"Scan Date: {timestamp}\n")
        f.write(f"Action: {action_taken}\n")
        f.write(f"Total non-ASCII paths found: {len(non_ascii_paths)}\n")
        f.write(f"{'='*80}\n\n")

        if non_ascii_paths:
            for full_path, name, is_dir in non_ascii_paths:
                path_type = "Directory" if is_dir else "File"
                normalized = normalize_filename(name)
                f.write(f"{path_type}: {full_path}\n")
                f.write(f"  Original name: {name}\n")
                f.write(f"  Normalized name: {normalized}\n")
                f.write(f"  Non-ASCII chars: {[c for c in name if ord(c) > 127]}\n")
                f.write(f"\n")
        else:
            f.write("No non-ASCII paths found.\n")


def rename_paths(non_ascii_paths, dry_run=False):
    """
    Rename files and directories to ASCII-safe names.

    Args:
        non_ascii_paths: List of tuples (full_path, name, is_directory)
        dry_run: If True, only simulate the renaming

    Returns:
        List of tuples (old_path, new_path, success, error_message)
    """
    results = []

    # Sort by depth (deepest first) to handle directories before their parents
    sorted_paths = sorted(non_ascii_paths, key=lambda x: x[0].count(os.sep), reverse=True)

    for full_path, name, is_dir in sorted_paths:
        parent_dir = os.path.dirname(full_path)
        normalized_name = normalize_filename(name)
        new_path = os.path.join(parent_dir, normalized_name)

        if dry_run:
            results.append((full_path, new_path, True, None))
        else:
            try:
                # Check if target already exists
                if os.path.exists(new_path):
                    error_msg = f"Target path already exists: {new_path}"
                    results.append((full_path, new_path, False, error_msg))
                else:
                    os.rename(full_path, new_path)
                    results.append((full_path, new_path, True, None))
            except Exception as e:
                results.append((full_path, new_path, False, str(e)))

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Check for and optionally fix non-ASCII characters in file and directory names."
    )
    parser.add_argument(
        "--root-dir", default=".", help="Root directory to scan (default: current directory)"
    )
    parser.add_argument(
        "--log-file",
        default="non_ascii_filenames.log",
        help="Path to log file (default: non_ascii_filenames.log)",
    )
    parser.add_argument(
        "--fix", action="store_true", help="Rename files and directories to ASCII-safe names"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Simulate renaming without actually changing files"
    )
    parser.add_argument(
        "--fail-on-found",
        action="store_true",
        help="Exit with non-zero status if non-ASCII paths are found",
    )

    args = parser.parse_args()

    root_dir = os.path.abspath(args.root_dir)

    print(f"Scanning directory: {root_dir}")
    print(f"Log file: {args.log_file}")

    # Find non-ASCII paths
    non_ascii_paths = find_non_ascii_paths(root_dir)

    if non_ascii_paths:
        print(f"\n⚠️  Found {len(non_ascii_paths)} path(s) with non-ASCII characters:\n")

        for full_path, name, is_dir in non_ascii_paths:
            path_type = "📁 Directory" if is_dir else "📄 File"
            normalized = normalize_filename(name)
            print(f"{path_type}: {full_path}")
            print(f"   Original: {name}")
            print(f"   → Would become: {normalized}")
            print()

        # Log the findings
        action = "detected"
        if args.fix or args.dry_run:
            action = "dry-run" if args.dry_run else "renamed"

        log_findings(non_ascii_paths, args.log_file, action)

        # Rename if requested
        if args.fix or args.dry_run:
            print(f"\n{'='*80}")
            if args.dry_run:
                print("DRY RUN MODE - No files will be modified")
            else:
                print("RENAMING FILES...")
            print(f"{'='*80}\n")

            results = rename_paths(non_ascii_paths, dry_run=args.dry_run)

            success_count = sum(1 for _, _, success, _ in results if success)
            failure_count = len(results) - success_count

            for old_path, new_path, success, error in results:
                if success:
                    status = "✓" if not args.dry_run else "→"
                    print(f"{status} {old_path}")
                    print(f"   → {new_path}")
                else:
                    print(f"✗ Failed to rename: {old_path}")
                    print(f"   Error: {error}")
                print()

            print(f"\nResults: {success_count} succeeded, {failure_count} failed")

        # Exit with error if requested
        if args.fail_on_found:
            print(f"\n❌ Exiting with error status (--fail-on-found was set)")
            sys.exit(1)
    else:
        print("✓ No non-ASCII characters found in any file or directory names.")
        log_findings([], args.log_file, "detected")
        sys.exit(0)


if __name__ == "__main__":
    main()
