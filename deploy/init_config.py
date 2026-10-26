"""Create a new private controller config without touching any existing service."""
import argparse
import getpass
import json
import os
from pathlib import Path
import secrets
from werkzeug.security import generate_password_hash


def initialize(root, username, password, certificate, private_key):
    root = Path(root).resolve()
    if not password or len(password) < 12:
        raise ValueError('Use an administrator password of at least 12 characters')
    # A nonempty destination may hold live data. Never initialize over it.
    if root.exists() and any(root.iterdir()):
        raise FileExistsError('Destination is not empty; choose a new private directory')
    root.mkdir(parents=True, exist_ok=True)
    if os.name != 'nt':
        root.chmod(0o700)
    config_dir = root / 'config'
    config_dir.mkdir()
    config = dict(data_dir=str(root / 'data'), username=username,
                  password_hash=generate_password_hash(password), session_secret=secrets.token_hex(32),
                  users_file=str(config_dir / 'users.json'), bind='127.0.0.1', port=8443,
                  secure_cookie=True, certificate=str(Path(certificate).resolve()),
                  private_key=str(Path(private_key).resolve()), nodes=[], registration_enabled=False)
    for name, value in [('server.json', config), ('users.json', [dict(id=secrets.token_hex(16),username=username,password_hash=config['password_hash'],role='admin',enabled=True)])]:
        path = config_dir / name
        with path.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
        if os.name != 'nt':
            path.chmod(0o600)
    return config_dir / 'server.json'


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--username', default='admin')
    parser.add_argument('--certificate', required=True)
    parser.add_argument('--private-key', required=True)
    args = parser.parse_args()
    password = getpass.getpass('New administrator password: ')
    if password != getpass.getpass('Repeat password: '):
        raise SystemExit('Passwords do not match')
    print('Created config:', initialize(args.root,args.username,password,args.certificate,args.private_key))
