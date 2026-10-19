"""Explicit file export; no traversal, delete, credential read, or Git operation.

Run from the internal release kit, passing its reviewed allowlist and source root.
The public copy can also run this tool against that same internal release kit.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys

STATE = '.public-export-state.json'
MANIFEST = 'PUBLIC_SOURCE_MANIFEST.json'
SUFFIXES = {'.py', '.ps1', '.json', '.md', '.txt', '.png', '.svg', '.ts', '.tsx', '.css', '.html', '.cjs', '.yml', '.yaml', '.java', '.xml', '.template'}
DENIED = {'.git', '.env', '.venv', 'node_modules', '__pycache__', 'runs', 'workspaces', 'state', 'dist', 'studio-authorizations', 'studio-manager-authorizations', 'studio-manager-api-authorizations'}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def plain(path):
    """Check lexical ancestors before any resolve/read/mkdir; never follow links."""
    path = Path(os.path.abspath(path))
    for node in [*reversed(path.parents), path]:
        if node.exists() or node.is_symlink():
            info = node.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 1024:
                raise ValueError('Reparse point or symlink rejected: ' + str(node))
    return path


def relative(raw):
    p = PurePosixPath(raw)
    if not raw or '\\' in raw or ':' in raw or p.is_absolute() or any(x in {'..', '.'} for x in raw.split('/')):
        raise ValueError('Invalid relative path')
    return p


def export(source, dest, allowlist, dry_run=False):
    source, dest, allowlist = plain(source), plain(dest), plain(allowlist)
    if dest == source or dest.is_relative_to(source) or source.is_relative_to(dest):
        raise ValueError('Source and destination must be independent directories')
    entries = json.loads(allowlist.read_text(encoding='utf8'))['files']
    prior = json.loads((plain(dest / STATE)).read_text(encoding='utf8')) if (dest / STATE).exists() else {}
    previous = prior.get('files', {})
    rows, buffers, summary = [], {}, {'added': [], 'updated': [], 'unchanged': [], 'dryRun': dry_run}
    folded = set()
    for entry in entries:
        origin, target = relative(entry['source']), relative(entry['path'])
        if any(x.casefold() in DENIED or x.casefold().startswith('measurements') for x in origin.parts):
            raise ValueError('Denied source path: ' + str(origin))
        if origin.name.startswith('.env') or origin.suffix not in SUFFIXES and origin.name not in {'.gitignore', '.gitattributes'}:
            raise ValueError('Denied source extension')
        if target.as_posix().casefold() in folded:
            raise ValueError('Duplicate destination')
        folded.add(target.as_posix().casefold())
        if any(x.casefold() in DENIED or x.casefold().startswith('measurements') for x in target.parts):
            raise ValueError('Denied destination: ' + str(target))
        if target.name not in {'.gitignore', '.gitattributes'} and target.suffix not in SUFFIXES:
            raise ValueError('Denied extension: ' + str(target))
        src, dst = plain(source / origin), plain(dest / target)
        if not src.is_file() or src.stat().st_size > 2_000_000:
            raise ValueError('Not a small regular source file: ' + str(origin))
        data = src.read_bytes()
        digest = sha(data)
        if dst.exists():
            current = sha(dst.read_bytes())
            if current != digest and previous.get(str(target)) != current:
                raise ValueError('Local conflict; preserve and review: ' + str(target))
            summary['unchanged' if current == digest else 'updated'].append(str(target))
        else:
            summary['added'].append(str(target))
        rows.append(dict(path=str(target), source=str(origin), bytes=len(data), sha256=digest))
        buffers[str(target)] = data
    removed = sorted(set(previous) - set(buffers))
    if removed:
        raise ValueError('Previously managed paths removed from allowlist; review manually: ' + ', '.join(removed))
    manifest = dict(schema=1, policy='Explicit reviewed files only; no historical Git or runtime data', files=rows)
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf8')
    manifest_path = plain(dest / MANIFEST)
    if manifest_path.exists():
        current = sha(manifest_path.read_bytes())
        if current != prior.get('manifestSha256') and current != sha(manifest_bytes):
            raise ValueError('Local manifest conflict')
    # Check all files before the first mutation. No implicit removals or recursion.
    if not dry_run:
        dest.mkdir(parents=True, exist_ok=True)
        for name, data in buffers.items():
            path = plain(dest / name)
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists() or sha(path.read_bytes()) != sha(data):
                temp = plain(path.with_name(path.name + '.export-tmp'))
                if temp.exists():
                    raise ValueError('Unexpected temporary file')
                with temp.open('xb') as stream:
                    stream.write(data)
                temp.replace(path)
        manifest_path.write_bytes(manifest_bytes)
        state = {row['path']: row['sha256'] for row in rows}
        # Keep manifest hash separately; it is not a managed source entry.
        (dest / STATE).write_text(json.dumps(dict(files=state,manifestSha256=sha(manifest_bytes)), indent=2) + '\n', encoding='utf8')
    summary.update(files=len(rows), bytes=sum(r['bytes'] for r in rows), manifestSha256=sha(manifest_bytes))
    return summary


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--dest', type=Path, required=True)
    p.add_argument('--allowlist', type=Path, required=True)
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()
    try:
        result = export(args.source, args.dest, args.allowlist, args.dry_run)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, KeyError) as exc:
        print('EXPORT REFUSED: ' + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
