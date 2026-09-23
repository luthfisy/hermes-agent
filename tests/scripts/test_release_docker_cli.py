"""Docker receipt CLI validates identity and hashes actual local archive bytes."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


def test_cli_manifest_and_verify(tmp_path):
    identity = ['--tag', 'v1.2.3', '--commit', 'a' * 40]
    digests = ['--digest-amd64', 'b' * 64, '--digest-arm64', 'c' * 64]
    archives = []
    expected = {}
    for arch, data in [('amd64', b'first archive'), ('arm64', b'second archive')]:
        path = tmp_path / f'{arch}.tar'
        path.write_bytes(data)
        archives.extend([f'--archive-{arch}', str(path)])
        expected[arch] = hashlib.sha256(data).hexdigest()

    def cli(*args):
        return subprocess.run([sys.executable, '-m', 'scripts.releases.docker', *args],
                              cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=30)

    for extra in ([], archives):
        result = cli('manifest', *identity, *digests, *extra)
        assert result.returncode == 0, result.stderr
        manifest = json.loads(result.stdout)
        assert manifest['digests'] == {'amd64': 'b' * 64, 'arm64': 'c' * 64}
        assert manifest.get('archives') == (expected if extra else None)
        out = tmp_path / 'manifest.json'
        out.write_text(result.stdout, encoding='utf-8')
        assert cli('verify', *identity, str(out)).returncode == 0
    for extra in (archives[:2], ['--digest-arm64', 'z' * 64]):
        result = cli('manifest', *identity, *digests, *extra)
        assert result.returncode == 1 and '::error::' in result.stderr
    for change in [
        {'tag': 'v1.2.4'}, {'commit': 'b' * 40}, {'schema': 2},
        {'digests': {'amd64': 'b' * 64}}, {'digests': {'amd64': 'b' * 64, 'arm64': 'z' * 64}},
        {'archives': {'amd64': 'd' * 64}}, {'archives': {'amd64': 'd' * 64, 'riscv64': 'e' * 64}},
        {'list-digest': 'sha256:wrong'}, None,
    ]:
        bad = copy.deepcopy(manifest)
        if change:
            bad.update(change)
        out.write_text(json.dumps(bad) if change else 'not json', encoding='utf-8')
        result = cli('verify', *identity, str(out))
        assert result.returncode == 1 and '::error::' in result.stderr


def test_promotion_reuses_the_receipt_digest_without_rebuilding():
    from scripts.releases.docker import DockerReleaseError, promote_stable

    digest = 'sha256:' + 'd' * 64
    calls = []

    def run(argv):
        calls.append(argv)
        if argv[:4] == ['docker', 'buildx', 'imagetools', 'inspect']:
            return digest
        if argv[:4] == ['docker', 'buildx', 'imagetools', 'create']:
            return ''
        raise AssertionError(argv)

    promote_stable('v1.2.3', digest, run=run)
    create = next(argv for argv in calls if argv[3] == 'create')
    assert create[-1] == f'nousresearch/hermes-agent@{digest}'
    assert all('build' not in argv for argv in calls)

    with pytest.raises(DockerReleaseError, match='versioned tag'):
        promote_stable('v1.2.3', digest, run=lambda _argv: 'sha256:' + 'e' * 64)
