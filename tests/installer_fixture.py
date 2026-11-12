"""Exercise installer publication only from a private temporary source tree."""
from pathlib import Path
import shutil


def copy_installer(root, destination):
    destination = Path(destination)
    shutil.copytree(Path(root) / 'bin', destination / 'bin', ignore=shutil.ignore_patterns('__pycache__'))
    target = destination / 'install.sh'
    shutil.copyfile(Path(root) / 'install.sh', target)
    target.chmod(0o700)
    return target
