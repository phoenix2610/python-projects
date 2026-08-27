#!/usr/bin/env python3
"""Custom import machinery: load modules from an archive, rewrite source on the way in, audit imports.

    importhook.py --demo
    python -c "import importhook, mymodule"     # after install_archive('lib.zip')

Three hooks, all built on the same protocol: a *finder* on sys.meta_path decides
whether it can supply a module and returns a spec; a *loader* produces the source
and executes it into the module's namespace. Once you have written one, `import`
stops being magic — it is a for-loop over sys.meta_path.
"""
from __future__ import annotations

import argparse
import importlib
import importlib.abc
import importlib.util
import io
import os
import re
import sys
import time
import zipfile
from types import ModuleType


class ArchiveLoader(importlib.abc.Loader):
    def __init__(self, archive: zipfile.ZipFile, name: str, path: str, transform=None):
        self.archive, self.name, self.path, self.transform = archive, name, path, transform

    def create_module(self, spec):
        return None                        # default module creation is fine

    def get_source(self, name: str) -> str:
        source = self.archive.read(self.path).decode("utf-8")
        return self.transform(source) if self.transform else source

    def exec_module(self, module: ModuleType) -> None:
        source = self.get_source(module.__name__)
        code = compile(source, f"{self.archive.filename}!{self.path}", "exec")
        exec(code, module.__dict__)


class ArchiveFinder(importlib.abc.MetaPathFinder):
    """Import modules straight out of a zip, without extracting it."""

    def __init__(self, archive_path: str, transform=None):
        self.archive = zipfile.ZipFile(archive_path)
        self.transform = transform
        self.names = set(self.archive.namelist())
        self.loaded: list[str] = []

    def find_spec(self, fullname: str, path=None, target=None):
        parts = fullname.split(".")
        candidates = ["/".join(parts) + ".py", "/".join(parts) + "/__init__.py"]
        for candidate in candidates:
            if candidate in self.names:
                self.loaded.append(fullname)
                is_package = candidate.endswith("__init__.py")
                spec = importlib.util.spec_from_loader(
                    fullname, ArchiveLoader(self.archive, fullname, candidate, self.transform),
                    origin=f"{self.archive.filename}!{candidate}", is_package=is_package)
                if is_package:
                    spec.submodule_search_locations = []
                return spec
        return None


class SourceRewriter(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Rewrite source before compilation — how `from __future__` style features get prototyped."""

    def __init__(self, root: str, rewrite):
        self.root, self.rewrite = os.path.abspath(root), rewrite
        self.rewritten: dict[str, int] = {}

    def find_spec(self, fullname: str, path=None, target=None):
        candidate = os.path.join(self.root, *fullname.split(".")) + ".py"
        if not os.path.exists(candidate):
            return None
        return importlib.util.spec_from_file_location(fullname, candidate, loader=self)

    def create_module(self, spec):
        return None

    def exec_module(self, module: ModuleType) -> None:
        path = module.__spec__.origin
        original = open(path, encoding="utf-8").read()
        source, count = self.rewrite(original)
        self.rewritten[module.__name__] = count
        exec(compile(source, path, "exec"), module.__dict__)


class ImportAuditor(importlib.abc.MetaPathFinder):
    """A finder that never finds anything — it just watches, and times the import that follows."""

    def __init__(self, threshold_ms: float = 0.0):
        self.records: list[tuple[str, float]] = []
        self.threshold = threshold_ms
        self.depth = 0

    def find_spec(self, fullname: str, path=None, target=None):
        if fullname in sys.modules:
            return None
        start = time.perf_counter()
        self.depth += 1
        try:
            for finder in sys.meta_path:
                if finder is self:
                    continue
                spec = finder.find_spec(fullname, path, target)
                if spec is not None:
                    return self._timed(spec, fullname, start)
        finally:
            self.depth -= 1
        return None

    def _timed(self, spec, fullname: str, start: float):
        original_exec = spec.loader.exec_module if spec.loader else None
        if original_exec is None:
            return spec

        auditor = self

        def exec_module(module):
            began = time.perf_counter()
            try:
                original_exec(module)
            finally:
                elapsed = (time.perf_counter() - began) * 1000
                auditor.records.append((fullname, elapsed))
        spec.loader.exec_module = exec_module
        return spec

    def report(self, top: int = 10) -> None:
        slow = sorted(self.records, key=lambda r: -r[1])[:top]
        total = sum(ms for _, ms in self.records)
        print(f"  {len(self.records)} modules imported, {total:.1f}ms total")
        for name, ms in slow:
            if ms >= self.threshold:
                print(f"    {ms:>7.2f}ms  {name}")


def install_archive(path: str, transform=None) -> ArchiveFinder:
    finder = ArchiveFinder(path, transform)
    sys.meta_path.insert(0, finder)
    return finder


def pipe_operator(source: str) -> tuple[str, int]:
    """Toy syntax extension: `value |> fn` becomes `fn(value)`."""
    pattern = re.compile(r"([\w\.\(\)\[\]\'\"]+)\s*\|>\s*(\w+)")
    count = 0
    while True:
        source, n = pattern.subn(r"\2(\1)", source)
        count += n
        if not n:
            return source, count


def demo() -> int:
    import tempfile
    workdir = tempfile.mkdtemp(prefix="importhook-")

    print("1. importing a module that only exists inside a zip\n")
    archive_path = os.path.join(workdir, "lib.zip")
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr("zipmod.py", "VALUE = 'loaded from inside the archive'\n"
                                 "def greet(name):\n    return f'hello {name}, from a zip'\n")
        zf.writestr("zippkg/__init__.py", "from .inner import answer\n")
        zf.writestr("zippkg/inner.py", "def answer():\n    return 42\n")
    finder = install_archive(archive_path)
    import zipmod
    import zippkg
    print(f"  zipmod.VALUE      = {zipmod.VALUE!r}")
    print(f"  zipmod.greet(...) = {zipmod.greet('tathya')!r}")
    print(f"  zippkg.answer()   = {zippkg.answer()}")
    print(f"  zipmod.__file__-ish origin: {zipmod.__spec__.origin}")
    print(f"  finder served: {finder.loaded}")

    print("\n2. rewriting source on the way in: a `|>` pipe operator Python does not have\n")
    src_dir = os.path.join(workdir, "src")
    os.makedirs(src_dir)
    open(os.path.join(src_dir, "piped.py"), "w").write(
        "def double(n):\n    return n * 2\n\n"
        "def shout(s):\n    return str(s).upper() + '!'\n\n"
        "result = 21 |> double |> shout\n")
    rewriter = SourceRewriter(src_dir, pipe_operator)
    sys.meta_path.insert(0, rewriter)
    import piped
    print(f"  source said:  result = 21 |> double |> shout")
    print(f"  python saw:   result = shout(double(21))")
    print(f"  piped.result = {piped.result!r}  ({rewriter.rewritten['piped']} rewrites)")

    print("\n3. auditing what an import actually pulls in\n")
    auditor = ImportAuditor(threshold_ms=0.0)
    sys.meta_path.insert(0, auditor)
    for name in ("json", "csv", "xml.etree.ElementTree", "http.server"):
        for mod in [m for m in list(sys.modules) if m.startswith(name.split(".")[0])]:
            sys.modules.pop(mod, None)
        importlib.import_module(name)
    auditor.report(top=8)

    print("\n4. what a finder chain looks like from the inside\n")
    for entry in sys.meta_path:
        name = type(entry).__name__ if not isinstance(entry, type) else entry.__name__
        print(f"    {name}")
    print("\n  import walks this list in order and takes the first spec it is handed")

    sys.meta_path.remove(auditor)
    sys.meta_path.remove(rewriter)
    sys.meta_path.remove(finder)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--archive", help="install an archive finder for this zip and import --module")
    ap.add_argument("--module", help="module to import from the archive")
    args = ap.parse_args()
    if args.archive and args.module:
        install_archive(args.archive)
        module = importlib.import_module(args.module)
        print(f"  imported {module.__name__} from {module.__spec__.origin}")
        print(f"  exports: {', '.join(n for n in dir(module) if not n.startswith('_'))}")
        return 0
    return demo()


if __name__ == "__main__":
    raise SystemExit(main())
