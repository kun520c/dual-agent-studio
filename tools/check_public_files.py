"""Check exact export bytes, privacy patterns, Markdown links and Git blobs.

Finding output contains rule/path/line only, never secret text. Independent
scanner and manual screenshot/license review complement this bounded check.
"""
from pathlib import Path
from urllib.parse import unquote, urlsplit
import argparse
import hashlib
import json
import re
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    'private-key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'github-token': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})'),
    'openai-shaped-key': re.compile(r'\bsk-(?:proj-)?[A-Za-z0-9_-]{28,}'),
    'user-home': re.compile(r'(?:[A-Za-z]:[\\/]+Users[\\/]+[^\s\\/"\']+|/(?:home|Users)/[^/\s]+)'),
    'author-layout': re.compile('IDEA_'+'PROJECTJ|AI双'+'Agent协作系统|AI修'+'图大师|302'+'56'),
    'aws-key': re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
}
SUFFIXES = {'.py','.ps1','.json','.md','.txt','.png','.svg','.ts','.tsx','.css','.html','.cjs','.yml','.yaml','.java','.xml','.template'}
DENIED = {'.git','.env','.venv','node_modules','__pycache__','runs','workspaces','state','dist','studio-authorizations','studio-manager-authorizations'}


def git(*args):
    return subprocess.check_output(['git','-C',str(ROOT),*args])


def scan(path, raw, findings):
    if path.endswith('.png'):
        if not raw.startswith(b'\x89PNG\r\n\x1a\n'):
            findings.append(dict(path=path,rule='invalid-png'))
            return
        index = 8
        while index < len(raw):
            size = struct.unpack('>I',raw[index:index+4])[0]
            kind = raw[index+4:index+8]
            if kind in {b'tEXt',b'zTXt',b'iTXt',b'eXIf'}:
                findings.append(dict(path=path,rule='image-text-metadata-needs-review'))
            index += 12 + size
        return
    text = raw.decode('utf8')
    for rule,pattern in PATTERNS.items():
        for match in pattern.finditer(text):
            # Test-only synthetic Fake API strings are generated at runtime;
            # no literal real-shaped token is allowlisted here.
            findings.append(dict(path=path,rule=rule,line=text.count('\n',0,match.start())+1))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--git',action='store_true',help='Verify index and every reachable commit/blob')
    args=parser.parse_args()
    manifest=json.loads((ROOT/'PUBLIC_SOURCE_MANIFEST.json').read_text(encoding='utf8'))
    expected={r['path']:r for r in manifest['files']}
    findings=[]
    for name,row in expected.items():
        path=ROOT/name
        if any(p.casefold() in DENIED or p.startswith('measurements') for p in path.relative_to(ROOT).parts):
            findings.append(dict(path=name,rule='denied-path'))
        if path.name not in {'.gitignore','.gitattributes'} and path.suffix not in SUFFIXES:
            findings.append(dict(path=name,rule='extension'))
        if path.is_symlink() or getattr(path.lstat(),'st_file_attributes',0)&1024:
            findings.append(dict(path=name,rule='reparse-point'))
            continue
        raw=path.read_bytes()
        if len(raw)>2_000_000 or hashlib.sha256(raw).hexdigest()!=row['sha256'] or len(raw)!=row['bytes']:
            findings.append(dict(path=name,rule='hash-or-size-mismatch'))
        scan(name,raw,findings)
        if name.endswith('.md'):
            # Code blocks are excluded; inspect Markdown links/images, including
            # local anchors. Remote link availability is checked separately.
            clean=re.sub(r'```.*?```','',raw.decode('utf8'),flags=re.S)
            for match in re.finditer(r'!?\[[^\]]*\]\(([^)]+)\)',clean):
                target=match.group(1).strip('<>')
                if urlsplit(target).scheme:
                    if urlsplit(target).scheme not in {'https','mailto'}:
                        findings.append(dict(path=name,rule='unsafe-link-scheme'))
                    continue
                dest,_,fragment=unquote(target).partition('#')
                linked=(path.parent/dest).resolve() if dest else path
                if not linked.is_relative_to(ROOT) or not linked.is_file():
                    findings.append(dict(path=name,rule='broken-local-link',target=target))
                elif fragment and linked.suffix=='.md':
                    headings=re.findall(r'^#+\s+(.+)$',linked.read_text(encoding='utf8'),re.M)
                    slugs=[re.sub(r'[^\w\- ]','',h.lower()).replace(' ','-') for h in headings]
                    if fragment not in slugs:
                        findings.append(dict(path=name,rule='missing-anchor',target=target))
    history_commits=[]
    if args.git:
        allowed=set(expected)|{'PUBLIC_SOURCE_MANIFEST.json'}
        tracked=set(git('ls-files','-z').decode().split('\0'))-{''}
        if tracked!=allowed:
            findings.append(dict(rule='tracked-set-mismatch',extra=sorted(tracked-allowed),missing=sorted(allowed-tracked)))
        for name in sorted(tracked):
            raw=git('show',':'+name)
            if raw!=(ROOT/name).read_bytes():
                findings.append(dict(path=name,rule='index-differs-from-reviewed-working-file'))
            scan(name,raw,findings)
        rev=git('rev-list','--all').decode().splitlines()
        for commit in rev:
            paths=[n for n in git('ls-tree','-rz','--name-only',commit).decode('utf8').split('\0') if n]
            for name in paths:
                if name not in allowed:
                    findings.append(dict(rule='history-unreviewed-path',path=name,commit=commit))
                scan(name,git('show',commit+':'+name),findings)
            history_commits.append(commit)
    result=dict(files=len(expected),bytes=sum(r['bytes'] for r in expected.values()),findings=findings,historyCommits=history_commits,success=not findings)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if not findings else 1


if __name__=='__main__':
    sys.exit(main())
