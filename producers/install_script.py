#!/usr/bin/env python3
"""A distribution release's install.sh: installs the orchard command and runs `orchard install` for that release.

  producers/install_script.py --name 'Omarchy A' --index URL --url URL --output install.sh

Standard library only; the text is install.sh beside this file.
"""
import argparse
from pathlib import Path
import re

TEMPLATE = Path(__file__).with_name('install.sh')


def write(output, name, index, url):
    """install.sh at OUTPUT for the release of NAME whose index is INDEX; URL is where the script itself is fetched from."""
    for value in (index, url):
        if not re.fullmatch(r'[A-Za-z0-9:/._~+-]+', value):
            raise ValueError(f'Not a plain URL or path: {value}')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ._-]*', name):
        raise ValueError(f'Not a plain name: {name}')
    text = TEMPLATE.read_text().replace('@NAME@', name).replace('@INDEX@', "'" + index + "'").replace('@URL@', url)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text)
    output.chmod(0o755)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--name', required=True, help="the distribution's name as people read it, such as 'Omarchy A'")
    parser.add_argument('--index', required=True, help="the release's index.toml: its URL, or a path for a local build")
    parser.add_argument('--url', required=True, help='where this install.sh is published')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    print(write(args.output, args.name, args.index, args.url))


if __name__ == '__main__':
    main()
