#!/usr/bin/env python3
"""
Script to scan all file paths in the repository for non-ASCII characters.
Generates a markdown report: repowidepathscannonascii.md
"""

import argparse
import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path


def is_ascii_path(path_str):
    """Check if a path string contains only ASCII characters."""
    try:
        path_str.encode('ascii')
        return True
    except UnicodeEncodeError:
        return False


def get_all_branches(repo_root):
    """
    Get a list of all branch names in the repository.
    
    Args:
        repo_root: Path to the repository root
        
    Returns:
        List of branch names (without 'origin/' prefix)
    """
    try:
        # Try to get remote branches first
        result = subprocess.run(
            ['git', 'branch', '-r'],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False
        )
        branches = []
        
        if result.returncode == 0 and result.stdout.strip():
            for line in result.stdout.strip().split('\n'):
                line = line.strip()
                if line and '->' not in line:  # Skip HEAD -> references
                    # Remove 'origin/' prefix
                    branch = line.replace('origin/', '')
                    branches.append(branch)
        
        # If no remote branches found, try local branches
        if not branches:
            result = subprocess.run(
                ['git', 'branch'],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=True
            )
            for line in result.stdout.strip().split('\n'):
                line = line.strip()
                if line:
                    # Remove the * indicator for current branch
                    branch = line.lstrip('* ')
                    branches.append(branch)
        
        return sorted(set(branches))  # Remove duplicates and sort
    except subprocess.CalledProcessError as e:
        print(f"Error getting branches: {e}")
        return []


def scan_repository_paths_safe(root_dir, skip_git=True):
    """
    Scan all file paths with error tracking.
    
    Args:
        root_dir: The root directory of the repository
        skip_git: If True, skip the .git directory (default: True)
        
    Returns:
        A tuple of (all_paths, non_ascii_paths, error_paths)
    """
    all_paths = []
    non_ascii_paths = []
    error_paths = []
    
    # Walk through the entire repository
    try:
        for dirpath, dirnames, filenames in os.walk(root_dir):
            try:
                # Conditionally skip .git directory
                if skip_git and '.git' in dirnames:
                    dirnames.remove('.git')
                
                # Get relative path from root
                rel_dirpath = os.path.relpath(dirpath, root_dir)
                
                # Check directory path
                if rel_dirpath != '.':
                    try:
                        all_paths.append(rel_dirpath)
                        if not is_ascii_path(rel_dirpath):
                            non_ascii_paths.append(('directory', rel_dirpath))
                    except Exception as e:
                        error_paths.append(('directory', rel_dirpath, str(e)))
                
                # Check each file in this directory
                for filename in filenames:
                    try:
                        rel_filepath = os.path.join(rel_dirpath, filename)
                        if rel_dirpath == '.':
                            rel_filepath = filename
                        
                        all_paths.append(rel_filepath)
                        if not is_ascii_path(rel_filepath):
                            non_ascii_paths.append(('file', rel_filepath))
                    except Exception as e:
                        error_paths.append(('file', filename, str(e)))
            except Exception as e:
                error_paths.append(('directory_walk', dirpath, str(e)))
    except Exception as e:
        error_paths.append(('root_walk', str(root_dir), str(e)))
    
    return all_paths, non_ascii_paths, error_paths


def scan_branch(repo_root, branch_name, skip_git=True):
    """
    Scan a specific branch by checking it out in a temporary worktree.
    
    Args:
        repo_root: Path to the repository root
        branch_name: Name of the branch to scan
        skip_git: If True, skip the .git directory
        
    Returns:
        Dictionary with scan results for the branch
    """
    result = {
        'branch': branch_name,
        'scanned': 0,
        'successful': 0,
        'errors': 0,
        'non_ascii': 0,
        'non_ascii_paths': [],
        'error_paths': [],
        'checkout_error': None
    }
    
    # Create a temporary directory for the worktree
    with tempfile.TemporaryDirectory() as temp_dir:
        worktree_path = Path(temp_dir) / 'scan_worktree'
        
        try:
            # Try with origin/ prefix first, then without if that fails
            branch_ref = f'origin/{branch_name}'
            
            # Check if the reference exists
            check_result = subprocess.run(
                ['git', 'rev-parse', '--verify', branch_ref],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False
            )
            
            # If origin/branch doesn't exist, try local branch
            if check_result.returncode != 0:
                # Only try local branch if it looks like a valid branch name
                # Avoid inadvertently matching HEAD, tags, or other refs
                if branch_name in ['HEAD', 'FETCH_HEAD', 'ORIG_HEAD', 'MERGE_HEAD']:
                    result['checkout_error'] = f"Invalid branch name '{branch_name}' - use explicit branch names only"
                    return result
                
                branch_ref = branch_name
                check_result = subprocess.run(
                    ['git', 'rev-parse', '--verify', branch_ref],
                    cwd=repo_root,
                    capture_output=True,
                    text=True,
                    check=False
                )
                
                if check_result.returncode != 0:
                    result['checkout_error'] = f"Branch '{branch_name}' not found (tried 'origin/{branch_name}' and '{branch_name}')"
                    return result
            
            # Add a worktree for this branch
            subprocess.run(
                ['git', 'worktree', 'add', str(worktree_path), branch_ref],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=True
            )
            
            # Scan the worktree
            all_paths, non_ascii_paths, error_paths = scan_repository_paths_safe(
                worktree_path, skip_git=skip_git
            )
            
            result['scanned'] = len(all_paths)
            result['successful'] = len(all_paths) - len(error_paths)
            result['errors'] = len(error_paths)
            result['non_ascii'] = len(non_ascii_paths)
            result['non_ascii_paths'] = non_ascii_paths
            result['error_paths'] = error_paths
            
        except subprocess.CalledProcessError as e:
            result['checkout_error'] = f"Failed to checkout branch: {e.stderr}"
        except Exception as e:
            result['checkout_error'] = f"Unexpected error: {str(e)}"
        finally:
            # Clean up the worktree
            try:
                subprocess.run(
                    ['git', 'worktree', 'remove', str(worktree_path), '--force'],
                    cwd=repo_root,
                    capture_output=True,
                    text=True,
                    check=False  # Don't fail if worktree doesn't exist
                )
            except Exception:
                pass  # Ignore cleanup errors
    
    return result


def scan_repository_paths(root_dir, skip_git=True):
    """
    Scan all file paths in the repository for non-ASCII characters.
    
    Args:
        root_dir: The root directory of the repository
        skip_git: If True, skip the .git directory (default: True)
        
    Returns:
        A tuple of (all_paths, non_ascii_paths)
    """
    all_paths, non_ascii_paths, _ = scan_repository_paths_safe(root_dir, skip_git)
    return all_paths, non_ascii_paths


def generate_report(all_paths, non_ascii_paths, output_file, git_included=False):
    """
    Generate a markdown report of the scan results.
    
    Args:
        all_paths: List of all paths scanned
        non_ascii_paths: List of tuples (type, path) with non-ASCII characters
        output_file: Path to the output markdown file
        git_included: Whether .git directory was included in the scan
    """
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write("# Repository-Wide Path ASCII Scan Report\n\n")
        
        scan_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        f.write(f"**Scan Date:** {scan_date}\n\n")
        f.write(f"**Total Paths Scanned:** {len(all_paths)}\n\n")
        f.write(f"**Paths with Non-ASCII Characters:** {len(non_ascii_paths)}\n\n")
        f.write(f"**.git Directory Included:** {'Yes' if git_included else 'No'}\n\n")
        
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


def generate_branch_report(branch_results, output_file, git_included=False):
    """
    Generate a markdown report for branch scanning results.
    
    Args:
        branch_results: List of dictionaries with scan results per branch
        output_file: Path to the output markdown file
        git_included: Whether .git directory was included in the scan
    """
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write("# Repository Branch-Wide Path ASCII Scan Report\n\n")
        
        scan_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        f.write(f"**Scan Date:** {scan_date}\n\n")
        f.write(f"**Branches Scanned:** {len(branch_results)}\n\n")
        f.write(f"**.git Directory Included:** {'Yes' if git_included else 'No'}\n\n")
        
        # Summary table
        f.write("## Summary by Branch\n\n")
        f.write("| Branch | Paths Scanned | Successful | Errors | Non-ASCII Paths |\n")
        f.write("|--------|---------------|------------|--------|----------------|\n")
        
        for result in branch_results:
            if result['checkout_error']:
                f.write(f"| {result['branch']} | N/A | N/A | Checkout Failed | N/A |\n")
            else:
                f.write(f"| {result['branch']} | {result['scanned']} | {result['successful']} | "
                       f"{result['errors']} | {result['non_ascii']} |\n")
        f.write("\n")
        
        # Detailed results for each branch
        f.write("## Detailed Results by Branch\n\n")
        
        for result in branch_results:
            f.write(f"### Branch: `{result['branch']}`\n\n")
            
            if result['checkout_error']:
                f.write(f"❌ **Checkout Error:** {result['checkout_error']}\n\n")
                continue
            
            f.write(f"- **Paths Scanned:** {result['scanned']}\n")
            f.write(f"- **Successful:** {result['successful']}\n")
            f.write(f"- **Errors:** {result['errors']}\n")
            f.write(f"- **Non-ASCII Paths:** {result['non_ascii']}\n\n")
            
            # Show errors if any
            if result['errors'] > 0 and result['error_paths']:
                f.write("#### Error Paths\n\n")
                f.write("The following paths encountered errors during scanning:\n\n")
                for error_type, path, error_msg in result['error_paths']:
                    f.write(f"- [{error_type}] `{path}`\n")
                    f.write(f"  - Error: {error_msg}\n")
                f.write("\n")
            
            # Show non-ASCII paths if any
            if result['non_ascii'] > 0 and result['non_ascii_paths']:
                f.write("#### Non-ASCII Paths\n\n")
                f.write("The following paths contain non-ASCII characters:\n\n")
                
                # Separate by type
                dirs = [p for t, p in result['non_ascii_paths'] if t == 'directory']
                files = [p for t, p in result['non_ascii_paths'] if t == 'file']
                
                if dirs:
                    f.write("**Directories:**\n\n")
                    for path in dirs:
                        non_ascii_chars = [c for c in path if ord(c) > 127]
                        f.write(f"- `{path}`\n")
                        if non_ascii_chars:
                            f.write(f"  - Non-ASCII characters: {', '.join(repr(c) for c in non_ascii_chars)}\n")
                    f.write("\n")
                
                if files:
                    f.write("**Files:**\n\n")
                    for path in files:
                        non_ascii_chars = [c for c in path if ord(c) > 127]
                        f.write(f"- `{path}`\n")
                        if non_ascii_chars:
                            f.write(f"  - Non-ASCII characters: {', '.join(repr(c) for c in non_ascii_chars)}\n")
                    f.write("\n")
            
            if result['errors'] == 0 and result['non_ascii'] == 0:
                f.write("✅ **All paths scanned successfully with no non-ASCII characters.**\n\n")
        
        # Overall recommendations
        f.write("## Recommendations\n\n")
        f.write("Non-ASCII characters in file paths can cause issues with:\n")
        f.write("- Cross-platform compatibility (Windows, macOS, Linux)\n")
        f.write("- Version control systems\n")
        f.write("- Build and deployment tools\n")
        f.write("- Shell scripts and command-line operations\n\n")
        f.write("Consider renaming paths to use only ASCII characters (A-Z, a-z, 0-9, hyphens, underscores).\n")


def main():
    """Main function to run the path scan."""
    # Parse command-line arguments
    parser = argparse.ArgumentParser(
        description='Scan repository file paths for non-ASCII characters.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                                    # Scan current working directory
  %(prog)s --include-git-dir                  # Include .git directory in scan
  %(prog)s --branch main                      # Scan the 'main' branch
  %(prog)s --branches main,dev,feature        # Scan multiple branches
  %(prog)s --all-branches                     # Scan all branches in repository
  %(prog)s --all-branches --include-git-dir   # Scan all branches including .git
        """
    )
    parser.add_argument(
        '--include-git-dir',
        action='store_true',
        help='Include .git directory in the scan (default: skip .git directory)'
    )
    parser.add_argument(
        '--branch',
        type=str,
        help='Scan a specific branch'
    )
    parser.add_argument(
        '--branches',
        type=str,
        help='Scan multiple branches (comma-separated list)'
    )
    parser.add_argument(
        '--all-branches',
        action='store_true',
        help='Scan all branches in the repository'
    )
    
    args = parser.parse_args()
    
    # Get the repository root directory
    script_dir = Path(__file__).parent.absolute()
    repo_root = script_dir
    
    skip_git = not args.include_git_dir
    
    # Determine if we're scanning branches
    scan_branches = args.branch or args.branches or args.all_branches
    
    if scan_branches:
        # Branch scanning mode
        print(f"Scanning branches in repository at: {repo_root}")
        if args.include_git_dir:
            print("Including .git directory in branch scans")
        else:
            print("Skipping .git directory in branch scans")
        print("This may take a moment for large repositories...\n")
        
        # Determine which branches to scan
        branches_to_scan = []
        if args.all_branches:
            print("Getting list of all branches...")
            branches_to_scan = get_all_branches(repo_root)
            if not branches_to_scan:
                print("❌ No branches found in repository")
                return 1
            print(f"Found {len(branches_to_scan)} branches to scan\n")
        elif args.branches:
            branches_to_scan = [b.strip() for b in args.branches.split(',')]
        elif args.branch:
            branches_to_scan = [args.branch]
        
        # Scan each branch
        branch_results = []
        for i, branch in enumerate(branches_to_scan, 1):
            print(f"[{i}/{len(branches_to_scan)}] Scanning branch: {branch}")
            result = scan_branch(repo_root, branch, skip_git=skip_git)
            branch_results.append(result)
            
            if result['checkout_error']:
                print(f"  ❌ Error: {result['checkout_error']}")
            else:
                print(f"  ✓ Scanned: {result['scanned']} paths")
                print(f"  ✓ Successful: {result['successful']}")
                if result['errors'] > 0:
                    print(f"  ⚠ Errors: {result['errors']}")
                if result['non_ascii'] > 0:
                    print(f"  ⚠ Non-ASCII: {result['non_ascii']}")
            print()
        
        # Generate the branch report
        output_file = repo_root / "repowidepathscannonascii.md"
        generate_branch_report(branch_results, output_file, git_included=args.include_git_dir)
        
        # Summary
        total_scanned = sum(r['scanned'] for r in branch_results if not r['checkout_error'])
        total_errors = sum(r['errors'] for r in branch_results if not r['checkout_error'])
        total_non_ascii = sum(r['non_ascii'] for r in branch_results if not r['checkout_error'])
        checkout_failures = sum(1 for r in branch_results if r['checkout_error'])
        
        print("=" * 60)
        print("SUMMARY")
        print("=" * 60)
        print(f"Branches scanned: {len(branch_results) - checkout_failures}/{len(branch_results)}")
        print(f"Total paths scanned: {total_scanned}")
        print(f"Total errors: {total_errors}")
        print(f"Total non-ASCII paths: {total_non_ascii}")
        if checkout_failures > 0:
            print(f"Checkout failures: {checkout_failures}")
        print(f"\n✅ Report generated: {output_file}")
        
    else:
        # Original single directory scanning mode
        print(f"Scanning repository at: {repo_root}")
        if args.include_git_dir:
            print("Including .git directory in scan")
        else:
            print("Skipping .git directory (use --include-git-dir to include it)")
        print("This may take a moment for large repositories...\n")
        
        # Scan all paths
        all_paths, non_ascii_paths = scan_repository_paths(repo_root, skip_git=skip_git)
        
        print(f"Total paths scanned: {len(all_paths)}")
        print(f"Paths with non-ASCII characters: {len(non_ascii_paths)}")
        
        if non_ascii_paths:
            print("\nNon-ASCII paths found:")
            for path_type, path in non_ascii_paths:
                print(f"  [{path_type}] {path}")
        
        # Generate the report
        output_file = repo_root / "repowidepathscannonascii.md"
        generate_report(all_paths, non_ascii_paths, output_file, git_included=args.include_git_dir)
        
        print(f"\n✅ Report generated: {output_file}")


if __name__ == "__main__":
    main()
