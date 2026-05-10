#!/usr/bin/env python3
# Copyright 2026 Oxide Computer Company

"""
Python 3 implementation of mf2tar - converts IPS (Image Packaging System)
repo (or manifests plus root proto) into TAR archives.

Implementation derived from the Rust-based `mf2tar` utility, designed to
extract files from IPS package contents from a repository (or manifests
plus root proto) and create TAR archives for building minimized sysroots.

Two operational modes:
  Repository + List of packages: Extract from IPS repository
    mf2tar.py -r /path/to/repo.redist -P package-name output.tar
  Root proto + Manifest files: Extract from proto root directory
    mf2tar.py -p /path/to/proto -m manifest.p5m output.tar

Command-Line Reference:

  usage: mf2tar.py [-h] [-r DIR] [-P NAME] [-p DIR] [-m FILE] [-d NAME=VALUE]
              [-a] [-E PATH] [-F PATH=LOCALFILE] [-L PATH=TARGET] TARFILE

positional arguments:
  TARFILE               output TAR file

optional arguments:
  -h, --help            show this help message and exit

Repository + packages mode:
  -r DIR, --repository DIR
                        IPS repository directory
  -P NAME, --package NAME
                        IPS package name (can be specified multiple times)

Root_Poto + Manifests mode
  -p DIR, --proto DIR         proto area (source directory)
  -m FILE, --manifest FILE    IPS manifest file
  -d NAME=VALUE, --define NAME=VALUE
                      variable substitution (can be specified multiple times)

Common options:
  -a, --append          append to TAR instead of overwriting
  -E PATH, --exclude-path PATH
                        exclude manifest object path (can be specified multiple times)
  -F PATH=LOCALFILE, --file PATH=LOCALFILE
                        add extra file in archive (can be specified multiple times)
  -L PATH=TARGET, --link PATH=TARGET
                        add extra symlink in archive (can be specified multiple times)

"""

import argparse
import gzip
import hashlib
import os
import re
import sys
import tarfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Iterator, Set
from urllib.parse import unquote, quote


class Entry:
    """Represents a manifest entry (dir, file, or link)"""
    pass


class Dir(Entry):
    """Directory entry"""
    def __init__(self, path: str, owner: str, group: str, mode: str):
        self.path = path
        self.owner = owner
        self.group = group
        self.mode = mode


class File(Entry):
    """File entry"""
    def __init__(self, path: str, owner: str, group: str, mode: str,
                 chash: Optional[str], cname: Optional[str]):
        self.path = path
        self.owner = owner
        self.group = group
        self.mode = mode
        self.chash = chash  # Content hash (for verification)
        self.cname = cname  # Content name (filename in repository)


class Link(Entry):
    """Symbolic link entry"""
    def __init__(self, path: str, target: str):
        self.path = path
        self.target = target


class Include(Entry):
    """Include directive"""
    def __init__(self, path: str):
        self.path = path


class ManifestReader:
    """
    Iterator-based manifest parser with variable substitution.
    Inspired by mf2tar/src/pkgmf/mod.rs
    """

    def __init__(self, manifest_path: Path, defines: Dict[str, str] = None,
                 include_stack: Optional[Set[Path]] = None):
        self.manifest_path = manifest_path
        self.defines = defines or {}
        self.include_stack = include_stack or set()

        if manifest_path in self.include_stack:
            raise ValueError(f"Circular include detected: {manifest_path}")

        self.include_stack.add(manifest_path)
        self.file = open(manifest_path, 'r')

    def __iter__(self):
        return self

    def __next__(self) -> Entry:
        """Parse next entry from manifest"""
        while True:
            line = self._get_full_line()
            if line is None:
                raise StopIteration

            # Skip comments and empty lines
            line = line.strip()
            if not line or line.startswith('#'):
                continue

            # Substitute variables $(VAR)
            line = self._substitute_vars(line)

            # Parse entry type
            entry = self._parse_line(line)
            if entry is not None:
                return entry

    def _get_full_line(self) -> Optional[str]:
        """Read a complete line, handling backslash continuations"""
        parts = []
        while True:
            line = self.file.readline()
            if not line:
                if parts:
                    return ''.join(parts)
                return None

            line = line.rstrip('\n')
            if line.endswith('\\'):
                parts.append(line[:-1])
            else:
                parts.append(line)
                return ''.join(parts)

    def _substitute_vars(self, line: str) -> str:
        """Substitute $(VAR) with values from defines"""
        def replace(match):
            var = match.group(1)
            return self.defines.get(var, match.group(0))

        return re.sub(r'\$\((\w+)\)', replace, line)

    def _parse_line(self, line: str) -> Optional[Entry]:
        """Parse a single manifest line into an Entry"""
        # Split into tokens
        tokens = line.split()
        if not tokens:
            return None

        action_type = tokens[0]

        # Parse attributes (key=value pairs) and positional arguments
        attrs = {}
        positional = []
        for token in tokens[1:]:
            if '=' in token:
                key, value = token.split('=', 1)
                # Remove quotes if present
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                attrs[key] = value
            else:
                # Positional argument (like hash for file actions)
                positional.append(token)

        # Create appropriate Entry type
        if action_type == 'dir':
            return Dir(
                path=attrs.get('path', ''),
                owner=attrs.get('owner', 'root'),
                group=attrs.get('group', 'bin'),
                mode=attrs.get('mode', '0755')
            )
        elif action_type == 'file':
            # First positional arg is the content hash (file name in repo)
            cname = positional[0] if positional else None
            return File(
                path=attrs.get('path', ''),
                owner=attrs.get('owner', 'root'),
                group=attrs.get('group', 'bin'),
                mode=attrs.get('mode', '0644'),
                chash=attrs.get('chash'),
                cname=cname
            )
        elif action_type in ('link', 'hardlink'):
            return Link(
                path=attrs.get('path', ''),
                target=attrs.get('target', '')
            )
        elif action_type == 'include':
            return Include(
                path=attrs.get('path', '')
            )
        else:
            # Unknown action type, skip
            return None

    def close(self):
        self.file.close()


class Repository:
    """
    IPS repository handler.
    Simplified version inspired by mf2tar/src/repo.rs
    """

    def __init__(self, repo_path: Path):
        self.repo_path = repo_path
        self.file_dir = repo_path / 'file'
        self.pkg_dir = repo_path / 'pkg'

        # Validate repository structure
        if not self.file_dir.is_dir():
            raise ValueError(f"Repository missing 'file' directory: {self.file_dir}")
        if not self.pkg_dir.is_dir():
            raise ValueError(f"Repository missing 'pkg' directory: {self.pkg_dir}")

        self.packages = self._scan_packages()

    def _scan_packages(self) -> Dict[str, List[str]]:
        """Scan repository for available packages and versions"""
        packages = defaultdict(list)

        for pkg_name_dir in self.pkg_dir.iterdir():
            if not pkg_name_dir.is_dir():
                continue

            # Decode percent-encoded package name (e.g., system%2Fheader -> system/header)
            pkg_name = unquote(pkg_name_dir.name)
            for version_file in pkg_name_dir.iterdir():
                if version_file.is_file():
                    packages[pkg_name].append(version_file.name)

        return dict(packages)

    def get_manifest_path(self, package_name: str) -> Path:
        """Get path to the latest manifest for a package"""
        if package_name not in self.packages:
            raise ValueError(f"Package not found in repository: {package_name}")

        versions = sorted(self.packages[package_name])
        if not versions:
            raise ValueError(f"No versions found for package: {package_name}")

        # Use the latest version
        latest_version = versions[-1]
        # Encode package name for filesystem access (e.g., system/header -> system%2Fheader)
        encoded_name = quote(package_name, safe='')
        return self.pkg_dir / encoded_name / latest_version

    def get_file(self, cname: str, chash: Optional[str] = None) -> bytes:
        """
        Retrieve and decompress a file from the repository.
        Validates hashes if chash is provided.
        """
        # File layout: file/XX/XXXX... (first 2 chars of cname)
        if len(cname) < 2:
            raise ValueError(f"Invalid content name: {cname}")

        file_path = self.file_dir / cname[:2] / cname

        if not file_path.exists():
            raise FileNotFoundError(f"File not found in repository: {file_path}")

        # Read compressed content
        with open(file_path, 'rb') as f:
            compressed_data = f.read()

        # Validate compressed hash if provided
        if chash:
            actual_chash = hashlib.sha1(compressed_data).hexdigest()
            if actual_chash != chash:
                raise ValueError(f"Compressed hash mismatch for {cname}: "
                               f"expected {chash}, got {actual_chash}")

        # Decompress
        try:
            uncompressed_data = gzip.decompress(compressed_data)
        except Exception as e:
            raise ValueError(f"Failed to decompress {cname}: {e}")

        # Validate uncompressed hash matches filename
        actual_cname = hashlib.sha1(uncompressed_data).hexdigest()
        if actual_cname != cname:
            raise ValueError(f"Uncompressed hash mismatch: "
                           f"expected {cname}, got {actual_cname}")

        return uncompressed_data


class TarBuilder:
    """Wrapper around tarfile with mf2tar-specific functionality"""

    def __init__(self, tar_path: Path, append: bool = False):
        self.tar_path = tar_path

        if append and tar_path.exists():
            # Open for appending
            self.tar = tarfile.open(tar_path, 'a', format=tarfile.USTAR_FORMAT)
        else:
            # Create new or overwrite
            self.tar = tarfile.open(tar_path, 'w', format=tarfile.USTAR_FORMAT)

        self.mtime = int(time.time())

    def add_dir(self, entry: Dir):
        """Add a directory entry to the TAR"""
        tarinfo = tarfile.TarInfo(name=entry.path)
        tarinfo.type = tarfile.DIRTYPE
        tarinfo.mode = int(entry.mode, 8)
        tarinfo.uid = self._owner_to_uid(entry.owner)
        tarinfo.gid = self._group_to_gid(entry.group)
        tarinfo.uname = entry.owner
        tarinfo.gname = entry.group
        tarinfo.mtime = self.mtime

        self.tar.addfile(tarinfo)

    def add_file(self, entry: File, data: bytes):
        """Add a file entry to the TAR"""
        tarinfo = tarfile.TarInfo(name=entry.path)
        tarinfo.type = tarfile.REGTYPE
        tarinfo.size = len(data)
        tarinfo.mode = int(entry.mode, 8)
        tarinfo.uid = self._owner_to_uid(entry.owner)
        tarinfo.gid = self._group_to_gid(entry.group)
        tarinfo.uname = entry.owner
        tarinfo.gname = entry.group
        tarinfo.mtime = self.mtime

        import io
        self.tar.addfile(tarinfo, io.BytesIO(data))

    def add_link(self, entry: Link):
        """Add a symbolic link entry to the TAR"""
        tarinfo = tarfile.TarInfo(name=entry.path)
        tarinfo.type = tarfile.SYMTYPE
        tarinfo.linkname = entry.target
        tarinfo.mode = 0o777
        tarinfo.mtime = self.mtime

        self.tar.addfile(tarinfo)

    def add_file_from_path(self, tar_path: str, local_path: Path,
                          owner: str = 'root', group: str = 'bin', mode: str = '0644'):
        """Add a file from the local filesystem"""
        with open(local_path, 'rb') as f:
            data = f.read()

        entry = File(tar_path, owner, group, mode, None, None)
        self.add_file(entry, data)

    def close(self):
        """Finalize and close the TAR file"""
        self.tar.close()

    @staticmethod
    def _owner_to_uid(owner: str) -> int:
        """Convert owner name to UID (simplified)"""
        if owner == 'root':
            return 0
        # Could use pwd.getpwnam() for real lookups
        return 0

    @staticmethod
    def _group_to_gid(group: str) -> int:
        """Convert group name to GID (simplified)"""
        if group in ('root', 'bin', 'sys'):
            return 0
        # Could use grp.getgrnam() for real lookups
        return 0


def iterate_manifest_with_includes(manifest_path: Path, defines: Dict[str, str],
                                   excludes: List[str]) -> Iterator[Entry]:
    """
    Iterate through manifest entries, recursively handling includes.
    Filter out excluded paths.
    """
    include_stack = set()

    def _iterate(mf_path: Path) -> Iterator[Entry]:
        reader = ManifestReader(mf_path, defines, include_stack.copy())

        for entry in reader:
            if isinstance(entry, Include):
                # Recursively process included manifest
                include_path = mf_path.parent / entry.path
                yield from _iterate(include_path)
            else:
                # Check if path should be excluded
                if hasattr(entry, 'path'):
                    excluded = False
                    for exclude in excludes:
                        if entry.path == exclude or entry.path.startswith(exclude + '/'):
                            excluded = True
                            break

                    if not excluded:
                        yield entry

        reader.close()

    yield from _iterate(manifest_path)


def main():
    parser = argparse.ArgumentParser(
        description='Convert IPS manifests to TAR archives',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  %(prog)s -r /path/to/repo -P system/header output.tar
  %(prog)s -m manifest.p5m -p /path/to/proto output.tar
'''
    )

    # Repository + packages mode
    parser.add_argument('-r', '--repository', metavar='DIR',
                       help='IPS repository directory')
    parser.add_argument('-P', '--package', action='append', metavar='NAME',
                       help='IPS package name (can be specified multiple times)')

    # Manifest + proto mode
    parser.add_argument('-p', '--proto', metavar='DIR',
                       help='proto area (source directory)')
    parser.add_argument('-m', '--manifest', action='append', metavar='FILE',
                       help='IPS manifest file (can be specified multiple times)')
    parser.add_argument('-d', '--define', action='append', metavar='NAME=VALUE',
                       help='variable substitution (can be specified multiple times)')

    # Common options
    parser.add_argument('-a', '--append', action='store_true',
                       help='append to TAR instead of overwriting')
    parser.add_argument('-E', '--exclude-path', action='append', metavar='PATH',
                       help='exclude manifest object path (can be specified multiple times)')
    parser.add_argument('-F', '--file', action='append', metavar='PATH=LOCALFILE',
                       help='add extra file in archive (can be specified multiple times)')
    parser.add_argument('-L', '--link', action='append', metavar='PATH=TARGET',
                       help='add extra symlink in archive (can be specified multiple times)')

    # Positional argument
    parser.add_argument('tarfile', metavar='TARFILE',
                       help='output TAR file')

    args = parser.parse_args()

    # Validate argument combinations
    has_repo_mode = args.repository or args.package
    has_manifest_mode = args.proto or args.manifest or args.define

    if has_repo_mode and has_manifest_mode:
        parser.error('-p, -m, & -d are exclusive with -r & -P')

    if not has_repo_mode and not has_manifest_mode:
        parser.error('Must specify either repository mode (-r/-P) or manifest mode (-m/-p)')

    # Parse defines
    defines = {}
    if args.define:
        for define in args.define:
            if '=' not in define:
                parser.error(f'Invalid define format: {define} (expected NAME=VALUE)')
            name, value = define.split('=', 1)
            defines[name] = value

    # Parse extra files and links
    extra_files = []
    if args.file:
        for spec in args.file:
            if '=' not in spec:
                parser.error(f'Invalid file format: {spec} (expected PATH=LOCALFILE)')
            tar_path, local_path = spec.split('=', 1)
            extra_files.append((tar_path, Path(local_path)))

    extra_links = []
    if args.link:
        for spec in args.link:
            if '=' not in spec:
                parser.error(f'Invalid link format: {spec} (expected PATH=TARGET)')
            link_path, target = spec.split('=', 1)
            extra_links.append((link_path, target))

    # Normalize excludes
    excludes = sorted(args.exclude_path or [])

    # Create TAR builder
    tar_path = Path(args.tarfile)
    tar_builder = TarBuilder(tar_path, append=args.append)

    try:
        # Process based on mode
        if args.repository:
            # Repository + packages mode
            repo = Repository(Path(args.repository))
            packages = args.package or []

            if not packages:
                parser.error('At least one package (-P) must be specified with -r')

            for package_name in packages:
                print(f"Processing package: {package_name}", file=sys.stderr)

                manifest_path = repo.get_manifest_path(package_name)

                # Read and process manifest
                for entry in iterate_manifest_with_includes(manifest_path, {}, excludes):
                    if isinstance(entry, Dir):
                        tar_builder.add_dir(entry)
                    elif isinstance(entry, File):
                        if entry.cname:
                            # Retrieve file from repository
                            data = repo.get_file(entry.cname, entry.chash)
                            tar_builder.add_file(entry, data)
                        else:
                            print(f"Warning: file {entry.path} has no cname, skipping",
                                  file=sys.stderr)
                    elif isinstance(entry, Link):
                        tar_builder.add_link(entry)

        else:
            # Manifest + proto mode
            if not args.manifest:
                parser.error('-m (manifest) is required in manifest mode')
            if not args.proto:
                parser.error('-p (proto) is required in manifest mode')

            proto_path = Path(args.proto).resolve()
            print(f"Proto directory: {proto_path}", file=sys.stderr)

            # Process each manifest
            for manifest_file in args.manifest:
                manifest_path = Path(manifest_file)
                print(f"Processing manifest: {manifest_path}", file=sys.stderr)

                # Read and process manifest
                for entry in iterate_manifest_with_includes(manifest_path, defines, excludes):
                    if isinstance(entry, Dir):
                        tar_builder.add_dir(entry)
                    elif isinstance(entry, File):
                        # Get file from proto area
                        file_path = proto_path / entry.path
                        if file_path.exists():
                            with open(file_path, 'rb') as f:
                                data = f.read()
                            tar_builder.add_file(entry, data)
                        else:
                            print(f"Warning: file not found in proto: {file_path}",
                                  file=sys.stderr)
                    elif isinstance(entry, Link):
                        tar_builder.add_link(entry)

        # Add extra files
        for tar_path_str, local_path in extra_files:
            if local_path.exists():
                tar_builder.add_file_from_path(tar_path_str, local_path)
            else:
                print(f"Warning: extra file not found: {local_path}", file=sys.stderr)

        # Add extra links
        for link_path, target in extra_links:
            tar_builder.add_link(Link(link_path, target))

        # Finalize TAR
        tar_builder.close()
        print(f"TAR archive created: {tar_path}", file=sys.stderr)

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
