"""Opt-in authentication for native clients behind a private HTTPS proxy.

The native backend binds only to loopback. The operator supplies the HTTPS
proxy and a separate device bearer key. Forwarded headers never establish
identity, and the browser bootstrap token is never a native credential.
"""
from __future__ import annotations

import ipaddress
import os
import re
import secrets
import stat
import urllib.parse
from contextlib import contextmanager
from pathlib import Path

_KEY_PATTERN = re.compile(r'[A-Za-z0-9_-]{43}\Z')
_DNS_LABEL = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z')


def validate_public_origin(value):
    """Return one canonical HTTPS origin, allowing only an optional root slash."""
    if (not isinstance(value, str) or not 1 <= len(value) <= 1000 or not value.isascii()
            or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value)
            or any(character in value for character in ('\\', '?', '#'))):
        raise ValueError('Enter an HTTPS public origin without credentials, path, query, or fragment')
    try:
        parsed = urllib.parse.urlsplit(value)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.path not in ('', '/')):
            raise ValueError()
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError()
        hostname = parsed.hostname.lower()
        try:
            address = ipaddress.ip_address(hostname)
            if '%' in hostname:
                raise ValueError()
            hostname = '[' + address.compressed + ']' if address.version == 6 else address.compressed
        except ValueError:
            if len(hostname) > 253 or not all(_DNS_LABEL.fullmatch(label) for label in hostname.split('.')):
                raise ValueError()
        if parsed.netloc.endswith(':'):
            raise ValueError()
    except ValueError:
        raise ValueError('Enter an HTTPS public origin without credentials, path, query, or fragment') from None
    return 'https://' + hostname + (':' + str(port) if port is not None and port != 443 else '')


def _key_path(value):
    if not isinstance(value, (str, os.PathLike)):
        raise ValueError('Enter a mobile key file path')
    path = Path(os.path.abspath(os.path.expanduser(os.fspath(value))))
    # Reject symlinked ancestors as well as the leaf. The parent must already
    # exist so key creation never modifies unrelated directory permissions.
    for parent in reversed(path.parents):
        details = parent.lstat()
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
            raise ValueError('Mobile key parent directories must exist and must not be symlinks')
    return path


def _validate_key(value):
    if not isinstance(value, str) or not _KEY_PATTERN.fullmatch(value):
        raise ValueError('Mobile keys must be 43 URL-safe characters; generate one with herness mobile-key')
    return value


@contextmanager
def _parent_descriptor(path):
    """Open each existing parent without following links, including races."""
    flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(path.anchor, flags)
    try:
        for component in path.parent.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def generate_mobile_key(path):
    """Create a new private key file exclusively; never return or print its key."""
    path = _key_path(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    with _parent_descriptor(path) as parent:
        try:
            descriptor = os.open(path.name, flags, 0o600, dir_fd=parent)
        except FileExistsError:
            raise ValueError('Mobile key output already exists; choose a new path') from None
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, 'w', encoding='ascii') as output:
                output.write(secrets.token_urlsafe(32) + '\n')
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            os.unlink(path.name, dir_fd=parent)
            raise
    return path


def read_mobile_key(path):
    """Read a single-owner, private, regular key file without following links."""
    path = _key_path(path)
    if path.is_symlink():
        raise ValueError('Mobile key files must not be symlinks')
    with _parent_descriptor(path) as parent:
        descriptor = os.open(path.name, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0), dir_fd=parent)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            raise ValueError('Mobile key must be a regular file with no hard links')
        if stat.S_IMODE(details.st_mode) not in (0o400, 0o600):
            raise ValueError('Mobile key permissions must be 0600 or 0400')
        if hasattr(os, 'geteuid') and details.st_uid != os.geteuid():
            raise ValueError('Mobile key must belong to the current server user')
        if details.st_size not in (43, 44):
            raise ValueError('Invalid mobile key file format')
        raw = os.read(descriptor, 45)
    finally:
        os.close(descriptor)
    try:
        key = raw.decode('ascii')
    except UnicodeError:
        raise ValueError('Invalid mobile key file format') from None
    return _validate_key(key[:-1] if key.endswith('\n') else key)


def _values(headers, name):
    if hasattr(headers, 'get_all'):
        return headers.get_all(name, [])
    return [value for key, value in headers.items() if key.lower() == name.lower()]


class MobilePolicy:
    def __init__(self, public_origin, key):
        self.public_origin = validate_public_origin(public_origin)
        self._key = _validate_key(key)
        self._authority = urllib.parse.urlsplit(self.public_origin).netloc

    @classmethod
    def from_key_file(cls, public_origin, path):
        # Validate the origin before touching any supplied key path.
        origin = validate_public_origin(public_origin)
        return cls(origin, read_mobile_key(path))

    @staticmethod
    def browser_request(headers):
        return bool(_values(headers, 'Origin')) or any(name.lower().startswith('sec-fetch-') for name in headers)

    def valid_host(self, headers, port):
        values = _values(headers, 'Host')
        if len(values) != 1 or not isinstance(values[0], str):
            return False
        allowed = {self._authority, f'127.0.0.1:{port}', f'localhost:{port}', f'[::1]:{port}'}
        if urllib.parse.urlsplit(self.public_origin).port is None:
            allowed.add(self._authority + ':443')
        return values[0].lower() in allowed

    def authorized(self, headers, port):
        if not self.valid_host(headers, port) or self.browser_request(headers) or _values(headers, 'X-Herness-Token'):
            return False
        values = _values(headers, 'Authorization')
        if len(values) != 1 or not isinstance(values[0], str) or not values[0].startswith('Bearer '):
            return False
        key = values[0][7:]
        if not _KEY_PATTERN.fullmatch(key):
            return False
        return secrets.compare_digest(key, self._key)

    def info(self):
        return {'protocol': 'herness-mobile', 'version': 1, 'public_origin': self.public_origin,
                'authentication': 'bearer', 'transport': 'private_https_proxy',
                'capabilities': ['chat', 'polling', 'stop', 'approvals', 'conversations', 'knowledge', 'settings', 'expected_session'],
                'session_scope': 'one active workspace conversation per backend'}


def serve_mobile(root, client, public_origin, key_file, port=8766, demo=False):
    from .web import make_server
    policy = MobilePolicy.from_key_file(public_origin, key_file)
    server = make_server(root, client, port, demo, force_demo=demo, mobile_policy=policy)
    print(f'Herness native API listening on loopback port {server.server_port}.')
    print(f'Configured private HTTPS origin: {policy.public_origin}')
    print('Route your authenticated private HTTPS proxy to this loopback port. No proxy or deployment was created.')
    print('Run one Herness backend per workspace. Press Ctrl+C to stop.')
    try:
        server.serve_forever()
    finally:
        server.server_close()
