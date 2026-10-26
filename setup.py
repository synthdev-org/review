"""Build hook only; all metadata is in ``pyproject.toml``.

The wheel is the local-mode CLI (ADR-0016), so test modules and test doubles stay out of it:
``packages.find`` selects packages, not modules, hence this ``build_py`` filter."""

from setuptools import setup
from setuptools.command.build_py import build_py

_TEST_DOUBLES = {"conftest", "fake_claude"}


class _BuildPyWithoutTests(build_py):
    def find_package_modules(self, package: str, package_dir: str) -> list[tuple[str, str, str]]:
        return [
            (pkg, module, path)
            for pkg, module, path in super().find_package_modules(package, package_dir)
            if not module.startswith("test_")
            and module not in _TEST_DOUBLES
            and ".testing" not in f".{pkg}"
        ]


setup(cmdclass={"build_py": _BuildPyWithoutTests})
