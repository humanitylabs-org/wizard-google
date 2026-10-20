"""Encrypted, atomic state for the Google app.

Everything the app needs to survive a move to another box lives in one directory
(`DATA_DIR`, the Runtipi `${APP_DATA_DIR}/data` bind mount):

    store.key    Fernet key (0600). Generated on first start unless GOOGLE_STORE_KEY is set.
    google.enc   One encrypted JSON snapshot: owner client, accounts + tokens, grants, keys, requests.

Copying the directory to another box carries the sign-ins over with no reconnect.
"""
import json
import os
import tempfile
import threading
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

SCHEMA = 1


def empty_state():
    return {
        'schema': SCHEMA,
        'client': None,          # owner's own Google OAuth client (id, secret, project)
        'setup': {'redirect_mode': 'box', 'checked': {}},
        'accounts': {},          # account id -> identity, scopes, tokens
        'grants': {},            # grant id -> {client, account, operations}
        'keys': {},              # app access keys (sha256 only), for apps without Wizard grants
        'requests': {},          # access requests from apps/agents, waiting for the owner
        'audit': [],             # last calls: who, which account, which operation, outcome. No content.
    }


def tighten(path, mode):
    try:
        os.chmod(path, mode)
    except (FileNotFoundError, PermissionError):
        pass


class StoreError(RuntimeError):
    pass


class Store:
    def __init__(self, directory, key=None):
        self.root = Path(directory)
        self.path = self.root / 'google.enc'
        self.key_path = self.root / 'store.key'
        self.lock = threading.RLock()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Runtipi may loosen app-data permissions on lifecycle actions; restore them on every start.
        tighten(self.root, 0o700)
        for name in ('google.enc', 'store.key'):
            tighten(self.root / name, 0o600)
        self.cipher = Fernet(self._key(key))

    def _key(self, supplied):
        if supplied:
            return supplied.encode() if isinstance(supplied, str) else supplied
        if self.key_path.exists():
            return self.key_path.read_bytes().strip()
        if self.path.exists():
            # Never mint a new key over existing ciphertext: that would silently orphan every sign-in.
            raise StoreError('store_key_missing')
        key = Fernet.generate_key()
        self._write(self.key_path, key)
        return key

    def _write(self, path, raw):
        fd, temporary = tempfile.mkstemp(prefix='.pending-', dir=self.root)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(self.root, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def load(self):
        with self.lock:
            if not self.path.exists():
                return empty_state()
            try:
                value = json.loads(self.cipher.decrypt(self.path.read_bytes()))
            except (InvalidToken, ValueError):
                raise StoreError('store_unreadable') from None
            base = empty_state()
            base.update(value)
            return base

    def save(self, state):
        with self.lock:
            state = dict(state, schema=SCHEMA, saved=int(time.time()))
            self._write(self.path, self.cipher.encrypt(json.dumps(state, separators=(',', ':')).encode()))
