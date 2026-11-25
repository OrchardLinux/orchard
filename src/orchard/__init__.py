"""Host-side primitives for installing verified image inputs."""
import json
from pathlib import Path


def version():
    """This orchard's version: the core's core.json, the installed package's metadata, or, for a source
    tree run as it is (PYTHONPATH), its pyproject.toml."""
    core = Path(__file__).resolve().parents[2] / 'core.json'
    if core.is_file():
        return json.loads(core.read_text())['version']
    from importlib.metadata import PackageNotFoundError, version as package_version
    try:
        return package_version('orchard')
    except PackageNotFoundError:
        import tomllib
        with (Path(__file__).resolve().parents[2] / 'pyproject.toml').open('rb') as stream:
            return tomllib.load(stream)['project']['version']
