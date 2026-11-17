import hashlib
import os
import shutil
import subprocess
from pathlib import Path, PurePosixPath


POLICY_FILES = {".clang-tidy", ".clang-format", "_clang-format", ".c-verify.json"}


def file_digest(path):
    if path.is_symlink():
        return "link:" + os.readlink(path)
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def git(repo, *args, timeout=180, env=None, data=None):
    executable = shutil.which("git")
    if not executable:
        raise ValueError("Git is required for incremental snapshots")
    environment = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", **(env or {})}
    process = subprocess.run([executable, "-C", str(repo), *args], input=data, capture_output=True, timeout=timeout, env=environment)
    if process.returncode:
        raise ValueError(process.stderr.decode("utf-8", "replace").strip() or "Git snapshot operation failed")
    return process.stdout


def safe_path(root, name):
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts or any(":" in part for part in relative.parts):
        raise ValueError("unsafe snapshot path: " + name)
    path = root.joinpath(*relative.parts)
    if path != root and not path.parent.resolve().is_relative_to(root.resolve()):
        raise ValueError("snapshot parent escapes its root: " + name)
    return path


def index_entries(raw):
    entries = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, name = record.split(b"\t", 1)
        mode, object_id, stage = metadata.split()
        if stage != b"0":
            raise ValueError("unmerged index cannot be verified as a snapshot")
        if mode == b"160000":
            raise ValueError("submodule compilation requires an independently prepared snapshot")
        entries.append((mode, object_id, name.decode("utf-8", "surrogateescape")))
    return entries


def checkout_tree(repo, destination, index, ref=None, entries=None, timeout=180):
    destination.mkdir(parents=True)
    environment = {"GIT_INDEX_FILE": str(index)}
    if ref:
        git(repo, "read-tree", ref, env=environment, timeout=timeout)
    else:
        git(repo, "read-tree", "--empty", env=environment, timeout=timeout)
        data = b"".join(mode + b" " + object_id + b"\t" + name.encode("utf-8", "surrogateescape") + b"\0" for mode, object_id, name in entries)
        git(repo, "update-index", "-z", "--index-info", env=environment, data=data, timeout=timeout)
    index_entries(git(repo, "ls-files", "--stage", "-z", env=environment, timeout=timeout))
    git(repo, "checkout-index", "--all", "--prefix=" + destination.as_posix() + "/", env=environment, timeout=timeout)


def copy_worktree_file(repo, destination, name):
    source = safe_path(repo, name)
    target = safe_path(destination, name)
    if not source.exists() and not source.is_symlink():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        os.symlink(os.readlink(source), target)
    elif source.is_file():
        shutil.copyfile(source, target)
    else:
        raise ValueError("unsupported worktree entry: " + name)


def changes(repo, baseline, staged, timeout):
    args = ["diff", "--no-ext-diff", "--no-textconv", "--name-status", "-z", "--find-renames"]
    if staged:
        args.append("--cached")
    fields = git(repo, *args, baseline, "--", timeout=timeout).decode("utf-8", "surrogateescape").split("\0")
    result, index = [], 0
    while index < len(fields) and fields[index]:
        status, first = fields[index:index + 2]
        second = fields[index + 2] if status.startswith("R") else first
        result.append((status, first, second))
        index += 3 if status.startswith("R") else 2
    return result


def capture(root, baseline, staged, destination, extra_files=(), timeout=180):
    root = Path(root).resolve()
    repo = Path(git(root, "rev-parse", "--show-toplevel", timeout=timeout).decode().strip()).resolve()
    if root != repo:
        raise ValueError("incremental project-root must be the repository root; select subprojects with -f/-d")
    base_sha = git(repo, "rev-parse", "--verify", "--end-of-options", baseline + "^{commit}", timeout=timeout).decode().strip()
    head_sha = git(repo, "rev-parse", "HEAD", timeout=timeout).decode().strip()
    index_before = git(repo, "ls-files", "--stage", "-z", timeout=timeout)
    entries = index_entries(index_before)
    changed = changes(repo, base_sha, staged, timeout)
    before, after = destination / "baseline-tree", destination / "current-tree"
    checkout_tree(repo, before, destination / "baseline.index", ref=base_sha, timeout=timeout)
    if staged:
        checkout_tree(repo, after, destination / "current.index", entries=entries, timeout=timeout)
    else:
        after.mkdir()
        names = {name for _, _, name in entries}
        names.update(git(repo, "ls-files", "--others", "--exclude-standard", "-z", timeout=timeout).decode("utf-8", "surrogateescape").split("\0"))
        names.update(extra_files)
        names.update(name for name in POLICY_FILES if (root / name).is_file())
        directories = {parent for name in names - {""} for parent in Path(name).parents}
        names.update((directory / name).as_posix() for directory in directories for name in POLICY_FILES if (repo / directory / name).is_file())
        fingerprints = {name: file_digest(safe_path(repo, name)) for name in names - {""}}
        for name in sorted(names - {""}):
            copy_worktree_file(repo, after, name)
        if any(file_digest(safe_path(repo, name)) != digest or file_digest(safe_path(after, name)) != digest for name, digest in fingerprints.items()):
            raise ValueError("worktree changed while creating the verification snapshot")
    if git(repo, "ls-files", "--stage", "-z", timeout=timeout) != index_before:
        raise ValueError("index changed while creating the verification snapshot")
    if git(repo, "rev-parse", "HEAD", timeout=timeout).decode().strip() != head_sha:
        raise ValueError("HEAD changed while creating the verification snapshot")
    selected, renames = set(), {}
    for status, first, second in changed:
        selected.update((first, second))
        if status.startswith("R"):
            renames[second] = first
    identity = {"baseline_commit": base_sha, "current_commit": head_sha, "snapshot": "index" if staged else "worktree", "index_digest": hashlib.sha256(index_before).hexdigest()}
    return before, after, selected, renames, identity


def synchronize_policy(before, after):
    for name in POLICY_FILES - {".c-verify.json"}:
        for path in before.rglob(name):
            path.unlink()
        for source in after.rglob(name):
            target = before / source.relative_to(after)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
