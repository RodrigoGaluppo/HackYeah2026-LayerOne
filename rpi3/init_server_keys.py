#!/usr/bin/env python3
"""Create the Central Command X25519 identity once and print its public key."""

import argparse
import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import x25519


parser = argparse.ArgumentParser()
parser.add_argument("--key", default="/home/russo/layerone/keys/server/exchange.key")
args = parser.parse_args()
path = Path(args.key)
path.parent.mkdir(parents=True, exist_ok=True)
if path.exists():
    private = x25519.X25519PrivateKey.from_private_bytes(path.read_bytes())
else:
    private = x25519.X25519PrivateKey.generate()
    raw = private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    path.write_bytes(raw)
    os.chmod(path, 0o600)
public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
print(base64.b64encode(public).decode("ascii"))
