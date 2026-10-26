"""Build application-only archives; never walk configuration or task data."""
import argparse
from pathlib import Path
import zipfile

SOURCE_DIRS = ('deploy', 'external', 'runtime_support', 'runtime_repairs', 'submission_tools', 'static', 'templates')
SUFFIXES = {'.py', '.ps1', '.js', '.css', '.html', '.md', '.json', '.svg', '.jpg', '.png', '.zip', '.txt'}


def source_files(root):
    root = Path(root).resolve()
    files = list(root.glob('*.py')) + list(root.glob('requirements*.txt'))
    for directory in SOURCE_DIRS:
        files += [p for p in (root / directory).rglob('*') if p.is_file() and p.suffix in SUFFIXES and '__pycache__' not in p.parts]
    for path in sorted(set(files)):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError('Refuse source symlink or escape: ' + str(path))
        if path.is_file():
            yield path


def build_app_archive(root, output):
    root, output = Path(root).resolve(), Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'x', zipfile.ZIP_DEFLATED) as archive:
        for path in source_files(root):
            archive.write(path, path.relative_to(root).as_posix())
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    print(build_app_archive(Path(__file__).resolve().parents[1], args.output))
